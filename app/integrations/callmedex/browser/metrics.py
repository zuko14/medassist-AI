"""Browser Operational Timing & Latency Baseline Tracker (Phase 4.5 Implementation)."""

import time
import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class WorkflowTimingMetrics(BaseModel):
    """Operational timing baselines model."""

    login_duration_ms: float = Field(default=0.0, description="Login execution time in ms")
    barcode_search_duration_ms: float = Field(default=0.0, description="Barcode search time in ms")
    report_lookup_duration_ms: float = Field(default=0.0, description="Report lookup time in ms")
    download_duration_ms: float = Field(default=0.0, description="PDF download time in ms")
    total_workflow_duration_ms: float = Field(default=0.0, description="End-to-end timing in ms")


class WorkflowTimer:
    """Timer helper for measuring browser automation stage durations."""

    def __init__(self):
        self.metrics = WorkflowTimingMetrics()
        self._start_time = time.perf_counter()
        self._stage_start = time.perf_counter()

    def mark_login_complete(self) -> float:
        now = time.perf_counter()
        duration = (now - self._stage_start) * 1000.0
        self.metrics.login_duration_ms = round(duration, 2)
        self._stage_start = now
        return duration

    def mark_search_complete(self) -> float:
        now = time.perf_counter()
        duration = (now - self._stage_start) * 1000.0
        self.metrics.barcode_search_duration_ms = round(duration, 2)
        self._stage_start = now
        return duration

    def mark_lookup_complete(self) -> float:
        now = time.perf_counter()
        duration = (now - self._stage_start) * 1000.0
        self.metrics.report_lookup_duration_ms = round(duration, 2)
        self._stage_start = now
        return duration

    def mark_download_complete(self) -> float:
        now = time.perf_counter()
        duration = (now - self._stage_start) * 1000.0
        self.metrics.download_duration_ms = round(duration, 2)
        self.metrics.total_workflow_duration_ms = round((now - self._start_time) * 1000.0, 2)
        self._stage_start = now
        return duration
