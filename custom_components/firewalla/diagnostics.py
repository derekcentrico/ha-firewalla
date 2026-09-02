"""Diagnostics support for Firewalla."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_TOKEN, CONF_BOX_GID, CONF_MSP_URL, DOMAIN

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_BOX_GID,
    CONF_MSP_URL,
    "access_token",
    "token",
    "gid",
    "box_gid",
    "msp_url",
    "name",
    "ip",
    "mac",
    "publicIP",
}

EXCLUDED_OPTIONS = {"include_filters", "exclude_filters", "dashboard_users"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if coordinator is None:
        return {
            "entry": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(
                {k: v for k, v in entry.options.items() if k not in EXCLUDED_OPTIONS},
                TO_REDACT,
            ),
            "coordinator_loaded": False,
        }

    data = coordinator.data or {}

    box_info = data.get("box_info", {})
    safe_box_info = {
        "model": box_info.get("model"),
        "online": box_info.get("online"),
        "version": box_info.get("version"),
    } if isinstance(box_info, dict) else {}

    filtered_options = {
        k: v for k, v in entry.options.items()
        if k not in EXCLUDED_OPTIONS
    }

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(filtered_options, TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "polling": {
            "base_poll_interval": coordinator._base_poll_interval,
            "full_rules_interval": coordinator._full_rules_interval,
            "devices_interval": coordinator._devices_interval,
            "users_cache_ttl": coordinator._users_cache_ttl,
            "poll_count": coordinator._poll_count,
        },
        "box_info": safe_box_info,
        "rule_count": data.get("rule_count"),
        "group_count": len(data.get("groups") or {}),
        "time_limit_count": len(data.get("time_limits") or {}),
        "data_keys": sorted(data.keys()) if data else [],
    }
