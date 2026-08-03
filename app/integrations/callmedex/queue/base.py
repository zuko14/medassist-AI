"""Base Queue Engine Abstract Interface (Phase 2 Contract)."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable, Awaitable
from app.integrations.callmedex.api.schemas import TaskStatus, ProcessReportRequest


class BaseQueue(ABC):
    """Abstract Base Class defining the contract for task queue engines.

    Allows plugging in different queue backends (APScheduler for dev,
    Redis/Celery for production) without modifying worker logic.
    """

    @abstractmethod
    async def enqueue_task(
        self, request: ProcessReportRequest, priority: int = 1
    ) -> str:
        """Enqueue a new report processing task.

        Args:
            request: Report processing request contract payload.
            priority: Task priority (higher number = higher priority).

        Returns:
            str: Unique task tracking ID assigned to the job.
        """
        pass

    @abstractmethod
    async def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """Fetch current execution status for a given task ID.

        Args:
            task_id: Unique task identifier.

        Returns:
            Optional[TaskStatus]: Current status of the task.
        """
        pass

    @abstractmethod
    async def register_handler(
        self,
        task_type: str,
        handler_fn: Callable[[ProcessReportRequest], Awaitable[Dict[str, Any]]],
    ) -> None:
        """Register an async worker handler function for a specific task type.

        Args:
            task_type: Identifier string for task category.
            handler_fn: Async callback function executed when worker processes task.
        """
        pass

    @abstractmethod
    async def move_to_dlq(self, task_id: str, error_reason: str) -> bool:
        """Move a permanently failed task to Dead Letter Queue (DLQ).

        Args:
            task_id: Task identifier.
            error_reason: Detailed failure reason.

        Returns:
            bool: True if task was logged to DLQ.
        """
        pass

    @abstractmethod
    async def start(self) -> None:
        """Start the queue worker polling / event processing loop."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully stop queue workers and release resources."""
        pass
