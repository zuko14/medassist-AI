"""Prometheus Metrics Collector and Exporter (W5.2, W5.3).

Tracks high-resolution operational and clinical safety metrics:
- Inbound webhook throughput and deduplication rate
- Dead letter queue (DLQ) depth
- Slot contention / slot_taken race events
- Automated refund outcomes
- Patient match gate NEEDS_REVIEW hold rate
- Connector sync outcomes
- Distributed scheduler lock contention and execution durations
- Database fail-closed events
"""

import time
import threading
from typing import Dict, Any, List
from collections import defaultdict


class MetricsRegistry:
    """Thread-safe Prometheus metrics registry without external C extensions."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.gauges: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.histograms: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    def inc_counter(self, name: str, amount: float = 1.0, labels: Dict[str, str] = None):
        label_str = self._format_labels(labels)
        with self._lock:
            self.counters[name][label_str] += amount

    def set_gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        label_str = self._format_labels(labels)
        with self._lock:
            self.gauges[name][label_str] = value

    def observe_duration(self, name: str, duration_seconds: float, labels: Dict[str, str] = None):
        label_str = self._format_labels(labels)
        with self._lock:
            self.histograms[name][label_str].append(duration_seconds)
            # Keep sample size bounded
            if len(self.histograms[name][label_str]) > 1000:
                self.histograms[name][label_str] = self.histograms[name][label_str][-500:]

    def _format_labels(self, labels: Dict[str, str] = None) -> str:
        if not labels:
            return ""
        items = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        return "{" + ",".join(items) + "}"

    def export_prometheus(self) -> str:
        """Export all registered metrics in standard Prometheus text format."""
        lines = []
        with self._lock:
            # Counters
            for name, labeled_vals in self.counters.items():
                lines.append(f"# TYPE {name} counter")
                for label_str, val in labeled_vals.items():
                    lines.append(f"{name}{label_str} {float(val)}")

            # Gauges
            for name, labeled_vals in self.gauges.items():
                lines.append(f"# TYPE {name} gauge")
                for label_str, val in labeled_vals.items():
                    lines.append(f"{name}{label_str} {float(val)}")

            # Histograms (export count, sum, average)
            for name, labeled_vals in self.histograms.items():
                lines.append(f"# TYPE {name}_seconds summary")
                for label_str, observations in labeled_vals.items():
                    count = len(observations)
                    total_sum = sum(observations)
                    lines.append(f"{name}_count{label_str} {count}")
                    lines.append(f"{name}_sum{label_str} {total_sum:.6f}")

        return "\n".join(lines) + "\n"


# Global singleton registry
metrics = MetricsRegistry()

# Initialize core baseline metrics
metrics.inc_counter("kriya_inbound_messages_total", 0, {"status": "received"})
metrics.inc_counter("kriya_inbound_messages_total", 0, {"status": "duplicate"})
metrics.inc_counter("kriya_dead_letter_total", 0)
metrics.inc_counter("kriya_slot_taken_total", 0)
metrics.inc_counter("kriya_refund_failures_total", 0)
metrics.inc_counter("kriya_needs_review_total", 0)
metrics.inc_counter("kriya_scheduler_lock_contention_total", 0)
metrics.inc_counter("kriya_fail_closed_total", 0)
metrics.inc_counter("kriya_tenant_scope_would_deny_total", 0)
metrics.inc_counter("kriya_unmatched_payment_total", 0)
metrics.inc_counter("kriya_message_queue_reaped_total", 0)
metrics.inc_counter("kriya_recovery_unreconstructable_total", 0)
metrics.inc_counter("kriya_lock_stolen_total", 0)
metrics.inc_counter("kriya_message_queue_fail_open_total", 0)
metrics.inc_counter("kriya_rate_limiter_degraded_total", 0)
metrics.inc_counter("kriya_booking_ref_collision_total", 0)
metrics.set_gauge("kriya_dlq_depth", 0)
