# Running Sisyphus as a supervised daemon (macOS / launchd)

The daemon is a normal `uvicorn` process. For "always on — survives crash and
reboot", run it under **launchd**. The `sisyphus daemon` CLI does the whole
setup for you; you never edit the plist by hand.

## One-time install
```
python sisyphus.py daemon install
```
This renders `deploy/com.sisyphus.daemon.plist.template` (filling in this repo's
path and the current Python interpreter), writes it to
`~/Library/LaunchAgents/com.sisyphus.daemon.plist`, and loads it. From now on the
daemon starts at login and is restarted automatically if it crashes.

Secrets come from neither the plist nor `.env`: enter tokens and keys in the
UI (Integrations → Keys & tokens, and a runner's Edit form for its API key).
They are kept in Sisyphus's own keychain, which the supervised daemon reads
without any shell environment.

## Day-to-day
```
python sisyphus.py daemon status     # running? installed? version
python sisyphus.py daemon restart     # pick up new code
python sisyphus.py daemon logs -f     # tail the daemon log
python sisyphus.py daemon stop        # stop now (launchd will not respawn)
python sisyphus.py daemon start       # start again
```

## Remove
```
python sisyphus.py daemon uninstall
```
Unloads and deletes the LaunchAgent. The repo is untouched.

## Manual mode (no launchd)
Without `install`, `daemon start` still works — it just spawns `uvicorn` in the
background and `daemon stop` kills it. No autostart, no crash-restart.
