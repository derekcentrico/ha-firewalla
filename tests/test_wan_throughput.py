"""Tests for WAN throughput feature."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.firewalla.coordinator import FirewallaDataUpdateCoordinator
from custom_components.firewalla.sensor import (
    FirewallaWanSensor,
    FirewallaWanUtilizationSensor,
)


def _wan_coordinator(wan_data: dict | None = None) -> SimpleNamespace:
    """Build a fake coordinator with WAN throughput data."""
    return SimpleNamespace(
        box_gid="test-gid-12345678",
        last_update_success=True,
        data={
            "wan_throughput": wan_data,
        },
    )


DEFAULT_WAN = {
    "download_mbps": 850.5,
    "upload_mbps": 120.3,
    "total_mbps": 970.8,
    "download_bytes": 12_750_000,
    "upload_bytes": 1_804_500,
    "sample_seconds": 120.0,
    "download_capacity_mbps": 2400.0,
    "upload_capacity_mbps": 880.0,
    "download_utilization": 35.4,
    "upload_utilization": 13.7,
}


class TestWanSensor:
    """Test WAN throughput sensors."""

    def test_download_value(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "download")
        assert sensor.native_value == 850.5
        assert sensor.name == "WAN Download"
        assert sensor.available is True

    def test_upload_value(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "upload")
        assert sensor.native_value == 120.3

    def test_total_value(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "total")
        assert sensor.native_value == 970.8

    def test_unavailable_when_no_data(self):
        coord = _wan_coordinator(None)
        sensor = FirewallaWanSensor(coord, "download")
        assert sensor.available is False
        assert sensor.native_value is None

    def test_attributes_include_sample_seconds(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "download")
        attrs = sensor.extra_state_attributes
        assert attrs["sample_seconds"] == 120.0
        assert attrs["bytes"] == 12_750_000

    def test_total_attributes_no_bytes(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "total")
        attrs = sensor.extra_state_attributes
        assert "bytes" not in attrs

    def test_unique_id_format(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanSensor(coord, "download")
        assert sensor.unique_id == "firewalla_test-gid-12345678_wan_download"


class TestWanUtilizationSensor:
    """Test WAN utilization sensors."""

    def test_download_utilization(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanUtilizationSensor(coord, "download")
        assert sensor.native_value == 35.4
        assert sensor.name == "WAN Download Utilization"
        assert sensor.available is True

    def test_upload_utilization(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanUtilizationSensor(coord, "upload")
        assert sensor.native_value == 13.7

    def test_unavailable_without_utilization_key(self):
        wan = dict(DEFAULT_WAN)
        del wan["download_utilization"]
        coord = _wan_coordinator(wan)
        sensor = FirewallaWanUtilizationSensor(coord, "download")
        assert sensor.available is False

    def test_attributes_include_capacity(self):
        coord = _wan_coordinator(DEFAULT_WAN)
        sensor = FirewallaWanUtilizationSensor(coord, "download")
        attrs = sensor.extra_state_attributes
        assert attrs["capacity_mbps"] == 2400.0
        assert attrs["current_mbps"] == 850.5


class TestFetchWanThroughput:
    """Test the coordinator's WAN throughput fetch logic."""

    @pytest.mark.asyncio
    async def test_mbps_calculation(self):
        mock_api = AsyncMock()
        mock_api.get_flow_bandwidth = AsyncMock(return_value={
            "results": [
                {"download": 150_000_000, "upload": 25_000_000},
            ]
        })
        mock_api.is_authenticated = True

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = mock_api
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 2400.0
        coord._wan_upload_capacity = 880.0
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(coord, 1000.0)

        assert result["download_bytes"] == 150_000_000
        assert result["upload_bytes"] == 25_000_000
        # 150M bytes * 8 / 120s / 1M = 10.0 Mbps
        assert result["download_mbps"] == 10.0
        # 25M bytes * 8 / 120s / 1M = ~1.667 Mbps
        assert abs(result["upload_mbps"] - 1.667) < 0.01
        assert result["download_utilization"] == round(10.0 / 2400.0 * 100, 1)

    @pytest.mark.asyncio
    async def test_empty_response(self):
        mock_api = AsyncMock()
        mock_api.get_flow_bandwidth = AsyncMock(return_value={"results": []})
        mock_api.is_authenticated = True

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = mock_api
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 0
        coord._wan_upload_capacity = 0
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(coord, 1000.0)

        assert result["download_mbps"] == 0.0
        assert result["upload_mbps"] == 0.0
        assert "download_utilization" not in result

    @pytest.mark.asyncio
    async def test_api_error_returns_cached(self):
        mock_api = AsyncMock()
        mock_api.get_flow_bandwidth = AsyncMock(side_effect=Exception("timeout"))

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = mock_api
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 0
        coord._wan_upload_capacity = 0
        coord.data = {"wan_throughput": {"download_mbps": 42.0}}

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(coord, 1000.0)

        assert result["download_mbps"] == 42.0

    @pytest.mark.asyncio
    async def test_list_response_format(self):
        mock_api = AsyncMock()
        mock_api.get_flow_bandwidth = AsyncMock(return_value=[
            {"download": 1_000_000, "upload": 500_000},
        ])

        coord = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coord.api = mock_api
        coord.box_gid = "test-gid"
        coord._wan_sample_interval = 120
        coord._wan_last_sample_end = 0
        coord._wan_download_capacity = 0
        coord._wan_upload_capacity = 0
        coord.data = None

        result = await FirewallaDataUpdateCoordinator._fetch_wan_throughput(coord, 1000.0)

        assert result["download_bytes"] == 1_000_000
        assert result["upload_bytes"] == 500_000
