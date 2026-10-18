---
name: semantic-versioning
description: "Use when releasing, versioning or tagging: decide the next MAJOR.MINOR.PATCH version (SemVer 2.0.0), analyze changes since the last tag, write the CHANGELOG.md (Keep a Changelog), bump the VERSION file and create the annotated git tag vX.Y.Z. Triggers: release, new version, bump, tag, changelog, release notes, breaking change, minor, major, patch."
argument-hint: "analyze | release [major|minor|patch]"
---

# Semantic versioning, changelog and tags

Industry standards used: [SemVer 2.0.0](https://semver.org), [Keep a Changelog 1.1.0](https://keepachangelog.com) and [Conventional Commits 1.0.0](https://www.conventionalcommits.org).

Artifacts (single source of truth in bold):
- **`VERSION`**: repository root, one line `MAJOR.MINOR.PATCH`, no `v` prefix.
- `CHANGELOG.md`: repository root, Keep a Changelog format ([template](./assets/CHANGELOG.template.md)).
- Annotated git tag `vMAJOR.MINOR.PATCH` on `main` (see the `git-flow-trunk` skill).

## What is the public API of this project

A change is judged by its effect on whoever uses the project. Details and examples: [bump rules](./references/bump-rules.md).

| Surface | Where |
|---|---|
| HTTP API (`/api/*`, `/health`, `/admin`) | `app/rag/main.py` |
| Configuration (environment variables) | `.env.example`, `app/rag/config.py` |
| Database schema (Postgres) | `app/rag/db.py` |
| Qdrant collection / embeddings (a change forces a reindex) | `app/rag/vectorstore.py`, `app/rag/embeddings.py` |
| Control folder and document layout | `documents/_control/`, `app/rag/control.py` |
| CLI modules (`python -m rag.*`, `python -m bench.*`) | `app/rag`, `app/bench` |

## Decision: which bump

Take the **highest** level found since the last tag.

| Level | When | Commit signal |
|---|---|---|
| **MAJOR** | Incompatible change to the public API: removed or renamed endpoint, changed response shape, removed or renamed env var, destructive schema change, change that requires manual migration | `type!:` or footer `BREAKING CHANGE:` |
| **MINOR** | New backwards-compatible functionality: new endpoint, optional env var, additive migration, new capability; or a deprecation | `feat:` |
| **PATCH** | Backwards-compatible fixes and internal improvements: bug fix, performance, security fix, refactor with user-visible fix | `fix:`, `perf:` |
| none | No effect on users: docs, tests, chores, CI | `docs:`, `test:`, `chore:`, `ci:`, `style:`, `build:`, `refactor:` |

Rules:
- `0.y.z` is initial development: the API is not stable. While `MAJOR` is 0, breaking changes bump MINOR. Moving to `1.0.0` is an explicit decision by the user.
- Never edit or reuse a published tag. A wrong release is fixed with a new PATCH.
- Pre-releases use `-rc.1`, `-beta.1` (`1.2.0-rc.1`); they sort before the final version.
- If the history has commits that do not follow Conventional Commits, classify them by reading the diff and say so. Never guess silently.

## Procedure: analyze (no files are changed)

1. Be on `main` with a clean tree (`git status --short`).
2. Run `python3 .github/skills/semantic-versioning/scripts/semver.py analyze`. It lists commits since the last `v*` tag by type, detects breaking-change signals in the diff (removed routes, removed env vars, schema drops) and prints the suggested bump and next version.
3. Review the output: confirm the bump, reclassify commits listed as `unclassified`, check each breaking signal.
4. Present the user with: suggested bump, next version, reasons (commit hashes), and ask for confirmation before releasing.

## Procedure: release

Precondition: `analyze` confirmed by the user; on `main`; tests pass (`docker compose run --rm --no-deps rag-app pytest -q`).

1. Run `python3 .github/skills/semantic-versioning/scripts/semver.py release <major|minor|patch>` (add `--dry-run` first to preview). It:
   - writes `VERSION`,
   - moves the commits since the last tag into a new `## [X.Y.Z] - YYYY-MM-DD` section of `CHANGELOG.md`, grouped as Added / Changed / Fixed / Removed / Security / Breaking (creating the file from the template if missing) and updates the comparison links at the bottom,
   - commits `chore(release): vX.Y.Z`,
   - creates the annotated tag `vX.Y.Z`.
2. Read the generated changelog section and rewrite entries into user-facing language when a commit subject is not clear. Amend the release commit (`git commit --amend --no-edit`) and move the tag only if it has **not** been pushed.
3. Publish only if the user asks: `git push origin main --follow-tags`.
4. Report: version, tag, changelog section.

## First release (repository without tags)

The history before this skill is not in Conventional Commits. For the first release do not auto-generate the changelog from 28+ old commits: ask the user for the baseline version (suggested `0.1.0`, or `1.0.0` if the API is considered stable), write a short manual `## [X.Y.Z]` summary of what the project does today, and tag the current `main`. From then on `analyze` and `release` are incremental. `semver.py release major --baseline X.Y.Z --notes-file <file.md>` supports this: the file holds the bullet list used as the `Added` section (without `--notes-file` a placeholder is written, which must be replaced before publishing).

## Commit message format (required for this to work)

```
<type>(<optional scope>)<!>: <imperative summary, max 72 chars>

<optional body: why, not how>

<optional footers: BREAKING CHANGE: <what breaks and how to migrate>>
```

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`, `style`, `revert`. Scopes suggested: `api`, `ingest`, `retrieval`, `llm`, `db`, `admin`, `ui`, `config`.
