"""Async background task supervisor ensuring strong references to prevent GC collection.

CPython's asyncio event loop only holds weak references to tasks created via
`asyncio.create_task()`. If a task is not referenced elsewhere, it can be garbage
collected and cancelled mid-flight. `spawn_background_task` maintains a strong
reference set until completion and logs any unhandled exceptions.
"""

import asyncio
import logging
from typing import Any, Coroutine, Set

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def spawn_background_task(
    coro: Coroutine[Any, Any, Any],
    name: str = "background_task",
) -> asyncio.Task:
    """Spawn an asyncio task while holding a strong reference until completion.

    Args:
        coro: Coroutine to execute.
        name: Diagnostic name for the task.

    Returns:
        The spawned asyncio.Task.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _done_callback(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception():
            logger.error(
                f"Background task '{t.get_name()}' raised unhandled exception: {t.exception()}",
                exc_info=t.exception(),
            )

    task.add_done_callback(_done_callback)
    return task
