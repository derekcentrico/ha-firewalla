"""Tests for WAN peak estimator and related sensors."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.firewalla.const import (
    WAN_PEAK_BUCKET_SECONDS,
    WAN_PEAK_MAX_PAGES,
    WAN_PEAK_MIN_FLOW_BYTES,
    WAN_PEAK_RETENTION_SECONDS,
    WAN_PEAK_TRIGGER_MBPS,
)
from custom_components.firewalla.sensor import (
    FirewallaWanNearCapacitySensor,
    FirewallaWanPeakSensor,
)
from custom_components.firewalla.wan_peak import WanPeakEstimator


def _make_flow(
    ts: float,
    duration: float,
    download: int = 0,
    upload: int = 0,
    gid: str = "gid-1",
    protocol: str = "tcp",
    device_id: str = "dev-1",
    source_id: str = "src-1",
    dest_id: str = "dst-1",
) -> dict[str, Any]:
    return {
        "ts": ts,
        "duration": duration,
        "download": download,
        "upload": upload,
        "gid": gid,
        "protocol": protocol,
        "device": {"id": device_id},
        "source": {"id": source_id},
        "destination": {"id": dest_id},
    }


# ---------------------------------------------------------------------------
# WanPeakEstimator unit tests
# ---------------------------------------------------------------------------


class TestConcurrentFlowsSum:
    """Flow A 625MB/5s + Flow B 750MB/5s same interval => 2200 Mbps."""

    def test_concurrent_flows_sum(self):
        estimator = WanPeakEstimator()
        base = 1000
        flow_a = _make_flow(
            ts=base + 5,
            duration=5,
            download=625_000_000,
            device_id="dev-a",
        )
        flow_b = _make_flow(
            ts=base + 5,
            duration=5,
            download=750_000_000,
            device_id="dev-b",
        )
        result = estimator.process_flows([flow_a, flow_b])

        assert result["flows_allocated"] == 2
        assert result["download_peak_mbps"] == 2200.0


class TestSingleFlowMbps:
    def test_single_flow_mbps_calculation(self):
        estimator = WanPeakEstimator()
        # 150 MB in 5 seconds = 150_000_000 * 8 / 5 / 1_000_000 = 240 Mbps
        flow = _make_flow(ts=1005, duration=5, download=150_000_000)
        result = estimator.process_flows([flow])

        assert result["download_peak_mbps"] == 240.0
        assert result["flows_allocated"] == 1


class TestBucketAllocationPartialOverlap:
    def test_partial_bucket_weighting(self):
        estimator = WanPeakEstimator()
        # Flow spans 2 buckets: 3s in first, 2s in second
        # Start at 1003, end at 1008 => buckets 1000 and 1005
        # 100 MB over 5s = 160 Mbps uniform
        # Bucket 1000: overlap = 1005 - 1003 = 2s => fraction = 2/5 => 64 Mbps
        # Bucket 1005: overlap = 1008 - 1005 = 3s => fraction = 3/5 => 96 Mbps
        flow = _make_flow(ts=1008, duration=5, download=100_000_000)
        result = estimator.process_flows([flow])

        assert result["download_peak_mbps"] == 96.0


class TestDuplicateFlowNotDoubleCounted:
    def test_duplicate_flow_is_not_double_counted(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        result1 = estimator.process_flows([flow])
        result2 = estimator.process_flows([flow])

        assert result1["flows_allocated"] == 1
        assert result2["flows_allocated"] == 0
        # Second call touched no buckets, so cycle peak is 0
        assert result2["download_peak_mbps"] == 0.0
        # But the underlying bucket value was not doubled
        assert estimator._buckets[1000]["download_mbps"] == pytest.approx(
            100_000_000 * 8 / 5 / 1_000_000, rel=0.01
        )


class TestZeroDurationFlowSkipped:
    def test_zero_duration_flow_skipped(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=0, download=100_000_000)
        result = estimator.process_flows([flow])

        assert result["flows_allocated"] == 0
        assert result["flows_skipped"] == 1
        assert result["download_peak_mbps"] == 0.0


class TestInvalidFlowSkipped:
    def test_missing_duration(self):
        estimator = WanPeakEstimator()
        flow = {"ts": 1005, "download": 100}
        result = estimator.process_flows([flow])
        assert result["flows_allocated"] == 0

    def test_non_dict_flow(self):
        estimator = WanPeakEstimator()
        result = estimator.process_flows(["not_a_dict", 42])
        assert result["flows_skipped"] == 2


class TestEmptyFlowList:
    def test_empty_flow_list(self):
        estimator = WanPeakEstimator()
        result = estimator.process_flows([])

        assert result["download_peak_mbps"] == 0.0
        assert result["upload_peak_mbps"] == 0.0
        assert result["total_peak_mbps"] == 0.0
        assert result["flows_allocated"] == 0


class TestPruning:
    def test_24h_pruning(self):
        estimator = WanPeakEstimator()
        old_ts = 1000
        new_ts = old_ts + WAN_PEAK_RETENTION_SECONDS + 100

        estimator.process_flows(
            [_make_flow(ts=old_ts + 5, duration=5, download=100_000_000)]
        )
        assert len(estimator._buckets) > 0

        estimator.process_flows(
            [
                _make_flow(
                    ts=new_ts + 5, duration=5, download=50_000_000, device_id="dev-2"
                )
            ]
        )
        estimator.prune(new_ts + 5)

        for bts in estimator._buckets:
            assert bts >= new_ts - WAN_PEAK_RETENTION_SECONDS


class TestFlowsFromSeparateSamplesCombine:
    def test_flows_ending_in_separate_samples_combine_in_old_buckets(self):
        estimator = WanPeakEstimator()
        # Two flows overlapping the same 5-second bucket but ending at different times
        flow_a = _make_flow(
            ts=1010, duration=10, download=200_000_000, device_id="dev-a"
        )
        flow_b = _make_flow(
            ts=1012, duration=7, download=100_000_000, device_id="dev-b"
        )

        estimator.process_flows([flow_a])
        estimator.process_flows([flow_b])

        # Bucket 1005 should have contributions from both flows
        bucket = estimator._buckets.get(1005)
        assert bucket is not None
        assert bucket["download_mbps"] > 0


class TestNearCapacityMinutes:
    def test_near_capacity_minutes(self):
        estimator = WanPeakEstimator()
        capacity = 1000.0
        # Create 12 buckets at 5s each = 60s = 1 minute
        # Each at 950 Mbps (>=90% of 1000)
        for i in range(12):
            ts = 1000 + (i + 1) * WAN_PEAK_BUCKET_SECONDS
            flow = _make_flow(
                ts=ts,
                duration=WAN_PEAK_BUCKET_SECONDS,
                download=int(950 * 1_000_000 / 8 * WAN_PEAK_BUCKET_SECONDS),
                device_id=f"dev-{i}",
            )
            estimator.process_flows([flow])

        minutes = estimator.near_capacity_minutes("download", capacity)
        assert minutes == 1.0

    def test_zero_capacity_returns_zero(self):
        estimator = WanPeakEstimator()
        assert estimator.near_capacity_minutes("download", 0) == 0.0


class TestCapacityDistribution:
    def test_capacity_distribution(self):
        estimator = WanPeakEstimator()
        capacity = 1000.0

        # Bucket at 600 Mbps (60% - above 50%, below 75%)
        estimator._buckets[1000] = {"download_mbps": 600.0, "upload_mbps": 0.0}
        # Bucket at 800 Mbps (80% - above 75%, below 90%)
        estimator._buckets[1005] = {"download_mbps": 800.0, "upload_mbps": 0.0}
        # Bucket at 950 Mbps (95% - above 90%, at 95%)
        estimator._buckets[1010] = {"download_mbps": 950.0, "upload_mbps": 0.0}
        # Bucket at 980 Mbps (98% - above 95%)
        estimator._buckets[1015] = {"download_mbps": 980.0, "upload_mbps": 0.0}

        dist = estimator.capacity_distribution("download", capacity)

        assert dist["buckets_gte_50pct"] == 4
        assert dist["buckets_gte_75pct"] == 3
        assert dist["buckets_gte_90pct"] == 2
        assert dist["buckets_gte_95pct"] == 2

    def test_zero_capacity_returns_empty(self):
        estimator = WanPeakEstimator()
        assert estimator.capacity_distribution("download", 0) == {}


class TestAbove100PercentLegal:
    def test_above_100_percent_capacity(self):
        estimator = WanPeakEstimator()
        capacity = 1000.0

        # Flow at 1200 Mbps (120% of capacity)
        flow = _make_flow(
            ts=1005,
            duration=5,
            download=int(1200 * 1_000_000 / 8 * 5),
        )
        estimator.process_flows([flow])

        assert estimator.near_capacity_minutes("download", capacity) > 0
        dist = estimator.capacity_distribution("download", capacity)
        assert dist["buckets_gte_95pct"] >= 1


# ---------------------------------------------------------------------------
# Sensor entity tests
# ---------------------------------------------------------------------------


def _peak_coordinator(peak_data=None, near_cap=None, cap_dist=None):
    """Build a fake coordinator with peak data."""
    wan = {}
    wan["peak"] = peak_data
    if near_cap:
        wan.update(near_cap)
    if cap_dist:
        wan.update(cap_dist)

    return SimpleNamespace(
        box_gid="test-gid-12345678",
        last_update_success=True,
        _wan_download_capacity=2437.0,
        _wan_upload_capacity=880.0,
        data={"wan_throughput": wan},
    )


DEFAULT_PEAK = {
    "download_peak_mbps": 1850.5,
    "upload_peak_mbps": 320.3,
    "total_peak_mbps": 2170.8,
    "download_peak_timestamp": 1000,
    "upload_peak_timestamp": 1005,
    "total_peak_timestamp": 1000,
    "bucket_seconds": 5,
    "estimation_method": "completed_flows",
    "flows_allocated": 15,
    "flows_skipped": 2,
    "detail_flow_count": 17,
    "detail_pages": 1,
    "detail_truncated": False,
    "download_coverage_pct": 97.2,
    "upload_coverage_pct": 93.1,
    "min_flow_bytes": 1_000_000,
}


class TestPeakSensor:
    def test_download_peak_value(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "download")
        assert sensor.native_value == 1850.5
        assert sensor.name == "WAN Download Peak Estimate"
        assert sensor.available is True

    def test_upload_peak_value(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "upload")
        assert sensor.native_value == 320.3

    def test_total_peak_value(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "total")
        assert sensor.native_value == 2170.8

    def test_unavailable_when_no_peak(self):
        coord = _peak_coordinator(None)
        sensor = FirewallaWanPeakSensor(coord, "download")
        assert sensor.available is False
        assert sensor.native_value is None

    def test_attributes(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "download")
        attrs = sensor.extra_state_attributes
        assert attrs["peak_timestamp"] == 1000
        assert attrs["bucket_seconds"] == 5
        assert attrs["estimation_method"] == "completed_flows"
        assert attrs["coverage_percent"] == 97.2
        assert attrs["detail_flow_count"] == 17
        assert attrs["detail_truncated"] is False
        assert attrs["min_flow_bytes"] == 1_000_000

    def test_total_coverage_is_none(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "total")
        attrs = sensor.extra_state_attributes
        assert attrs["coverage_percent"] is None

    def test_unique_id(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanPeakSensor(coord, "download")
        assert sensor.unique_id == "firewalla_test-gid-12345678_wan_download_peak"


class TestNearCapacitySensor:
    def test_near_capacity_value(self):
        coord = _peak_coordinator(
            DEFAULT_PEAK,
            near_cap={
                "download_near_capacity_minutes": 3.5,
                "upload_near_capacity_minutes": 0.0,
            },
            cap_dist={
                "download_capacity_distribution": {
                    "buckets_gte_50pct": 100,
                    "buckets_gte_75pct": 42,
                    "buckets_gte_90pct": 12,
                    "buckets_gte_95pct": 3,
                },
                "upload_capacity_distribution": {},
            },
        )
        sensor = FirewallaWanNearCapacitySensor(coord, "download")
        assert sensor.native_value == 3.5
        assert sensor.name == "WAN Download Near Capacity"
        assert sensor.available is True

    def test_unavailable_without_key(self):
        coord = _peak_coordinator(DEFAULT_PEAK)
        sensor = FirewallaWanNearCapacitySensor(coord, "download")
        assert sensor.available is False

    def test_attributes_include_distribution(self):
        coord = _peak_coordinator(
            DEFAULT_PEAK,
            near_cap={"download_near_capacity_minutes": 1.0},
            cap_dist={
                "download_capacity_distribution": {
                    "buckets_gte_50pct": 50,
                    "buckets_gte_75pct": 20,
                    "buckets_gte_90pct": 5,
                    "buckets_gte_95pct": 1,
                }
            },
        )
        sensor = FirewallaWanNearCapacitySensor(coord, "download")
        attrs = sensor.extra_state_attributes
        assert attrs["capacity_mbps"] == 2437.0
        assert attrs["threshold_pct"] == 90
        assert attrs["buckets_gte_90pct"] == 5


# ---------------------------------------------------------------------------
# Integration-level tests (coordinator interaction)
# ---------------------------------------------------------------------------


class TestDetailSkippedBelowThreshold:
    @pytest.mark.asyncio
    async def test_detail_skipped_below_threshold(self):
        """Grouped result < 25 Mbps should not trigger detail fetch."""
        from custom_components.firewalla.coordinator import (
            FirewallaDataUpdateCoordinator,
        )

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = AsyncMock()
        coord.api.get_flow_bandwidth = AsyncMock(
            return_value={"results": [{"download": 1_000_000, "upload": 500_000}]}
        )
        coord.api.get_flow_details = AsyncMock()
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 2437.0
        coord._wan_upload_capacity = 880.0
        coord._wan_peak_estimator = WanPeakEstimator()
        coord._wan_last_peak = None
        coord._wan_store_dirty = False
        coord._async_save_wan_state = AsyncMock()
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(
            coord, 1000.0
        )

        # < 25 Mbps, so detail fetch should NOT be called
        coord.api.get_flow_details.assert_not_called()
        assert result["peak"] is None


class TestDetailTriggeredAboveThreshold:
    @pytest.mark.asyncio
    async def test_detail_triggered_above_threshold(self):
        """Grouped result >= 25 Mbps should trigger detail fetch."""
        from custom_components.firewalla.coordinator import (
            FirewallaDataUpdateCoordinator,
        )

        # 25 Mbps over 120s = 25 * 120 / 8 * 1_000_000 = 375_000_000 bytes
        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = AsyncMock()
        coord.api.get_flow_bandwidth = AsyncMock(
            return_value={
                "results": [{"download": 375_000_000, "upload": 1_000_000}]
            }
        )
        coord.api.get_flow_details = AsyncMock(
            return_value=(
                [_make_flow(ts=1005, duration=5, download=375_000_000)],
                1,
                False,
            )
        )
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 2437.0
        coord._wan_upload_capacity = 880.0
        coord._wan_peak_estimator = WanPeakEstimator()
        coord._wan_last_peak = None
        coord._wan_store_dirty = False
        coord._async_save_wan_state = AsyncMock()
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(
            coord, 1000.0
        )

        coord.api.get_flow_details.assert_called_once()
        assert result["peak"] is not None
        assert result["peak"]["flows_allocated"] >= 1


class TestDetailApiFailurePreservesWanSensors:
    @pytest.mark.asyncio
    async def test_detail_failure_preserves_wan(self):
        """Exception in detail fetch should not break normal WAN data."""
        from custom_components.firewalla.coordinator import (
            FirewallaDataUpdateCoordinator,
        )

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = AsyncMock()
        coord.api.get_flow_bandwidth = AsyncMock(
            return_value={
                "results": [{"download": 500_000_000, "upload": 10_000_000}]
            }
        )
        coord.api.get_flow_details = AsyncMock(
            side_effect=Exception("API timeout")
        )
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 0
        coord._wan_upload_capacity = 0
        coord._wan_peak_estimator = WanPeakEstimator()
        coord._wan_last_peak = None
        coord._wan_store_dirty = False
        coord._async_save_wan_state = AsyncMock()
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(
            coord, 1000.0
        )

        assert result["download_mbps"] > 0
        assert result["peak"] is None


class TestCoverageCalculation:
    @pytest.mark.asyncio
    async def test_coverage_matches_bytes(self):
        from custom_components.firewalla.coordinator import (
            FirewallaDataUpdateCoordinator,
        )

        grouped_dl = 1_000_000_000
        detail_dl = 970_000_000

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = AsyncMock()
        coord.api.get_flow_bandwidth = AsyncMock(
            return_value={
                "results": [{"download": grouped_dl, "upload": 10_000_000}]
            }
        )
        coord.api.get_flow_details = AsyncMock(
            return_value=(
                [_make_flow(ts=1005, duration=5, download=detail_dl)],
                1,
                False,
            )
        )
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 2437.0
        coord._wan_upload_capacity = 880.0
        coord._wan_peak_estimator = WanPeakEstimator()
        coord._wan_last_peak = None
        coord._wan_store_dirty = False
        coord._async_save_wan_state = AsyncMock()
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(
            coord, 1000.0
        )

        expected_pct = round(detail_dl / grouped_dl * 100, 1)
        assert result["peak"]["download_coverage_pct"] == expected_pct


class TestTruncatedPagination:
    @pytest.mark.asyncio
    async def test_truncated_flag(self):
        from custom_components.firewalla.coordinator import (
            FirewallaDataUpdateCoordinator,
        )

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = AsyncMock()
        coord.api.get_flow_bandwidth = AsyncMock(
            return_value={
                "results": [{"download": 500_000_000, "upload": 10_000_000}]
            }
        )
        coord.api.get_flow_details = AsyncMock(
            return_value=(
                [_make_flow(ts=1005, duration=5, download=500_000_000)],
                WAN_PEAK_MAX_PAGES,
                True,
            )
        )
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 0
        coord._wan_upload_capacity = 0
        coord._wan_peak_estimator = WanPeakEstimator()
        coord._wan_last_peak = None
        coord._wan_store_dirty = False
        coord._async_save_wan_state = AsyncMock()
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(
            coord, 1000.0
        )

        assert result["peak"]["detail_truncated"] is True
        assert result["peak"]["detail_pages"] == WAN_PEAK_MAX_PAGES


class TestLocalFlowExclusionInQuery:
    @pytest.mark.asyncio
    async def test_local_excluded(self):
        from custom_components.firewalla.coordinator import FirewallaMSPClient

        session = MagicMock()
        ctx = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"results": [], "count": 0})
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.request = MagicMock(return_value=ctx)

        client = FirewallaMSPClient(session, "test.firewalla.net", "token")
        await client.get_flow_details("gid-1", 1000, 1120)

        call_kwargs = session.request.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        query = params.get("query", "")
        assert "-direction:local" in query
        assert "status:ok" in query
        assert "total:>1MB" in query


class TestMaxPeak:
    """Test the 24h max peak method."""

    def test_max_peak_download(self):
        estimator = WanPeakEstimator()
        estimator._buckets = {
            1000: {"download_mbps": 500.0, "upload_mbps": 100.0},
            1005: {"download_mbps": 1200.0, "upload_mbps": 50.0},
            1010: {"download_mbps": 800.0, "upload_mbps": 200.0},
        }
        val, ts = estimator.max_peak("download")
        assert val == 1200.0
        assert ts == 1005

    def test_max_peak_upload(self):
        estimator = WanPeakEstimator()
        estimator._buckets = {
            1000: {"download_mbps": 500.0, "upload_mbps": 100.0},
            1005: {"download_mbps": 1200.0, "upload_mbps": 50.0},
            1010: {"download_mbps": 800.0, "upload_mbps": 200.0},
        }
        val, ts = estimator.max_peak("upload")
        assert val == 200.0
        assert ts == 1010

    def test_max_peak_total(self):
        estimator = WanPeakEstimator()
        estimator._buckets = {
            1000: {"download_mbps": 500.0, "upload_mbps": 100.0},
            1005: {"download_mbps": 1200.0, "upload_mbps": 50.0},
            1010: {"download_mbps": 800.0, "upload_mbps": 200.0},
        }
        val, ts = estimator.max_peak("total")
        assert val == 1250.0
        assert ts == 1005

    def test_max_peak_empty(self):
        estimator = WanPeakEstimator()
        val, ts = estimator.max_peak("download")
        assert val == 0.0
        assert ts is None


class TestLastPeakSensor:
    """Test the last-peak sensor."""

    def test_last_peak_value(self):
        from custom_components.firewalla.sensor import FirewallaWanLastPeakSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={
                "wan_throughput": {
                    "last_peak": {
                        "download_peak_mbps": 812.5,
                        "upload_peak_mbps": 120.0,
                        "total_peak_mbps": 932.5,
                        "download_peak_timestamp": 1000,
                        "upload_peak_timestamp": 1005,
                        "total_peak_timestamp": 1000,
                        "estimation_method": "completed_flows",
                        "download_coverage_pct": 97.2,
                        "upload_coverage_pct": 95.1,
                        "detail_flow_count": 42,
                        "detail_truncated": False,
                    }
                }
            },
        )
        sensor = FirewallaWanLastPeakSensor(coord, "download")
        assert sensor.native_value == 812.5
        assert sensor.available is True
        assert sensor.extra_state_attributes["peak_timestamp"] == 1000
        assert sensor.extra_state_attributes["coverage_percent"] == 97.2

    def test_last_peak_unavailable_when_none(self):
        from custom_components.firewalla.sensor import FirewallaWanLastPeakSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={"wan_throughput": {"last_peak": None}},
        )
        sensor = FirewallaWanLastPeakSensor(coord, "download")
        assert sensor.available is False
        assert sensor.native_value is None


class TestMaxPeakSensor:
    """Test the 24h max peak sensor."""

    def test_max_peak_sensor_value(self):
        from custom_components.firewalla.sensor import FirewallaWanMaxPeakSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={
                "wan_throughput": {
                    "download_max_peak_mbps": 1850.0,
                    "download_max_peak_timestamp": 1005,
                    "upload_max_peak_mbps": 450.0,
                    "upload_max_peak_timestamp": 1010,
                    "total_max_peak_mbps": 2100.0,
                    "total_max_peak_timestamp": 1005,
                }
            },
        )
        dl = FirewallaWanMaxPeakSensor(coord, "download")
        assert dl.native_value == 1850.0
        assert dl.available is True
        assert dl.extra_state_attributes["peak_timestamp"] == 1005

        total = FirewallaWanMaxPeakSensor(coord, "total")
        assert total.native_value == 2100.0

    def test_max_peak_sensor_unavailable(self):
        from custom_components.firewalla.sensor import FirewallaWanMaxPeakSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={"wan_throughput": {}},
        )
        sensor = FirewallaWanMaxPeakSensor(coord, "download")
        assert sensor.available is False


class TestNearCapacityMaxAttrs:
    """Test that near-capacity sensors include 24h max in attributes."""

    def test_near_capacity_includes_max(self):
        from custom_components.firewalla.sensor import FirewallaWanNearCapacitySensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            _wan_download_capacity=2437.0,
            data={
                "wan_throughput": {
                    "download_near_capacity_minutes": 0.0,
                    "download_capacity_distribution": {
                        "buckets_gte_50pct": 0,
                        "buckets_gte_75pct": 0,
                        "buckets_gte_90pct": 0,
                        "buckets_gte_95pct": 0,
                    },
                    "download_max_peak_mbps": 620.0,
                    "download_max_utilization_pct": 25.4,
                }
            },
        )
        sensor = FirewallaWanNearCapacitySensor(coord, "download")
        attrs = sensor.extra_state_attributes
        assert attrs["max_peak_24h_mbps"] == 620.0
        assert attrs["max_utilization_24h_pct"] == 25.4
        assert attrs["capacity_mbps"] == 2437.0


class TestPersistence:
    """Test serialize/restore cycle."""

    def test_roundtrip(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])
        assert len(estimator._buckets) > 0
        assert len(estimator._fingerprints) == 1

        saved = estimator.to_dict()
        assert saved["schema_version"] == 2

        restored = WanPeakEstimator()
        restored.restore(saved, 1010.0)

        assert len(restored._buckets) == len(estimator._buckets)
        assert len(restored._fingerprints) == len(estimator._fingerprints)

    def test_restore_prunes_old_data(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])
        saved = estimator.to_dict()

        restored = WanPeakEstimator()
        future = 1005 + WAN_PEAK_RETENTION_SECONDS + 100
        restored.restore(saved, future)

        assert len(restored._buckets) == 0
        assert len(restored._fingerprints) == 0

    def test_duplicate_rejected_after_restore(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])
        saved = estimator.to_dict()

        restored = WanPeakEstimator()
        restored.restore(saved, 1010.0)
        result = restored.process_flows([flow])

        assert result["flows_allocated"] == 0

    def test_restore_invalid_data(self):
        estimator = WanPeakEstimator()
        estimator.restore({"schema_version": 99}, 1000.0)
        assert len(estimator._buckets) == 0

        estimator.restore("not a dict", 1000.0)
        assert len(estimator._buckets) == 0


class TestTouchedBucketsRegression:
    """Verify that unrelated historical peaks are not pulled into the current cycle."""

    def test_old_peak_not_reported_as_current(self):
        estimator = WanPeakEstimator()

        # Create an old 1800 Mbps peak at t=1000
        old_flow = _make_flow(
            ts=1005, duration=5, download=int(1800 * 1_000_000 / 8 * 5),
            device_id="old-device",
        )
        result_old = estimator.process_flows([old_flow])
        assert result_old["download_peak_mbps"] == 1800.0

        # Later, a short flow at t=50000 (does NOT overlap old bucket)
        new_flow = _make_flow(
            ts=50005, duration=5, download=200_000_000,
            device_id="new-device",
        )
        result_new = estimator.process_flows([new_flow])

        # The cycle peak should be the new flow's rate, not the old 1800 Mbps
        assert result_new["download_peak_mbps"] == 320.0
        # The old 1800 bucket still exists but was not touched this cycle
        assert estimator._buckets[1000]["download_mbps"] == pytest.approx(1800.0, rel=0.01)


class TestDailySummaries:
    """Test daily max tracking from bucket data."""

    def test_daily_max_created(self):
        estimator = WanPeakEstimator()
        # Bucket at timestamp 1000 (1970-01-01 UTC)
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])

        assert len(estimator._daily_summaries) == 1
        day = list(estimator._daily_summaries.values())[0]
        assert day["download_max_mbps"] == 160.0

    def test_larger_peak_replaces_smaller(self):
        estimator = WanPeakEstimator()
        small = _make_flow(ts=1005, duration=5, download=50_000_000, device_id="a")
        big = _make_flow(ts=1010, duration=5, download=200_000_000, device_id="b")
        estimator.process_flows([small])
        estimator.process_flows([big])

        day = list(estimator._daily_summaries.values())[0]
        assert day["download_max_mbps"] == 320.0

    def test_smaller_peak_does_not_replace(self):
        estimator = WanPeakEstimator()
        big = _make_flow(ts=1005, duration=5, download=200_000_000, device_id="a")
        small = _make_flow(ts=1010, duration=5, download=50_000_000, device_id="b")
        estimator.process_flows([big])
        estimator.process_flows([small])

        day = list(estimator._daily_summaries.values())[0]
        assert day["download_max_mbps"] == 320.0
        assert day["download_max_timestamp"] == 1000

    def test_retroactive_previous_day_update(self):
        estimator = WanPeakEstimator()
        # First day: modest flow
        day1_flow = _make_flow(ts=50000, duration=5, download=50_000_000, device_id="a")
        estimator.process_flows([day1_flow])
        day1_date = estimator._date_key(50000)
        day1_max = estimator._daily_summaries[day1_date]["download_max_mbps"]

        # Later: a long flow that started on day1 but ends on day2
        # This flow's buckets span into day1 territory
        day2_flow = _make_flow(
            ts=90000, duration=50000, download=500_000_000, device_id="b"
        )
        estimator.process_flows([day2_flow])

        # Day1's max should be updated if the long flow raised a day1 bucket
        day1_max_after = estimator._daily_summaries[day1_date]["download_max_mbps"]
        assert day1_max_after >= day1_max

    def test_timestamp_belongs_to_winning_peak(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])

        day = list(estimator._daily_summaries.values())[0]
        assert day["download_max_timestamp"] == 1000

    def test_7day_window_expiration(self):
        estimator = WanPeakEstimator()
        # Create flows on 8 different days
        for i in range(8):
            ts = 86400 * i + 5
            flow = _make_flow(
                ts=ts, duration=5, download=(i + 1) * 10_000_000, device_id=f"d{i}"
            )
            estimator.process_flows([flow])

        # 7-day max should be from the 7 most recent days
        val, _ = estimator.rolling_max_peak("download", 7)
        # Day 7 (i=7) has the highest value: 80MB/5s = 128 Mbps
        assert val == 128.0

        # Day 0 (oldest, i=0) should NOT be in the 7-day window
        day0_val = 10_000_000 * 8 / 5 / 1_000_000
        assert val != round(day0_val, 1) or val > round(day0_val, 1)

    def test_30day_window_expiration(self):
        estimator = WanPeakEstimator()
        # Create flows on 32 days
        for i in range(32):
            ts = 86400 * i + 5
            flow = _make_flow(
                ts=ts, duration=5, download=(i + 1) * 10_000_000, device_id=f"d{i}"
            )
            estimator.process_flows([flow])

        # Prune to 31 days max
        estimator.prune_daily_summaries()
        assert len(estimator._daily_summaries) <= 31

        val_30, _ = estimator.rolling_max_peak("download", 30)
        val_7, _ = estimator.rolling_max_peak("download", 7)
        assert val_30 >= val_7

    def test_persistence_roundtrip(self):
        estimator = WanPeakEstimator()
        flow = _make_flow(ts=1005, duration=5, download=100_000_000)
        estimator.process_flows([flow])
        assert len(estimator._daily_summaries) > 0

        saved = estimator.to_dict()
        assert "daily_summaries" in saved
        assert saved["schema_version"] == 2

        restored = WanPeakEstimator()
        restored.restore(saved, 1010.0)
        assert len(restored._daily_summaries) == len(estimator._daily_summaries)

        val_orig, _ = estimator.rolling_max_peak("download", 7)
        val_restored, _ = restored.rolling_max_peak("download", 7)
        assert val_orig == val_restored

    def test_malformed_daily_summary_data(self):
        estimator = WanPeakEstimator()
        data = {
            "schema_version": 2,
            "buckets": {},
            "fingerprints": {},
            "daily_summaries": {
                "2024-01-01": "not a dict",
                42: {"download_max_mbps": 100},
                "2024-01-02": {"download_max_mbps": 200.0, "download_max_timestamp": 1000},
            },
        }
        estimator.restore(data, 2000000000.0)
        # Only the valid entry should be restored (but may be pruned by date)
        # The malformed ones should not cause errors

    def test_empty_summaries_return_zero(self):
        estimator = WanPeakEstimator()
        val, ts = estimator.rolling_max_peak("download", 7)
        assert val == 0.0
        assert ts is None


class TestRollingMaxSensor:
    """Test the rolling max peak sensor entity."""

    def test_sensor_value_and_attrs(self):
        from custom_components.firewalla.sensor import FirewallaWanRollingMaxSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={
                "wan_throughput": {
                    "download_7d_max_peak_mbps": 1250.0,
                    "download_7d_max_peak_timestamp": 86400,
                    "download_30d_max_peak_mbps": 1850.0,
                    "download_30d_max_peak_timestamp": 172800,
                }
            },
        )
        sensor_7d = FirewallaWanRollingMaxSensor(coord, "download", 7)
        assert sensor_7d.native_value == 1250.0
        assert sensor_7d.available is True
        attrs = sensor_7d.extra_state_attributes
        assert attrs["window_days"] == 7
        assert attrs["peak_timestamp"] == 86400

        sensor_30d = FirewallaWanRollingMaxSensor(coord, "download", 30)
        assert sensor_30d.native_value == 1850.0

    def test_sensor_unavailable(self):
        from custom_components.firewalla.sensor import FirewallaWanRollingMaxSensor

        coord = SimpleNamespace(
            box_gid="test-gid-12345678",
            last_update_success=True,
            data={"wan_throughput": {}},
        )
        sensor = FirewallaWanRollingMaxSensor(coord, "download", 7)
        assert sensor.available is False
        assert sensor.native_value is None
