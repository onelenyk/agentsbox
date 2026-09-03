"""A HOME of our own for worker runs — the only place a CLI's config can be
controlled from.

opencode resolves its config, cache, data and state from ``$HOME``, and it does
so ahead of every environment variable meant to move them: a live worker roster
was unchanged by ``XDG_CONFIG_HOME``, ``XDG_CACHE_HOME``, ``OPENCODE_CONFIG``,
``OPENCODE_CONFIG_DIR``, ``--pure``, an empty project ``"plugin": []`` and all
three ``OPENCODE_DISABLE_*`` switches. Under a private HOME, the same probe
returns ``plugin: []`` and only our own MCP server. HOME is the lever; there
wasn't a second one.

The point is not to take things away. The owner's plugins are theirs and they
use them — in their terminal. A Sisyphus worker is a different context, and what
reaches it should be a decision rather than a leak of whatever the machine
happens to have installed.

So this builds a directory whose contents are an ALLOWLIST: nothing is present
unless it is named below, with a reason. A worker that needs something new fails
visibly and someone adds a line here, which is the opposite of the failure this
replaces — a worker quietly carrying the whole desktop.
"""
import logging
import os
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# (path relative to HOME, why a worker needs it). Order is irrelevant; a missing
# source is skipped without complaint — most machines will not have all of them.
LINKED: List[Tuple[str, str]] = [
    # The CLI's credentials. opencode keeps them in its DATA dir, apart from the
    # config that carries plugins, which is exactly why a private HOME can drop
    # the config and keep the login.
    (".local/share/opencode", "opencode credentials and session store"),
    # Claude Code's own login and settings live here; a private HOME without it
    # would leave that runner unable to authenticate at all.
    (".claude", "Claude Code login and settings"),
    (".claude.json", "Claude Code account state"),
    # Any git the AGENT runs itself (the sanctioned `commit` tool runs in the
    # daemon, but a persona may still shell out) needs an identity, or it stops
    # with "Please tell me who you are".
    (".gitconfig", "git identity for commands the agent runs itself"),
    # Cloning or pushing over ssh needs the keys and the known_hosts that stop
    # a first connection from prompting. Read-only to the run either way: the
    # Seatbelt profile already denies writes to credential paths.
    (".ssh", "git over ssh"),
]


def build(root: str, real_home: str = "") -> str:
    """Create (or refresh) the worker HOME under *root* and return its path.

    Idempotent and cheap — symlinks are replaced only when they point somewhere
    stale, so this can run on every spawn. Never raises: a home we could not
    fully assemble is still better than handing the run the user's own, and the
    missing piece shows up as a runner error naming what it could not find.
    """
    real_home = real_home or os.path.expanduser("~")
    home = Path(root) / "runner-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("could not create the worker HOME at %s", home)
        return real_home

    _sweep_strays(home)
    for rel, _why in LINKED:
        src = Path(real_home) / rel
        if not src.exists():
            continue
        dst = home / rel
        try:
            if dst.is_symlink():
                if os.readlink(dst) == str(src):
                    continue
                dst.unlink()
            elif dst.exists():
                continue          # a real file someone put there wins; do not clobber
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
        except OSError:
            logger.exception("could not link %s into the worker HOME", rel)
    return str(home)


def _sweep_strays(home: Path) -> None:
    """Remove everything in the worker HOME that the allowlist did not put
    there. The home persists between runs, and a run can write into it — a
    repo's own opencode config making opencode install a plugin left
    .config/, .cache/opencode/packages/, .omo/, .npm/ and Library/ behind
    (docs/known-gaps.md), which every LATER worker on every repo would then
    inherit. Sweeping on each build makes "a worker carries what we chose"
    true at spawn time rather than only on the first spawn. The cost is
    opencode re-bootstrapping its package cache (~12s of a run that lasts
    minutes); the alternative is one repo's plugin quietly becoming every
    repo's plugin. Never raises — a stray we could not remove is logged and
    the probe in runner_profiles remains the belt to these braces."""
    import shutil
    allowed = {Path(rel).parts[0] for rel, _ in LINKED}
    removed = []
    try:
        entries = list(home.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in allowed:
            continue
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
            removed.append(entry.name)
        except OSError:
            logger.exception("runner home: could not sweep %s", entry.name)
    if removed:
        logger.warning("runner home: swept %s — a previous run left them, and a "
                       "persisted extra is how one repo's config becomes every "
                       "repo's config", ", ".join(sorted(removed)))
