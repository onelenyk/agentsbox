"""Sisyphus's vault: a keychain of its own that no worker can open.

Every credential entered in the UI — a runner's API key, the Jira token, a
repo's forge token — is kept here and nowhere else: not in the database, not
in .env, not in config.yaml.

Why not simply the login keychain: a worker runs as the same user, and
`security find-generic-password -s … -w` from an agent's shell reads any item
the `security` tool created, with no prompt. So the vault is a separate
keychain *file* (``~/Library/Keychains/sisyphus.keychain-db``), not on the
user's search list, and core.sandbox denies every worker any access to that
file — inside the sandbox the item cannot even be found. The daemon, outside
the sandbox, opens it with a random password created with the vault and kept
in the login keychain; a worker that reads that password still cannot read
the file it unlocks.

Every command that carries a secret — the vault password, a value being
stored — goes to `security -i` on stdin, so none appears in a process listing.
"""
import logging
import os
import re
import secrets as _random
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE = "sisyphus-runner"          # runner API keys, one account per runner id
_UNLOCK_SERVICE = "sisyphus-vault"   # the vault's own password, in the login keychain
_UNLOCK_ACCOUNT = "unlock"

# Provider keys are opaque tokens. Anything with whitespace or quotes is almost
# certainly a paste accident, and would also break the `security -i` line.
_KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,512}$")
# Any other value kept here (core.credentials): URLs and emails too, so only
# what would break the quoted `security -i` line is refused.
_VALUE_RE = re.compile(r"^[^\s\"'\\$`]{1,2048}$")

_lock = threading.Lock()
_password: Optional[str] = None


class KeychainError(RuntimeError):
    pass


def vault_path() -> str:
    return os.environ.get("SISYPHUS_VAULT") or str(Path.home() / "Library" / "Keychains"
                                                   / "sisyphus.keychain-db")


def available() -> bool:
    # Tests switch the real keychain off (tests/conftest.py): a suite must not
    # create a vault in the developer's home.
    if os.environ.get("SISYPHUS_KEYCHAIN", "on") == "off":
        return False
    return shutil.which("security") is not None


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def valid_value(value: str) -> bool:
    return bool(_VALUE_RE.match(value or ""))


def _interactive(lines: str) -> subprocess.CompletedProcess:
    return subprocess.run(["security", "-i"], input=lines, capture_output=True, text=True, timeout=20)


def _failed(proc: subprocess.CompletedProcess) -> bool:
    err = (proc.stderr or "").lower()
    return proc.returncode != 0 or "error" in err or "returned -" in err or "could not" in err


def _vault_password() -> str:
    """The vault's password, creating the vault on first use."""
    global _password
    with _lock:
        if _password:
            return _password
        path = vault_path()
        found = subprocess.run(["security", "find-generic-password", "-s", _UNLOCK_SERVICE,
                                "-a", _UNLOCK_ACCOUNT, "-w"], capture_output=True, text=True, timeout=15)
        pw = (found.stdout or "").strip() if found.returncode == 0 else ""
        if pw and os.path.exists(path):
            _password = pw
            return pw
        if os.path.exists(path):
            raise KeychainError(f"the Sisyphus vault exists at {path} but its password is gone "
                                f"from the login keychain — delete the vault file to start over")
        pw = _random.token_hex(24)
        proc = _interactive(
            f'add-generic-password -U -s "{_UNLOCK_SERVICE}" -a "{_UNLOCK_ACCOUNT}" -w "{pw}"\n'
            f'create-keychain -p "{pw}" "{path}"\n'
            f'set-keychain-settings "{path}"\n')
        if _failed(proc) or not os.path.exists(path):
            raise KeychainError(f"could not create the Sisyphus vault: {(proc.stderr or '').strip()[:200]}")
        logger.info("created the Sisyphus vault at %s", path)
        _password = pw
        return pw


def _unlock_line() -> str:
    return f'unlock-keychain -p "{_vault_password()}" "{vault_path()}"\n'


def store(account: str, key: str, service: str = SERVICE) -> None:
    if not available():
        raise KeychainError("the macOS keychain is not available on this machine")
    if service == SERVICE and not valid_key(key):
        raise KeychainError("that does not look like an API key (no spaces or quotes, 8+ characters)")
    if not valid_value(key):
        raise KeychainError("that value has spaces, quotes or a $ in it — paste it again without them")
    proc = _interactive(_unlock_line()
                        + f'add-generic-password -U -s "{service}" -a "{account}" -w "{key}" "{vault_path()}"\n')
    if _failed(proc):
        raise KeychainError(f"could not save to the Sisyphus vault: {(proc.stderr or '').strip()[:200]}")


def read(account: str, service: str = SERVICE) -> Optional[str]:
    if not account or not available():
        return None
    try:
        if os.path.exists(vault_path()):
            proc = _interactive(_unlock_line()
                                + f'find-generic-password -s "{service}" -a "{account}" -w "{vault_path()}"\n')
            value = (proc.stdout or "").strip()
            if value:
                return value
        return _adopt_legacy(account, service)
    except Exception:  # noqa: BLE001 — a locked or missing vault reads as "no key"
        logger.debug("vault read failed for %s", account, exc_info=True)
        return None


def delete(account: str, service: str = SERVICE) -> None:
    if not account or not available():
        return
    if os.path.exists(vault_path()):
        _interactive(_unlock_line()
                     + f'delete-generic-password -s "{service}" -a "{account}" "{vault_path()}"\n')
    # And any copy left in the login keychain from before the vault.
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, text=True, timeout=15)


def _adopt_legacy(account: str, service: str) -> Optional[str]:
    """A key saved before the vault existed sits in the login keychain, where a
    worker can read it. Move it into the vault the first time it is asked for."""
    proc = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                          capture_output=True, text=True, timeout=15)
    value = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not value:
        return None
    try:
        store(account, value, service=service)
    except KeychainError:
        logger.warning("could not move %s/%s into the vault; it stays in the login keychain",
                       service, account)
        return value
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, text=True, timeout=15)
    logger.info("moved %s/%s from the login keychain into the vault", service, account)
    return value
