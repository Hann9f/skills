#!/usr/bin/env python3
"""Sync the Hann9f/skills fork and link its skills into every agent harness on this machine.

Python 3.8+, standard library only, safe to re-run.

  python3 fork-sync.py            update the checkout, then link the skills
  python3 fork-sync.py --check    change nothing, report drift, exit 1 on any

What it does:
  1. Clones the fork into <home>/src/skills, or fast-forwards it. Without git it
     downloads the main branch tarball instead.
  2. Links verify-spec, implement-spec and ask-matt into each harness skills
     folder that exists here (Codex, Codex cliproxy, shared ~/.agents, Claude
     Code, opencode, Antigravity, Gemini). Windows uses directory junctions.
  3. Moves any older real copy to <home>/.skill-sync-backup/<time>/, never deletes.
  4. Points the matching ~/.agents/.skill-lock.json entries at the fork.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_URL = os.environ.get("FORK_SYNC_REPO", "https://github.com/Hann9f/skills.git")
TARBALL_URL = os.environ.get("FORK_SYNC_TARBALL", "https://github.com/Hann9f/skills/archive/refs/heads/main.tar.gz")
COMMIT_URL = "https://api.github.com/repos/Hann9f/skills/commits/main"
STAMP = ".fork-sync-commit"
FORK_SLUG = "Hann9f/skills"
BRANCH = "main"
SKILLS = {
    "verify-spec": "skills/engineering/verify-spec",
    "implement-spec": "skills/engineering/implement-spec",
    "ask-matt": "skills/engineering/ask-matt",
}


def exists(path: Path) -> bool:
    """Path.exists that treats an unreadable path as absent, so one locked folder cannot stop the sync."""
    try:
        return path.exists()
    except OSError:
        return False


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(os.path, "isjunction", None)
    return bool(checker and checker(path))


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def harness_dirs(home: Path) -> list[tuple[str, Path]]:
    """Skill folders to link into: (label, path). Created only when the harness is present."""
    # CODEX_HOME belongs to the real user; a --home sandbox must never write through it.
    own_home = home == Path.home().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex") if own_home else home / ".codex"
    table = [
        ("agents (shared: opencode, antigravity, codex)", home / ".agents" / "skills", True),
        ("codex", home / ".codex" / "skills", (home / ".codex").exists()),
        ("codex CODEX_HOME", codex_home / "skills", exists(codex_home) and codex_home != home / ".codex"),
        ("codex-cliproxy", home / ".codex-cliproxy" / "skills", (home / ".codex-cliproxy").exists()),
        ("codex-cliproxy-filtered", home / ".codex-cliproxy-filtered" / "skills",
         (home / ".codex-cliproxy-filtered").exists()),
        ("claude", home / ".claude" / "skills", (home / ".claude").exists()),
        ("opencode", home / ".config" / "opencode" / "skills",
         (home / ".config" / "opencode").exists() or shutil.which("opencode") is not None),
        ("antigravity", home / ".gemini" / "antigravity" / "skills", (home / ".gemini" / "antigravity").exists()),
        ("gemini", home / ".gemini" / "skills", (home / ".gemini" / "skills").exists()),
    ]
    seen, out = set(), []
    for label, path, wanted in table:
        if wanted and str(path) not in seen:
            seen.add(str(path))
            out.append((label, path))
    return out


def remote_commit() -> str | None:
    """Short sha of the fork's main branch from the GitHub API, or None when it cannot be read."""
    request = urllib.request.Request(COMMIT_URL, headers={"Accept": "application/vnd.github.sha",
                                                          "User-Agent": "fork-sync"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode().strip()[:7] or None
    except OSError:
        return None


def ensure_checkout(dest: Path, check: bool, log: list[str]) -> str | None:
    """Return the commit the checkout is at, or None when it came from a tarball."""
    have_git = shutil.which("git") is not None
    if have_git and (dest / ".git").exists():
        run_git(dest, "fetch", "--quiet", "origin", BRANCH)
        branch = run_git(dest, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        clean = not run_git(dest, "status", "--porcelain").stdout.strip()
        behind = run_git(dest, "rev-list", "--count", f"HEAD..origin/{BRANCH}").stdout.strip()
        head = run_git(dest, "rev-parse", "--short", "HEAD").stdout.strip()
        if branch != BRANCH or behind not in ("0", ""):
            if check:
                log.append(f"DRIFT checkout on '{branch}', {behind or '?'} commits behind origin/{BRANCH}")
            elif not clean:
                log.append("WARN  checkout has local changes; left as is")
            else:
                if branch != BRANCH:
                    run_git(dest, "checkout", "--quiet", BRANCH)
                result = run_git(dest, "merge", "--ff-only", f"origin/{BRANCH}")
                log.append("OK    fast-forwarded" if result.returncode == 0 else f"WARN  cannot fast-forward: {result.stderr.strip()}")
        return run_git(dest, "rev-parse", "--short", "HEAD").stdout.strip() or head
    if check:
        if not (dest / "skills").exists():
            log.append("DRIFT no checkout")
            return None
        stamp = dest / STAMP
        have = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
        latest = remote_commit()
        if latest is None:
            log.append("WARN  cannot reach GitHub to compare the downloaded checkout")
        elif have != latest:
            log.append(f"DRIFT downloaded checkout is {have or 'unknown'}, fork main is {latest}; re-run to update")
        return have
    if (dest / ".git").exists():
        log.append(f"WARN  {dest} is a git checkout but git is not on PATH; install git or move it aside")
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if have_git and not dest.exists():
        result = subprocess.run(["git", "clone", "--quiet", "--branch", BRANCH, REPO_URL, str(dest)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            log.append(f"OK    cloned {REPO_URL}")
            return run_git(dest, "rev-parse", "--short", "HEAD").stdout.strip()
        log.append(f"WARN  git clone failed ({result.stderr.strip()}); using tarball")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp, "main.tar.gz")
        with urllib.request.urlopen(TARBALL_URL, timeout=60) as response, open(archive, "wb") as handle:
            shutil.copyfileobj(response, handle)
        with tarfile.open(archive) as bundle:
            try:
                bundle.extractall(tmp, filter="data")
            except TypeError:
                bundle.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots:
            raise SystemExit("fork-sync: tarball held no directory")
        latest = remote_commit()
        if latest:
            (roots[0] / STAMP).write_text(latest, encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)  # a tarball checkout is a pure download; nothing local lives in it
        shutil.move(str(roots[0]), str(dest))
    log.append("OK    downloaded tarball (no git here, so re-run to update)")
    return latest


def make_link(link: Path, target: Path) -> str:
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode == 0:
            return "junction"
        shutil.copytree(target, link)
        return "COPY (junction failed, will go stale)"
    os.symlink(target, link, target_is_directory=True)
    return "symlink"


def remove_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def link_skill(label: str, folder: Path, name: str, target: Path, backup: Path, check: bool) -> str:
    entry = folder / name
    if entry.exists() or is_link(entry):
        if os.path.realpath(entry) == os.path.realpath(target) and is_link(entry):
            return f"OK    {label}: {name}"
        if check:
            return f"DRIFT {label}: {name} is {'a link to ' + os.path.realpath(entry) if is_link(entry) else 'a separate copy'}"
        if is_link(entry):
            remove_link(entry)
        else:
            moved = backup / label.split(" ")[0] / name
            moved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(moved))
    elif check:
        return f"DRIFT {label}: {name} missing"
    folder.mkdir(parents=True, exist_ok=True)
    return f"FIXED {label}: {name} ({make_link(entry, target)})"


def update_lock(home: Path, dest: Path, commit: str | None, check: bool) -> str:
    lock_path = home / ".agents" / ".skill-lock.json"
    if not lock_path.is_file():
        return "SKIP  no skill lock file"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    skills = lock.get("skills", {})
    template = next((v for v in skills.values() if v.get("sourceType") == "github"), None)
    changed = []
    for name, rel in SKILLS.items():
        entry = skills.get(name)
        if entry is None and template is None:
            continue
        if entry is None:
            entry = dict(template)
            entry["skillPath"] = rel + "/SKILL.md"
        wanted = {"source": FORK_SLUG, "sourceUrl": REPO_URL if REPO_URL.startswith("https://github.com/") else entry.get("sourceUrl")}
        if commit:
            tree = run_git(dest, "rev-parse", f"HEAD:{rel}").stdout.strip()
            if tree:
                wanted["skillFolderHash"] = tree
        if any(entry.get(k) != v for k, v in wanted.items()) or name not in skills:
            changed.append(name)
            entry.update(wanted)
            skills[name] = entry
    if not changed:
        return "OK    skill lock already points at the fork"
    if check:
        return "DRIFT skill lock differs for " + ", ".join(changed)
    lock["skills"] = skills
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    return "FIXED skill lock now points at the fork for " + ", ".join(changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    parser.add_argument("--home", default=str(Path.home()), help="home directory to sync (default: the current user's)")
    parser.add_argument("--dest", help="where the fork lives (default: <home>/src/skills)")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    dest = Path(args.dest).expanduser().resolve() if args.dest else home / "src" / "skills"
    log: list[str] = []
    commit = ensure_checkout(dest, args.check, log)
    backup = home / ".skill-sync-backup" / time.strftime("%Y%m%dT%H%M%S")
    have_skills = all((dest / rel / "SKILL.md").is_file() for rel in SKILLS.values())
    if not have_skills and not args.check:
        log.append(f"WARN  {dest} lacks the fork's skills; nothing linked")
    for label, folder in harness_dirs(home) if have_skills else []:
        for name, rel in SKILLS.items():
            try:
                log.append(link_skill(label, folder, name, dest / rel, backup, args.check))
            except OSError as error:
                log.append(f"WARN  {label}: {name}: {error.strerror or error} ({error.filename})")
    log.append(update_lock(home, dest, commit, args.check))
    print(f"fork-sync: home={home} checkout={dest} commit={commit or 'n/a'}")
    print("\n".join(log))
    bad = [line for line in log if line.startswith(("DRIFT", "WARN"))]
    if args.check:
        return 1 if bad else 0
    return 1 if any(line.startswith("WARN") for line in log) else 0


if __name__ == "__main__":
    sys.exit(main())
