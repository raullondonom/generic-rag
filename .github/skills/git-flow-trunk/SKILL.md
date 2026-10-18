---
name: git-flow-trunk
description: "Use for any git branching, committing, merging or code-change workflow in this repo: trunk-based flow with ONE stable branch (main) and every change developed first in a feature branch, merged locally (no pull requests for now). Triggers: new feature, start work, create branch, commit, merge to main, finish feature, hotfix, bugfix, sync with main, clean branches, git flow, branching strategy."
argument-hint: "start <name> | commit | sync | finish | status | cleanup"
---

# Git flow: stable `main` + feature branches (no PRs)

## Rules (non-negotiable)

1. **`main` is the only long-lived branch and is always stable**: it builds and its tests pass. No `develop`, no `release/*` branches.
2. **Never commit, edit or experiment on `main`.** Every change (feature, fix, refactor, docs, chore, even one line) starts in a branch created from an up-to-date `main`.
3. Only two operations touch `main`: **fast-forward merge of a finished feature branch** and **the release commit + tag** from the `semantic-versioning` skill.
4. History stays **linear**: rebase the feature on `main`, then merge with `--ff-only`. No merge commits, so the changelog analysis sees only Conventional Commits.
5. Never force-push `main`. Never rewrite commits already merged into `main` or tagged.
6. No pull requests for now: the merge is local. Because nobody reviews, the checks in "finish" (tests + self-review of the diff) are mandatory.
7. Branches are short-lived: merge soon, delete after merging.

## Branch names

`<type>/<kebab-case-summary>`, with the same types as Conventional Commits:

| Prefix | Use | Example |
|---|---|---|
| `feature/` | new functionality | `feature/hybrid-search` |
| `fix/` | bug fix (also urgent fixes: they branch from `main` too) | `fix/citation-page` |
| `refactor/` `perf/` | internal improvements | `refactor/split-chat` |
| `docs/` `chore/` `ci/` `test/` | non-functional | `chore/graphify-setup` |

Commits inside the branch follow Conventional Commits (see the `semantic-versioning` skill): they are what decides the next version.

## Procedure

Helper: `bash .github/skills/git-flow-trunk/scripts/gitflow.sh <command>`. Each command prints the git commands it runs.

### start `<type>/<name>`
1. Working tree must be clean (otherwise commit or stash on the current branch first).
2. Script: switch to `main`, `git pull --ff-only` if `origin` exists, create and switch to the branch.

### work
- Small, atomic commits, each leaving the project working. Message: `type(scope): imperative summary`; mark breaking changes with `!` and a `BREAKING CHANGE:` footer.
- Check `git branch --show-current` before every commit; if it says `main`, stop: create the branch first (`git switch -c <type>/<name>` carries uncommitted changes).

### sync (long-lived work, or before finishing)
Script: `git rebase main` on the feature branch. Resolve conflicts, `git rebase --continue`. The rebase is allowed because the branch is private.

### finish
Preconditions the agent must verify and report, in order:
1. Tree clean; branch is not `main`.
2. `sync` done (the branch contains the tip of `main`).
3. Tests pass: `docker compose run --rm --no-deps rag-app pytest -q` (or the relevant subset if Docker is unavailable; say so).
4. Self-review of `git diff main...HEAD` and `git log main..HEAD`: no secrets, no debug leftovers, commit messages conventional. Optionally refresh the code graph: `graphify update .`.
5. **Ask the user to confirm the merge into `main`.**

Then the script: `git switch main && git merge --ff-only <branch> && git branch -d <branch>`. Report the new `main` HEAD. Pushing (`git push origin main`) only when the user asks.

### status
Script prints current branch, whether it is behind `main`, commits ahead, and local branches already merged.

### cleanup
Script deletes local branches already merged into `main` (`git branch --merged main`), never `main` itself. Remote branches are only deleted if the user asks.

### Releases
After one or more features are merged, use the `semantic-versioning` skill from `main`: its `release` command is the one allowed direct commit on `main` (`chore(release): vX.Y.Z`) plus the annotated tag `vX.Y.Z`.

### Urgent fix of a released version
There are no maintenance branches: `fix/<name>` from `main`, merge, release a PATCH. If `main` holds unreleased work that is not ready, revert or finish it first; `main` must always be releasable.

## Optional: enforce the rules with a git hook
[commit-msg hook](./assets/commit-msg.sh) rejects commits on `main` (except `chore(release):`) and non-Conventional messages. Install it only if the user asks: `cp .github/skills/git-flow-trunk/assets/commit-msg.sh .git/hooks/commit-msg && chmod +x .git/hooks/commit-msg`. Hooks are local and not versioned; every clone must install it.

## Adopting pull requests later
When PRs are introduced, keep rules 1, 2, 4, and replace the local `finish` merge with `git push -u origin <branch>` and a PR merged by "Rebase and merge" (or squash with a Conventional Commit title).
