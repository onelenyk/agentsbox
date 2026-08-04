# MR/PR Scan Report — All Repos

**Date:** 2026-08-04
**Scope:** All repos under `/Users/lenyk/workproject/`

---

## 1. OPEN PULL REQUEST (GitHub — actionable)

| Repo | Remote | PR | Title | Opened | Age | Status |
|------|--------|-----|-------|--------|-----|--------|
| **testrepo-bug** | `github.com/onelenyk/testrepo-bug` | [#1](https://github.com/onelenyk/testrepo-bug/pull/1) | fix: factorial off-by-one in range (n excluded from product) | 2026-07-31 | **4 days** | **OPEN** — no reviews, no labels, 1 file changed (mathx.py) |

**Action needed:** Review and merge or close PR #1.

---

## 2. STUCK SISYPHUS TASK BRANCHES (testrepo — no remote)

`/Users/lenyk/workproject/agentsbox/testrepo` has no remote origin. Sisyphus branches left behind:

| Branch | Last Commit | Description | Merged? |
|--------|------------|-------------|----------|
| `sisyphus/task-1d02d2f0` | f52e652 (2026-07-31) | Add multiply and divide functions | **NO** — stuck |
| `sisyphus/task-7cbe7a7a` | bdb5d69 (2026-07-31) | feat: variadic add(*nums) | **NO** — stuck |
| `sisyphus/task-583130e0` | eff6b51 (2026-07-31) | feat: add subtract(a, b) function | YES (merged to main) — stale branch |
| `sisyphus/task-bdbe3ec7` | 1839939 (2026-07-31) | Add clamp(x, lo, hi) function | YES (merged to develop) — stale branch |
| `sisyphus/task-7cbe7a7a_engineer` | d9ea7c6 | same as HEAD (main) | redundant |

**Also unmerged:**
- `develop` — 1 commit ahead of `main` (clamp function via MR mr-c9fe0180)
- `feature` — tweak add
- `feature-power-function` — add power(base, exp) function

**Action needed:** Clean up stale branches. Either merge the 2 stuck task branches or archive them.

---

## 3. STAGING AHEAD OF MASTER (pipelining/test-repo)

`/Users/lenyk/workproject/pipelining/test-repo` (local remote only):
- `staging` is **7 commits ahead** of `master` (CI bumps, merge commits)
- No remote GitLab/GitHub to check for open MRs

**Action needed:** Merge staging → master or determine if these are pending releases.

---

## 4. GitLab REPOS (private — cannot query MRs remotely)

These repos have remotes on private GitLab instances. No API token available — remote MR status **unknown**.

### 4a. rumors-mobile (`gitlab.rumors.app`)

| Branch | Last Commit | Date | Ahead of develop? |
|--------|------------|------|-------------------|
| `develop` | 5e4e44f9 Bump version | 2026-04-06 | YES (5 commits ahead of remote develop) |
| `feature/RUM-2601-filters-vibe-lifestyle` | 693705bf | 2026-03-29 | YES |
| `feature/RUM-2729-transactions-screen` | 1df8af70 | 2026-03-29 | YES |
| `feature/RUM-2733-feed-enhancements` | 0754c365 | 2026-03-21 | YES |
| `feature/RUM-2748-referral-program-ui` | 8366ea5d | 2026-03-22 | YES |
| `feature/dream-details-proof` | e970e7d4 | 2026-02-27 | YES |
| `feature/dream-donations-aggregation` | b8d11023 | 2026-03-04 | YES |
| `feature/gifts-and-wishlists` | 6485438b | 2026-04-02 | YES |
| `stage` | c7f50958 | 2026-07-22 | N/A (release branch) |

**8 of 8 unmerged branches** — some inactive since February 2026.

### 4b. kmp-driver (`sln-gtl.vanongo.com`)

| Branch | Last Commit | Date |
|--------|------------|------|
| `develop` | 844aa30 | 2026-05-12 |
| `chore/kotlin-2.2-upgrade` | a71cfa3 | 2026-05-20 |
| `chore/migrate-bundle-ids` | 4a80a89 | 2026-05-05 |
| `ci/test-pipeline` | 14eae42 | 2026-03-16 |
| `docs/agent-onboarding` | 1f87aac | 2026-05-06 |
| `feature/architecture-refactor` | 1b8e646 | 2026-04-14 |
| `feature/auth-fcm-persist` | 9e43510 | 2026-04-29 |
| `feature/firebase-kmp-integration` | 7306328 | 2026-03-04 |
| `feature/kmp-auth-manager` | 97ced75 | 2026-03-20 |
| `feature/notifications` | 6893f57 | 2026-03-12 |
| `feature/phase4-main-flow-ui` | 572a81c | 2026-04-28 |
| `feature/waybill-details` | 39583e1 | **2026-08-03 (yesterday!)** |
| `feature/waybill-details-rewrite` | e83c27a | 2026-07-08 |
| `feature/waybill-lists` | 06b7fce | 2026-05-04 |

**14 branches unmerged** to master. Recent activity on `feature/waybill-details`.

### 4c. ios-driver (`sln-gtl.vanongo.com`)

| Branch | Last Commit | Date |
|--------|------------|------|
| `develop` | 7af14fb8 | 2026-05-11 |
| `feat/local-cargo-in-van` | 0986f9bc | 2026-05-14 |
| `feature/smartpost-brand` | 1ef06793 | 2026-07-02 |
| `fix/SLN-25143-remove-shipment-autoselection` | 1b540d4a | 2026-05-11 |
| `fix/disable-ios26-liquid-glass` | 9cd4af3b | 2026-05-21 |
| `fix/local-cargo-in-van-intersection` | e9adf1b7 | 2026-05-15 |
| `fix/sln-25211-25212-nps-active-toggle` | ed61b536 | 2026-05-19 |
| `staging` | fc967294 | 2026-07-18 |

**8 branches unmerged** to develop. Most inactive since May 2026.

### 4d. android-driver (`sln-gtl.vanongo.com`)

| Branch | Last Commit | Date |
|--------|------------|------|
| `staging` | df91b4aac | 2026-07-31 |
| `develop` | a317c0d6b | 2026-03-03 |
| `dent-neon` | a317c0d6b (same as develop) | 2026-03-03 |
| `feature/debug-tools-and-error-logging` | 2fb6d503e | 2026-03-18 |
| `feature/maestro-e2e-testing` | 5a179eff6 | 2026-02-24 |
| `feature/nearby-stops-modal-component` | 8571c5a2e | 2026-03-23 |
| `fix/firebase-resources-wrapper-npe` | 2f55d5994 | 2026-03-19 |
| `fix/telemetry-foreground-service-crash` | edcdc1cee | 2026-03-19 |
| `poc/stop-gps-locating` | 45ef7d06f | 2026-03-02 |
| `refactor/grouped-stops-state-cleanup` | b882adf7f | 2026-03-23 |
| `worktree-agent-a277715f/a70859c7/af745c64` | a317c0d6b (same as develop) | 2026-03-03 |

**12+ unique branches unmerged** to develop. `staging` is the most recently active (2026-07-31). The 3 `worktree-agent-*` branches are identical to develop (stale worktrees).

---

## 5. CLEAN REPOS (no issues)

| Repo | Status |
|------|--------|
| `agentsbox` (root) | Clean — only `main`, no remote |
| `testrepo2` | Clean — only `main`, no remote |
| `pipelining` (root) | Clean — only `main`, no remote |
| `sisyphus/workspace/test-repo` | Clean — only `Staging` and `master`, all merged |

---

## SUMMARY

- **1 open PR** on GitHub (testrepo-bug) — needs review
- **2 stuck Sisyphus task branches** (testrepo) plus stale branches
- **1 staging → master gap** (pipelining/test-repo)
- **4 private GitLab repos** with 30+ unmerged branches total — remote MR status unknown (no API token)
- **Total repos scanned:** 11
- **Repos with remote-traceable MRs:** 1 (testrepo-bug via GitHub)
- **Repos blocked by missing auth:** 4 (rumors-mobile, kmp-driver, ios-driver, android-driver)
