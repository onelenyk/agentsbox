"""API keys a runner was given by hand, kept in the macOS login keychain.

A pasted key never goes into the daemon's database: every worker can read that
file (the sandbox denies writes, not reads — see core.sandbox), so a key stored
there would be handed to every agent on every task. The keychain keeps it at
rest outside anything a worker opens; the launcher reads it only for the run
of the runner it belongs to.

Items live under one service name, one account per runner id. They are written
through `security -i` on stdin so the key never appears in a process listing.
"""
import logging
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE = "sisyphus-runner"

# Provider keys are opaque tokens. Anything with whitespace or quotes is almost
# certainly a paste accident, and would also break the `security -i` line.
_KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,512}$")


class KeychainError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("security") is not None


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def store(account: str, key: str) -> None:
    if not available():
        raise KeychainError("the macOS keychain is not available on this machine")
    if not valid_key(key):
        raise KeychainError("that does not look like an API key (no spaces or quotes, 8+ characters)")
    line = f'add-generic-password -U -s "{SERVICE}" -a "{account}" -w "{key}"\n'
    proc = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0 or "error" in (proc.stderr or "").lower():
        raise KeychainError(f"could not save the key to the keychain: {(proc.stderr or '').strip()[:200]}")


def read(account: str) -> Optional[str]:
    if not account or not available():
        return None
    try:
        proc = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                              capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 — a locked or missing keychain reads as "no key"
        logger.debug("keychain read failed for %s", account, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def delete(account: str) -> None:
    if not account or not available():
        return
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                   capture_output=True, text=True, timeout=15)
