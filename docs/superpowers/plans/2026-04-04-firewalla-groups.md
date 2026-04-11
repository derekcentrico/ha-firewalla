# Firewalla Groups & "Pause Internet" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Firewalla group and user support — each group becomes an HA device with an "Internet Access" switch (ON=internet allowed, OFF=blocked) and a device-count sensor, dynamically synced from the Firewalla API.

**Architecture:** The coordinator fetches `/v2/devices` and `/v2/users` alongside `/v2/rules` each poll. Groups are extracted from device data; friendly names are resolved via the users API (`affiliatedTag` maps user names to group IDs). Each group gets its own HA device. Groups with an internet-block rule get a `FirewallaGroupInternetSwitch`. All groups get a `FirewallaGroupSensor` showing device count and metadata.

**Tech Stack:** Python 3.12, Home Assistant 2026.4, aiohttp, pytest-asyncio

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `custom_components/firewalla/const.py` | Modify | Add `devices` and `users` API endpoints |
| `custom_components/firewalla/coordinator.py` | Modify | Add `get_devices()`, `get_users()` to client; add group/user processing to `_async_update_data` |
| `custom_components/firewalla/switch.py` | Modify | Add `FirewallaGroupInternetSwitch` class and wire into the coordinator listener |
| `custom_components/firewalla/sensor.py` | Modify | Add `FirewallaGroupSensor` class and wire into a coordinator listener |
| `tests/test_coordinator.py` | Modify | Tests for group/user data processing |
| `tests/test_switch.py` | Modify | Tests for group internet switch |
| `tests/test_sensor.py` | Modify | Tests for group sensor |

---

### Task 1: Add API endpoints and client methods

**Files:**
- Modify: `custom_components/firewalla/const.py:22-30`
- Modify: `custom_components/firewalla/coordinator.py:307-327`
- Test: `tests/test_coordinator.py`

- [ ] **Step 1: Write the failing test for get_devices**

Add to `tests/test_coordinator.py`:

```python
class TestFirewallaMSPClientDevicesUsers:
    """Tests for devices and users API methods."""

    async def test_get_devices(self, mock_aiohttp_session):
        """Test fetching devices from API."""
        client = FirewallaMSPClient(mock_aiohttp_session, "test.firewalla.net", "test_token")
        mock_devices = [
            {"id": "AA:BB:CC:DD:EE:FF", "name": "Test Phone", "online": True, "group": {"id": "28", "name": "Alice"}}
        ]
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_devices)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp_session.request = MagicMock(return_value=mock_response)

        result = await client.get_devices()
        assert result == mock_devices

    async def test_get_users(self, mock_aiohttp_session):
        """Test fetching users from API."""
        client = FirewallaMSPClient(mock_aiohttp_session, "test.firewalla.net", "test_token")
        mock_users = [
            {"id": "box:29", "name": "Alice", "affiliatedTag": "28", "devices": ["AA:BB:CC:DD:EE:FF"]}
        ]
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_users)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp_session.request = MagicMock(return_value=mock_response)

        result = await client.get_users()
        assert result == mock_users
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_coordinator.py::TestFirewallaMSPClientDevicesUsers -v`
Expected: FAIL with `AttributeError: 'FirewallaMSPClient' object has no attribute 'get_devices'`

- [ ] **Step 3: Add endpoints to const.py and methods to client**

In `custom_components/firewalla/const.py`, add to `API_ENDPOINTS`:

```python
API_ENDPOINTS = {
    # V2 endpoints (preferred)
    "rules": "/rules",
    "rule_pause": "/rules/{rule_id}/pause",
    "rule_resume": "/rules/{rule_id}/resume",
    "rule_detail": "/rules/{rule_id}",
    "devices": "/devices",
    "users": "/users",
    # V1 endpoints (legacy, for fallback)
    "legacy_rules": "/rule/list",
}
```

In `custom_components/firewalla/coordinator.py`, add after `get_rule_status`:

```python
    async def get_devices(self) -> list:
        """Get all devices from MSP API."""
        return await self._make_request("GET", API_ENDPOINTS["devices"])

    async def get_users(self) -> list:
        """Get all users from MSP API."""
        return await self._make_request("GET", API_ENDPOINTS["users"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_coordinator.py::TestFirewallaMSPClientDevicesUsers -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/firewalla/const.py custom_components/firewalla/coordinator.py tests/test_coordinator.py
git commit -m "feat: add devices and users API client methods"
```

---

### Task 2: Process groups and users in coordinator

**Files:**
- Modify: `custom_components/firewalla/coordinator.py:362-410`
- Test: `tests/test_coordinator.py`

- [ ] **Step 1: Write failing tests for group processing**

Add to `tests/test_coordinator.py`:

```python
class TestGroupProcessing:
    """Tests for group and user data processing."""

    def test_build_groups_from_devices(self):
        """Test building groups dict from device data."""
        from custom_components.firewalla.coordinator import _build_groups

        devices = [
            {"id": "AA:BB:CC:DD:EE:01", "name": "Phone", "online": True, "deviceType": "phone",
             "group": {"id": "28", "name": "Alice"}},
            {"id": "AA:BB:CC:DD:EE:02", "name": "Tablet", "online": False, "deviceType": "tablet",
             "group": {"id": "28", "name": "Alice"}},
            {"id": "AA:BB:CC:DD:EE:03", "name": "Camera", "online": True, "deviceType": "camera",
             "group": {"id": "25", "name": "Cameras"}},
            {"id": "AA:BB:CC:DD:EE:04", "name": "Laptop", "online": True, "deviceType": "desktop"},
        ]
        users = [
            {"id": "box:29", "name": "Alice", "affiliatedTag": "28",
             "devices": ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"],
             "download": 1000, "upload": 500},
        ]
        rules = {
            "rule1": {"id": "rule1", "action": "block", "type": "internet", "scope_type": "group", "scope_value": "28", "paused": False},
            "rule2": {"id": "rule2", "action": "block", "type": "category", "scope_type": "group", "scope_value": "28", "paused": False},
        }

        groups = _build_groups(devices, users, rules)

        assert "28" in groups
        assert groups["28"]["name"] == "Alice"
        assert groups["28"]["is_user_group"] is True
        assert groups["28"]["user_id"] == "box:29"
        assert groups["28"]["device_count"] == 2
        assert groups["28"]["internet_block_rule_id"] == "rule1"
        assert groups["28"]["internet_blocked"] is True
        assert groups["28"]["download"] == 1000

        assert "25" in groups
        assert groups["25"]["name"] == "Cameras"
        assert groups["25"]["is_user_group"] is False
        assert groups["25"]["internet_block_rule_id"] is None

    def test_build_groups_resolves_uuid_names(self):
        """Test that UUID group names are resolved via user affiliatedTag."""
        from custom_components.firewalla.coordinator import _build_groups

        devices = [
            {"id": "AA:BB:CC:DD:EE:01", "name": "Bob Tablet", "online": True, "deviceType": "tablet",
             "group": {"id": "32", "name": "BFB913AE-49E7-4465-961D-6FB1496147DF"}},
        ]
        users = [
            {"id": "box:33", "name": "Bob", "affiliatedTag": "32", "devices": ["AA:BB:CC:DD:EE:01"],
             "download": 0, "upload": 0},
        ]

        groups = _build_groups(devices, users, {})
        assert groups["32"]["name"] == "Bob"

    def test_build_groups_internet_paused(self):
        """Test internet_blocked is False when the rule is paused."""
        from custom_components.firewalla.coordinator import _build_groups

        devices = [
            {"id": "AA:BB:CC:DD:EE:01", "name": "Phone", "online": True, "deviceType": "phone",
             "group": {"id": "28", "name": "Alice"}},
        ]
        rules = {
            "rule1": {"id": "rule1", "action": "block", "type": "internet", "scope_type": "group", "scope_value": "28", "paused": True},
        }

        groups = _build_groups(devices, [], rules)
        assert groups["28"]["internet_blocked"] is False

    def test_build_groups_no_devices(self):
        """Test with empty device list."""
        from custom_components.firewalla.coordinator import _build_groups

        groups = _build_groups([], [], {})
        assert groups == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_coordinator.py::TestGroupProcessing -v`
Expected: FAIL with `ImportError: cannot import name '_build_groups'`

- [ ] **Step 3: Implement `_build_groups` and wire into coordinator**

Add module-level function to `custom_components/firewalla/coordinator.py` (after `_format_schedule`):

```python
def _build_groups(
    devices: list,
    users: list,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Build groups dict from device and user data, cross-referenced with rules."""
    # Build user name map: affiliatedTag -> user data
    user_by_tag: dict[str, dict] = {}
    for user in users:
        tag = user.get("affiliatedTag")
        if tag:
            user_by_tag[tag] = user

    # Build groups from devices
    groups: dict[str, dict[str, Any]] = {}
    for device in devices:
        group_info = device.get("group")
        if not group_info or not isinstance(group_info, dict):
            continue
        gid = str(group_info.get("id", ""))
        if not gid:
            continue

        if gid not in groups:
            # Resolve name: prefer user name over raw group name
            raw_name = group_info.get("name", gid)
            user_data = user_by_tag.get(gid)
            resolved_name = user_data["name"] if user_data else raw_name

            groups[gid] = {
                "name": resolved_name,
                "is_user_group": user_data is not None,
                "user_id": user_data["id"] if user_data else None,
                "device_count": 0,
                "devices": [],
                "internet_block_rule_id": None,
                "internet_blocked": False,
                "rule_count": 0,
                "download": user_data.get("download", 0) if user_data else 0,
                "upload": user_data.get("upload", 0) if user_data else 0,
            }

        groups[gid]["device_count"] += 1
        groups[gid]["devices"].append({
            "name": device.get("name", "Unknown"),
            "mac": device.get("id", ""),
            "online": device.get("online", False),
            "type": device.get("deviceType", ""),
            "ip": device.get("ip", ""),
        })

    # Cross-reference rules to find internet-block rules per group
    for rule_id, rule in rules.items():
        scope_type = rule.get("scope_type", "")
        scope_value = str(rule.get("scope_value", ""))
        if scope_type != "group" or scope_value not in groups:
            continue

        groups[scope_value]["rule_count"] += 1

        # Check for internet block rule
        if rule.get("type") == "internet" and rule.get("action") == "block":
            groups[scope_value]["internet_block_rule_id"] = rule_id
            groups[scope_value]["internet_blocked"] = not rule.get("paused", False)

    return groups
```

Then in `_async_update_data`, after the rules processing and before building `processed_data`, add:

```python
            # Fetch devices and users for group data
            devices_response = await self.api.get_devices()
            devices_list = devices_response if isinstance(devices_response, list) else []

            users_response = await self.api.get_users()
            users_list = users_response if isinstance(users_response, list) else []

            # Build groups
            groups_data = _build_groups(devices_list, users_list, rules_data)
```

Add `"groups": groups_data,` to the `processed_data` dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_coordinator.py::TestGroupProcessing -v`
Expected: PASS

- [ ] **Step 5: Run full coordinator tests**

Run: `python3 -m pytest tests/test_coordinator.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add custom_components/firewalla/coordinator.py tests/test_coordinator.py
git commit -m "feat: build groups from devices/users data in coordinator"
```

---

### Task 3: Group internet switch entity

**Files:**
- Modify: `custom_components/firewalla/switch.py`
- Test: `tests/test_switch.py`

- [ ] **Step 1: Write failing tests for FirewallaGroupInternetSwitch**

Add to `tests/test_switch.py`:

```python
class TestFirewallaGroupInternetSwitch:
    """Tests for group internet access switch."""

    def _make_coordinator(self, groups=None, rules=None):
        coordinator = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coordinator.data = {
            "groups": groups or {},
            "rules": rules or {},
            "box_info": {"gid": "test-box", "name": "Test Box", "model": "gold"},
        }
        coordinator.last_update_success = True
        coordinator.box_gid = "test-box"
        return coordinator

    def test_init_internet_access_switch(self):
        from custom_components.firewalla.switch import FirewallaGroupInternetSwitch

        groups = {
            "28": {
                "name": "Alice", "is_user_group": True, "user_id": "box:29",
                "device_count": 5, "devices": [],
                "internet_block_rule_id": "rule1", "internet_blocked": True,
                "rule_count": 6, "download": 1000, "upload": 500,
            }
        }
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupInternetSwitch(coordinator, "28")

        assert switch._attr_unique_id == "firewalla_group_28_internet"
        assert "Alice" in switch._attr_name
        assert switch._attr_has_entity_name is True

    def test_is_on_internet_allowed(self):
        """ON when internet block rule is paused (internet is flowing)."""
        from custom_components.firewalla.switch import FirewallaGroupInternetSwitch

        groups = {"28": {"name": "Alice", "internet_block_rule_id": "rule1", "internet_blocked": False,
                         "is_user_group": True, "user_id": None, "device_count": 1, "devices": [],
                         "rule_count": 1, "download": 0, "upload": 0}}
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupInternetSwitch(coordinator, "28")
        assert switch.is_on is True

    def test_is_on_internet_blocked(self):
        """OFF when internet block rule is active (internet is cut)."""
        from custom_components.firewalla.switch import FirewallaGroupInternetSwitch

        groups = {"28": {"name": "Alice", "internet_block_rule_id": "rule1", "internet_blocked": True,
                         "is_user_group": True, "user_id": None, "device_count": 1, "devices": [],
                         "rule_count": 1, "download": 0, "upload": 0}}
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupInternetSwitch(coordinator, "28")
        assert switch.is_on is False

    async def test_turn_off_pauses_internet_block_rule(self):
        """Turn OFF should resume the internet block rule (activate the block)."""
        from custom_components.firewalla.switch import FirewallaGroupInternetSwitch

        groups = {"28": {"name": "Alice", "internet_block_rule_id": "rule1", "internet_blocked": False,
                         "is_user_group": True, "user_id": None, "device_count": 1, "devices": [],
                         "rule_count": 1, "download": 0, "upload": 0}}
        rules = {"rule1": {"id": "rule1", "paused": True, "status": "paused"}}
        coordinator = self._make_coordinator(groups=groups, rules=rules)
        coordinator.async_resume_rule = AsyncMock(return_value=True)
        coordinator.async_write_ha_state = MagicMock()

        switch = FirewallaGroupInternetSwitch(coordinator, "28")
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_off()

        coordinator.async_resume_rule.assert_awaited_once_with("rule1")

    async def test_turn_on_pauses_block_rule(self):
        """Turn ON should pause the internet block rule (deactivate the block, allow internet)."""
        from custom_components.firewalla.switch import FirewallaGroupInternetSwitch

        groups = {"28": {"name": "Alice", "internet_block_rule_id": "rule1", "internet_blocked": True,
                         "is_user_group": True, "user_id": None, "device_count": 1, "devices": [],
                         "rule_count": 1, "download": 0, "upload": 0}}
        rules = {"rule1": {"id": "rule1", "paused": False, "status": "active"}}
        coordinator = self._make_coordinator(groups=groups, rules=rules)
        coordinator.async_pause_rule = AsyncMock(return_value=True)

        switch = FirewallaGroupInternetSwitch(coordinator, "28")
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_on()

        coordinator.async_pause_rule.assert_awaited_once_with("rule1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_switch.py::TestFirewallaGroupInternetSwitch -v`
Expected: FAIL with `ImportError: cannot import name 'FirewallaGroupInternetSwitch'`

- [ ] **Step 3: Implement FirewallaGroupInternetSwitch**

Add to `custom_components/firewalla/switch.py`:

```python
class FirewallaGroupInternetSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control internet access for a Firewalla group.

    ON = internet is allowed (block rule is paused).
    OFF = internet is blocked (block rule is active).
    """

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({
        "group_id", "is_user_group", "user_id", "device_count",
        "rule_count", "internet_block_rule_id",
    })

    def __init__(
        self,
        coordinator: FirewallaDataUpdateCoordinator,
        group_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        group = self._get_group_data()
        group_name = group["name"] if group else group_id
        self._attr_unique_id = f"firewalla_group_{group_id}_internet"
        self._attr_name = f"{group_name} Internet Access"
        self._attr_icon = "mdi:web"
        self._attr_device_info = self._build_device_info()

    def _get_group_data(self) -> dict[str, Any] | None:
        if not self.coordinator.data or "groups" not in self.coordinator.data:
            return None
        return self.coordinator.data["groups"].get(self._group_id)

    def _build_device_info(self) -> DeviceInfo:
        group = self._get_group_data()
        group_name = group["name"] if group else self._group_id
        return DeviceInfo(
            identifiers={(DOMAIN, f"group_{self._group_id}")},
            name=f"Firewalla Group: {group_name}",
            manufacturer=DEVICE_MANUFACTURER,
            model="Group",
            via_device=(DOMAIN, self.coordinator.box_gid),
        )

    @property
    def is_on(self) -> bool:
        """ON when internet is allowed (block rule paused)."""
        group = self._get_group_data()
        if not group:
            return False
        return not group.get("internet_blocked", False)

    @property
    def available(self) -> bool:
        group = self._get_group_data()
        return (
            self.coordinator.last_update_success
            and group is not None
            and group.get("internet_block_rule_id") is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        group = self._get_group_data()
        if not group:
            return {"group_id": self._group_id}
        return {
            "group_id": self._group_id,
            "group_name": group["name"],
            "is_user_group": group.get("is_user_group", False),
            "user_id": group.get("user_id"),
            "device_count": group.get("device_count", 0),
            "rule_count": group.get("rule_count", 0),
            "internet_block_rule_id": group.get("internet_block_rule_id"),
            "download": group.get("download", 0),
            "upload": group.get("upload", 0),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow internet (pause the block rule)."""
        group = self._get_group_data()
        if not group:
            raise HomeAssistantError(f"Group {self._group_id} not found")
        rule_id = group.get("internet_block_rule_id")
        if not rule_id:
            raise HomeAssistantError(f"No internet block rule for group {self._group_id}")

        success = await self.coordinator.async_pause_rule(rule_id)
        if not success:
            raise HomeAssistantError(f"Failed to allow internet for group {group['name']}")

        # Optimistic update
        group["internet_blocked"] = False
        rule = self.coordinator.data.get("rules", {}).get(rule_id)
        if rule:
            rule["paused"] = True
            rule["status"] = "paused"
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block internet (resume the block rule)."""
        group = self._get_group_data()
        if not group:
            raise HomeAssistantError(f"Group {self._group_id} not found")
        rule_id = group.get("internet_block_rule_id")
        if not rule_id:
            raise HomeAssistantError(f"No internet block rule for group {self._group_id}")

        success = await self.coordinator.async_resume_rule(rule_id)
        if not success:
            raise HomeAssistantError(f"Failed to block internet for group {group['name']}")

        # Optimistic update
        group["internet_blocked"] = True
        rule = self.coordinator.data.get("rules", {}).get(rule_id)
        if rule:
            rule["paused"] = False
            rule["status"] = "active"
        self.async_write_ha_state()
```

Then in `async_setup_entry`, extend the `_async_update_entities` callback to also manage group switches. Add a second tracked set `known_group_ids` and add/remove `FirewallaGroupInternetSwitch` entities for groups that have an `internet_block_rule_id`.

```python
    known_group_ids: set[str] = set()

    # Inside _async_update_entities, after the rule switch logic:
        # --- Group internet switches ---
        current_groups = set()
        if coordinator.data and "groups" in coordinator.data:
            for gid, gdata in coordinator.data["groups"].items():
                if gdata.get("internet_block_rule_id"):
                    current_groups.add(gid)

        new_groups = current_groups - known_group_ids
        if new_groups:
            group_entities = [
                FirewallaGroupInternetSwitch(coordinator, gid)
                for gid in new_groups
            ]
            async_add_entities(group_entities)
            known_group_ids.update(new_groups)

        removed_groups = known_group_ids - current_groups
        if removed_groups:
            ent_reg = er.async_get(hass)
            for gid in removed_groups:
                entity_id = ent_reg.async_get_entity_id(
                    "switch", DOMAIN, f"firewalla_group_{gid}_internet"
                )
                if entity_id:
                    ent_reg.async_remove(entity_id)
            known_group_ids.difference_update(removed_groups)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_switch.py::TestFirewallaGroupInternetSwitch -v`
Expected: PASS

- [ ] **Step 5: Run full switch tests**

Run: `python3 -m pytest tests/test_switch.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add custom_components/firewalla/switch.py tests/test_switch.py
git commit -m "feat: add group internet access switch (ON=allowed, OFF=blocked)"
```

---

### Task 4: Group sensor entity

**Files:**
- Modify: `custom_components/firewalla/sensor.py`
- Test: `tests/test_sensor.py`

- [ ] **Step 1: Write failing tests for FirewallaGroupSensor**

Add to `tests/test_sensor.py`:

```python
class TestFirewallaGroupSensor:
    """Tests for group sensor entity."""

    def _make_coordinator(self, groups=None):
        coordinator = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coordinator.data = {
            "groups": groups or {},
            "box_info": {"gid": "test-box", "name": "Test Box", "model": "gold"},
        }
        coordinator.last_update_success = True
        coordinator.box_gid = "test-box"
        return coordinator

    def test_init(self):
        from custom_components.firewalla.sensor import FirewallaGroupSensor

        groups = {"28": {"name": "Alice", "device_count": 5, "devices": [
            {"name": "Phone", "online": True}, {"name": "Tablet", "online": False}],
            "is_user_group": True, "user_id": "box:29", "internet_blocked": True,
            "internet_block_rule_id": "rule1", "rule_count": 6, "download": 1000, "upload": 500}}
        coordinator = self._make_coordinator(groups=groups)
        sensor = FirewallaGroupSensor(coordinator, "28")

        assert sensor._attr_unique_id == "firewalla_group_28"
        assert "Alice" in sensor._attr_name
        assert sensor._attr_has_entity_name is True

    def test_native_value_is_device_count(self):
        from custom_components.firewalla.sensor import FirewallaGroupSensor

        groups = {"28": {"name": "Alice", "device_count": 5, "devices": [],
                         "is_user_group": True, "user_id": None, "internet_blocked": False,
                         "internet_block_rule_id": None, "rule_count": 0, "download": 0, "upload": 0}}
        coordinator = self._make_coordinator(groups=groups)
        sensor = FirewallaGroupSensor(coordinator, "28")
        assert sensor.native_value == 5

    def test_extra_state_attributes(self):
        from custom_components.firewalla.sensor import FirewallaGroupSensor

        devices = [{"name": "Phone", "online": True, "mac": "AA:BB", "type": "phone", "ip": "1.2.3.4"},
                    {"name": "Tablet", "online": False, "mac": "CC:DD", "type": "tablet", "ip": "1.2.3.5"}]
        groups = {"28": {"name": "Alice", "device_count": 2, "devices": devices,
                         "is_user_group": True, "user_id": "box:29", "internet_blocked": True,
                         "internet_block_rule_id": "rule1", "rule_count": 6, "download": 1000, "upload": 500}}
        coordinator = self._make_coordinator(groups=groups)
        sensor = FirewallaGroupSensor(coordinator, "28")
        attrs = sensor.extra_state_attributes

        assert attrs["online_devices"] == 1
        assert attrs["device_names"] == ["Phone", "Tablet"]
        assert attrs["internet_blocked"] is True
        assert attrs["rule_count"] == 6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_sensor.py::TestFirewallaGroupSensor -v`
Expected: FAIL with `ImportError: cannot import name 'FirewallaGroupSensor'`

- [ ] **Step 3: Implement FirewallaGroupSensor**

Add to `custom_components/firewalla/sensor.py`:

```python
class FirewallaGroupSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing device count and info for a Firewalla group."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"
    _unrecorded_attributes = frozenset({
        "group_id", "is_user_group", "user_id", "device_names",
    })

    def __init__(
        self,
        coordinator: FirewallaDataUpdateCoordinator,
        group_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        group = self._get_group_data()
        group_name = group["name"] if group else group_id
        self._attr_unique_id = f"firewalla_group_{group_id}"
        self._attr_name = f"{group_name} Devices"
        self._attr_icon = "mdi:account-group"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"group_{group_id}")},
            name=f"Firewalla Group: {group_name}",
            manufacturer=DEVICE_MANUFACTURER,
            model="Group",
            via_device=(DOMAIN, coordinator.box_gid),
        )

    def _get_group_data(self) -> dict[str, Any] | None:
        if not self.coordinator.data or "groups" not in self.coordinator.data:
            return None
        return self.coordinator.data["groups"].get(self._group_id)

    @property
    def native_value(self) -> int:
        group = self._get_group_data()
        return group.get("device_count", 0) if group else 0

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._get_group_data() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        group = self._get_group_data()
        if not group:
            return {"group_id": self._group_id}
        devices = group.get("devices", [])
        return {
            "group_id": self._group_id,
            "group_name": group["name"],
            "is_user_group": group.get("is_user_group", False),
            "user_id": group.get("user_id"),
            "online_devices": sum(1 for d in devices if d.get("online")),
            "device_names": [d["name"] for d in devices],
            "internet_blocked": group.get("internet_blocked", False),
            "rule_count": group.get("rule_count", 0),
            "download": group.get("download", 0),
            "upload": group.get("upload", 0),
        }
```

Then add a coordinator listener in `sensor.py`'s `async_setup_entry` to dynamically add/remove group sensors, same pattern as the switch. Track `known_group_ids` and create `FirewallaGroupSensor` for every group (not just ones with internet rules).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_sensor.py::TestFirewallaGroupSensor -v`
Expected: PASS

- [ ] **Step 5: Run full sensor tests**

Run: `python3 -m pytest tests/test_sensor.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add custom_components/firewalla/sensor.py tests/test_sensor.py
git commit -m "feat: add group sensor showing device count and metadata"
```

---

### Task 5: Integration test and deployment

**Files:**
- All modified files from Tasks 1-4

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: 188+ pass (pre-existing config_flow and real_api failures are known)

- [ ] **Step 2: Deploy to HA server**

```bash
scp custom_components/firewalla/*.py custom_components/firewalla/*.json root@192.168.1.65:/config/custom_components/firewalla/
scp custom_components/firewalla/translations/en.json root@192.168.1.65:/config/custom_components/firewalla/translations/
ssh root@192.168.1.65 'ha core restart'
```

- [ ] **Step 3: Verify in HA logs**

```bash
ssh root@192.168.1.65 'sleep 20 && ha core logs 2>&1 | grep -i firewalla | tail -30'
```

Expected: No errors. Should see "Processed 103 valid rules" and group data loaded.

- [ ] **Step 4: Verify entities in HA UI**

Check Settings > Devices & Services > Firewalla:
- 15 group devices should appear (Alice, Bob, Carol, Cameras, TVs, etc.)
- Groups with internet-block rules have "Internet Access" switch
- All groups have a "Devices" sensor
- Toggle Alice's internet switch OFF → verify in Firewalla app that internet is blocked
- Toggle it back ON → verify internet restored

- [ ] **Step 5: Commit final state**

```bash
git add -A
git commit -m "feat: firewalla groups with internet access control and device sensors"
```
