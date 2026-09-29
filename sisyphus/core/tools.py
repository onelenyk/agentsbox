"""The tools workers can reach — what is found, where, which version, and the
PATH that decides it.

A worker inherits the daemon's environment plus ``worker_env`` plus the install
dirs of core.runner_detect (see core.verify.check_env — the exact environment a
daemon-run command gets). Whether ``adb`` or ``maestro`` works for an agent is
decided there, not in the human's shell, so this module answers against that
environment and lets the human add directories and toolchain homes to
``worker_env`` without editing config.yaml by hand.
"""
import os
import shutil
from typing import List, Optional

from .runner_catalog import _cached, _run
from .runner_profiles import parse_version

ANDROID_SDK_CANDIDATES = ("$ANDROID_HOME", "$ANDROID_SDK_ROOT", "~/Library/Android/sdk", "~/Android/Sdk")

# (group, binary, version argv tail or None, what it is for, how to get it)
CATALOG = [
    ("Android", "adb", ["version"], "talks to devices and emulators",
     "Android Studio → SDK Manager → SDK Platform-Tools"),
    ("Android", "emulator", ["-version"], "runs Android virtual devices",
     "Android Studio → SDK Manager → Android Emulator"),
    ("Android", "avdmanager", None, "creates and deletes virtual devices",
     "Android Studio → SDK Manager → Android SDK Command-line Tools"),
    ("Android", "sdkmanager", None, "installs system images",
     "Android Studio → SDK Manager → Android SDK Command-line Tools"),
    ("iOS", "xcrun", ["--version"], "simctl / devicectl — simulators and iPhones",
     "Xcode, then `xcode-select --install`"),
    ("iOS", "xcodebuild", ["-version"], "builds iOS apps", "Xcode from the App Store"),
    ("Testing", "maestro", ["--version"], "drives apps on devices (UI flows)",
     "curl -Ls https://get.maestro.mobile.dev | bash"),
    ("Toolchains", "java", ["-version"], "Gradle and Android builds", "a JDK 17+ (Android Studio ships one)"),
    ("Toolchains", "python3", ["--version"], "Python projects", "brew install python"),
    ("Toolchains", "node", ["--version"], "JS projects and some CLIs", "brew install node"),
    ("Toolchains", "git", ["--version"], "every repository operation", "xcode-select --install"),
    ("Toolchains", "make", ["--version"], "projects' own entry points", "xcode-select --install"),
    ("Agent CLIs", "opencode", ["--version"], "runs agents", "see Runners"),
    ("Agent CLIs", "claude", ["--version"], "runs agents", "see Runners"),
]


def effective_env() -> dict:
    """The environment a worker / daemon-run command actually gets."""
    from .verify import check_env
    return check_env()


def android_sdk() -> Optional[str]:
    """The Android SDK directory, if one is found."""
    for c in ANDROID_SDK_CANDIDATES:
        p = os.path.expanduser(os.path.expandvars(c))
        if "$" not in p and os.path.isdir(os.path.join(p, "platform-tools")):
            return p
    return None


def android_tool(name: str) -> Optional[str]:
    """adb / emulator / avdmanager / sdkmanager, by the worker PATH first, then
    inside the SDK — the daemon must find them even before PATH is set up."""
    found = shutil.which(name, path=effective_env().get("PATH", ""))
    if found:
        return found
    sdk = android_sdk()
    if not sdk:
        return None
    for sub in ("platform-tools", "emulator", "cmdline-tools/latest/bin"):
        p = os.path.join(sdk, sub, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _version(path: str, args: Optional[list]) -> str:
    if not args:
        return ""

    def probe():
        p = _run([path, *args], timeout=20)
        return parse_version(((p.stdout or "") + (p.stderr or "")) if p else "")
    return _cached(("tool-version", path), probe)


def tools() -> List[dict]:
    """One row per catalog tool, resolved against the worker PATH."""
    path = effective_env().get("PATH", "")
    rows = []
    for group, binary, vargs, what, hint in CATALOG:
        found = shutil.which(binary, path=path)
        rows.append({"group": group, "binary": binary, "what": what, "hint": hint,
                     "path": found or "", "ok": bool(found),
                     "version": _version(found, vargs) if found else ""})
    return rows


def path_entries() -> List[dict]:
    """The worker PATH in order: {path, exists, source}. ``source`` says why
    the dir is there — the daemon's own PATH, worker_env, or the install dirs
    the daemon appends so CLIs are found (core.runner_detect)."""
    from .config import worker_env
    daemon = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    configured = (worker_env() or {}).get("PATH", "")
    configured = [p for p in configured.split(os.pathsep) if p] if configured else []
    out, seen = [], set()
    for p in effective_env().get("PATH", "").split(os.pathsep):
        if not p or p in seen:
            continue
        seen.add(p)
        if configured and p in configured and p not in daemon:
            source = "worker_env"
        elif p in daemon:
            source = "daemon"
        else:
            source = "install dirs"
        out.append({"path": p, "exists": os.path.isdir(p), "source": source})
    return out


# ── editing worker_env ───────────────────────────────────────────────────────

EDITABLE_VARS = ("ANDROID_HOME", "JAVA_HOME")


def settings() -> dict:
    """The raw worker_env settings this page edits: extra PATH dirs (what comes
    before ${PATH}) and the toolchain homes — placeholders intact."""
    from .repos import _read_config_file
    raw = (_read_config_file().get("worker_env") or {})
    path = str(raw.get("PATH") or "")
    extra = [p for p in path.split(os.pathsep) if p and p not in ("${PATH}", "$PATH")]
    return {"extra_path": extra, **{v: str(raw.get(v) or "") for v in EDITABLE_VARS}}


def save_settings(extra_path: List[str], homes: dict) -> None:
    """Write extra PATH dirs (prepended to the daemon's) and toolchain homes to
    worker_env. Every other worker_env key survives untouched."""
    from .repos import _read_config_file, _write_config_file
    cfg = _read_config_file()
    env = dict(cfg.get("worker_env") or {})
    dirs = []
    for d in extra_path:
        d = d.strip()
        if d and d not in dirs:
            dirs.append(d)
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + ["${PATH}"])
    else:
        env.pop("PATH", None)
    for name in EDITABLE_VARS:
        value = (homes.get(name) or "").strip()
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    if env:
        cfg["worker_env"] = env
    else:
        cfg.pop("worker_env", None)
    _write_config_file(cfg)


def suggestions() -> List[dict]:
    """Directories that hold tools but are not on the worker PATH: {path, why}."""
    on_path = {e["path"] for e in path_entries()}
    cand = []
    sdk = android_sdk()
    if sdk:
        cand += [(os.path.join(sdk, "platform-tools"), "adb"),
                 (os.path.join(sdk, "emulator"), "emulator"),
                 (os.path.join(sdk, "cmdline-tools", "latest", "bin"), "avdmanager, sdkmanager")]
    cand.append((os.path.expanduser("~/.maestro/bin"), "maestro"))
    return [{"path": p, "why": why} for p, why in cand if os.path.isdir(p) and p not in on_path]
