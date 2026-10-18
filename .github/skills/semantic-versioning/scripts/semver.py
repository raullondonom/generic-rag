#!/usr/bin/env python3
"""SemVer helper: analyze changes since the last tag and cut a release (VERSION, CHANGELOG.md, git tag).

Usage:
  semver.py analyze
  semver.py release <major|minor|patch> [--dry-run] [--baseline X.Y.Z]
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"^(?P<type>[a-z]+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<desc>.+)$")
SECTIONS = ["Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
TYPE_SECTION = {"feat": "Added", "fix": "Fixed", "perf": "Changed", "refactor": "Changed", "revert": "Removed"}
SILENT_TYPES = {"docs", "test", "chore", "ci", "build", "style"}
ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def parse_version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(x) for x in match.groups()) if match else None


def fmt(version):
    return ".".join(str(x) for x in version)


def last_tag():
    tags = [t for t in git("tag", "--merged", "HEAD", "--list", "v*").splitlines() if TAG_RE.match(t)]
    return max(tags, key=lambda t: parse_version(t)) if tags else None


def commits_since(tag):
    rng = f"{tag}..HEAD" if tag else "HEAD"
    raw = git("log", rng, "--format=%h%x1f%s%x1f%b%x1e")
    commits = []
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        sha, subject, body = (entry.split("\x1f") + ["", ""])[:3]
        match = COMMIT_RE.match(subject)
        info = {"sha": sha, "subject": subject, "type": None, "scope": None, "desc": subject, "breaking": False}
        if match:
            info.update(type=match["type"], scope=match["scope"], desc=match["desc"], breaking=bool(match["bang"]))
        if re.search(r"^BREAKING[ -]CHANGE:", body, re.M):
            info["breaking"] = True
        commits.append(info)
    return commits


def breaking_signals(tag):
    """Heuristics on the diff: removed routes, removed env vars, destructive schema changes."""
    if not tag:
        return []
    signals = []

    def removed_lines(path):
        diff = git("diff", f"{tag}..HEAD", "--unified=0", "--", path, check=False)
        return [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]

    for line in removed_lines("app/rag/main.py"):
        if re.match(r"\s*@app\.(get|post|put|delete|patch)\(", line):
            signals.append(f"endpoint removed or changed: {line.strip()}")
    for line in removed_lines(".env.example"):
        match = re.match(r"#?\s*([A-Z][A-Z0-9_]+)=", line)
        if match:
            signals.append(f"env var removed or changed: {match.group(1)}")
    for line in removed_lines("app/rag/db.py"):
        if re.search(r"DROP\s+(TABLE|COLUMN)|RENAME|ALTER\s+COLUMN", line, re.I):
            signals.append(f"schema change: {line.strip()}")
    return signals


def suggest(current, commits, signals):
    breaking = signals or any(c["breaking"] for c in commits)
    if breaking:
        return "minor" if current[0] == 0 else "major"
    if any(c["type"] == "feat" for c in commits):
        return "minor"
    if any(c["type"] in ("fix", "perf") for c in commits):
        return "patch"
    return None


def bump(version, level):
    major, minor, patch = version
    return {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}[level]


def current_version(tag):
    return parse_version(tag) if tag else (0, 0, 0)


def cmd_analyze(_):
    tag = last_tag()
    current = current_version(tag)
    commits = commits_since(tag)
    signals = breaking_signals(tag)
    print(f"Last tag: {tag or '(none: first release)'}   Commits since: {len(commits)}")
    groups = {}
    for c in commits:
        key = "breaking" if c["breaking"] else (c["type"] or "unclassified")
        groups.setdefault(key, []).append(c)
    for key in sorted(groups):
        print(f"\n[{key}]")
        for c in groups[key]:
            print(f"  {c['sha']} {c['subject']}")
    if signals:
        print("\nBreaking-change signals in the diff (verify each):")
        for s in signals:
            print(f"  - {s}")
    level = suggest(current, commits, signals)
    print()
    if not tag:
        print("No tags yet: use `release minor --baseline X.Y.Z` after agreeing the baseline with the user.")
    elif level is None:
        print("Suggested bump: none (no user-facing changes). No release needed.")
    else:
        print(f"Suggested bump: {level.upper()}  ->  v{fmt(bump(current, level))}")
        if current[0] == 0 and (signals or any(c["breaking"] for c in commits)):
            print("Note: MAJOR is 0 (initial development): breaking changes bump MINOR.")
    if "unclassified" in groups:
        print("Warning: unclassified commits must be classified by reading their diff.")


def repo_url():
    url = git("remote", "get-url", "origin", check=False)
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    return re.sub(r"\.git$", "", url)


def build_section(version, commits, baseline, signals=()):
    today = datetime.date.today().isoformat()
    lines = [f"## [{fmt(version)}] - {today}", ""]
    if baseline:
        return "\n".join(lines + ["### Added", "", "- Initial release. <describe what the project does today>", ""])
    buckets = {s: [] for s in SECTIONS}
    for c in commits:
        if c["type"] in SILENT_TYPES and not c["breaking"]:
            continue
        text = c["desc"] + (f" ({c['scope']})" if c["scope"] else "")
        if c["breaking"]:
            buckets["Breaking"].append(f"{text} ({c['sha']})")
        if c["type"] == "fix" and c["scope"] == "security":
            buckets["Security"].append(f"{text} ({c['sha']})")
        elif not c["breaking"]:
            buckets[TYPE_SECTION.get(c["type"], "Changed")].append(f"{text} ({c['sha']})")
    buckets["Breaking"] += [f"{sig} (detected in diff, verify)" for sig in signals]
    for section in SECTIONS:
        if buckets[section]:
            lines += [f"### {section}", ""] + [f"- {item}" for item in buckets[section]] + [""]
    return "\n".join(lines)


def update_changelog(version, previous_tag, section, dry_run):
    path = ROOT / "CHANGELOG.md"
    template = Path(__file__).resolve().parent.parent / "assets" / "CHANGELOG.template.md"
    text = path.read_text() if path.exists() else template.read_text()
    link_re = re.compile(r"^\[(Unreleased|\d+\.\d+\.\d+[^\]]*)\]: .*$", re.M)
    old_links = [m.group(0) for m in link_re.finditer(text) if m.group(1) != "Unreleased"]
    body = link_re.sub("", text).rstrip() + "\n"
    marker = re.search(r"^## \[Unreleased\]\s*$", body, re.M)
    if not marker:
        sys.exit("CHANGELOG.md has no '## [Unreleased]' heading")
    head, rest = body[:marker.end()], body[marker.end():].strip("\n")
    body = head + "\n\n" + section.rstrip() + "\n" + ("\n" + rest + "\n" if rest else "")
    body = re.sub(r"\n{3,}", "\n\n", body)
    url = repo_url()
    new_tag = f"v{fmt(version)}"
    new_link = f"[{fmt(version)}]: {url}/compare/{previous_tag}...{new_tag}" if previous_tag else f"[{fmt(version)}]: {url}/releases/tag/{new_tag}"
    links = [f"[Unreleased]: {url}/compare/{new_tag}...HEAD", new_link] + old_links
    result = body.rstrip() + "\n\n" + "\n".join(links) + "\n"
    if not dry_run:
        path.write_text(result)


def cmd_release(args):
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        sys.exit("Releases are cut from main only.")
    if git("status", "--porcelain"):
        sys.exit("Working tree is not clean: commit or stash first.")
    tag = last_tag()
    commits = commits_since(tag)
    if args.baseline:
        if tag:
            sys.exit("--baseline is only for the first release (a tag already exists).")
        version = parse_version(args.baseline)
        if not version:
            sys.exit("--baseline must look like X.Y.Z")
    else:
        if not tag:
            sys.exit("No tags yet: run with --baseline X.Y.Z for the first release.")
        version = bump(current_version(tag), args.level)
    new_tag = f"v{fmt(version)}"
    if git("tag", "--list", new_tag):
        sys.exit(f"Tag {new_tag} already exists. Published tags are never reused.")
    section = build_section(version, commits, bool(args.baseline), breaking_signals(tag))
    print(section)
    if args.dry_run:
        print(f"[dry-run] would write VERSION={fmt(version)}, update CHANGELOG.md, commit and tag {new_tag}")
        return
    (ROOT / "VERSION").write_text(fmt(version) + "\n")
    update_changelog(version, tag, section, dry_run=False)
    git("add", "VERSION", "CHANGELOG.md")
    git("commit", "-m", f"chore(release): {new_tag}")
    git("tag", "-a", new_tag, "-m", f"Release {new_tag}")
    print(f"Released {new_tag}. Publish with: git push origin main --follow-tags")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("analyze").set_defaults(func=cmd_analyze)
    rel = sub.add_parser("release")
    rel.add_argument("level", choices=["major", "minor", "patch"])
    rel.add_argument("--dry-run", action="store_true")
    rel.add_argument("--baseline", help="first release version X.Y.Z (repository without tags)")
    rel.set_defaults(func=cmd_release)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
