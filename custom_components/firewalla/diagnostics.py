"""Diagnostics support for Firewalla."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_TOKEN, DOMAIN

TO_REDACT = {CONF_ACCESS_TOKEN, "access_token", "token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    data = coordinator.data or {}

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "polling": {
            "base_poll_interval": coordinator._base_poll_interval,
            "full_rules_interval": coordinator._full_rules_interval,
            "devices_interval": coordinator._devices_interval,
            "users_cache_ttl": coordinator._users_cache_ttl,
            "poll_count": coordinator._poll_count,
        },
        "box_info": data.get("box_info"),
        "rule_count": data.get("rule_count"),
        "group_count": len(data.get("groups", {})),
        "time_limit_count": len(data.get("time_limits", {})),
        "data_keys": sorted(data.keys()) if data else [],
    }
