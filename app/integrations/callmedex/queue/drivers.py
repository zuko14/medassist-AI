"""Task Queue Drivers Implementation (Phase 3 & Async Worker Loop Implementation)."""

import asyncio
import logging
from typing import Dict, Any, Optional, Callable, Awaitable
from uuid import uuid4
from datetime import datetime, timezone
from app.integrations.callmedex.queue.base import BaseQueue
from app.integrations.callmedex.api.schemas import TaskStatus, ProcessReportRequest
from app.integrations.callmedex.config.settings import callmedex_settings

logger = logging.getLogger(__name__)


class InMemoryQueue(BaseQueue):
    """In-memory task queue driver supporting asynchronous background worker execution."""

    def __init__(self):
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._dlq: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[[ProcessReportRequest], Awaitable[Any]]] = {}
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._async_queue: asyncio.Queue = asyncio.Queue()

    async def enqueue_task(
        self, request: ProcessReportRequest, priority: int = 1
    ) -> str:
        """Enqueue task into memory queue and notify background worker loop."""
        task_id = str(uuid4())
        task_item = {
            "task_id": task_id,
            "request": request,
            "priority": priority,
            "status": TaskStatus.PENDING,
            "retries": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._tasks[task_id] = task_item
        await self._async_queue.put(task_id)
        logger.info(f"Enqueued task {task_id} [Report: {request.external_report_id}] into InMemoryQueue")
        return task_id

    async def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """Fetch current task status."""
        if task_id in self._tasks:
            return self._tasks[task_id]["status"]
        if task_id in self._dlq:
            return TaskStatus.FAILED
        return None

    async def register_handler(
        self,
        task_type: str,
        handler_fn: Callable[[ProcessReportRequest], Awaitable[Any]],
    ) -> None:
        """Register worker handler function."""
        logger.info(f"Registered queue handler for task type '{task_type}'")
        self._handlers[task_type] = handler_fn

    async def move_to_dlq(self, task_id: str, error_reason: str) -> bool:
        """Move failed task to Dead Letter Queue."""
        if task_id in self._tasks:
            task = self._tasks.pop(task_id)
            task["status"] = TaskStatus.FAILED
            task["error_reason"] = error_reason
            self._dlq[task_id] = task
            logger.warning(f"Moved task {task_id} to DLQ: {error_reason}")
            return True
        return False

    async def _worker_loop(self) -> None:
        """Background worker loop consuming enqueued tasks without blocking HTTP request threads with automatic loop recovery."""
        logger.info("InMemoryQueue background worker loop active and waiting for tasks")
        while self._running:
            try:
                try:
                    task_id = await asyncio.wait_for(self._async_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                if task_id not in self._tasks:
                    continue

                task_item = self._tasks[task_id]
                task_item["status"] = TaskStatus.PROCESSING
                request: ProcessReportRequest = task_item["request"]

                handler_fn = self._handlers.get("process_report") or self._handlers.get("default")
                if not handler_fn:
                    try:
                        from app.integrations.callmedex.api.router import global_runner
                        handler_fn = global_runner.execute_report_job
                        self._handlers["process_report"] = handler_fn
                    except Exception as lazy_err:
                        logger.warning(f"Could not lazily resolve queue worker handler: {lazy_err}")

                if handler_fn:
                    try:
                        logger.info(f"Background queue worker processing task {task_id} [Report: {request.external_report_id}]...")
                        result = await handler_fn(request)
                        task_item["status"] = TaskStatus.COMPLETED
                        task_item["result"] = result
                        logger.info(f"Background queue worker successfully completed task {task_id}")
                    except Exception as err:
                        retries = task_item.get("retries", 0) + 1
                        task_item["retries"] = retries
                        max_retries = getattr(callmedex_settings, "max_worker_retries", 3)
                        backoff_base = getattr(callmedex_settings, "retry_backoff_seconds", 2.0)

                        if retries < max_retries:
                            task_item["status"] = TaskStatus.PENDING
                            task_item["last_error"] = str(err)
                            delay = backoff_base * (2 ** (retries - 1))
                            logger.warning(
                                f"Task {task_id} failed (attempt {retries}/{max_retries}): {err}. "
                                f"Retrying in {delay:.1f}s..."
                            )
                            await asyncio.sleep(delay)
                            await self._async_queue.put(task_id)
                        else:
                            logger.error(
                                f"Task {task_id} execution failed and exhausted max retries ({retries}/{max_retries}): {err}"
                            )
                            await self.move_to_dlq(task_id, str(err))
                else:
                    logger.warning(f"No registered queue handler for task {task_id}")
            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.error(f"Unexpected exception in background worker loop (recovering): {loop_err}")
                await asyncio.sleep(0.5)

    async def start(self) -> None:
        """Start queue listener and background worker loop."""
        logger.info("Started InMemoryQueue driver and worker loop")
        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        # A worker task is reusable only if it is still alive AND belongs to
        # THIS event loop. The container holding this queue is a module-level
        # singleton, so a task left behind by a previous loop is neither None
        # nor done() — it is simply orphaned, and its loop will never run it
        # again. Reusing it meant start() created no worker at all and the
        # queue silently stopped consuming: enqueued tasks sat at PENDING
        # forever, with no error logged anywhere.
        reusable = (
            self._worker_task is not None
            and not self._worker_task.done()
            and self._worker_task.get_loop() is loop
        )
        if not reusable:
            if self._worker_task is not None and not self._worker_task.done():
                logger.warning(
                    "Discarding orphaned queue worker task from a previous "
                    "event loop and starting a fresh one"
                )

            # Rebuild the asyncio.Queue on loop change too.
            #
            # _async_queue is built in __init__ on a module-level singleton, so
            # it outlives any single event loop. asyncio.Queue parks waiting
            # consumers as Futures bound to the loop that awaited get(); a
            # later put() from a different loop calls set_result on one of
            # those dead futures, so the new loop's consumer is never woken and
            # the item is never delivered. The symptom is silent: the task sits
            # at PENDING forever with nothing logged.
            #
            # Carry over anything still queued so a restart does not lose work.
            pending_ids = [
                task_id
                for task_id, item in self._tasks.items()
                if item.get("status") == TaskStatus.PENDING
            ]
            self._async_queue = asyncio.Queue()
            for task_id in pending_ids:
                self._async_queue.put_nowait(task_id)
            if pending_ids:
                logger.info(
                    f"Re-queued {len(pending_ids)} pending task(s) onto the new "
                    f"event loop's queue"
                )

            self._worker_task = loop.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        """Shutdown queue cleanly."""
        logger.info("Shutdown InMemoryQueue driver cleanly")
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
