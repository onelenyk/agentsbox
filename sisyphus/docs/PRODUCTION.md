# Production readiness

A production repository demands more than being cloned and discoverable. Its agent
work must not see the home directory, it must ship through a forge (GitHub, GitLab)
with CI and branch protection, the daemon must isolate network access, and every MR
must pass a check before merging. This document walks the rollout steps and how to
verify them.

## What changes on a production repo

### Isolation
Workers run with read access to the home directory and unrestricted network by
default. On a production machine, both are off-limits:

**Read isolation** (`sandbox.read_isolation: true`): workers cannot read files
outside an allowlist (the repo, workspace, and a whitelist you define). This
prevents a misguided agent from pulling production secrets sitting next to the
codebase or from reading your SSH keys.

**Egress control** (`sandbox.egress: true`): worker network is routed through an
allowlisted proxy. The default is open — the worker can dial any host. This
gate lets you:

- Audit which hosts tasks are contacting (and catch a worker trying to exfil data).
- Whitelist internal forge servers without exposing them to the internet.
- Block free model APIs, public APIs, or outbound SSH.

Both are required to pass the production readiness check. They are daemon-wide
config, not per-repo.

### Delivery
A production repo must ship through a forge — GitHub or GitLab — not a local
path. The forge provides:

- CI gates: jobs that run the check and block merges when it fails.
- Branch protection: the main branch cannot be force-pushed and requires reviews.
- Audit trail: every change is traced to a specific person and commit.

Set `platform: github` or `platform: gitlab` in the repo config. The daemon will
infer the platform from the clone URL if it is not explicit.

**Auto-merge must be off** (`auto_merge: false`). On a forge, MRs that clear the
gate do NOT merge themselves — a human must approve and land them. This is
non-negotiable in production: the forge's CI is a gate, not a verdict. Approval
is a second gate, and only the two together authorize a merge.

### Check gate
A repo must have a `check:` command — a script the daemon runs against every
proposed change. It must exist and it must exit non-zero if the change is bad:

```yaml
repos:
  - name: prod-api
    check: python -m pytest -q --tb=short
```

The gate is mandatory. Every MR opened by an agent must pass it before merging.

### Credentials
For a GitHub or GitLab repo, the daemon needs a token to open and manage MRs:

```yaml
github:
  token: ${GITHUB_TOKEN}
```

The token can also be per-repo (higher precedence):

```yaml
repos:
  - name: prod-api
    token: ${PROD_API_TOKEN}
```

The readiness check verifies a token is present. It never reads the token itself.

### Pilot mode
Start with `mode: read-only`. The agent can read files, run checks, and propose
changes, but cannot write to the repo (no pushes, no merges). This is a safe
first week: you see what the agent suggests without letting it land anything.

Once you trust the system, set `mode: write` (the default) to allow MRs to be opened.

## Rollout steps

### Step 1: Enable isolation
This is daemon-wide and affects all repos. Do this first:

```yaml
sandbox:
  read_isolation: true
  egress: true
```

Restart the daemon. Every worker now runs confined.

### Step 2: Pilot a repo in read-only mode
Add your production repo and set it to read-only:

```yaml
repos:
  - name: my-app
    source: https://github.com/my-org/my-app
    platform: github
    branch: main
    check: python -m pytest -q
    mode: read-only
```

Point the repo's token to your GitHub personal access token or a repo-specific
one you created in GitHub. The daemon will not push anything.

Run `python3 sisyphus.py repos readiness my-app` to verify. Fix any block items
before proceeding.

### Step 3: Live in the pilot
Create a few tasks and watch them run. The agent will propose changes on read-only
branches — they won't land anywhere, but you can review them on the board and
see what the system produces under real load.

After a week or when confident, proceed.

### Step 4: Graduate to prod (writes on)
Change `mode: write` (or remove it — write is the default):

```yaml
repos:
  - name: my-app
    ...
    mode: write
```

The daemon will now open MRs on the real forge. Every MR is still read-only to
the main branch: it lives in a feature branch and the forge's CI and review
process gate it.

### Step 5: Automate branch protection
In the forge UI (GitHub / GitLab settings for the repo), configure:

- **Require CI to pass**: the branch protection rule enforces your check.
- **Require at least one approval**: MRs cannot merge without review.
- **Dismiss stale reviews**: re-running the check clears approvals, forcing fresh
  eyes.

The daemon respects these rules: it will not try to merge a commit that the forge
itself blocks.

## Assessing readiness

Check the status at any time with the readiness panel on the repo detail page, or
from the terminal:

```sh
python3 sisyphus.py repos readiness my-app
```

The output lists each check with a tick or cross and a fix if needed:

```
✓ check                 [BLOCK]  A check command runs against every MR to gate merges.
✓ delivery             [WARN]   Delivery is through a forge (github/gitlab).
✓ auto_merge           [BLOCK]  Auto-merge is off; MRs require human review.
✓ read_isolation       [BLOCK]  Read isolation is enabled; workers cannot read home outside an allowlist.
✓ egress               [BLOCK]  Egress is controlled; worker network limited to an allowlisted proxy.
✓ forge_token          [BLOCK]  A github token is configured (global).
✗ pilot_mode           [WARN]   Repo mode is 'write'; start with mode: read-only for a safe pilot.
  → Set mode: read-only in the repo config.
```

Exit code is 0 when ready, 1 when any block items fail.

## What each check means

- **check**: A repo must have a check command that validates every MR. Without it,
  the daemon has no gate and merges whatever an agent wrote.

- **delivery**: A production repo should use a forge so CI and branch protection
  apply. Local delivery (a path on disk) bypasses the forge entirely. This is a
  warning, not a block, because local repos are useful for development and testing.

- **auto_merge**: Auto-merge must be off for a forge repo. When on, MRs merge
  themselves without review. Production demands a human sign-off on every merge.

- **read_isolation**: Workers must not read the home directory outside an
  allowlist. Without it, an agent can steal credentials or secrets sitting next
  to the repo.

- **egress**: Worker network must be limited to an allowlisted proxy. Without it,
  an agent has unrestricted internet access and could exfiltrate data.

- **forge_token**: For GitHub and GitLab repos, the daemon needs a token to open
  and manage MRs. Without it, every MR attempt fails.

- **pilot_mode**: A repo should start in read-only mode before writes are allowed.
  This is a warning to think about it first; changing from write to read-only is
  a conscious choice, not a default.

---

See also: [`GOAL.md`](GOAL.md) for the roadmap and [`testing.md`](testing.md) for
how to verify the system end-to-end.
