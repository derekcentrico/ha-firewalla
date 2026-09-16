"""The Firewalla integration for rule management."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_POLL_INTERVAL,
    CONF_BOX_GID,
    CONF_DASHBOARD_USERS,
    CONF_DEVICES_INTERVAL,
    CONF_EXCLUDE_FILTERS,
    CONF_FULL_RULES_INTERVAL,
    CONF_INCLUDE_FILTERS,
    CONF_MSP_URL,
    CONF_USERS_CACHE_TTL,
    CONF_WAN_DOWNLOAD_CAPACITY,
    CONF_WAN_SAMPLE_INTERVAL,
    CONF_WAN_UPLOAD_CAPACITY,
    DEFAULT_BASE_POLL_INTERVAL,
    DEFAULT_DEVICES_INTERVAL,
    DEFAULT_FULL_RULES_INTERVAL,
    DEFAULT_USERS_CACHE_TTL,
    DEFAULT_WAN_DOWNLOAD_CAPACITY,
    DEFAULT_WAN_SAMPLE_INTERVAL,
    DEFAULT_WAN_UPLOAD_CAPACITY,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL_MAPPINGS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import FirewallaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

DASHBOARD_URL_PATH = "dashboard-firewalla"


def _build_user_section(name: str) -> dict:
    """Build a dashboard section for a single Firewalla user."""
    slug = name.strip().lower().replace(" ", "_")
    return {
        "type": "grid",
        "title": name.strip(),
        "cards": [
            {
                "type": "tile",
                "entity": f"binary_sensor.firewalla_group_{slug}_{slug}_active",
                "name": "Activity",
                "vertical": True,
                "color": "green",
            },
            {
                "type": "tile",
                "entity": f"switch.firewalla_group_{slug}_{slug}_internet_access",
                "name": "Internet",
                "vertical": True,
                "color": "blue",
            },
            {
                "type": "glance",
                "title": "Data Transferred",
                "show_state": True,
                "show_name": True,
                "entities": [
                    {
                        "entity": f"sensor.{slug}_upload",
                        "name": "Upload",
                        "icon": "mdi:arrow-up-bold",
                    },
                    {
                        "entity": f"sensor.{slug}_download",
                        "name": "Download",
                        "icon": "mdi:arrow-down-bold",
                    },
                ],
            },
            {
                "type": "history-graph",
                "hours_to_show": 1,
                "entities": [
                    {"entity": f"sensor.{slug}_download", "name": "Download"},
                    {"entity": f"sensor.{slug}_upload", "name": "Upload"},
                ],
            },
            {
                "type": "custom:auto-entities",
                "card": {"type": "entities", "title": "Time Limits"},
                "filter": {
                    "include": [
                        {
                            "entity_id": f"sensor.{slug}_*",
                            "attributes": {"quota_minutes": ">= 0"},
                            "options": {
                                "type": "custom:entity-progress-card",
                                "attribute": "usage_percent",
                                "unit": "%",
                                "severity": [
                                    {"from": 0, "to": 50, "color": "#4CAF50"},
                                    {"from": 50, "to": 80, "color": "#FFC107"},
                                    {"from": 80, "to": 100, "color": "#F44336"},
                                ],
                            },
                        },
                        {
                            "entity_id": f"sensor.firewalla_group_{slug}_{slug}_*_time",
                            "options": {
                                "type": "custom:entity-progress-card",
                                "attribute": "usage_percent",
                                "unit": "%",
                                "severity": [
                                    {"from": 0, "to": 50, "color": "#4CAF50"},
                                    {"from": 50, "to": 80, "color": "#FFC107"},
                                    {"from": 80, "to": 100, "color": "#F44336"},
                                ],
                            },
                        },
                    ]
                },
                "sort": {"method": "friendly_name"},
                "show_empty": False,
            },
            {
                "type": "custom:auto-entities",
                "card": {"type": "entities", "title": "Devices"},
                "filter": {
                    "include": [
                        {
                            "entity_id": f"binary_sensor.firewalla_group_{slug}_{slug}_*",
                            "not": {"entity_id": f"*_{slug}_active"},
                            "options": {"secondary_info": "last-changed"},
                        }
                    ]
                },
                "sort": {"method": "friendly_name"},
                "show_empty": False,
            },
            {
                "type": "custom:auto-entities",
                "card": {"type": "entities", "title": "Blocks"},
                "filter": {
                    "include": [
                        {"entity_id": f"switch.firewalla_group_{slug}_{slug}_*_block"},
                        {"entity_id": f"switch.{slug}_block_*"},
                    ],
                    "exclude": [
                        {"entity_id": "*_doh_block*"},
                        {"state": "unavailable"},
                    ],
                },
                "sort": {"method": "friendly_name"},
                "show_empty": False,
            },
        ],
    }


async def _async_generate_dashboard(hass: HomeAssistant, dashboard_users: str) -> None:
    """Generate and push the parental control dashboard for configured users."""
    users = [u.strip() for u in dashboard_users.split(",") if u.strip()]
    if not users:
        return

    config = {
        "title": "Firewalla Parental Controls",
        "views": [
            {
                "title": "Users",
                "path": "users",
                "icon": "mdi:shield-account",
                "type": "sections",
                "max_columns": min(len(users), 4),
                "badges": [
                    {
                        "type": "entity",
                        "entity": "button.firewalla_refresh",
                        "tap_action": {
                            "action": "perform-action",
                            "perform_action": "button.press",
                            "target": {
                                "entity_id": "button.firewalla_refresh",
                            },
                        },
                    },
                    {
                        "type": "entity",
                        "entity": "sensor.firewalla_rules_summary",
                    },
                ],
                "sections": [_build_user_section(name) for name in users],
            }
        ],
    }

    try:
        dashboards = hass.data["lovelace"].dashboards
        if DASHBOARD_URL_PATH not in dashboards:
            _LOGGER.warning(
                "Dashboard '%s' does not exist — create it in "
                "Settings > Dashboards first, then reload the integration",
                DASHBOARD_URL_PATH,
            )
            return
        await dashboards[DASHBOARD_URL_PATH].async_save(config)
        _LOGGER.info(
            "Generated parental control dashboard for %d users: %s",
            len(users),
            ", ".join(users),
        )
    except Exception as err:
        _LOGGER.warning(
            "Could not generate dashboard (create '%s' dashboard manually first): %s",
            DASHBOARD_URL_PATH,
            err,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Firewalla rule management from a config entry."""
    try:
        msp_domain = entry.data.get(CONF_MSP_URL)
        access_token = entry.data.get(CONF_ACCESS_TOKEN)
        box_gid = entry.data.get(CONF_BOX_GID)

        if not msp_domain or not access_token or not box_gid:
            raise ConfigEntryNotReady("Missing required configuration data")

        session = async_get_clientsession(hass)

        include_filters = entry.options.get(CONF_INCLUDE_FILTERS, [])
        exclude_filters = entry.options.get(CONF_EXCLUDE_FILTERS, [])

        base_poll_interval = entry.options.get(
            CONF_BASE_POLL_INTERVAL, DEFAULT_BASE_POLL_INTERVAL
        )
        full_rules_interval = entry.options.get(
            CONF_FULL_RULES_INTERVAL, DEFAULT_FULL_RULES_INTERVAL
        )
        devices_interval = entry.options.get(
            CONF_DEVICES_INTERVAL, DEFAULT_DEVICES_INTERVAL
        )
        users_cache_ttl = entry.options.get(
            CONF_USERS_CACHE_TTL, DEFAULT_USERS_CACHE_TTL
        )
        wan_sample_interval = entry.options.get(
            CONF_WAN_SAMPLE_INTERVAL, DEFAULT_WAN_SAMPLE_INTERVAL
        )
        wan_download_capacity = entry.options.get(
            CONF_WAN_DOWNLOAD_CAPACITY, DEFAULT_WAN_DOWNLOAD_CAPACITY
        )
        wan_upload_capacity = entry.options.get(
            CONF_WAN_UPLOAD_CAPACITY, DEFAULT_WAN_UPLOAD_CAPACITY
        )

        coordinator = FirewallaDataUpdateCoordinator(
            hass=hass,
            session=session,
            msp_domain=msp_domain,
            access_token=access_token,
            box_gid=box_gid,
            config_entry=entry,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            base_poll_interval=base_poll_interval,
            full_rules_interval=full_rules_interval,
            devices_interval=devices_interval,
            users_cache_ttl=users_cache_ttl,
            wan_sample_interval=wan_sample_interval,
            wan_download_capacity=wan_download_capacity,
            wan_upload_capacity=wan_upload_capacity,
        )

        await coordinator.async_restore_wan_state()
        await coordinator.async_config_entry_first_refresh()

        try:
            box_info = {}
            if coordinator.data and "box_info" in coordinator.data:
                box_info = coordinator.data["box_info"]
            box_name = box_info.get("name", f"Firewalla Box {box_gid[:8]}")
            box_model = box_info.get("model", "unknown")

            dev_reg = dr.async_get(hass)
            box_device = dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, box_gid)},
                name=box_name,
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL_MAPPINGS.get(
                    box_model, f"Firewalla {box_model.title()}"
                ),
                sw_version=box_info.get("version"),
            )
            coordinator.box_device_id = box_device.id
        except Exception:
            pass

        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = coordinator

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

        dashboard_users = entry.options.get(CONF_DASHBOARD_USERS, "")
        if dashboard_users:
            await _async_generate_dashboard(hass, dashboard_users)

        return True

    except ConfigEntryAuthFailed:
        raise
    except aiohttp.ClientConnectorError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Firewalla MSP API: {err}"
        ) from err
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            raise ConfigEntryAuthFailed(
                f"MSP API authentication error (HTTP {err.status})"
            ) from err
        raise ConfigEntryNotReady(
            f"MSP API error (HTTP {err.status}): {err.message}"
        ) from err
    except aiohttp.ClientError as err:
        raise ConfigEntryNotReady(
            f"Network error connecting to Firewalla MSP API: {err}"
        ) from err
    except HomeAssistantError:
        raise
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Unexpected error setting up Firewalla: {err}"
        ) from err


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Firewalla rule management config entry."""
    try:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except Exception:
        return False
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if DOMAIN in hass.data and not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a Firewalla rule management config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
