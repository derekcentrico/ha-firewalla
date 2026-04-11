# Group Controls & Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose all controllable per-group rules as switches (category blocks, app blocks) and per-user time limits as sensors, so HA mirrors the Firewalla app's Controls panel and Time Limit section per user/group.

**Architecture:** Generalize group rule handling in `_build_groups` to track ALL group-scoped rules (not just internet). Add `FirewallaGroupRuleSwitch` — one per group-scoped rule. Add `FirewallaTimeLimitSensor` — one per user-scoped timelimit rule. Time limit sensors show minutes used as state, with quota/remaining/reached/schedule as attributes.

**Tech Stack:** Python 3.12, Home Assistant 2026.4, aiohttp, pytest-asyncio

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `custom_components/firewalla/coordinator.py` | Modify | Expand `_build_groups` to track all group-scoped rules; add user time-limit processing |
| `custom_components/firewalla/switch.py` | Modify | Add `FirewallaGroupRuleSwitch` for category/app block rules per group |
| `custom_components/firewalla/sensor.py` | Modify | Add `FirewallaTimeLimitSensor` for per-user app time limits |
| `tests/test_coordinator.py` | Modify | Tests for expanded group rules + time limit processing |
| `tests/test_switch.py` | Modify | Tests for group rule switches |
| `tests/test_sensor.py` | Modify | Tests for time limit sensors |

---

### Task 1: Expand coordinator to track all group-scoped rules and user time limits

**Files:**
- Modify: `custom_components/firewalla/coordinator.py`
- Test: `tests/test_coordinator.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_coordinator.py`:

```python
class TestGroupRulesAndTimeLimits:
    def test_build_groups_tracks_all_rules(self):
        """All group-scoped rules are tracked, not just internet."""
        from custom_components.firewalla.coordinator import _build_groups

        devices = [
            {"id": "AA:BB", "name": "Phone", "online": True, "deviceType": "phone",
             "group": {"id": "28", "name": "Alice"}},
        ]
        rules = {
            "r1": {"id": "r1", "action": "block", "type": "internet", "scope_type": "group", "scope_value": "28", "paused": False},
            "r2": {"id": "r2", "action": "block", "type": "category", "value": "porn", "scope_type": "group", "scope_value": "28", "paused": False},
            "r3": {"id": "r3", "action": "block", "type": "app", "value": "tiktok", "scope_type": "group", "scope_value": "28", "paused": True},
            "r4": {"id": "r4", "action": "block", "type": "category", "value": "vpn", "scope_type": "group", "scope_value": "28", "paused": False},
        }

        groups = _build_groups(devices, [], rules)
        g = groups["28"]

        assert g["internet_block_rule_id"] == "r1"
        assert len(g["group_rules"]) == 4
        assert g["group_rules"]["r2"]["type"] == "category"
        assert g["group_rules"]["r2"]["value"] == "porn"
        assert g["group_rules"]["r2"]["paused"] is False
        assert g["group_rules"]["r3"]["value"] == "tiktok"
        assert g["group_rules"]["r3"]["paused"] is True

    def test_build_time_limits(self):
        """User time limit rules are extracted into a time_limits structure."""
        from custom_components.firewalla.coordinator import _build_time_limits

        users = [
            {"id": "box:33", "name": "Bob", "affiliatedTag": "32", "devices": [], "download": 0, "upload": 0},
        ]
        rules = {
            "r1": {"id": "r1", "action": "timelimit", "type": "app", "value": "roblox",
                   "scope_type": "user", "scope_value": "33", "paused": False,
                   "time_quota_minutes": 60, "time_used_minutes": 61,
                   "schedule_display": "daily at 00:00 all day", "hit_count": 8789},
            "r2": {"id": "r2", "action": "timelimit", "type": "app", "value": "youtube",
                   "scope_type": "user", "scope_value": "33", "paused": False,
                   "time_quota_minutes": 60, "time_used_minutes": 62,
                   "schedule_display": "Sun, Mon, Tue, Wed, Thu at 00:00 all day", "hit_count": 29328},
        }

        time_limits = _build_time_limits(users, rules)

        assert "33" in time_limits
        assert time_limits["33"]["user_name"] == "Bob"
        assert len(time_limits["33"]["limits"]) == 2
        roblox = time_limits["33"]["limits"]["r1"]
        assert roblox["app"] == "roblox"
        assert roblox["quota"] == 60
        assert roblox["used"] == 61
        assert roblox["reached"] is True

    def test_build_time_limits_not_reached(self):
        from custom_components.firewalla.coordinator import _build_time_limits

        users = [{"id": "box:33", "name": "Bob", "affiliatedTag": "32", "devices": [], "download": 0, "upload": 0}]
        rules = {
            "r1": {"id": "r1", "action": "timelimit", "type": "app", "value": "facebook",
                   "scope_type": "user", "scope_value": "33", "paused": False,
                   "time_quota_minutes": 60, "time_used_minutes": 2,
                   "schedule_display": "daily at 00:00 all day", "hit_count": 0},
        }
        time_limits = _build_time_limits(users, rules)
        fb = time_limits["33"]["limits"]["r1"]
        assert fb["remaining"] == 58
        assert fb["reached"] is False

    def test_build_time_limits_no_users(self):
        from custom_components.firewalla.coordinator import _build_time_limits
        assert _build_time_limits([], {}) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_coordinator.py::TestGroupRulesAndTimeLimits -v`

- [ ] **Step 3: Implement changes**

In `coordinator.py`, modify `_build_groups` to also populate a `group_rules` dict on each group:

```python
# Inside the rule cross-reference loop, for ALL group-scoped rules (not just internet):
        groups[scope_value]["group_rules"][rule_id] = {
            "type": rule.get("type", ""),
            "value": rule.get("value", ""),
            "action": rule.get("action", ""),
            "paused": rule.get("paused", False),
            "status": rule.get("status", "active"),
            "hit_count": rule.get("hit_count", 0),
        }
```

Initialize `"group_rules": {}` in the group dict.

Add module-level `_build_time_limits(users, rules)`:

```python
def _build_time_limits(
    users: list,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Build per-user time limit data from timelimit rules."""
    user_by_scope: dict[str, dict] = {}
    for user in users:
        uid = user.get("id", "")
        # Extract the numeric user ID from "box_gid:NN"
        parts = uid.rsplit(":", 1)
        if len(parts) == 2:
            user_by_scope[parts[1]] = user

    time_limits: dict[str, Any] = {}
    for rule_id, rule in rules.items():
        if rule.get("action") != "timelimit":
            continue
        scope_type = rule.get("scope_type", "")
        scope_value = str(rule.get("scope_value", ""))
        if scope_type != "user" or not scope_value:
            continue

        if scope_value not in time_limits:
            user_data = user_by_scope.get(scope_value, {})
            time_limits[scope_value] = {
                "user_name": user_data.get("name", f"User {scope_value}"),
                "user_id": user_data.get("id", ""),
                "limits": {},
            }

        quota = rule.get("time_quota_minutes") or 0
        used = rule.get("time_used_minutes") or 0
        remaining = max(0, quota - used)

        time_limits[scope_value]["limits"][rule_id] = {
            "app": rule.get("value", "unknown"),
            "quota": quota,
            "used": used,
            "remaining": remaining,
            "reached": used >= quota if quota > 0 else False,
            "paused": rule.get("paused", False),
            "schedule_display": rule.get("schedule_display"),
            "hit_count": rule.get("hit_count", 0),
        }

    return time_limits
```

Wire into `_async_update_data`:
```python
            time_limits_data = _build_time_limits(users_list, rules_data)
```
Add `"time_limits": time_limits_data,` to `processed_data`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_coordinator.py -v`

- [ ] **Step 5: Commit**

```bash
git add custom_components/firewalla/coordinator.py tests/test_coordinator.py
git commit -m "feat: track all group rules and user time limits in coordinator"
```

---

### Task 2: Add generic group rule switches

**Files:**
- Modify: `custom_components/firewalla/switch.py`
- Test: `tests/test_switch.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_switch.py`:

```python
class TestFirewallaGroupRuleSwitch:
    """Tests for per-group rule switches (category/app blocks)."""

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

    def test_init_category_block(self):
        from custom_components.firewalla.switch import FirewallaGroupRuleSwitch
        group_rules = {
            "r1": {"type": "category", "value": "porn", "action": "block", "paused": False, "status": "active", "hit_count": 100},
        }
        groups = {"28": {"name": "Alice", "is_user_group": True, "user_id": "box:29",
                         "device_count": 5, "devices": [], "internet_block_rule_id": "r0",
                         "internet_blocked": True, "rule_count": 6, "download": 0, "upload": 0,
                         "group_rules": group_rules}}
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupRuleSwitch(coordinator, "28", "r1")
        assert switch._attr_unique_id == "firewalla_group_28_rule_r1"
        assert "Alice" in switch._attr_name
        assert "Porn" in switch._attr_name

    def test_is_on_active_block(self):
        """ON = block is active."""
        from custom_components.firewalla.switch import FirewallaGroupRuleSwitch
        group_rules = {"r1": {"type": "category", "value": "porn", "action": "block", "paused": False, "status": "active", "hit_count": 0}}
        groups = {"28": {"name": "Alice", "is_user_group": True, "user_id": None,
                         "device_count": 1, "devices": [], "internet_block_rule_id": None,
                         "internet_blocked": False, "rule_count": 1, "download": 0, "upload": 0,
                         "group_rules": group_rules}}
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupRuleSwitch(coordinator, "28", "r1")
        assert switch.is_on is True

    def test_is_off_paused_block(self):
        """OFF = block is paused."""
        from custom_components.firewalla.switch import FirewallaGroupRuleSwitch
        group_rules = {"r1": {"type": "app", "value": "tiktok", "action": "block", "paused": True, "status": "paused", "hit_count": 0}}
        groups = {"28": {"name": "Alice", "is_user_group": True, "user_id": None,
                         "device_count": 1, "devices": [], "internet_block_rule_id": None,
                         "internet_blocked": False, "rule_count": 1, "download": 0, "upload": 0,
                         "group_rules": group_rules}}
        coordinator = self._make_coordinator(groups=groups)
        switch = FirewallaGroupRuleSwitch(coordinator, "28", "r1")
        assert switch.is_on is False

    @pytest.mark.asyncio
    async def test_turn_off_pauses_rule(self):
        from custom_components.firewalla.switch import FirewallaGroupRuleSwitch
        group_rules = {"r1": {"type": "category", "value": "porn", "action": "block", "paused": False, "status": "active", "hit_count": 0}}
        groups = {"28": {"name": "Alice", "is_user_group": True, "user_id": None,
                         "device_count": 1, "devices": [], "internet_block_rule_id": None,
                         "internet_blocked": False, "rule_count": 1, "download": 0, "upload": 0,
                         "group_rules": group_rules}}
        rules = {"r1": {"id": "r1", "paused": False, "status": "active"}}
        coordinator = self._make_coordinator(groups=groups, rules=rules)
        coordinator.async_pause_rule = AsyncMock(return_value=True)
        switch = FirewallaGroupRuleSwitch(coordinator, "28", "r1")
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_off()
        coordinator.async_pause_rule.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_turn_on_resumes_rule(self):
        from custom_components.firewalla.switch import FirewallaGroupRuleSwitch
        group_rules = {"r1": {"type": "app", "value": "tiktok", "action": "block", "paused": True, "status": "paused", "hit_count": 0}}
        groups = {"28": {"name": "Alice", "is_user_group": True, "user_id": None,
                         "device_count": 1, "devices": [], "internet_block_rule_id": None,
                         "internet_blocked": False, "rule_count": 1, "download": 0, "upload": 0,
                         "group_rules": group_rules}}
        rules = {"r1": {"id": "r1", "paused": True, "status": "paused"}}
        coordinator = self._make_coordinator(groups=groups, rules=rules)
        coordinator.async_resume_rule = AsyncMock(return_value=True)
        switch = FirewallaGroupRuleSwitch(coordinator, "28", "r1")
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_on()
        coordinator.async_resume_rule.assert_awaited_once_with("r1")
```

- [ ] **Step 2: Run tests to confirm fail**

- [ ] **Step 3: Implement `FirewallaGroupRuleSwitch`**

This is a straightforward switch: ON = rule active (block on), OFF = rule paused (block off). Unlike the internet switch, there's no inversion — it matches Firewalla's "Block On/Off" labels.

```python
class FirewallaGroupRuleSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for a group-scoped block rule (category/app). ON=block active, OFF=block paused."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"group_id", "rule_id", "rule_type", "target_value", "hit_count"})

    def __init__(self, coordinator, group_id: str, rule_id: str) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._rule_id = rule_id
        group = self._get_group_data()
        rule_info = self._get_rule_info()
        group_name = group["name"] if group else group_id
        target = (rule_info["value"] if rule_info else "unknown").title()
        self._attr_unique_id = f"firewalla_group_{group_id}_rule_{rule_id}"
        self._attr_name = f"{group_name} {target} Block"
        self._attr_icon = "mdi:shield-lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"group_{group_id}")},
            name=f"Firewalla Group: {group_name}",
            manufacturer=DEVICE_MANUFACTURER,
            model="Group",
            via_device=(DOMAIN, coordinator.box_gid),
        )
    # ... (standard _get_group_data, _get_rule_info, is_on, available, turn_on/off)
```

Wire into `_async_update_entities` — track `known_group_rule_keys: set[tuple[str,str]]` (group_id, rule_id) pairs, add/remove `FirewallaGroupRuleSwitch` for all group rules EXCEPT internet (internet has its own inverted switch).

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

---

### Task 3: Add time limit sensors

**Files:**
- Modify: `custom_components/firewalla/sensor.py`
- Test: `tests/test_sensor.py`

- [ ] **Step 1: Write failing tests**

```python
class TestFirewallaTimeLimitSensor:
    def _make_coordinator(self, time_limits=None, groups=None):
        coordinator = MagicMock(spec=FirewallaDataUpdateCoordinator)
        coordinator.data = {
            "time_limits": time_limits or {},
            "groups": groups or {},
            "box_info": {"gid": "test-box", "name": "Test Box", "model": "gold"},
        }
        coordinator.last_update_success = True
        coordinator.box_gid = "test-box"
        return coordinator

    def test_init(self):
        from custom_components.firewalla.sensor import FirewallaTimeLimitSensor
        time_limits = {"33": {"user_name": "Bob", "user_id": "box:33", "limits": {
            "r1": {"app": "roblox", "quota": 60, "used": 61, "remaining": 0, "reached": True,
                   "paused": False, "schedule_display": "daily at 00:00 all day", "hit_count": 8789}}}}
        groups = {"32": {"name": "Bob", "is_user_group": True, "user_id": "box:33",
                         "device_count": 5, "devices": [], "internet_block_rule_id": None,
                         "internet_blocked": False, "rule_count": 0, "download": 0, "upload": 0, "group_rules": {}}}
        coordinator = self._make_coordinator(time_limits=time_limits, groups=groups)
        sensor = FirewallaTimeLimitSensor(coordinator, "33", "r1")
        assert sensor._attr_unique_id == "firewalla_timelimit_33_r1"
        assert "Bob" in sensor._attr_name
        assert "Roblox" in sensor._attr_name

    def test_native_value_is_minutes_used(self):
        from custom_components.firewalla.sensor import FirewallaTimeLimitSensor
        time_limits = {"33": {"user_name": "Bob", "user_id": "box:33", "limits": {
            "r1": {"app": "roblox", "quota": 60, "used": 45, "remaining": 15, "reached": False,
                   "paused": False, "schedule_display": None, "hit_count": 0}}}}
        coordinator = self._make_coordinator(time_limits=time_limits)
        sensor = FirewallaTimeLimitSensor(coordinator, "33", "r1")
        assert sensor.native_value == 45

    def test_attributes_include_quota_and_reached(self):
        from custom_components.firewalla.sensor import FirewallaTimeLimitSensor
        time_limits = {"33": {"user_name": "Bob", "user_id": "box:33", "limits": {
            "r1": {"app": "roblox", "quota": 60, "used": 61, "remaining": 0, "reached": True,
                   "paused": False, "schedule_display": "daily at 00:00 all day", "hit_count": 8789}}}}
        coordinator = self._make_coordinator(time_limits=time_limits)
        sensor = FirewallaTimeLimitSensor(coordinator, "33", "r1")
        attrs = sensor.extra_state_attributes
        assert attrs["quota_minutes"] == 60
        assert attrs["remaining_minutes"] == 0
        assert attrs["reached"] is True
        assert attrs["schedule"] == "daily at 00:00 all day"
```

- [ ] **Step 2: Run tests to confirm fail**

- [ ] **Step 3: Implement `FirewallaTimeLimitSensor`**

```python
class FirewallaTimeLimitSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing app time usage for a user's time limit rule."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "min"
    _unrecorded_attributes = frozenset({"user_id", "rule_id", "schedule"})

    def __init__(self, coordinator, user_scope_id: str, rule_id: str) -> None:
        super().__init__(coordinator)
        self._user_scope_id = user_scope_id
        self._rule_id = rule_id
        limit_data = self._get_limit_data()
        user_data = self._get_user_data()
        user_name = user_data["user_name"] if user_data else f"User {user_scope_id}"
        app_name = (limit_data["app"] if limit_data else "unknown").title()
        self._attr_unique_id = f"firewalla_timelimit_{user_scope_id}_{rule_id}"
        self._attr_name = f"{user_name} {app_name} Time"
        self._attr_icon = "mdi:timer-outline"
        # Device info: attach to the user's affiliated group device
        affiliated_tag = None
        for u in (coordinator.data or {}).get("time_limits", {}).get(user_scope_id, {}).items():
            pass
        # Find group via users data
        self._attr_device_info = self._build_device_info(coordinator, user_name)

    # native_value returns used minutes, attributes include quota/remaining/reached/schedule
```

Wire into sensor `_async_update_group_sensors` — add time limit sensor tracking alongside group sensors.

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

---

### Task 4: Full integration test and deployment

- [ ] **Step 1: Run full test suite**
- [ ] **Step 2: Deploy to HA server and restart**
- [ ] **Step 3: Verify all entities in HA UI**
- [ ] **Step 4: Commit final state**
