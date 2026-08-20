> **Archived 2026-08-20** — implemented; local MRs are a first-class entity with review, verified checks and merge actions.

# Plan — Local Merge Requests (a mini in-app forge)

## Motivation

A local repo (no GitHub/GitLab) currently gets only a read-only diff patch in the
thread. Make it a **real merge request**: a first-class entity with a lifecycle
(open → merged/closed), a review view, and actions that operate on the user's
**actual** local repo — including **choosing which branch to merge into** (not a
hardcoded `main`).

The engineer's work already lives on a real branch `sisyphus/<task_id>` in the
per-task bare clone (`workspace/repos/<task_id>_<repo>`). We turn that into a
mergeable, reviewable MR.

## Entity

A new `merge_requests` table (mirrors runners/agents style):

```
merge_requests {
  id, task_id, repo,
  source_branch,           -- sisyphus/<task_id>
  target_branch,           -- chosen by the user; defaults to the repo's default
  title, description,
  status,                  -- open | merged | closed
  bare_path,               -- where the source branch + diff live
  base_branch,             -- the branch the diff was computed against
  merge_sha, merged_at,
  created_at, updated_at
}
```

`open_mr` on a **local** platform, in addition to saving the diff patch, creates
one `merge_requests` row (status=open). Forge MRs are unchanged (they get a URL).

## Target-branch selection

The MR view offers a **dropdown of the real repo's branches**
(`git -C <real_repo> branch --format='%(refname:short)'`), defaulting to the
repo's configured default branch. Mergeability and merge both run against the
selected target. Changing the selection re-runs the mergeability check.

## Mergeability check (before any write)

A dry run, entirely inside a throwaway worktree of the bare clone — never
touches the user's repo:

```
git worktree add --detach <tmp> <target>
git -C <tmp> merge --no-commit --no-ff <source>   # then --abort
```

→ report **🟢 merges cleanly** or **🔴 N conflicting files**. The **Merge** action
is only enabled when green (and, optionally, when a reviewer has APPROVED).

## Actions on a local MR

1. **Land branch** *(non-destructive default)* — fetch the source branch into the
   user's real repo so they can review/merge with their own tools:
   `git -C <real_repo> fetch <bare_path> <source>:<source>`. Adds a ref only —
   never touches the working tree.
2. **Merge** *(the real thing, guarded)* — merge `source` into the **chosen**
   target in the real repo, producing a merge commit; record `merge_sha`,
   set status=merged, post to the thread. Safe mechanics (see below).
3. **Close** — status=closed, no write.
4. **View diff** — the colourised dialog (already built).
5. **Comment** — reuse the task thread.

### Safe merge mechanics

Never disturb the user's working tree, work for any target (even one not checked
out), always reversible:

- Fetch `source` into the real repo.
- If the target is **not** checked out anywhere: `git worktree add <tmp> <target>`
  in the real repo, `git -C <tmp> merge --no-ff <source>` (advances the real
  branch ref), remove the worktree. The user's main working tree is untouched.
- If the target **is** the current branch and the tree is **clean**: merge in
  place.
- If it's the current branch and **dirty**: refuse with a clear message
  (commit/stash first) — never merge over uncommitted work.
- Never force, never delete a branch, never touch remotes. A merge is reversible
  with `git reset --hard @{1}`; we surface that in the success message.

## UI

- **On the task detail Result card:** an "Open merge request ↗" that opens the MR
  view (or, once merged, shows the merged state).
- **MR view** (modal or page): title, `source → [target ▼]`, mergeability badge,
  colourised diff, and the action buttons (Land / Merge / Close), each with an
  `hx-confirm`. Merge shows exactly what will happen ("merge sisyphus/… into
  <target> in /Users/…/repo").
- **MR list** (`/merge-requests`): all open MRs across tasks — a forge-style
  inbox. Sidebar entry under Operate.

## Safety

Merging writes to the user's real repo — so every write action is **opt-in +
confirmed**, gated on a clean mergeability check, never touches the working tree
by default, never forces or deletes, and is reversible. The daemon shows the
exact repo path and branches before acting.

## Slices (each shippable + tested)

1. **Entity + creation hook** — table, CRUD, create an MR on local `open_mr`.
2. **MR view + diff + branch list + mergeability** (read-only; no writes yet).
3. **Land branch** action (non-destructive write).
4. **Merge** action (guarded, safe worktree mechanics) + **Close**.
5. **MR list page** + sidebar + Result-card entry point.

## Verification

Unit tests for the entity, branch listing, and mergeability (real git in a temp
repo). Live: run a task on the local `sisyphus` repo → open its MR → pick a
target branch → see the green mergeability + diff → Land, then Merge into a
throwaway branch → confirm the merge commit lands and the working tree is
untouched.
