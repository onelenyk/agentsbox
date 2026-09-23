# Sisyphus.app

The desktop app is the daemon's owner. Open it and the work runs; quit it and
the work stops.

- **Open**: starts the daemon (`uvicorn web.app:app` on :7677) as its child and
  shows the UI in a native window. The UI is the web UI, rendered by the
  system's WebKit, the same engine Safari uses. There is no bundled browser
  and no second copy of the interface.
- **Quit** (⌘Q, or closing the window): if agents are working, the app asks
  first. The daemon then parks their tasks as `waiting` (a reply resumes them),
  stops its loops, and exits. See `core/lifecycle.py`.
- **Crash or force quit**: the daemon watches the app's pid
  (`SISYPHUS_PARENT_PID`) and leaves on its own within about a second.

While the app is closed, nothing runs. That includes scheduled autopilots,
backups, forge polling and webhooks.

## Build

```sh
./build.sh             # → build/Sisyphus.app
./build.sh --install   # and copy it to ~/Applications
```

The build bakes two paths into `Info.plist`:
- this repo's `sisyphus/` directory, which is the daemon's working directory
  and holds `.env`, `config.yaml` and `workspace/`;
- the Python that runs the daemon: `SISYPHUS_PYTHON`, default Homebrew's
  `python3.14`.

The build is ad-hoc signed, which is enough for this machine but not for
distribution.

## First launch

If the old launchd login item (`sisyphus daemon install`) is loaded, the app
offers to turn it off. While it is on, it would restart any daemon the app
stops. If another daemon already holds the port, the app offers to stop it.

## Development

- `SISYPHUS_PORT`, `WORKSPACE_ROOT` and `SISYPHUS_DB` run a second copy against
  a scratch database:

  ```sh
  open -n build/Sisyphus.app --env SISYPHUS_PORT=7698 \
      --env WORKSPACE_ROOT=/tmp/ws --env SISYPHUS_DB=/tmp/ws/sisyphus.db
  ```

- Without the app, `python3 sisyphus.py daemon start` still runs the daemon
  headless.
- Safari → Develop → Sisyphus opens the web inspector on the app's window.
