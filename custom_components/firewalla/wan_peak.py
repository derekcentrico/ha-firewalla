"""WAN burst/peak throughput estimator.

Reconstructs short-duration bandwidth peaks from completed Firewalla flow
records by allocating each flow's bytes across 5-second buckets proportional
to its duration. Concurrent flows sharing a bucket are summed, which lets the
estimator detect aggregate WAN demand that individual-flow averages would miss.

The estimator assumes uniform transfer rate across each flow's duration. It
cannot reproduce the packet-level live throughput that Firewalla's local app
shows, but it is substantially better at detecting capacity-relevant bursts
than dividing total bytes by the 120-second sample window.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .const import WAN_PEAK_BUCKET_SECONDS, WAN_PEAK_RETENTION_SECONDS

_LOGGER = logging.getLogger(__name__)

_CAPACITY_THRESHOLDS = (0.50, 0.75, 0.90, 0.95)


class WanPeakEstimator:
    """Reconstruct bandwidth peaks from completed flow records."""

    def __init__(self) -> None:
        self._buckets: dict[int, dict[str, float]] = {}
        self._fingerprints: dict[bytes, float] = {}

    def _bucket_ts(self, ts: float) -> int:
        """Round a timestamp down to the nearest bucket boundary."""
        return int(ts) // WAN_PEAK_BUCKET_SECONDS * WAN_PEAK_BUCKET_SECONDS

    def _flow_fingerprint(self, flow: dict) -> bytes:
        device = flow.get("device")
        device_id = device.get("id", "") if isinstance(device, dict) else ""
        source = flow.get("source")
        source_id = source.get("id", "") if isinstance(source, dict) else ""
        destination = flow.get("destination")
        dest_id = destination.get("id", "") if isinstance(destination, dict) else ""
        key = (
            f"{flow.get('gid', '')}|{flow.get('ts', 0)}|{flow.get('duration', 0)}|"
            f"{flow.get('protocol', '')}|{device_id}|{source_id}|{dest_id}|"
            f"{flow.get('download', 0)}|{flow.get('upload', 0)}"
        )
        return hashlib.md5(key.encode()).digest()

    def _allocate_flow(self, flow: dict) -> bool:
        """Allocate one flow into 5-second buckets. Returns False if skipped."""
        try:
            ts = float(flow.get("ts", 0))
            duration = float(flow.get("duration", 0))
            dl_bytes = max(int(flow.get("download", 0)), 0)
            ul_bytes = max(int(flow.get("upload", 0)), 0)
        except (TypeError, ValueError):
            return False

        if duration <= 0 or ts <= 0:
            return False

        fp = self._flow_fingerprint(flow)
        if fp in self._fingerprints:
            return False
        self._fingerprints[fp] = ts

        flow_start = max(ts - duration, ts - WAN_PEAK_RETENTION_SECONDS)
        dl_mbps = dl_bytes * 8 / duration / 1_000_000
        ul_mbps = ul_bytes * 8 / duration / 1_000_000

        first_bucket = self._bucket_ts(flow_start)
        last_bucket = self._bucket_ts(ts - 0.001) if ts > flow_start else first_bucket

        bucket_ts = first_bucket
        while bucket_ts <= last_bucket:
            bucket_start = float(bucket_ts)
            bucket_end = bucket_start + WAN_PEAK_BUCKET_SECONDS
            overlap_start = max(flow_start, bucket_start)
            overlap_end = min(ts, bucket_end)
            overlap = max(overlap_end - overlap_start, 0.0)

            if overlap > 0:
                fraction = overlap / WAN_PEAK_BUCKET_SECONDS
                entry = self._buckets.setdefault(
                    bucket_ts, {"download_mbps": 0.0, "upload_mbps": 0.0}
                )
                entry["download_mbps"] += dl_mbps * fraction
                entry["upload_mbps"] += ul_mbps * fraction

            bucket_ts += WAN_PEAK_BUCKET_SECONDS

        return True

    def process_flows(self, flows: list[dict]) -> dict[str, Any]:
        """Allocate flows into buckets and return cycle peak results."""
        touched_buckets: set[int] = set()
        allocated = 0
        skipped = 0

        for flow in flows:
            if not isinstance(flow, dict):
                skipped += 1
                continue
            if self._allocate_flow(flow):
                allocated += 1
            else:
                skipped += 1

        if flows:
            try:
                min_ts = min(
                    float(f.get("ts", 0)) - max(float(f.get("duration", 0)), 0)
                    for f in flows
                    if isinstance(f, dict) and f.get("ts")
                )
                max_ts = max(
                    float(f.get("ts", 0))
                    for f in flows
                    if isinstance(f, dict) and f.get("ts")
                )
                touched_buckets = {
                    bts
                    for bts in self._buckets
                    if self._bucket_ts(min_ts) <= bts <= self._bucket_ts(max_ts)
                }
            except (ValueError, TypeError):
                touched_buckets = set(self._buckets.keys())

        dl_peak = 0.0
        ul_peak = 0.0
        total_peak = 0.0
        dl_peak_ts = None
        ul_peak_ts = None
        total_peak_ts = None

        for bts in touched_buckets:
            bucket = self._buckets.get(bts)
            if not bucket:
                continue
            dl = bucket["download_mbps"]
            ul = bucket["upload_mbps"]
            total = dl + ul
            if dl > dl_peak:
                dl_peak = dl
                dl_peak_ts = bts
            if ul > ul_peak:
                ul_peak = ul
                ul_peak_ts = bts
            if total > total_peak:
                total_peak = total
                total_peak_ts = bts

        return {
            "download_peak_mbps": round(dl_peak, 1),
            "upload_peak_mbps": round(ul_peak, 1),
            "total_peak_mbps": round(total_peak, 1),
            "download_peak_timestamp": dl_peak_ts,
            "upload_peak_timestamp": ul_peak_ts,
            "total_peak_timestamp": total_peak_ts,
            "bucket_seconds": WAN_PEAK_BUCKET_SECONDS,
            "estimation_method": "completed_flows",
            "flows_allocated": allocated,
            "flows_skipped": skipped,
        }

    def prune(self, now: float) -> None:
        """Remove buckets and fingerprints older than the retention period."""
        cutoff = now - WAN_PEAK_RETENTION_SECONDS
        cutoff_bucket = self._bucket_ts(cutoff)
        stale_buckets = [bts for bts in self._buckets if bts < cutoff_bucket]
        for bts in stale_buckets:
            del self._buckets[bts]

        stale_fps = [fp for fp, ts in self._fingerprints.items() if ts < cutoff]
        for fp in stale_fps:
            del self._fingerprints[fp]

    def near_capacity_minutes(self, direction: str, capacity_mbps: float) -> float:
        """Count minutes where buckets >= 90% capacity in the retained window."""
        if capacity_mbps <= 0:
            return 0.0
        threshold = capacity_mbps * 0.90
        key = f"{direction}_mbps"
        count = sum(
            1 for bucket in self._buckets.values() if bucket.get(key, 0.0) >= threshold
        )
        return round(count * WAN_PEAK_BUCKET_SECONDS / 60.0, 2)

    def capacity_distribution(
        self, direction: str, capacity_mbps: float
    ) -> dict[str, int]:
        """Return bucket counts at various capacity thresholds."""
        if capacity_mbps <= 0:
            return {}
        key = f"{direction}_mbps"
        dist: dict[str, int] = {}
        for pct in _CAPACITY_THRESHOLDS:
            threshold = capacity_mbps * pct
            label = f"buckets_gte_{int(pct * 100)}pct"
            dist[label] = sum(
                1
                for bucket in self._buckets.values()
                if bucket.get(key, 0.0) >= threshold
            )
        return dist
