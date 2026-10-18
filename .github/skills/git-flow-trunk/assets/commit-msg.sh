#!/usr/bin/env bash
# Rejects commits made directly on main (except releases) and non-Conventional Commit messages.
msg_file="$1"
subject="$(head -n1 "$msg_file")"
branch="$(git branch --show-current)"

case "$subject" in Merge\ *|Revert\ \"*|fixup!*|squash!*) exit 0 ;; esac

if [ "$branch" = "main" ] && [[ "$subject" != chore\(release\):* ]]; then
  echo "Blocked: do not commit on main. Create a branch: bash .github/skills/git-flow-trunk/scripts/gitflow.sh start feature/<name>" >&2
  exit 1
fi

re='^(feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(\([a-z0-9._-]+\))?!?: .{1,72}$'
if ! [[ "$subject" =~ $re ]]; then
  echo "Blocked: use Conventional Commits, e.g. 'feat(retrieval): add hybrid search'" >&2
  exit 1
fi
