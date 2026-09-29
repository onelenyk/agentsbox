"""Devices agents may use — Android emulators, iOS simulators, connected phones.

Discovered live from the tools themselves (adb, emulator, avdmanager, xcrun
simctl / devicectl); only what the human decides is stored:

  * ``devices`` — may agents use this device? Emulators and simulators default
    to yes; a physical phone defaults to NO and is allowed one by one. A phone
    on the desk is somebody's phone.
  * ``device_leases`` — which device a running worker holds. Sisyphus hands a
    device to an agent that needs one before it starts and takes it back in a
    ``finally`` after it ends, however it ends (core.orchestrator).
  * ``device_boots`` — what WE started. Only those are shut down when idle; an
    emulator the human launched is theirs.

Emulators stay warm after a lease — the next check starts in seconds, not a
cold boot — and are stopped after ``devices.idle_minutes`` unused.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .tools import android_sdk, android_tool

logger = logging.getLogger(__name__)

PLATFORMS = ("android", "ios")
BOOT_TIMEOUT = 240
EMULATOR_PORTS = range(5554, 5586, 2)
ACTIVE_LEASE_STATES = ("ready", "booting")

# Memory/disk are what make an emulator usable for a Gradle-built app; the
# device profile is the screen. iOS simulators have no RAM knob — the preset
# picks the device type instead.
PRESETS = {
    "phone-small": {"label": "Phone · small", "ram_mb": 2048, "disk": "4G", "cores": 2,
                    "android_device": "pixel_4", "ios_type": "iPhone 16e"},
    "phone": {"label": "Phone", "ram_mb": 4096, "disk": "8G", "cores": 4,
              "android_device": "pixel_7", "ios_type": "iPhone 17"},
    "tablet": {"label": "Tablet", "ram_mb": 4096, "disk": "16G", "cores": 4,
               "android_device": "pixel_tablet", "ios_type": "iPad (A16)"},
}


class DeviceError(RuntimeError):
    """A device action that could not be done — the message says why."""


# ── storage ──────────────────────────────────────────────────────────────────

def init_devices_tables() -> None:
    from .db import get_conn
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id         TEXT PRIMARY KEY,          -- android-avd:<name> | android-usb:<serial> | ios-sim:<udid> | ios-usb:<udid>
            allowed    INTEGER NOT NULL,          -- may agents lease it
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_leases (
            device_id  TEXT PRIMARY KEY,
            task_id    TEXT NOT NULL,
            role       TEXT NOT NULL,
            serial     TEXT NOT NULL DEFAULT '',
            since      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_boots (
            device_id     TEXT PRIMARY KEY,       -- started by Sisyphus, so ours to stop
            serial        TEXT NOT NULL DEFAULT '',
            booted_at     TEXT NOT NULL,
            last_released TEXT
        );
    """)
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(sql, args=(), one=False):
    from .db import get_conn
    conn = get_conn()
    try:
        cur = conn.execute(sql, args)
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            conn.commit()
            return None
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def set_allowed(device_id: str, allowed: bool) -> None:
    _q("INSERT INTO devices (id, allowed, updated_at) VALUES (?, ?, ?) "
       "ON CONFLICT(id) DO UPDATE SET allowed = excluded.allowed, updated_at = excluded.updated_at",
       (device_id, 1 if allowed else 0, _now()))
    _invalidate()


def _allowed_map() -> dict:
    return {r["id"]: bool(r["allowed"]) for r in _q("SELECT id, allowed FROM devices")}


def leases() -> List[dict]:
    return _q("SELECT * FROM device_leases ORDER BY since")


def _boots() -> dict:
    return {r["device_id"]: r for r in _q("SELECT * FROM device_boots")}


def _record_boot(device_id: str, serial: str) -> None:
    _q("INSERT INTO device_boots (device_id, serial, booted_at, last_released) VALUES (?, ?, ?, NULL) "
       "ON CONFLICT(device_id) DO UPDATE SET serial = excluded.serial, booted_at = excluded.booted_at, "
       "last_released = NULL", (device_id, serial, _now()))


def clear_stale_leases() -> int:
    """At boot: no worker survives a daemon restart, so no lease does either."""
    n = len(leases())
    _q("DELETE FROM device_leases")
    return n


# ── running tools ────────────────────────────────────────────────────────────

def _run(argv: List[str], timeout: int = 30, input: Optional[str] = None) -> subprocess.CompletedProcess:
    from .verify import check_env
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input, env=check_env())
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]} not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{' '.join(argv[:3])} timed out after {timeout}s")


def _adb(*args, timeout: int = 20) -> subprocess.CompletedProcess:
    adb = android_tool("adb")
    if not adb:
        return subprocess.CompletedProcess(["adb"], 127, "", "adb not found")
    return _run([adb, *args], timeout=timeout)


def _xcrun(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run(["xcrun", *args], timeout=timeout)


# ── parsing (pure; tested on recorded output) ────────────────────────────────

def parse_adb_devices(out: str) -> List[dict]:
    """`adb devices -l` → [{serial, state, model, product, usb}]."""
    rows = []
    for line in (out or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        rows.append({"serial": parts[0], "state": parts[1],
                     "model": (info.get("model") or "").replace("_", " "),
                     "product": info.get("product", ""), "usb": info.get("usb", "")})
    return rows


def parse_ini(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _size_gb(v: str) -> Optional[float]:
    """"6G" / "6144M" / "6442450944" → GB."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", v or "", re.I)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).upper()
    factor = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]
    return round(n * factor, 1)


def avd_specs(cfg: dict) -> dict:
    image = cfg.get("image.sysdir.1", "")
    api = re.search(r"android-(\d+(?:\.\d+)?)", image)
    ram = cfg.get("hw.ramSize", "")
    ram_mb = None
    if ram:
        g = _size_gb(ram if re.search(r"[KMGT]", ram, re.I) else ram + "M")
        ram_mb = int(g * 1024) if g else None
    w, h = cfg.get("hw.lcd.width"), cfg.get("hw.lcd.height")
    return {"os": f"Android API {api.group(1)}" if api else "Android", "api": api.group(1) if api else "",
            "ram_mb": ram_mb, "disk_gb": _size_gb(cfg.get("disk.dataPartition.size", "")),
            "cpu": cfg.get("hw.cpu.ncore", ""), "screen": f"{w}×{h}" if w and h else "",
            "profile": cfg.get("hw.device.name", ""), "play": cfg.get("PlayStore.enabled") == "true",
            "image": image.strip("/")}


def parse_simctl(data: dict) -> List[dict]:
    """`simctl list devices available -j` → simulators."""
    out = []
    for runtime, devs in (data.get("devices") or {}).items():
        m = re.search(r"(iOS|watchOS|tvOS|xrOS|visionOS)-(\d+)-(\d+)", runtime)
        if not m or m.group(1) != "iOS":
            continue
        os_name = f"iOS {m.group(2)}.{m.group(3)}"
        for d in devs:
            if d.get("isAvailable") is False:
                continue
            out.append({"udid": d["udid"], "name": d["name"], "state": d.get("state", ""),
                        "os": os_name, "type": d.get("deviceTypeIdentifier", "")})
    return out


# ── discovery ────────────────────────────────────────────────────────────────

def avd_home() -> Path:
    return Path(os.environ.get("ANDROID_AVD_HOME") or os.path.expanduser("~/.android/avd"))


def _avd_dir(name: str) -> Optional[Path]:
    ini = avd_home() / f"{name}.ini"
    if ini.is_file():
        p = parse_ini(ini.read_text(errors="replace")).get("path")
        if p and Path(p).is_dir():
            return Path(p)
    d = avd_home() / f"{name}.avd"
    return d if d.is_dir() else None


def list_avds() -> List[str]:
    emu = android_tool("emulator")
    if not emu:
        return []
    r = _run([emu, "-list-avds"], timeout=20)
    return [ln.strip() for ln in (r.stdout or "").splitlines()
            if ln.strip() and not ln.startswith(("INFO", "WARNING", "ERROR"))]


def _emulator_avd(serial: str) -> str:
    r = _adb("-s", serial, "emu", "avd", "name", timeout=10)
    return (r.stdout or "").splitlines()[0].strip() if r.returncode == 0 and r.stdout else ""


def _booted(serial: str) -> bool:
    r = _adb("-s", serial, "shell", "getprop", "sys.boot_completed", timeout=10)
    return r.returncode == 0 and (r.stdout or "").strip() == "1"


_cache = {"at": 0.0, "devices": []}
_cache_lock = threading.Lock()


def _invalidate() -> None:
    _cache["at"] = 0.0


def discover(fresh: bool = False) -> List[dict]:
    """Every device with its live state, human settings and lease. Cached a
    few seconds: the page and the lease loop both ask often."""
    with _cache_lock:
        if not fresh and time.monotonic() - _cache["at"] < 5:
            return _cache["devices"]
        devs = _discover()
        _cache.update(at=time.monotonic(), devices=devs)
        return devs


def _discover() -> List[dict]:
    allowed = _allowed_map()
    held = {l["device_id"]: l for l in leases()}
    ours = _boots()
    out: List[dict] = []

    def add(d):
        d["allowed"] = allowed.get(d["id"], d["kind"] != "physical")
        d["lease"] = held.get(d["id"])
        d["booted_by_us"] = d["id"] in ours
        out.append(d)

    # Android: running devices first, so AVDs can be matched to their serial.
    adb_rows = parse_adb_devices(_adb("devices", "-l").stdout or "")
    running_avds = {}
    for row in adb_rows:
        if row["serial"].startswith("emulator-"):
            name = _emulator_avd(row["serial"]) if row["state"] == "device" else ""
            if name:
                running_avds[name] = row
        else:
            state = {"device": "ready", "unauthorized": "unauthorized"}.get(row["state"], "offline")
            ver = ""
            if state == "ready":
                ver = (_adb("-s", row["serial"], "shell", "getprop", "ro.build.version.release",
                            timeout=10).stdout or "").strip()
            add({"id": f"android-usb:{row['serial']}", "platform": "android", "kind": "physical",
                 "name": row["model"] or row["serial"], "serial": row["serial"], "state": state,
                 "specs": {"os": f"Android {ver}".strip(), "connection": "USB" if row["usb"] else "network"}})
    for name in list_avds():
        d = _avd_dir(name)
        cfg = parse_ini((d / "config.ini").read_text(errors="replace")) if d and (d / "config.ini").is_file() else {}
        row = running_avds.get(name)
        state = "broken" if not cfg else ("off" if not row else ("ready" if _booted(row["serial"]) else "booting"))
        add({"id": f"android-avd:{name}", "platform": "android", "kind": "emulator",
             "name": name.replace("_", " "), "avd": name, "serial": row["serial"] if row else "",
             "state": state, "specs": avd_specs(cfg) if cfg else {}})

    # iOS simulators, then connected iPhones.
    r = _xcrun("simctl", "list", "devices", "available", "-j", timeout=30)
    if r.returncode == 0:
        try:
            for s in parse_simctl(json.loads(r.stdout)):
                state = {"Booted": "ready", "Booting": "booting"}.get(s["state"], "off")
                add({"id": f"ios-sim:{s['udid']}", "platform": "ios", "kind": "simulator",
                     "name": s["name"], "serial": s["udid"], "state": state,
                     "specs": {"os": s["os"]}})
        except (ValueError, KeyError):
            logger.warning("could not parse simctl output")
    for p in _ios_physical():
        add(p)
    return out


def _ios_physical() -> List[dict]:
    """Connected iPhones/iPads via devicectl (Xcode 15+). Best-effort: no Xcode
    or no device is simply none."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        r = _xcrun("devicectl", "list", "devices", "--quiet", "--json-output", f.name, timeout=30)
        if r.returncode != 0:
            return []
        try:
            data = json.loads(Path(f.name).read_text() or "{}")
        except ValueError:
            return []
    out = []
    for d in (data.get("result") or {}).get("devices") or []:
        hw, props = d.get("hardwareProperties") or {}, d.get("deviceProperties") or {}
        conn = d.get("connectionProperties") or {}
        if hw.get("reality") == "virtual":
            continue
        udid = hw.get("udid") or d.get("identifier", "")
        paired = conn.get("pairingState") == "paired"
        out.append({"id": f"ios-usb:{udid}", "platform": "ios", "kind": "physical",
                    "name": props.get("name") or hw.get("marketingName") or "iPhone", "serial": udid,
                    "state": "ready" if paired and conn.get("tunnelState") != "unavailable" else "offline",
                    "specs": {"os": f"iOS {props.get('osVersionNumber', '')}".strip(),
                              "model": hw.get("marketingName", "")}})
    return out


def get(device_id: str, fresh: bool = False) -> Optional[dict]:
    return next((d for d in discover(fresh) if d["id"] == device_id), None)


# ── actions ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    used = {int(r["serial"].split("-")[1]) for r in parse_adb_devices(_adb("devices").stdout or "")
            if r["serial"].startswith("emulator-")}
    for p in EMULATOR_PORTS:
        if p not in used:
            return p
    raise DeviceError("no free emulator port — too many emulators running")


def _wait_android_boot(serial: str, timeout: int = BOOT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _booted(serial):
            return
        time.sleep(3)
    raise DeviceError(f"{serial} did not finish booting in {timeout}s")


def start(device_id: str, window: bool = False, wipe: bool = False, wait: bool = True) -> dict:
    """Boot an emulator/simulator. Headless unless *window*; *wipe* boots with
    fresh user data. Returns the device, ready (when *wait*)."""
    d = get(device_id, fresh=True)
    if not d:
        raise DeviceError(f"no device {device_id}")
    if d["kind"] == "physical":
        raise DeviceError("a physical device is not started from here")
    if d["state"] in ("ready", "booting") and not wipe:
        return d
    if d["kind"] == "emulator":
        emu = android_tool("emulator")
        if not emu:
            raise DeviceError("emulator not found — see Tools")
        port = _free_port()
        serial = f"emulator-{port}"
        argv = [emu, "-avd", d["avd"], "-port", str(port), "-no-boot-anim", "-no-snapshot-save"]
        if not window:
            argv += ["-no-window", "-no-audio"]
        if wipe:
            argv.append("-wipe-data")
        from .verify import check_env
        log = open(os.path.join(tempfile.gettempdir(), f"sisyphus-{d['avd']}.log"), "ab")
        # Its own session: the emulator must outlive this request and never be
        # reaped as a child of whoever asked.
        subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=check_env(), start_new_session=True)
        _record_boot(device_id, serial)
        _invalidate()
        if wait:
            _adb("-s", serial, "wait-for-device", timeout=BOOT_TIMEOUT)
            _wait_android_boot(serial)
    else:
        r = _xcrun("simctl", "boot", d["serial"])
        if r.returncode != 0 and "current state: Booted" not in (r.stderr or ""):
            raise DeviceError((r.stderr or r.stdout).strip() or "simctl boot failed")
        _record_boot(device_id, d["serial"])
        _invalidate()
        if window:
            # simctl boots headless; the Simulator app is the window onto it.
            subprocess.Popen(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", d["serial"]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            r = _xcrun("simctl", "bootstatus", d["serial"], "-b", timeout=BOOT_TIMEOUT)
            if r.returncode != 0:
                raise DeviceError((r.stderr or r.stdout).strip()[-300:] or "simulator did not boot")
    _invalidate()
    return get(device_id, fresh=True) or d


def stop(device_id: str, force: bool = False) -> None:
    d = get(device_id, fresh=True)
    if not d:
        raise DeviceError(f"no device {device_id}")
    if d["lease"] and not force:
        raise DeviceError(f"in use by task {d['lease']['task_id']} — stop that first")
    if d["kind"] == "physical":
        raise DeviceError("a physical device is not stopped from here")
    if d["state"] in ("off", "broken"):
        return
    if d["kind"] == "emulator":
        _adb("-s", d["serial"], "emu", "kill", timeout=20)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(
                r["serial"] == d["serial"] for r in parse_adb_devices(_adb("devices").stdout or "")):
            time.sleep(1)
    else:
        _xcrun("simctl", "shutdown", d["serial"])
    _q("DELETE FROM device_boots WHERE device_id = ?", (device_id,))
    _invalidate()


def wipe(device_id: str) -> dict:
    """Factory-fresh: all apps and data gone. Emulators come back up wiped;
    simulators are erased and left off."""
    d = get(device_id, fresh=True)
    if not d:
        raise DeviceError(f"no device {device_id}")
    if d["kind"] == "physical":
        raise DeviceError("physical devices are never wiped from here")
    if d["lease"]:
        raise DeviceError(f"in use by task {d['lease']['task_id']}")
    stop(device_id)
    if d["kind"] == "emulator":
        return start(device_id, wipe=True)
    r = _xcrun("simctl", "erase", d["serial"])
    if r.returncode != 0:
        raise DeviceError((r.stderr or r.stdout).strip() or "simctl erase failed")
    _invalidate()
    return get(device_id, fresh=True) or d


def delete(device_id: str) -> None:
    d = get(device_id, fresh=True)
    if not d:
        raise DeviceError(f"no device {device_id}")
    if d["kind"] == "physical":
        raise DeviceError("a physical device is not deleted from here")
    if d["lease"]:
        raise DeviceError(f"in use by task {d['lease']['task_id']}")
    stop(device_id)
    if d["kind"] == "emulator":
        mgr = android_tool("avdmanager")
        if not mgr:
            raise DeviceError("avdmanager not found — see Tools")
        r = _run([mgr, "delete", "avd", "-n", d["avd"]], timeout=60)
        if r.returncode != 0:
            raise DeviceError((r.stderr or r.stdout).strip()[-300:] or "avdmanager failed")
    else:
        r = _xcrun("simctl", "delete", d["serial"])
        if r.returncode != 0:
            raise DeviceError((r.stderr or r.stdout).strip() or "simctl delete failed")
    _q("DELETE FROM devices WHERE id = ?", (device_id,))
    _invalidate()


def android_images() -> List[dict]:
    """Installed system images: [{package, api, tag, abi, label}], newest first."""
    sdk = android_sdk()
    out = []
    if not sdk:
        return out
    base = Path(sdk) / "system-images"
    for p in sorted(base.glob("android-*/*/*")):
        if not (p / "system.img").exists() and not (p / "source.properties").exists():
            continue
        api, tag, abi = p.parts[-3], p.parts[-2], p.parts[-1]
        out.append({"package": f"system-images;{api};{tag};{abi}", "api": api.split("-", 1)[1],
                    "tag": tag, "abi": abi,
                    "label": f"Android API {api.split('-', 1)[1]} · {tag.replace('_', ' ')}"})
    return sorted(out, key=lambda i: [int(x) if x.isdigit() else 0 for x in re.split(r"\D", i["api"])],
                  reverse=True)


def ios_runtimes() -> List[dict]:
    r = _xcrun("simctl", "list", "runtimes", "available", "-j", timeout=30)
    try:
        rts = json.loads(r.stdout or "{}").get("runtimes") or []
    except ValueError:
        return []
    return [{"id": x["identifier"], "label": x["name"]} for x in rts
            if x.get("platform") == "iOS" or x["identifier"].split(".")[-1].startswith("iOS")]


def create(platform: str, preset: str, name: str, image: str = "") -> str:
    """A new emulator/simulator from a preset. Returns its device id."""
    p = PRESETS.get(preset)
    if not p:
        raise DeviceError(f"unknown preset {preset!r}")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("_")
    if not name:
        raise DeviceError("a name is required")
    if platform == "android":
        images = android_images()
        img = next((i for i in images if i["package"] == image), images[0] if images else None)
        if not img:
            raise DeviceError("no Android system image installed — install one in Android Studio → SDK Manager")
        mgr = android_tool("avdmanager")
        if not mgr:
            raise DeviceError("avdmanager not found — see Tools")
        if name in list_avds():
            raise DeviceError(f"an emulator named {name} already exists")
        r = _run([mgr, "create", "avd", "-n", name, "-k", img["package"], "-d", p["android_device"]],
                 timeout=120, input="no\n")
        if r.returncode != 0:
            raise DeviceError((r.stderr or r.stdout).strip()[-400:] or "avdmanager failed")
        d = _avd_dir(name)
        if d and (d / "config.ini").is_file():
            cfg_path = d / "config.ini"
            lines = cfg_path.read_text().splitlines()
            want = {"hw.ramSize": str(p["ram_mb"]), "disk.dataPartition.size": p["disk"],
                    "hw.cpu.ncore": str(p["cores"]), "hw.keyboard": "yes"}
            kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in want]
            cfg_path.write_text("\n".join(kept + [f"{k}={v}" for k, v in want.items()]) + "\n")
        _invalidate()
        return f"android-avd:{name}"
    if platform == "ios":
        rts = ios_runtimes()
        rt = next((x for x in rts if x["id"] == image), rts[-1] if rts else None)
        if not rt:
            raise DeviceError("no iOS runtime installed — Xcode → Settings → Components")
        r = _xcrun("simctl", "create", name, p["ios_type"], rt["id"])
        if r.returncode != 0:
            raise DeviceError((r.stderr or r.stdout).strip() or "simctl create failed")
        _invalidate()
        return f"ios-sim:{r.stdout.strip()}"
    raise DeviceError(f"unknown platform {platform!r}")


def screenshot(device: dict) -> bytes:
    """A PNG of what the device shows right now (Android devices and iOS
    simulators — simctl cannot reach a physical iPhone)."""
    if device["platform"] == "ios" and device.get("kind") == "physical":
        raise DeviceError("screenshots of a physical iPhone are not supported yet")
    if device["platform"] == "android":
        adb = android_tool("adb")
        if not adb:
            raise DeviceError("adb not found")
        p = subprocess.run([adb, "-s", device["serial"], "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=30)
        if p.returncode != 0 or not p.stdout.startswith(b"\x89PNG"):
            raise DeviceError((p.stderr or b"").decode(errors="replace").strip() or "screencap failed")
        return p.stdout
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "shot.png")
        r = _xcrun("simctl", "io", device["serial"], "screenshot", out, timeout=30)
        if r.returncode != 0 or not os.path.isfile(out):
            raise DeviceError((r.stderr or r.stdout).strip() or "simctl screenshot failed")
        return Path(out).read_bytes()


# ── leasing ──────────────────────────────────────────────────────────────────

_lease_lock = threading.Lock()


def max_leases() -> int:
    from .config import get_config
    try:
        return max(1, int(get_config("devices.max_leases", 1) or 1))
    except (TypeError, ValueError):
        return 1


def idle_minutes() -> float:
    from .config import get_config
    try:
        return max(1.0, float(get_config("devices.idle_minutes", 30) or 30))
    except (TypeError, ValueError):
        return 30.0


def _pick(devs: List[dict], platform: str) -> Optional[dict]:
    """Free and allowed; running virtual first (instant), then a running phone
    the human allowed, then a virtual one that has to boot."""
    free = [d for d in devs if d["platform"] == platform and d["allowed"] and not d["lease"]]
    for want in (lambda d: d["kind"] != "physical" and d["state"] == "ready",
                 lambda d: d["kind"] == "physical" and d["state"] == "ready",
                 lambda d: d["kind"] != "physical" and d["state"] == "off"):
        for d in free:
            if want(d):
                return d
    return None


def acquire(platform: str, task_id: str, role: str) -> Optional[dict]:
    """Lease a device for (task, role), booting one if needed. None when none
    is free right now (the caller waits and asks again). Raises DeviceError
    when the chosen device would not boot — the lease is dropped first."""
    with _lease_lock:
        if len(leases()) >= max_leases():
            return None
        d = _pick(discover(fresh=True), platform)
        if not d:
            return None
        _q("INSERT INTO device_leases (device_id, task_id, role, serial, since) VALUES (?, ?, ?, ?, ?)",
           (d["id"], task_id, role, d.get("serial", ""), _now()))
        _invalidate()
    try:
        if d["state"] != "ready":
            d = start(d["id"])
            _q("UPDATE device_leases SET serial = ? WHERE device_id = ?", (d["serial"], d["id"]))
    except Exception:
        release(d["id"])
        raise
    logger.info("device %s leased to %s on %s", d["id"], role, task_id)
    return {"device_id": d["id"], "platform": d["platform"], "kind": d["kind"], "name": d["name"],
            "serial": d["serial"], "os": (d.get("specs") or {}).get("os", "")}


def release(device_id: str) -> None:
    _q("DELETE FROM device_leases WHERE device_id = ?", (device_id,))
    _q("UPDATE device_boots SET last_released = ? WHERE device_id = ?", (_now(), device_id))
    _invalidate()
    logger.info("device %s released", device_id)


def lease_of(task_id: str, role: str) -> Optional[dict]:
    row = _q("SELECT * FROM device_leases WHERE task_id = ? AND role = ?", (task_id, role), one=True)
    if not row:
        return None
    d = get(row["device_id"]) or {}
    return {"device_id": row["device_id"], "serial": row["serial"] or d.get("serial", ""),
            "platform": d.get("platform", row["device_id"].split("-")[0]), "kind": d.get("kind", ""),
            "name": d.get("name", row["device_id"])}


async def wait_acquire(platform: str, task_id: str, role: str, on_wait=None, poll: float = 5.0) -> dict:
    """acquire() until it succeeds; *on_wait* is called once when it has to."""
    told = False
    while True:
        lease = await asyncio.to_thread(acquire, platform, task_id, role)
        if lease:
            return lease
        if not told and on_wait:
            on_wait()
            told = True
        await asyncio.sleep(poll)


def env_for(lease: dict) -> dict:
    """What the worker gets so its tools pick the leased device by default."""
    env = {"SISYPHUS_DEVICE": json.dumps(lease)}
    if lease["platform"] == "android":
        env["ANDROID_SERIAL"] = lease["serial"]
    else:
        env["SISYPHUS_DEVICE_UDID"] = lease["serial"]
    return env


def prompt_for(lease: dict) -> str:
    kind = {"emulator": "Android emulator", "simulator": "iOS simulator"}.get(lease["kind"], "physical device")
    if lease["platform"] == "android":
        how = (f"`adb -s {lease['serial']} …` (ANDROID_SERIAL is set), "
               f"`maestro --udid {lease['serial']} test …`, Gradle installs with DEVICE={lease['serial']}")
    else:
        how = (f"`xcrun simctl … {lease['serial']}` (SISYPHUS_DEVICE_UDID is set), "
               f"`maestro --udid {lease['serial']} test …`")
    extra = (" It is a real phone someone owns: never clear app data, never factory-reset, never "
             "change system settings." if lease["kind"] == "physical" else "")
    return (f"\n\n## Your device\nSisyphus leased you **{lease['name']}** ({kind}, {lease['os'] or lease['platform']}, "
            f"serial `{lease['serial']}`) for this run. Use ONLY this device: {how}. Other devices on this "
            f"machine belong to others — never target them, never start or stop emulators yourself. "
            f"Take screenshots of what you claim with the device_screenshot tool.{extra}")


# ── idle shutdown ────────────────────────────────────────────────────────────

def idle_sweep(now: Optional[datetime] = None) -> List[str]:
    """Stop emulators/simulators WE booted that nobody leased for the idle
    window. Ones the human started are never touched."""
    now = now or datetime.now(timezone.utc)
    limit = timedelta(minutes=idle_minutes())
    held = {l["device_id"] for l in leases()}
    stopped = []
    for dev_id, b in _boots().items():
        if dev_id in held:
            continue
        since = datetime.fromisoformat(b["last_released"] or b["booted_at"])
        if now - since < limit:
            continue
        try:
            stop(dev_id)
            stopped.append(dev_id)
        except DeviceError as e:
            logger.info("idle sweep: %s: %s", dev_id, e)
            _q("DELETE FROM device_boots WHERE device_id = ?", (dev_id,))
    return stopped


async def devices_forever(every: int = 60) -> None:
    from . import heartbeat
    heartbeat.register("devices", every=every, note="idle emulators off")
    while True:
        try:
            stopped = await asyncio.to_thread(idle_sweep)
            heartbeat.beat("devices", note=(f"stopped {', '.join(stopped)}" if stopped else
                                            f"{len(leases())} leased"))
        except Exception:  # noqa: BLE001
            logger.exception("devices loop pass failed")
        await asyncio.sleep(every)
