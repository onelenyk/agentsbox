"""core.devices — discovery parsing, leasing, the lease's lifecycle around a
worker run, and screenshots as task resources."""
import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import devices, store


ADB_L = """List of devices attached
3B161JEHN01255         device usb:0-1.4.2 product:lynx model:Pixel_7a device:lynx transport_id:5
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 transport_id:7
R58M1234               unauthorized usb:0-1.1 transport_id:9
"""

CONFIG_INI = """PlayStore.enabled=true
disk.dataPartition.size=6G
hw.cpu.ncore=4
hw.device.name=pixel_4
hw.lcd.height=2280
hw.lcd.width=1080
hw.ramSize=2048
image.sysdir.1=system-images/android-33/google_apis_playstore/arm64-v8a/
"""

SIMCTL = {"devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-2": [
        {"udid": "AAA", "name": "iPhone 17", "state": "Booted", "isAvailable": True,
         "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17"},
        {"udid": "BBB", "name": "iPad (A16)", "state": "Shutdown", "isAvailable": True}],
    "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
        {"udid": "WWW", "name": "Watch", "state": "Shutdown", "isAvailable": True}]}}


# ── parsing ──────────────────────────────────────────────────────────────────

def test_adb_devices_parses_state_model_and_usb():
    rows = devices.parse_adb_devices(ADB_L)
    assert [(r["serial"], r["state"]) for r in rows] == [
        ("3B161JEHN01255", "device"), ("emulator-5554", "device"), ("R58M1234", "unauthorized")]
    assert rows[0]["model"] == "Pixel 7a" and rows[0]["usb"] == "0-1.4.2"


def test_avd_specs_from_config_ini():
    s = devices.avd_specs(devices.parse_ini(CONFIG_INI))
    assert s["os"] == "Android API 33" and s["ram_mb"] == 2048 and s["disk_gb"] == 6.0
    assert s["cpu"] == "4" and s["screen"] == "1080×2280" and s["play"] is True


def test_sizes():
    assert devices._size_gb("6G") == 6.0
    assert devices._size_gb("6144M") == 6.0
    assert devices._size_gb("6442450944") == 6.0
    assert devices._size_gb("junk") is None


def test_simctl_keeps_ios_only():
    sims = devices.parse_simctl(SIMCTL)
    assert [(s["udid"], s["os"], s["state"]) for s in sims] == [
        ("AAA", "iOS 26.2", "Booted"), ("BBB", "iOS 26.2", "Shutdown")]


# ── picking and leasing ──────────────────────────────────────────────────────

def _dev(id, kind="emulator", state="off", allowed=True, lease=None, platform="android"):
    return {"id": id, "kind": kind, "state": state, "allowed": allowed, "lease": lease,
            "platform": platform, "name": id, "serial": f"s-{id}", "specs": {"os": "Android API 33"}}


def test_pick_prefers_running_virtual_then_allowed_phone_then_boot():
    phone = _dev("phone", kind="physical", state="ready")
    off = _dev("off")
    running = _dev("running", state="ready")
    assert devices._pick([phone, off, running], "android")["id"] == "running"
    assert devices._pick([off, phone], "android")["id"] == "phone"
    assert devices._pick([off], "android")["id"] == "off"


def test_pick_never_takes_a_phone_that_is_not_allowed_or_a_leased_device():
    phone = _dev("phone", kind="physical", state="ready", allowed=False)
    busy = _dev("busy", state="ready", lease={"task_id": "t"})
    assert devices._pick([phone, busy], "android") is None
    assert devices._pick([_dev("ios", platform="ios")], "android") is None


def test_physical_defaults_to_not_allowed(db_path):
    with patch.object(devices, "_adb") as adb, patch.object(devices, "list_avds", return_value=[]), \
         patch.object(devices, "_xcrun") as xc, patch.object(devices, "_ios_physical", return_value=[]):
        adb.return_value.stdout = ADB_L.split("emulator")[0]
        adb.return_value.returncode = 0
        xc.return_value.returncode = 1
        devs = devices.discover(fresh=True)
        assert devs[0]["kind"] == "physical" and devs[0]["allowed"] is False
        devices.set_allowed(devs[0]["id"], True)
        assert devices.discover(fresh=True)[0]["allowed"] is True


def test_acquire_leases_boots_and_respects_the_cap(db_path):
    state = {"devs": [_dev("a", state="ready"), _dev("b", state="ready")]}

    def discover(fresh=False):
        held = {l["device_id"]: l for l in devices.leases()}
        return [dict(d, lease=held.get(d["id"])) for d in state["devs"]]

    with patch.object(devices, "discover", side_effect=discover), \
         patch.object(devices, "max_leases", return_value=1):
        lease = devices.acquire("android", "task-1", "qa")
        assert lease["device_id"] == "a" and lease["serial"] == "s-a"
        assert devices.acquire("android", "task-2", "qa") is None      # cap: one at a time
        devices.release("a")
        assert devices.acquire("android", "task-2", "qa")["device_id"] == "a"


def test_a_device_that_will_not_boot_releases_its_lease(db_path):
    with patch.object(devices, "discover", return_value=[_dev("a")]), \
         patch.object(devices, "start", side_effect=devices.DeviceError("no boot")):
        with pytest.raises(devices.DeviceError):
            devices.acquire("android", "task-1", "qa")
    assert devices.leases() == []


def test_stale_leases_are_cleared_at_boot(db_path):
    devices._q("INSERT INTO device_leases (device_id, task_id, role, serial, since) VALUES "
               "('x', 't', 'qa', '', 'now')")
    assert devices.clear_stale_leases() == 1 and devices.leases() == []


def test_idle_sweep_stops_only_what_we_booted_and_left_idle(db_path):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    devices._q("INSERT INTO device_boots VALUES ('idle', 'emulator-5554', ?, ?)", (old, old))
    devices._q("INSERT INTO device_boots VALUES ('fresh', 'emulator-5556', ?, NULL)",
               (datetime.now(timezone.utc).isoformat(),))
    devices._q("INSERT INTO device_boots VALUES ('held', 'emulator-5558', ?, ?)", (old, old))
    devices._q("INSERT INTO device_leases VALUES ('held', 't', 'qa', '', 'now')")
    with patch.object(devices, "stop") as stop:
        assert devices.idle_sweep() == ["idle"]
    stop.assert_called_once_with("idle")


def test_env_and_prompt_name_only_the_leased_device():
    lease = {"device_id": "android-avd:Pixel_4", "platform": "android", "kind": "emulator",
             "name": "Pixel 4", "serial": "emulator-5554", "os": "Android API 33"}
    env = devices.env_for(lease)
    assert env["ANDROID_SERIAL"] == "emulator-5554" and json.loads(env["SISYPHUS_DEVICE"]) == lease
    text = devices.prompt_for(lease)
    assert "emulator-5554" in text and "Use ONLY this device" in text
    phone = dict(lease, kind="physical", name="Pixel 7a", serial="3B16")
    assert "never clear app data" in devices.prompt_for(phone)


# ── the lease around a worker run ────────────────────────────────────────────

def test_a_device_agent_gets_a_device_and_gives_it_back_even_when_it_crashes(db_path):
    from core import orchestrator
    task = store.create_task("check it on a phone")
    got = {}

    async def fake_spawn(task_id, role, instruction, repo_name=None, prefer_next_runner=False,
                         device=None):
        got["device"] = device
        raise RuntimeError("runner crashed")

    lease = {"device_id": "android-avd:Pixel_4", "platform": "android", "kind": "emulator",
             "name": "Pixel 4", "serial": "emulator-5554", "os": ""}

    async def fake_wait(platform, task_id, role, on_wait=None, poll=5.0):
        got["platform"] = platform
        return lease

    with patch.object(orchestrator, "spawn_worker", side_effect=fake_spawn), \
         patch.object(devices, "wait_acquire", side_effect=fake_wait), \
         patch.object(devices, "release") as release, \
         patch.object(orchestrator, "emit"):
        asyncio.run(orchestrator._spawn_worker_safe(task["id"], "qa", "check", None, queued=True,
                                                    device="ios"))
    assert got["platform"] == "ios" and got["device"] == lease
    release.assert_called_once_with("android-avd:Pixel_4")
    assert any("got device Pixel 4" in m["text"] for m in store.get_thread(task["id"]))


def test_an_agent_without_devices_never_touches_the_pool(db_path):
    from core import orchestrator
    task = store.create_task("x")

    async def fake_spawn(*a, device=None, **k):
        assert device is None

    with patch.object(orchestrator, "spawn_worker", side_effect=fake_spawn), \
         patch.object(devices, "wait_acquire") as wait:
        asyncio.run(orchestrator._spawn_worker_safe(task["id"], "engineer", "x", None, queued=True))
    wait.assert_not_called()


# ── screenshots as task resources ────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _client():
    from web.app import app
    return TestClient(app)


def test_device_screenshot_is_stored_and_served_as_an_image(db_path):
    from core import sessions, timeline
    task = store.create_task("x")
    token = sessions.mint(task["id"], "qa")
    lease = {"device_id": "d", "platform": "android", "kind": "emulator", "name": "Pixel 4",
             "serial": "emulator-5554"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "device_screenshot", "arguments": {"caption": "12345 after backspace"}}}
    c = _client()
    with patch.object(devices, "lease_of", return_value=lease), \
         patch.object(devices, "screenshot", return_value=PNG):
        out = c.post("/mcp/agent", json=body, headers={"Authorization": f"Bearer {token}"}).json()
    assert out["result"]["isError"] is False
    m = store.get_thread(task["id"])[-1]
    assert m["text"] == "12345 after backspace" and m["attachment"].endswith(".png")
    shot = [f for f in timeline.files(task["id"], store.get_thread(task["id"]))["produced"]]
    assert shot[0]["kind"] == "image" and shot[0]["caption"] == "12345 after backspace"
    r = c.get(f"/api/tasks/{task['id']}/file/{m['attachment']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.content == PNG
    modal = c.get(f"/api/tasks/{task['id']}/image/{m['attachment']}").text
    assert "12345 after backspace" in modal and "1 of 1" in modal


def test_screenshot_without_a_lease_is_refused(db_path):
    from core import sessions
    task = store.create_task("x")
    token = sessions.mint(task["id"], "qa")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "device_screenshot", "arguments": {"caption": "x"}}}
    out = _client().post("/mcp/agent", json=body, headers={"Authorization": f"Bearer {token}"}).json()
    assert out["result"]["isError"] is True


def test_file_route_serves_only_raster_images(db_path):
    from core import attachments
    task = store.create_task("x")
    attachments.save_text(task["id"], "evil.svg", "<svg onload=alert(1)>")
    attachments.save_text(task["id"], "notes.md", "# hi")
    c = _client()
    assert c.get(f"/api/tasks/{task['id']}/file/evil.svg").status_code == 404
    assert c.get(f"/api/tasks/{task['id']}/file/notes.md").status_code == 404
    assert c.get(f"/api/tasks/{task['id']}/file/missing.png").status_code == 404


def test_qa_is_a_seeded_device_agent_without_commit(db_path):
    from core import agents
    from core.roles import ROLES
    tools = agents.agent_tools("qa")
    assert "device_screenshot" in tools and "commit" not in tools and "open_mr" not in tools
    assert ROLES["qa"]["devices"] == ["android", "ios"]
