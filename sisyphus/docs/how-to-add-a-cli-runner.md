# How to add a new CLI runner

Sisyphus drives agent CLIs — today `opencode` and `claude-cli` — as
non-interactive subprocesses, hands each one a stdio MCP bridge to Sisyphus's
own control plane, and lets the model work a task through that bridge. Every
thing that differs between one CLI and another is owned by a *runner profile*:
one entry in `sisyphus/core/runner_profiles.py`. The launcher and the worker
are type-agnostic — they ask the profile module for argv, environment, MCP
config and tool naming, and never hardcode a CLI's flags.

This guide is the step-by-step for adding a third runner. It uses the two
existing entries — `opencode` and `claude-cli` — as the concrete reference for
every section. It is documentation only; it does not change any code.

- [1. The seam: `runner_profiles.py`](#1-the-seam-runner_profilespy)
- [2. Register the type](#2-register-the-type)
- [3. Fill in the per-type profile data](#3-fill-in-the-per-type-profile-data)
- [4. Implement the per-type functions](#4-implement-the-per-type-functions)
- [5. Wire it up outside the profiles](#5-wire-it-up-outside-the-profiles)
- [6. Tests to add and update](#6-tests-to-add-and-update)
- [7. Config and docs touchpoints](#7-config-and-docs-touchpoints)
- [8. Checklist](#8-checklist)

---

## 1. The seam: `runner_profiles.py`

`sisyphus/core/runner_profiles.py` is the seam. Its module docstring says it
plainly: each profile owns exactly the four things that differ per CLI —

| concern | what it decides |
| --- | --- |
| `argv` | how prompt, model and working directory reach the binary |
| `env` | which credential variables that CLI actually reads |
| `tool_prefix` | how the model must spell our agent-MCP tools in a tool call |
| `config` | the on-disk file that points the CLI at our MCP bridge |

Adding a third runner is a new entry here, *not* an edit to the launcher.
The launcher (`core/launcher.py`), the worker (`core/worker.py`), the decider,
the smoke test (`core/runner_smoke.py`) and the preflight all call into
`runner_profiles` and are otherwise type-agnostic.

A "profile entry" is not a single class or dict — it is a scatter of
per-type records and per-type branches inside one module, each keyed by the
runner type string. Adding a runner means adding a value to every one of those
records/branches. Sections 2–4 walk them in the order you meet them.

## 2. Register the type

**`SUPPORTED_TYPES`** (`core/runner_profiles.py:33`) is the registry.

```python
SUPPORTED_TYPES = ("opencode", "claude-cli")
```

Add your type string here, e.g. `("opencode", "claude-cli", "your-cli")`.

This single list is what makes a type *launchable and selectable*:

- `normalize_type()` (`runner_profiles.py:42`) validates every runner row
  against it at launch time and raises `UnsupportedRunner` otherwise — so a
  row that somehow gets a type with no profile fails with a clear message
  instead of being fed another CLI's flags.
- `core/runners.py:27` imports it as `RUNNER_TYPES`, and the web UI builds the
  runner-form type dropdown from `RUNNER_TYPES` (`web/routes.py:673`). The UI
  previously advertised `ollama`/`openai-api`/`custom`, which had no profile
  and could never run — the whole point of one registry is that a type becomes
  selectable exactly when it becomes launchable.

Two related registries live outside the profiles and are covered in section 5:
the binary→type map in `core/runner_detect.py` (for autodetect) and the display
labels in `web/routes.py`.

## 3. Fill in the per-type profile data

These are the per-type *records* in `runner_profiles.py`. Each one has a
consumer that iterates or defaults by type; miss one and the new runner is
either invisible or mis-reported in the UI and health check.

### 3.1 `_SUGGESTED` / `suggested_config(rtype)` — UI prefill

`runner_profiles.py:60` and `:87`. What a *new* runner row of this type should
be prefilled with when the operator clicks "Add" on the autodetect panel
(`web/routes.py:771`). Four fields, all strings:

```python
"claude-cli": {
    "api_key_env": "",   # which env var carries an API key; "" = none
    "base_url": "",      # provider base URL; "" = the CLI's own default
    "model": "",         # blank = "whatever the CLI is configured to use"
    "hint": "Uses your existing Claude Code login (OAuth or ANTHROPIC_API_KEY).",
},
```

Field meanings:

- `api_key_env` — the environment variable an API key for this runner arrives
  in. Both existing runners keep it deliberately blank because both log in for
  themselves; the `opencode` entry documents *why* (the provider is chosen by
  the model's prefix, so naming one provider's key is wrong for every other
  model).
- `base_url` — provider base URL; blank means the CLI's own default.
- `model` — default model; blank means "whatever the CLI is configured to use".
- `hint` — human sentence shown in the UI, explains how the runner
  authenticates.

`test_runner_profiles.py::TestSuggestedConfig` asserts every `SUPPORTED_TYPES`
entry has an empty `api_key_env`/`base_url` and a non-empty `hint`, so a new
type flows through it automatically once registered.

### 3.2 `_CREDENTIAL_STORE` / `credential_store(rtype)` — health note

`runner_profiles.py:98` and `:104`. A human description of where this CLI finds
credentials *without* an env var, or `None` if the CLI can only authenticate
from the environment:

```python
_CREDENTIAL_STORE = {
    "opencode": "opencode's own login (~/.local/share/opencode/auth.json — `opencode auth list`)",
    "claude-cli": "your Claude Code login (OAuth in the keychain, or ANTHROPIC_API_KEY)",
}
```

Consumer: `core/runners.py::health_check` (`runners.py:331`). Both supported
CLIs log in for themselves, so an unset `api_key_env` is **not** by itself a
broken runner; when the store description exists, the health check downgrades
a missing variable from an *issue* (unhealthy) to a *note*. A runner whose CLI
can only authenticate from the environment should leave this `None` — then a
missing variable correctly reports unhealthy.

### 3.3 `VERIFIED_VERSIONS` — the version the flags were tested against

`runner_profiles.py:125`:

```python
VERIFIED_VERSIONS = {
    "opencode": "1.18.31",
    "claude-cli": "2.1.278",
}
```

Every flag in the argv builders (section 4) is this project's reading of a CLI
it does not own. A CLI that renames a flag breaks every run silently, deep in a
worker nobody is watching. The version actually exercised is written down, and
`version_note()` (`runner_profiles.py:140`) turns a mismatch into a *note* on
the runner's health ("your-cli X is installed; this daemon's launch flags were
verified against Y") — a pointer for the next debugging session, never an
issue. `parse_version()` (`:131`) extracts a bare `\d+\.\d+(\.\d+)?` from the
CLI's `--version` output and degrades to `""` for anything unreadable. Update
the value after running the suite against the new CLI.

### 3.4 `_WORKSPACE_ARTIFACTS` — what the CLI leaves in a worktree

`runner_profiles.py:163`:

```python
_WORKSPACE_ARTIFACTS = {
    "opencode": (
        ("opencode.json", "opencode config"),
        (".opencode", "opencode config dir"),
    ),
    "claude-cli": (
        (".claude", "Claude Code config dir"),
    ),
}
```

`(path, human label)` for the files and directories the CLI owns inside a repo
working tree — its generated config and session state. Two consumers, both of
which used to hardcode opencode's names:

- `core/git_ops.py:26` excludes these from an engineer's commits (a Claude
  worker's cwd is the same worktree; without `.claude` here its state was one
  `git add -A` away from landing in a real commit).
- `core/repo_context.py:26` surfaces them as recognised config when scanning a
  repo; `core/worker.py:102` excludes them when salvaging uncommitted work.

`workspace_artifacts(rtype)` (`:174`) returns the paths for one type,
`workspace_artifacts()` with no arg the union across every runner (used for
diff computation, when nothing records which CLI produced a tree), and
`workspace_artifact_labels()` the `(path, label)` pairs for listings.

### 3.5 `tool_prefix` — how the model names our MCP tools

`runner_profiles.py:202`:

```python
def tool_prefix(rtype: str, server_name: str) -> str:
    if rtype == "claude-cli":
        return f"mcp__{server_name}__"
    return f"{server_name}_"
```

Each CLI exposes a tool from our MCP server under a different name shape:
opencode spells it `<server>_<tool>`, Claude Code `mcp__<server>__<tool>`.
The worker *names the tools literally in the prompt* (`core/worker.py:198`,
`_build_prompt`), because a repo that itself contains Sisyphus's source could
otherwise confuse the model into calling a control-plane tool. Getting the
prefix wrong means the model is told to call a tool that does not exist under
that name. This one default branch — anything that is not `claude-cli` gets
opencode's shape — is a place a new runner must not blindly inherit: if the new
CLI has its own convention, add an explicit branch.

## 4. Implement the per-type functions

These are the per-type *branches* in `runner_profiles.py`. Each is what the
launcher/worker actually run with.

### 4.1 `write_mcp_config` — point the CLI at our MCP bridge

`runner_profiles.py:361`. Writes the config that makes the CLI launch our stdio
MCP bridge (`tools/agent_mcp_stdio.py`) for this one run. Returns a path to
pass back as `mcp_config`, or `None` when the CLI discovers the config from the
working directory on its own:

- **`claude-cli`** (`:379`): writes `mcp-config.json` under the run's *output
  dir* (not the worktree — the worktree is a real git checkout whose porcelain
  status is read and salvaged), with shape `{"mcpServers": {name: {command,
  args, env}}}`, and returns the path; Claude Code takes it as `--mcp-config`.
- **`opencode`** (`:399`): writes `.opencode/opencode.json` into the worktree
  (the CLI discovers it from its cwd), returns `None`. It also writes our own
  `agent` entry with `"mode": "primary"` — and **must** name a model, because a
  primary agent without one makes `opencode run` hang forever with no output —
  and disables *by name* every MCP server that would otherwise merge in from the
  user's global config or the checkout's own config (`foreign_mcp_servers`,
  `repo_declared_opencode`).

For a new runner you must decide which shape its CLI takes. The invariants that
matter (each learned the hard way, documented in the code):

- the config must not pollute the worktree unless the CLI genuinely discovers
  it from cwd;
- the config must isolate the run from the user's *global* configuration — a
  foreign globally-registered server can win a name collision and leave the
  model with none of our tools;
- an agent/persona you mint yourself, with an explicit model, is more durable
  than borrowing one of the CLI's built-in agent names, which a user's global
  config can reclassify out from under you.

### 4.2 `build_argv` — the full command line

`runner_profiles.py:497`. One non-interactive run of this CLI:

```python
def build_argv(rtype, binary, extra_args, prompt, *, worktree_path="",
               pure=False, agent=None, model="", mcp_config=None):
    if rtype == "claude-cli":
        return _claude_argv(binary, extra_args, prompt, model=model,
                            mcp_config=mcp_config)
    return _opencode_argv(binary, extra_args, prompt, worktree_path=worktree_path,
                          pure=pure, agent=agent)
```

Reference shapes:

```python
# opencode:  opencode run [extra_args] <prompt> --format json --auto [--pure]
#            [--agent NAME] [--dir /path]
# claude-cli: claude -p [extra_args] <prompt> --output-format stream-json
#             --verbose --permission-mode bypassPermissions
#             --setting-sources project [--mcp-config PATH --strict-mcp-config]
#             [--model NAME]
```

Design constraints every argv builder must satisfy (all measured in production,
see the comments at `_opencode_argv`/`_claude_argv`):

- **Non-interactive mode** — the worker is a subprocess with no terminal.
- **Output that streams.** The launcher kills a run that produces no output for
  `idle_timeout` seconds (`core/launcher.py:141`). Claude Code's plain
  `--output-format json` buffers everything and writes it in one burst at exit,
  so a run that thought for longer than the idle timeout was killed, while short
  runs passed — the worst failure shape. `stream-json` emits an event per step,
  keeping the idle timer honest. If your CLI can stream per-step events, use
  them.
- **No permission prompts.** Nothing can answer a prompt in non-interactive
  mode, so any gated tool call hangs until the idle timeout kills the run.
  Claude Code needs `--permission-mode bypassPermissions`; write confinement
  actually comes from the Seatbelt sandbox (`core/sandbox.py`).
- **Isolation from the user's global config.** Claude Code takes
  `--setting-sources project` (deliberately not `--bare`, which forces
  API-key-only auth and breaks OAuth-logged-in machines) and
  `--strict-mcp-config` (use only the server we pass in).
- **Streaming or line-oriented output for usage accounting** — see section 5.4.

### 4.3 `build_env` / `env_to_drop` — the environment

`runner_profiles.py:594` (`build_env`) and `:583`/`:588`
(`_LOGIN_OVERRIDES`/`env_to_drop`). Only the variables the target CLI actually
reads:

```python
# claude-cli:
#   ANTHROPIC_API_KEY  (from the row's api_key_env, and also mirrored into
#                       ANTHROPIC_API_KEY directly)
#   ANTHROPIC_BASE_URL (from the row's base_url)
# opencode:
#   <api_key_env>, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
#   + OPENCODE_DISABLE_DEFAULT_PLUGINS / OPENCODE_DISABLE_EXTERNAL_SKILLS /
#     OPENCODE_DISABLE_CLAUDE_CODE_SKILLS  (trim the user's global tool surface)
```

`env_to_drop` (`:588`) removes inherited variables that would *override* a
CLI's own sign-in — Claude Code prefers an `ANTHROPIC_API_KEY` in its
environment over the OAuth login in the keychain, so a daemon started from a
shell that exports one would bill that key instead of the runner's own login
(`_LOGIN_OVERRIDES`, `:583`). The drop is applied only when the runner's
`auth_mode` is `"login"` (`core/runners.py::auth_mode`).

For a new runner: name its real credential variables (not another CLI's), and
if it reads a login from its own store, make sure no inherited variable can
silently override that login.

## 5. Wire it up outside the profiles

These modules have per-runner knowledge that lives *outside*
`runner_profiles.py`. They are the rest of the "new entry" work.

### 5.1 `core/runner_detect.py` — autodetect

`runner_detect.py:14`:

```python
KNOWN = {
    "opencode": "opencode",
    "claude": "claude-cli",
}
```

Maps binary name → runner type. `detect()` (`:76`) lists every known runner
found on this machine (used by the "Add runner" autodetect panel), and the web
UI derives the default binary name for a type from this map (`web/routes.py:674`).
Add `"your-cli": "your-cli"` so an installed binary is offered. `resolve()`
(`:34`) searches PATH plus the common install dirs, so a new CLI installed in a
non-PATH dir may also need an entry in `_EXTRA_DIRS` (`:20`).

### 5.2 `web/routes.py` — UI labels and form hints

- `RUNNER_LABELS` (`web/routes.py:666`): the human label shown in the UI —
  `{"opencode": "opencode", "claude-cli": "Claude Code"}`. Add a label, else the
  type string itself is shown.
- `_runner_kinds()` (`:669`): iterates `RUNNER_TYPES` and pairs each with the
  binary/path/version from `runner_detect` — works for a new type once
  registered.
- `runners_form_body` (`:731`): per-type key hints and model placeholders are
  branchy (`if rtype == "claude-cli": … else: …`). Add a branch so the new
  runner's form explains *its* auth and model convention instead of opencode's.

### 5.3 `core/runner_catalog.py` — models and sign-in

The runner form lists the models a runner accepts and whether it is signed in,
asked from the CLI itself rather than a list kept here. All three functions
currently branch `claude-cli` vs everything-else:

- `models(rtype, binary)` (`runner_catalog.py:146`) — model picker.
- `login_status(rtype, binary)` (`:191`) — `claude auth status` vs opencode's
  `auth.json`.
- `provider_env(rtype, model_id)` (`:99`) — which env var a key for a model
  must arrive in; claude-cli returns `ANTHROPIC_API_KEY` unconditionally.

A new runner needs each branch decided: how do you list its models, how do you
check its sign-in, and which variable does a pasted key go into? Everything
degrades to "nothing known" rather than raising, so a form that cannot list
models still lets the operator type one.

### 5.4 `core/usage.py` — token/cost accounting

`parse_usage(stdout)` (`usage.py:117`) reads whichever runner produced the
output. The two existing parsers are:

- Claude Code: find the terminal `{"type": "result"}` event in the
  `stream-json` stream (`_parse_claude_usage`, `:69`) — one cumulative
  `usage` block plus `total_cost_usd` and a `modelUsage` map (the model that
  served the run is the one that cost most).
- opencode: sum `step-finish` NDJSON events carrying `tokens`/`cost`
  (`:126`).

If the new CLI's output format is neither, add a parser branch. If it is
streamed per-step events, prefer that so the idle timer stays honest (section
4.2) and totals sum correctly. `launcher.py:412` calls this for every run, so a
new runner without a parser just records zeros — silently.

### 5.5 `core/runner_home.py` — the worker's private HOME

Every worker runs with a `$HOME` of its own (`launcher.py:294`), assembled by
**allowlist** — nothing is present unless named in `LINKED`
(`runner_home.py:31`):

```python
LINKED = [
    (".local/share/opencode", "opencode credentials and session store"),
    (".claude", "Claude Code login and settings"),
    (".claude.json", "Claude Code account state"),
    (".gitconfig", "git identity for commands the agent runs itself"),
    (".ssh", "git over ssh"),
]
```

A new runner whose credentials/config live under `$HOME` **must** add its paths
here, or it cannot authenticate inside a worker. `_sweep_strays()` (`:87`) then
deletes anything else a run leaves behind, so a repo's config can never become
every repo's config; the keep-list is derived from `LINKED`, so the two stay in
step.

### 5.6 The smoke test — mostly free

`core/runner_smoke.py` reuses the profiles deliberately ("a probe that takes a
different path than production proves nothing about production"). Once sections
3–4 are done, `smoke_test()` (`runner_smoke.py:53`) should work for the new
type with no change — it calls `normalize_type`, `tool_prefix`,
`write_mcp_config` and `build_argv` exactly the way the launcher does. The
thing to check is the *tool name*: the probe builds `tool = f"{prefix}report"`
(`:83`) and requires the model to call it, so a wrong `tool_prefix` (section
3.5) fails here first, which is the point.

### 5.7 Stale references

`mcp_server.py:318` still documents the runner types as "opencode, claude-cli,
ollama, openai-api, custom" — the legacy menu entries that were removed. Update
this string when you add a type so the MCP API description matches reality.

## 6. Tests to add and update

The suite is pytest (`sisyphus/pytest.ini`). The canonical file is
`tests/test_runner_profiles.py`; the "every supported type" tests there pick up
a new type automatically once registered:

- `TestSuggestedConfig::test_no_runner_is_prefilled_with_a_provider_key` —
  asserts blank `api_key_env`/`base_url` and a hint, per type.
- `TestCredentialStore::test_every_supported_type_documents_its_login`.
- `TestWorkspaceArtifacts::test_every_supported_type_declares_its_artifacts`
  and the union/labels tests.

Add explicit tests for the new type, mirroring the existing classes:

- `TestArgv` — assert the exact argv shape (`test_claude_is_non_interactive_json`
  is the template), that it streams for the idle timer, that it carries no
  permission prompt, that MCP flags are present/strict when a config is given
  and absent otherwise, and — the cross-contamination test —
  `test_claude_never_gets_opencode_flags`: none of the other CLI's flags leak
  in.
- `TestEnv` — `build_env` exports only the new CLI's variables, never
  `OPENROUTER_*`/`ANTHROPIC_*`; `env_to_drop` strips login overrides.
- `TestWriteMcpConfig` — the config file's shape and *where it is written*
  (worktree vs output dir), and that no other runner's config file appears.
- `TestWorkspaceArtifacts` — the new CLI's own state dirs are listed, excluded
  from commits (`tests/test_runner_detect.py::test_commit_excludes_cover_every_runner`
  asserts every artifact has a `:(exclude)` rule in `git_ops._COMMIT_EXCLUDES`),
  and recognised by `repo_context` (`test_repo_context_recognises_every_runner_config`).
- `tests/test_runner_catalog.py`, `tests/test_usage.py` (output parsing), and
  `tests/test_web_routes.py` (the `RUNNER_TYPES`-driven form-body) — extend for
  the new type where the module branches.
- `tests/tiers.py` lists the runner test modules per tier; add the new type's
  tests to the appropriate tier.

Run the suite before opening an MR:

```sh
# from the sisyphus/ directory
python -m pytest -q
```

## 7. Config and docs touchpoints

- Runners are **database rows**, not YAML. `core/runners.py::init_runners_table`
  creates the `runners` table and seeds one default opencode row; operators add
  and edit runners through the web UI. There is no per-runner key in
  `config.example.yaml`.
- `config.example.yaml` `worker_env` (`:66`) applies environment extras to
  *every* runner's worker — a new runner inherits it for free.
- Health-check semantics come from the profile data in section 3 (`credential_store`,
  `VERIFIED_VERSIONS`), not from a config file.

## 8. Checklist

1. Add the type string to `SUPPORTED_TYPES` in `core/runner_profiles.py`.
2. Add the `_SUGGESTED` entry (UI prefill), `_CREDENTIAL_STORE` entry (health
   note), `VERIFIED_VERSIONS` entry (flag-version note), and `_WORKSPACE_ARTIFACTS`
   entry (commit/salvage/context exclusions).
3. Decide `tool_prefix` — an explicit branch if the new CLI's tool-naming
   convention differs from opencode's `<server>_<tool>`.
4. Implement `write_mcp_config` (config shape + where it lives) and a
   `build_argv` branch satisfying: non-interactive, streaming output,
   no permission prompts, isolation from the user's global config.
5. Implement `build_env` and, if the CLI reads a login it could be overridden
   out of, an `env_to_drop`/`_LOGIN_OVERRIDES` entry.
6. Add the binary→type map entry in `core/runner_detect.py::KNOWN` (and
   `_EXTRA_DIRS` if installed outside PATH).
7. Add the UI label in `web/routes.py::RUNNER_LABELS` and the per-type form
   hints in `runners_form_body`.
8. Handle model listing / sign-in / key variable in `core/runner_catalog.py`.
9. Handle output parsing in `core/usage.py::parse_usage` if the format differs.
10. Link the CLI's `$HOME` paths into `core/runner_home.py::LINKED`.
11. Update the stale `mcp_server.py:318` type list.
12. Extend `tests/test_runner_profiles.py` (argv/env/config/artifacts) and the
    catalog/usage/web tests; run `python -m pytest -q`.
13. Run a real end-to-end smoke test against the new CLI and record its version
    in `VERIFIED_VERSIONS`.