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
from datetime import datetime, timezone
from typing import Any

from .const import WAN_PEAK_BUCKET_SECONDS, WAN_PEAK_RETENTION_SECONDS

_DAILY_SUMMARY_RETENTION = 31

_LOGGER = logging.getLogger(__name__)

_CAPACITY_THRESHOLDS = (0.50, 0.75, 0.90, 0.95)


class WanPeakEstimator:
    """Reconstruct bandwidth peaks from completed flow records."""

    def __init__(self) -> None:
        self._buckets: dict[int, dict[str, float]] = {}
        self._fingerprints: dict[bytes, float] = {}
        self._daily_summaries: dict[str, dict[str, Any]] = {}

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
        return hashlib.md5(key.encode(), usedforsecurity=False).digest()

    def _allocate_flow(self, flow: dict) -> set[int]:
        """Allocate one flow into 5-second buckets. Returns set of modified bucket timestamps."""
        try:
            ts = float(flow.get("ts", 0))
            duration = float(flow.get("duration", 0))
            dl_bytes = max(int(flow.get("download", 0)), 0)
            ul_bytes = max(int(flow.get("upload", 0)), 0)
        except (TypeError, ValueError):
            return set()

        if duration <= 0 or ts <= 0:
            return set()

        fp = self._flow_fingerprint(flow)
        if fp in self._fingerprints:
            return set()
        self._fingerprints[fp] = ts

        flow_start = max(ts - duration, ts - WAN_PEAK_RETENTION_SECONDS)
        dl_mbps = dl_bytes * 8 / duration / 1_000_000
        ul_mbps = ul_bytes * 8 / duration / 1_000_000

        first_bucket = self._bucket_ts(flow_start)
        last_bucket = max(
            first_bucket,
            self._bucket_ts(ts - 0.001) if ts > flow_start else first_bucket,
        )

        touched: set[int] = set()
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
                touched.add(bucket_ts)

            bucket_ts += WAN_PEAK_BUCKET_SECONDS

        return touched

    def process_flows(self, flows: list[dict]) -> dict[str, Any]:
        """Allocate flows into buckets and return cycle peak results."""
        touched_buckets: set[int] = set()
        allocated = 0
        skipped = 0

        for flow in flows:
            if not isinstance(flow, dict):
                skipped += 1
                continue
            modified = self._allocate_flow(flow)
            if modified:
                allocated += 1
                touched_buckets.update(modified)
            else:
                skipped += 1

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

        self.update_daily_summaries(touched_buckets)
        self.prune_daily_summaries()

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

    def max_peak(self, direction: str) -> tuple[float, int | None]:
        """Return the highest reconstructed value and its timestamp from retained buckets."""
        if not self._buckets:
            return 0.0, None

        best_value = 0.0
        best_ts = None

        for ts, bucket in self._buckets.items():
            if direction == "total":
                value = bucket.get("download_mbps", 0.0) + bucket.get(
                    "upload_mbps", 0.0
                )
            else:
                value = bucket.get(f"{direction}_mbps", 0.0)

            if value > best_value:
                best_value = value
                best_ts = ts

        return round(best_value, 1), best_ts

    @staticmethod
    def _date_key(ts: int) -> str:
        """Convert a bucket timestamp to a YYYY-MM-DD date string."""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def update_daily_summaries(self, touched_buckets: set[int]) -> None:
        """Update daily max values from modified buckets."""
        for bts in touched_buckets:
            bucket = self._buckets.get(bts)
            if not bucket:
                continue
            date = self._date_key(bts)
            dl = bucket.get("download_mbps", 0.0)
            ul = bucket.get("upload_mbps", 0.0)

            day = self._daily_summaries.get(date)
            if day is None:
                self._daily_summaries[date] = {
                    "date": date,
                    "download_max_mbps": round(dl, 1),
                    "download_max_timestamp": bts,
                    "upload_max_mbps": round(ul, 1),
                    "upload_max_timestamp": bts,
                }
                continue

            if dl > day.get("download_max_mbps", 0.0):
                day["download_max_mbps"] = round(dl, 1)
                day["download_max_timestamp"] = bts
            if ul > day.get("upload_max_mbps", 0.0):
                day["upload_max_mbps"] = round(ul, 1)
                day["upload_max_timestamp"] = bts

    def prune_daily_summaries(self) -> None:
        """Keep only the most recent daily summaries."""
        if len(self._daily_summaries) <= _DAILY_SUMMARY_RETENTION:
            return
        sorted_dates = sorted(self._daily_summaries.keys())
        for date in sorted_dates[:-_DAILY_SUMMARY_RETENTION]:
            del self._daily_summaries[date]

    def rolling_max_peak(
        self, direction: str, window_days: int
    ) -> tuple[float, int | None]:
        """Return the max peak over the most recent N calendar days."""
        if not self._daily_summaries:
            return 0.0, None

        sorted_dates = sorted(self._daily_summaries.keys(), reverse=True)
        best_value = 0.0
        best_ts = None

        for date in sorted_dates[:window_days]:
            day = self._daily_summaries[date]
            key = f"{direction}_max_mbps"
            ts_key = f"{direction}_max_timestamp"
            val = day.get(key, 0.0)
            if val > best_value:
                best_value = val
                best_ts = day.get(ts_key)

        return round(best_value, 1), best_ts

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for persistence."""
        return {
            "schema_version": 2,
            "buckets": {str(k): v for k, v in self._buckets.items()},
            "fingerprints": {fp.hex(): ts for fp, ts in self._fingerprints.items()},
            "daily_summaries": dict(self._daily_summaries),
        }

    def restore(self, data: dict[str, Any], now: float) -> None:
        """Restore state from persisted data and prune stale entries."""
        if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
            return
        raw_buckets = data.get("buckets", {})
        if isinstance(raw_buckets, dict):
            for k, v in raw_buckets.items():
                try:
                    self._buckets[int(k)] = v
                except (ValueError, TypeError):
                    continue
        raw_fps = data.get("fingerprints", {})
        if isinstance(raw_fps, dict):
            for hex_fp, ts in raw_fps.items():
                try:
                    self._fingerprints[bytes.fromhex(hex_fp)] = float(ts)
                except (ValueError, TypeError):
                    continue
        raw_daily = data.get("daily_summaries", {})
        if isinstance(raw_daily, dict):
            for date, summary in raw_daily.items():
                if isinstance(date, str) and isinstance(summary, dict):
                    self._daily_summaries[date] = summary
        self.prune(now)
        self.prune_daily_summaries()
