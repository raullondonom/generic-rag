#!/usr/bin/env bash
# Trunk-based flow helper: main is stable, all work happens in branches, merges are local fast-forwards.
set -euo pipefail

TRUNK=main
NAME_RE='^(feature|fix|refactor|perf|docs|chore|ci|test|build)/[a-z0-9][a-z0-9._-]*$'

run() { echo "+ $*"; "$@"; }
die() { echo "error: $*" >&2; exit 1; }
current() { git branch --show-current; }
has_origin() { git remote get-url origin >/dev/null 2>&1; }
# Rule: main must match origin/main before any branch is created, rebased or merged.
refresh_trunk() {
  has_origin || return 0
  if [ "${SKIP_FETCH:-}" = "1" ]; then
    echo "warning: SKIP_FETCH=1, using the last known origin/$TRUNK (run 'git fetch' yourself first)"
  else
    run git fetch origin "$TRUNK" || die "cannot fetch origin (credentials/network). Run 'git fetch origin' yourself, then retry with SKIP_FETCH=1"
  fi
  git merge-base --is-ancestor "origin/$TRUNK" "$TRUNK" && return 0
  if git merge-base --is-ancestor "$TRUNK" "origin/$TRUNK"; then
    [ "$(current)" = "$TRUNK" ] && run git merge --ff-only "origin/$TRUNK" || run git update-ref "refs/heads/$TRUNK" "origin/$TRUNK"
  else
    die "local $TRUNK and origin/$TRUNK diverged (local commits made on $TRUNK): rebase them with 'git rebase origin/$TRUNK' on $TRUNK, or move them to a branch"
  fi
}
clean_tree() { [ -z "$(git status --porcelain)" ] || die "working tree not clean: commit or stash first"; }

cmd_start() {
  local branch="${1:-}"
  [[ "$branch" =~ $NAME_RE ]] || die "branch name must match <type>/<kebab-name>, e.g. feature/hybrid-search"
  git show-ref --verify --quiet "refs/heads/$branch" && die "branch $branch already exists"
  clean_tree
  run git switch "$TRUNK"
  refresh_trunk
  run git switch -c "$branch"
}

cmd_sync() {
  local branch; branch="$(current)"
  [ "$branch" != "$TRUNK" ] || die "sync runs on a feature branch, not on $TRUNK"
  clean_tree
  refresh_trunk
  run git rebase "$TRUNK"
}

cmd_finish() {
  local branch; branch="$(current)"
  [ "$branch" != "$TRUNK" ] || die "finish runs on a feature branch, not on $TRUNK"
  clean_tree
  refresh_trunk
  git merge-base --is-ancestor "$TRUNK" "$branch" || die "$branch is behind $TRUNK: run 'sync' first"
  [ -n "$(git log "$TRUNK..$branch" --oneline)" ] || die "$branch has no commits ahead of $TRUNK"
  echo "Commits to merge:"; git log "$TRUNK..$branch" --format='  %h %s'
  [ "${1:-}" = "--yes" ] || die "tests and review must pass and the user must confirm; re-run with --yes"
  run git switch "$TRUNK"
  run git merge --ff-only "$branch"
  run git branch -d "$branch"
  echo "main is now at $(git rev-parse --short HEAD). Push only when asked: git push origin $TRUNK"
}

cmd_status() {
  local branch; branch="$(current)"
  if has_origin && git fetch -q origin "$TRUNK" 2>/dev/null; then
    echo "$TRUNK vs origin/$TRUNK: ahead $(git rev-list --count "origin/$TRUNK..$TRUNK"), behind $(git rev-list --count "$TRUNK..origin/$TRUNK")"
  fi
  echo "Branch: $branch"
  if [ "$branch" = "$TRUNK" ]; then
    echo "WARNING: on $TRUNK. Do not commit here; run 'start <type>/<name>'."
  else
    echo "Ahead of $TRUNK:  $(git rev-list --count "$TRUNK..$branch")"
    echo "Behind $TRUNK:    $(git rev-list --count "$branch..$TRUNK")"
  fi
  echo "Uncommitted files: $(git status --porcelain | wc -l)"
  echo "Merged branches (cleanup candidates):"
  git branch --merged "$TRUNK" --format='  %(refname:short)' | grep -v "^  $TRUNK$" || echo "  none"
}

cmd_cleanup() {
  local merged
  merged="$(git branch --merged "$TRUNK" --format='%(refname:short)' | grep -v "^$TRUNK$" | grep -v "^$(current)$" || true)"
  [ -n "$merged" ] || { echo "nothing to clean"; return; }
  while read -r b; do run git branch -d "$b"; done <<< "$merged"
}

case "${1:-}" in
  start)   shift; cmd_start "$@" ;;
  sync)    cmd_sync ;;
  finish)  shift; cmd_finish "$@" ;;
  status)  cmd_status ;;
  cleanup) cmd_cleanup ;;
  *) echo "usage: gitflow.sh start <type>/<name> | sync | finish [--yes] | status | cleanup"; exit 2 ;;
esac
