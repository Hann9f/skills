#!/usr/bin/env python3
"""Deterministic core of verify-spec. Python 3 standard library only.

  freeze  --repo DIR --spec FILE [--spec FILE...] --lineage NAME [--kind initial|fix|retry]
          [--candidate REF] [--base REF] [--overlay PATH...] [--note TEXT...] [--depth D]
          [--root DIR] [--human-decision TEXT]  create a run; enforce the loop caps
  export  RUN [--base]                          export the frozen candidate (or base)
  check   RUN [--audit]                         validate recorded evidence (and audit)
  seal    RUN                                   hash the verifier's evidence
  verdict RUN                                   compute verdict.json, report.md, next action
  accept  RUN --row ID --by NAME --reason TEXT  record a human's acceptance of an open gap

Exit codes: 0 ok/continue, 1 error or check problems, 2 usage,
3 next=audit, 4 next=fix, 5 next=stop, 6 freeze refused by the loop rules.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CAPS = {"fix": 2, "retry": 1}
CRITERION_STATUS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "INCONCLUSIVE", "NEEDS_SPEC_DECISION", "NOT_APPLICABLE"}
CHECK_STATUS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "INCONCLUSIVE"}
AUDIT_STATUS = {"AUDIT_PASS", "AUDIT_FINDINGS", "AUDIT_BLOCKED"}
SERVICE_KINDS = {"http", "ui"}
UNSEALED = {"checkout", "base", "audit", "seal.json", "verdict.json", "report.md"}
ACCEPTABLE = {"BLOCKED", "NOT_RUN", "INCONCLUSIVE"}
MIN_QUOTE = 15
MAX_QUOTES = 3
MIN_AUDIT_QUOTE = 8
EXIT_NEXT = {"continue": 0, "audit": 3, "fix": 4, "stop": 5}
REFUSED = 6


class Refused(Exception):
    pass


def fail(message: str) -> None:
    sys.exit(f"verify_check: {message}")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_text(path, errors: str = "strict") -> str:
    """Text is UTF-8 on every platform (Windows defaults to a legacy code page)."""
    return Path(path).read_text(encoding="utf-8", errors=errors)


def write_text(path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: Path) -> str:
    """A file's hash, or for a directory the hash of its sorted (relative path, file hash) pairs."""
    path = Path(path)
    if path.is_file():
        return sha256_file(path)
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(f"{item.relative_to(path).as_posix()}\0{sha256_file(item)}\n".encode())
    return digest.hexdigest()


def read_json(path: Path, problems: list[str] | None = None):
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        if problems is None:
            fail(f"{path}: invalid JSON ({error})")
        problems.append(f"{Path(path).name} is not valid JSON ({error})")
        return None


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, indent=2) + "\n")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", text)).strip()


def inside(path, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- git

def git(repo, *args, index=None, work_tree=None, check=True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    command = ["git", "-C", str(repo)] + (["--work-tree", str(work_tree)] if work_tree else []) + list(args)
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        fail(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result


def git_out(repo, *args, **kwargs) -> str:
    return git(repo, *args, **kwargs).stdout.strip()


def toplevel(path) -> Path:
    return Path(git_out(path, "rev-parse", "--show-toplevel")).resolve()


def identity(repo: Path) -> dict:
    """The candidate's immutable identity: HEAD when clean, else the tree of the working files."""
    head = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip() or None
    if head and not git_out(repo, "status", "--porcelain"):
        return {"tree": git_out(repo, "rev-parse", "HEAD^{tree}"), "commit": head, "uncommitted": False}
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp, "index")
        git(repo, "add", "-A", index=index)
        tree = git_out(repo, "write-tree", index=index)
    return {"tree": tree, "commit": None, "uncommitted": True, "head": head}


def resolve_ref(repo: Path, ref: str) -> dict:
    tree = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{tree}}", check=False).stdout.strip()
    if not tree:
        fail(f"cannot resolve {ref!r} to a commit or tree")
    commit = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).stdout.strip() or None
    return {"ref": ref, "tree": tree, "commit": commit, "uncommitted": False}


def current_tree(repo: Path, candidate: dict) -> str | None:
    """What the candidate's source resolves to now: the same ref, or the working tree."""
    ref = candidate.get("ref") or ("HEAD" if candidate.get("commit") else None)
    if ref:
        found = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{tree}}", check=False).stdout.strip()
        return found or None
    return identity(repo)["tree"]


def export_tree(repo, tree: str, dest: Path) -> list[str]:
    if dest.exists() and any(dest.iterdir()):
        fail(f"{dest} already exists; each run exports once")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp, "index")
        git(repo, "read-tree", tree, index=index)
        git(repo, "checkout-index", "-a", f"--prefix={dest}{os.sep}", index=index)
        staged = git_out(repo, "ls-files", "-s", index=index)
    return [line.split("\t", 1)[1] for line in staged.splitlines() if line.startswith("160000")]


def source_changes(repo, tree: str, dest: Path) -> tuple[list[str], list[str]]:
    """Tracked files that differ from the tree, and untracked files not covered by .gitignore."""
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp, "index")
        git(repo, "read-tree", tree, index=index)
        diff = git(repo, "diff", "--name-status", index=index, work_tree=dest, check=False)
        extra = git(repo, "ls-files", "--others", "--exclude-standard", index=index, work_tree=dest, check=False)
    if diff.returncode or extra.returncode:
        return [f"git failed: {diff.stderr.strip()} {extra.stderr.strip()}"], []
    return ([line for line in diff.stdout.splitlines() if line.strip()],
            [line for line in extra.stdout.splitlines() if line.strip()])


# ---------------------------------------------------------------- ledger and freeze

def slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    if not value:
        fail("the lineage needs letters or digits")
    return value


def default_root(repo: Path) -> Path:
    """Shared by every worktree of one repository, so caps survive a change of checkout."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    common = Path(git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    name = common.parent.name if common.name == ".git" else common.name
    return state / "verify-spec" / f"{name}-{hashlib.sha256(str(common).encode()).hexdigest()[:8]}"


def load_ledger(lineage_dir: Path, lineage: str) -> dict:
    """Decisions, acceptances and caps are stored; the run history is read from the run directories."""
    ledger = read_json(lineage_dir / "ledger.json") or {}
    ledger.setdefault("lineage", lineage)
    ledger.setdefault("caps", dict(CAPS))
    ledger.setdefault("decisions", [])
    ledger.setdefault("acceptances", [])
    runs = []
    for manifest_path in sorted(lineage_dir.glob("*/manifest.json")):
        manifest = read_json(manifest_path) or {}
        verdict = read_json(manifest_path.parent / "verdict.json") or {}
        runs.append({"run_id": manifest_path.parent.name, "kind": manifest.get("kind"),
                     "candidate_tree": (manifest.get("candidate") or {}).get("tree"),
                     "verdict": verdict.get("headline"), "next": verdict.get("next"),
                     "spec_questions": verdict.get("spec_questions", [])})
    ledger["runs"] = runs
    return ledger


def save_ledger(lineage_dir: Path, ledger: dict) -> None:
    write_json(lineage_dir / "ledger.json", {k: v for k, v in ledger.items() if k != "runs"})


def current_round(ledger: dict) -> list[dict]:
    """Runs since the latest initial run; caps count within a round."""
    starts = [i for i, run in enumerate(ledger["runs"]) if run["kind"] == "initial"]
    return ledger["runs"][starts[-1]:] if starts else ledger["runs"]


def plan_run(ledger: dict, kind: str, tree: str, human_decision: str | None) -> None:
    runs = ledger["runs"]
    prior = runs[-1] if runs else None
    unfinished = prior is not None and prior.get("next") in (None, "audit")
    if unfinished and kind == "fix":
        raise Refused(f"run {prior['run_id']} has no verdict yet; finish it, or start --kind retry if its "
                      "verifier or auditor failed")
    if kind == "initial":
        passed = prior and prior["verdict"] in ("PASS", "PASS_WITH_ACCEPTED_RISK")
        if prior and not passed and not human_decision:
            raise Refused("this lineage stopped without passing; a new round needs --human-decision "
                          "recording what the human decided")
        return
    if not prior:
        raise Refused(f"--kind {kind} needs a prior run in this lineage")
    this_round = current_round(ledger)
    if kind == "fix":
        if prior["next"] != "fix":
            raise Refused(f"the prior run's next action is {prior['next']!r}, not 'fix'")
        if tree == prior["candidate_tree"]:
            raise Refused("the candidate is unchanged since the prior run; a fix run needs changed code")
        used = sum(run["kind"] == "fix" for run in this_round)
        if used >= ledger["caps"]["fix"]:
            raise Refused(f"fix cap reached ({used}/{ledger['caps']['fix']}); a human decides the next step")
    else:
        if prior["verdict"] == "FAIL":
            raise Refused("a FAIL needs a fix run, not a retry")
        if (prior["verdict"] in ("INCONCLUSIVE",) and not unfinished
                and prior.get("spec_questions")):
            raise Refused("the prior run is waiting on a spec decision; a retry cannot answer it")
        if prior["next"] == "continue":
            raise Refused("the prior run already passed")
        if tree != prior["candidate_tree"]:
            raise Refused("a retry re-verifies the same candidate, but the code changed")
        used = sum(run["kind"] == "retry" and run["candidate_tree"] == tree for run in this_round)
        if used >= ledger["caps"]["retry"]:
            raise Refused(f"retry cap reached for this candidate ({used}/{ledger['caps']['retry']})")


def check_root(repo: Path, root: Path) -> None:
    if inside(root, repo) and git(repo, "check-ignore", "-q", str(root), check=False).returncode:
        fail(f"evidence root {root} is inside the repository and not gitignored, so it would change "
             "the candidate identity; use --root outside the repository")


def cmd_freeze(args) -> int:
    repo = toplevel(args.repo)
    root = Path(args.root).expanduser().resolve() if args.root else default_root(repo)
    check_root(repo, root)
    lineage = slug(args.lineage)
    lineage_dir = root / lineage
    ledger = load_ledger(lineage_dir, lineage)
    candidate = resolve_ref(repo, args.candidate) if args.candidate else identity(repo)
    try:
        plan_run(ledger, args.kind, candidate["tree"], args.human_decision)
    except Refused as refusal:
        print(f"verify_check: refused: {refusal}", file=sys.stderr)
        return REFUSED
    prior = ledger["runs"][-1] if ledger["runs"] else None
    if args.kind == "initial":
        if not args.spec:
            fail("an initial run needs --spec")
        sources = [Path(src).expanduser().resolve() for src in args.spec]
        for source in sources:
            if not source.is_file():
                fail(f"spec source {source} is not a file")
    elif args.spec:
        print("verify_check: refused: fix and retry runs reuse the round's frozen spec; a changed spec "
              "starts a new round (--kind initial --human-decision ...)", file=sys.stderr)
        return REFUSED
    inherited = {}
    if args.kind != "initial":
        inherited = read_json(lineage_dir / prior["run_id"] / "manifest.json") or {}
        if not args.base and inherited.get("base"):
            args.base = inherited["base"].get("commit") or inherited["base"]["tree"]
        args.overlay = args.overlay or [item["path"] for item in inherited.get("overlay", [])]
        args.depth = args.depth or inherited.get("depth")
        args.note = inherited.get("notes", []) + (args.note or [])
    args.depth = args.depth or "standard"
    overlay = []
    for rel in [str(r).rstrip("/") for r in args.overlay or []]:
        path = repo / rel
        if not path.exists():
            fail(f"overlay {rel} does not exist in {repo}")
        overlay.append({"path": rel, "sha256": sha256_path(path)})
    run_id = f"{len(ledger['runs']) + 1:02d}-{args.kind}-{now()}"
    run = lineage_dir / run_id
    run.mkdir(parents=True)
    if args.kind == "initial":
        write_text(run / "spec.md", "\n\n".join(
            f"<!-- verify-spec source: {source} -->\n{read_text(source).rstrip()}" for source in sources) + "\n")
    else:
        prior_manifest = read_json(lineage_dir / prior["run_id"] / "manifest.json")
        shutil.copy2(lineage_dir / prior["run_id"] / "spec.md", run / "spec.md")
        sources = [Path(src) for src in prior_manifest["spec"]["sources"]]
    prior_info = None
    if prior:
        prior_dir = lineage_dir / prior["run_id"]
        prior_verdict = read_json(prior_dir / "verdict.json") or {}
        audit = prior_dir / "audit" / "audit.json"
        prior_info = {"run_id": prior["run_id"], "verdict": prior["verdict"],
                      "failed_rows": prior_verdict.get("failed_rows", []),
                      "results": str(prior_dir / "results.json"), "report": str(prior_dir / "report.md"),
                      "audit": str(audit) if audit.exists() else None}
    write_json(run / "manifest.json", {
        "schema": 1, "run_id": run_id, "lineage": lineage, "kind": args.kind, "frozen_at": now(),
        "repo": str(repo), "candidate": candidate,
        "base": resolve_ref(repo, args.base) if args.base else None,
        "spec": {"snapshot": "spec.md", "sources": [str(s) for s in sources],
                 "sha256": sha256_file(run / "spec.md")},
        "overlay": overlay, "depth": args.depth, "notes": args.note or [], "prior": prior_info,
    })
    if args.human_decision:
        ledger["decisions"].append({"run_id": run_id, "decision": args.human_decision, "recorded_at": now()})
    save_ledger(lineage_dir, ledger)
    if candidate["uncommitted"]:
        print("note: candidate is uncommitted; its identity is the working-tree tree "
              f"{candidate['tree']}", file=sys.stderr)
    print(run)
    return 0


def manifest_of(run: Path) -> dict:
    manifest = read_json(run / "manifest.json")
    if manifest is None:
        fail(f"{run} has no manifest.json; create runs with `freeze`")
    return manifest


def cmd_export(args) -> int:
    run = Path(args.run).resolve()
    manifest = manifest_of(run)
    ident = manifest["base"] if args.base else manifest["candidate"]
    if ident is None:
        fail("this run was frozen without --base")
    dest = run / ("base" if args.base else "checkout")
    gitlinks = export_tree(manifest["repo"], ident["tree"], dest)
    for item in manifest["overlay"]:
        source = Path(manifest["repo"]) / item["path"]
        if not source.exists() or sha256_path(source) != item["sha256"]:
            fail(f"overlay {item['path']} changed since freeze")
        target = dest / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    if gitlinks:
        print("warning: submodules are not exported (" + ", ".join(gitlinks) +
              "); record them as a limit or mark dependent checks BLOCKED", file=sys.stderr)
    print(dest)
    return 0


# ---------------------------------------------------------------- evaluation

def obligation_lines(text: str) -> list[str]:
    """Matt-format obligations: numbered user stories and ticket checkboxes."""
    lines, in_stories = [], False
    for raw in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)", raw)
        if heading:
            in_stories = "user stor" in heading.group(1).lower()
            continue
        box = re.match(r"^\s*[-*]\s+\[[ xX]\]\s+(.*)", raw)
        story = re.match(r"^\s*\d+\.\s+(.*)", raw) if in_stories else None
        match = box or story
        if match and norm(match.group(1)):
            lines.append(norm(match.group(1)))
    return lines


def evidence_problems(run: Path, paths, area: str | None = None) -> list[str]:
    """Evidence must be non-empty files in a sealed run subdirectory (or in `area`, e.g. audit/)."""
    if not isinstance(paths, list) or not paths:
        return ["no evidence"]
    issues = []
    for rel in paths:
        path = run / str(rel)
        if not inside(path, run):
            issues.append(f"evidence {rel} is outside the run")
            continue
        parts = path.resolve().relative_to(run.resolve()).parts
        if area is not None and parts[0] != area:
            issues.append(f"evidence {rel} must be under {area}/")
        elif area is None and (len(parts) < 2 or parts[0] in UNSEALED):
            issues.append(f"evidence {rel} must be in a sealed subdirectory such as evidence/")
        elif not path.is_file() or path.stat().st_size == 0:
            issues.append(f"evidence {rel} is missing or empty")
    return issues


def is_int(value) -> bool:
    return type(value) is int


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def by_id(items, label: str, problems: list[str]) -> dict:
    table = {}
    for item in items if isinstance(items, list) else []:
        key = item.get("id") if isinstance(item, dict) else None
        if not key:
            problems.append(f"a {label} has no id")
        elif key in table:
            problems.append(f"duplicate {label} id {key}")
        else:
            table[key] = item
    return table


def evaluate_check(run: Path, check: dict, services: set, problems: list[str], has_base: bool = False) -> dict:
    cid, status, kind = check["id"], check.get("status"), check.get("kind")
    if status not in CHECK_STATUS:
        problems.append(f"{cid}: status {status!r} is not one of {sorted(CHECK_STATUS)}")
        return {"status": "BLOCKED", "reasons": ["invalid status"], "proof": False}
    if not kind:
        problems.append(f"{cid}: kind is missing")
    if kind != "inspection" and not isinstance(check.get("real"), bool):
        problems.append(f"{cid}: real must be true or false")
    if check.get("mandatory") and not check.get("mandatory_source"):
        problems.append(f"{cid}: mandatory checks need mandatory_source")
    counts = check.get("counts") if isinstance(check.get("counts"), dict) else None
    if kind == "suite" and counts is None:
        problems.append(f"{cid}: suite checks need counts {{executed, failed, skipped}}")
    for label, table in (("counts", counts), ("base_counts", check.get("base_counts"))):
        if isinstance(table, dict) and any(not is_int(table.get(k, 0)) or table.get(k, 0) < 0
                                           for k in ("executed", "failed", "skipped")):
            problems.append(f"{cid}: {label} values must be non-negative integers")
    if status in ("FAIL", "PASS"):
        problems.extend(f"{cid}: {issue}" for issue in evidence_problems(run, check.get("evidence")))
    elif not check.get("reason"):
        problems.append(f"{cid}: {status} needs a reason")
    if status != "PASS":
        return {"status": status, "reasons": [], "proof": False}
    reasons = []
    if kind not in ("inspection", "manual"):
        if not check.get("cmd"):
            reasons.append("no command recorded")
        if not is_int(check.get("exit_code")) or check.get("exit_code") != 0:
            reasons.append(f"exit_code is {check.get('exit_code')!r}, not 0")
    if kind == "suite" and counts is not None:
        if not counts.get("executed"):
            reasons.append("suite executed zero tests")
        if counts.get("failed"):
            reasons.append(f"suite has {counts.get('failed')} failed tests")
        if counts.get("skipped") and not check.get("skipped_reason"):
            reasons.append(f"{counts.get('skipped')} skipped tests without skipped_reason")
        base_counts = check.get("base_counts") if isinstance(check.get("base_counts"), dict) else None
        if check.get("mandatory") and counts.get("skipped"):
            if base_counts is None:
                reasons.append("a mandatory suite with skipped tests needs base_counts from the same suite on base/")
            elif not has_base or evidence_problems(run, check.get("base_evidence")):
                reasons.append("base_counts needs a frozen --base and base_evidence: the suite's output on base/")
            elif (counts.get("skipped") or 0) > (base_counts.get("skipped") or 0):
                reasons.append(f"{counts['skipped']} skipped vs {base_counts.get('skipped') or 0} on the base")
    if kind == "manual":
        reasons.append("manual checks need a human; record them as BLOCKED")
    if kind in SERVICE_KINDS and check.get("service") not in services:
        reasons.append("not bound to a service in results.binding")
    effective = "BLOCKED" if reasons else "PASS"
    proof = (effective == "PASS" and kind != "inspection" and check.get("real") is True
             and not (counts or {}).get("skipped"))
    return {"status": effective, "reasons": reasons, "proof": proof}


def evaluate(run: Path) -> dict:
    """Validate results.json; a malformed shape becomes an integrity problem, never a crash."""
    try:
        return evaluate_results(run)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as error:
        return {"manifest": manifest_of(run), "results": None, "rows": {}, "checks": {},
                "problems": [f"results.json has an unexpected shape ({type(error).__name__}: {error})"]}


def evaluate_results(run: Path) -> dict:
    """Validate results.json against the manifest, spec and checkout; derive effective row statuses."""
    manifest = manifest_of(run)
    problems: list[str] = []
    results = read_json(run / "results.json", problems)
    state = {"manifest": manifest, "results": results, "problems": problems, "rows": {}, "checks": {}}
    if results is None or not isinstance(results, dict):
        problems.append("results.json is missing or not a JSON object")
        state["results"] = None
        return state
    spec_file = run / "spec.md"
    if not spec_file.is_file() or sha256_file(spec_file) != manifest["spec"]["sha256"]:
        problems.append("spec.md changed after freeze")
    spec_lines = [norm(line) for line in read_text(spec_file).splitlines()] if spec_file.is_file() else []
    spec_lines = [line for line in spec_lines if line]
    if results.get("run_id") != manifest["run_id"]:
        problems.append("results.run_id does not match the manifest")
    if results.get("candidate_tree") != manifest["candidate"]["tree"]:
        problems.append("results.candidate_tree does not match the manifest")
    checkout = run / "checkout"
    if not checkout.is_dir():
        problems.append("checkout/ is missing: export the candidate with `export`")
    else:
        changes, added = source_changes(manifest["repo"], manifest["candidate"]["tree"], checkout)
        if changes:
            problems.append("checkout differs from the candidate: " + "; ".join(changes[:5]))
        declared = [str(g.get("path", "")).rstrip("/") for g in results.get("generated") or [] if isinstance(g, dict)
                    and g.get("path") and g.get("reason")] + [item["path"] for item in manifest["overlay"]]
        undeclared = [f for f in added if not any(f == d or f.startswith(d + "/") for d in declared)]
        if undeclared:
            problems.append("files added to checkout without a `generated` entry: " + ", ".join(undeclared[:5]))
        for item in manifest["overlay"]:
            path = checkout / item["path"]
            if not path.exists() or sha256_path(path) != item["sha256"]:
                problems.append(f"overlay {item['path']} changed during the run")
    services = set()
    for binding in results.get("binding") or []:
        name = binding.get("service") if isinstance(binding, dict) else None
        if not name:
            problems.append("a binding has no service name")
            continue
        services.add(name)
        if not (inside(binding.get("cwd"), checkout) or inside(binding.get("cwd"), run / "base")):
            problems.append(f"binding {name}: cwd {binding.get('cwd')!r} is not inside this run's export")
        if not binding.get("cmd"):
            problems.append(f"binding {name}: cmd is missing")
    checks = by_id(results.get("checks"), "check", problems)
    criteria = by_id(results.get("criteria"), "criterion", problems)
    has_base = bool(manifest.get("base"))
    check_state = {cid: evaluate_check(run, check, services, problems, has_base) for cid, check in checks.items()}
    state["checks"] = check_state
    rows = state["rows"]
    proving = set()
    quoted: list[str] = []
    for aid, criterion in criteria.items():
        status = criterion.get("status")
        if status not in CRITERION_STATUS:
            problems.append(f"{aid}: status {status!r} is not one of {sorted(CRITERION_STATUS)}")
            status = "BLOCKED"
        quotes = criterion.get("quote")
        quotes = [quotes] if isinstance(quotes, str) else quotes if isinstance(quotes, list) else []
        good = [norm(q) for q in quotes if isinstance(q, str) and len(norm(q)) >= MIN_QUOTE
                and any(norm(q) in line for line in spec_lines)]
        if not quotes or len(good) != len(quotes) or len(quotes) > MAX_QUOTES:
            problems.append(f"{aid}: give 1-{MAX_QUOTES} quotes, each exact text of at least {MIN_QUOTE} "
                            "characters from a single spec.md line")
        quoted.extend(good)
        refs = criterion.get("checks") if isinstance(criterion.get("checks"), list) else []
        for ref in refs:
            if ref not in checks:
                problems.append(f"{aid}: unknown check {ref}")
        known = [ref for ref in refs if ref in checks]
        reasons = []
        if status == "PASS":
            if not known:
                reasons.append("no checks")
            reasons += [f"{ref} is {check_state[ref]['status']}" for ref in known
                        if check_state[ref]["status"] != "PASS"]
            if not criterion.get("static") and not any(check_state[ref]["proof"] for ref in known):
                reasons.append("no runtime proof: needs a real, non-inspection check with no skipped tests")
            if not reasons:
                proving.update(ref for ref in known if check_state[ref]["proof"])
        elif status == "NOT_APPLICABLE":
            exclusion = criterion.get("exclusion_quote") or ""
            if len(norm(exclusion)) < MIN_QUOTE or not any(norm(exclusion) in line for line in spec_lines):
                problems.append(f"{aid}: NOT_APPLICABLE needs an exclusion_quote found in spec.md")
        elif status == "NEEDS_SPEC_DECISION":
            if not criterion.get("question"):
                problems.append(f"{aid}: NEEDS_SPEC_DECISION needs the question for the spec owner")
        elif status != "FAIL" and not criterion.get("reason"):
            problems.append(f"{aid}: {status} needs a reason")
        rows[aid] = {"type": "criterion", "reported": status,
                     "status": "BLOCKED" if reasons else status, "reasons": reasons,
                     "detail": criterion.get("question") or criterion.get("reason") or "",
                     "quote": (quotes[0] if quotes and isinstance(quotes[0], str) else "")[:70],
                     "checks": refs}
        if status == "PASS" and criterion.get("static") and not reasons:
            rows[aid]["note"] = "static: inspection evidence"
    if not criteria:
        problems.append("no criteria recorded")
    for line in obligation_lines(read_text(spec_file) if spec_file.is_file() else ""):
        if not any(q in line for q in quoted):
            problems.append(f"spec obligation has no criterion quoting it: {line[:80]!r}")
    referenced = {ref for c in criteria.values() for ref in as_list(c.get("checks"))}
    for cid, check in checks.items():
        if check.get("mandatory") or (cid not in referenced and check_state[cid]["status"] != "PASS"):
            rows[cid] = {"type": "gate", "reported": check.get("status"),
                         "status": check_state[cid]["status"], "reasons": check_state[cid]["reasons"],
                         "detail": check.get("reason") or "", "quote": str(check.get("mandatory_source", ""))[:70],
                         "checks": [cid]}
    state["proving"] = proving
    if criteria and not proving:
        rows["RUNTIME"] = {"type": "control", "reported": None, "status": "BLOCKED", "detail": "",
                           "quote": "", "checks": [],
                           "reasons": ["no criterion is proven by a real runtime check"]}
    if proving:
        valid = []
        for control in results.get("red_controls") or []:
            if not isinstance(control, dict) or control.get("check") not in proving:
                continue
            mutated, restored = control.get("mutated_exit"), control.get("restored_exit")
            if (control.get("mutation") and is_int(mutated) and mutated != 0 and is_int(restored)
                    and restored == 0 and not evidence_problems(run, control.get("evidence"))):
                valid.append(control["check"])
        rows["RED-CONTROL"] = {
            "type": "control", "reported": None, "status": "PASS" if valid else "BLOCKED", "detail": "",
            "quote": "", "checks": valid,
            "reasons": [] if valid else ["no valid red control on a check that proves a PASS criterion"]}
        state["red_control_checks"] = valid
    defects = {d.get("row"): d for d in results.get("defects") or [] if isinstance(d, dict)}
    for row_id, row in rows.items():
        if row["status"] == "FAIL":
            defect = defects.get(row_id)
            if not defect:
                problems.append(f"{row_id}: FAIL needs a defect entry")
            else:
                if not (defect.get("expected") and defect.get("observed")):
                    problems.append(f"{row_id}: defect needs expected and observed")
                if evidence_problems(run, [defect.get("repro")] if defect.get("repro") else []):
                    problems.append(f"{row_id}: defect needs a repro file inside the run")
    state["defects"] = defects
    return state


def sealed_files(run: Path) -> dict:
    files = {}
    for path in sorted(run.rglob("*")):
        rel = path.relative_to(run)
        if rel.parts[0] in UNSEALED or "__pycache__" in rel.parts or not path.is_file():
            continue
        files[rel.as_posix()] = sha256_file(path)
    return files


def evaluate_audit(run: Path, state: dict) -> tuple[str, list[str]]:
    try:
        return audit_state(run, state)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as error:
        return "invalid", [f"audit.json has an unexpected shape ({type(error).__name__}: {error})"]


def file_has(path: Path, quote: str) -> bool:
    return path.is_file() and norm(quote) in norm(read_text(path, "replace"))


def audit_state(run: Path, state: dict) -> tuple[str, list[str]]:
    problems: list[str] = []
    audit = read_json(run / "audit" / "audit.json", problems)
    if problems:
        return "invalid", problems
    if audit is None:
        return "missing", []
    if not isinstance(audit, dict):
        return "invalid", ["audit.json is not a JSON object"]
    seal_path = run / "seal.json"
    if not seal_path.is_file():
        return "invalid", ["the run is not sealed"]
    if audit.get("seal_sha256") != sha256_file(seal_path):
        return "invalid", ["the audit names a different seal_sha256 than seal.json"]
    status = audit.get("status")
    if status not in AUDIT_STATUS:
        return "invalid", [f"audit status {status!r} is not one of {sorted(AUDIT_STATUS)}"]
    findings = [f"{','.join(map(str, as_list(f.get('rows'))))}: {f.get('detail')}"
                for f in as_list(audit.get("findings")) if isinstance(f, dict)]
    if status != "AUDIT_PASS":
        return status.lower(), findings or ["the auditor gave no findings"]
    issues = []
    if audit.get("missing_obligations"):
        issues.append("missing obligations: " + "; ".join(map(str, audit["missing_obligations"])))
    if any(isinstance(f, dict) and f.get("must_fix") for f in audit.get("findings") or []):
        issues.append("AUDIT_PASS with must-fix findings")
    sealed = (read_json(seal_path) or {}).get("files", {})
    results = state.get("results") or {}
    checks = {c["id"]: c for c in results.get("checks") or [] if isinstance(c, dict) and c.get("id")}
    rows = state["rows"]
    passing = [r for r, row in rows.items() if row["type"] == "criterion" and row["status"] == "PASS"]
    reviewed = {}
    for entry in audit.get("reviewed") or []:
        if not isinstance(entry, dict):
            continue
        row, cid = entry.get("row"), entry.get("check")
        ev_quote, probe_quote = entry.get("evidence_quote") or "", entry.get("probe_quote") or ""
        if row not in rows or cid not in (rows[row].get("checks") or []) or cid not in checks:
            continue
        if not rows[row].get("note") and not state["checks"].get(cid, {}).get("proof"):
            continue
        evidence = [run / str(e) for e in checks[cid].get("evidence") or [] if str(e) in sealed]
        sources = [run / rel for rel in sealed if rel.startswith("probes/")] + [
            p for p in (run / "checkout").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        if (len(norm(ev_quote)) >= MIN_AUDIT_QUOTE and any(file_has(e, ev_quote) for e in evidence)
                and len(norm(probe_quote)) >= MIN_AUDIT_QUOTE and any(file_has(f, probe_quote) for f in sources)):
            reviewed[row] = True
    unreviewed = [r for r in passing if r not in reviewed]
    if unreviewed:
        issues.append("PASS rows without a verified review entry (row, its check, an evidence quote, a probe "
                      "or test quote): " + ", ".join(unreviewed[:10]))
    proving = state.get("proving") or set()
    replays = [r for r in as_list(audit.get("replayed")) if isinstance(r, dict) and r.get("matched") is True
               and r.get("check") in proving and not evidence_problems(run, r.get("evidence"), area="audit")]
    if proving and not replays:
        issues.append("no matched replay of a proving check with evidence under audit/")
    if state.get("red_control_checks") and audit.get("red_controls_genuine") is not True:
        issues.append("the auditor did not confirm the red control mutates product or data")
    return ("pass", [f"non-blocking finding: {f}" for f in findings]) if not issues else ("invalid", issues)


def describe(row_id: str, row: dict) -> str:
    text = f"{row_id}: {row['status']}"
    if row.get("detail"):
        text += f": {row['detail']}"
    if row["reasons"]:
        text += f" (checker: {'; '.join(row['reasons'])})"
    return text


def decide(state: dict, audit_state: str, accepted: dict, audit_issues: list[str] = ()) -> tuple[str, list[str]]:
    rows, problems = state["rows"], state["problems"]
    if problems:
        return "BLOCKED", [f"integrity: {p}" for p in problems]
    open_rows = {k: r for k, r in rows.items() if r["status"] not in ("PASS", "NOT_APPLICABLE") and k not in accepted}
    for statuses, headline in ((("FAIL",), "FAIL"), (("BLOCKED", "NOT_RUN"), "BLOCKED"),
                               (("INCONCLUSIVE", "NEEDS_SPEC_DECISION"), "INCONCLUSIVE")):
        hits = [k for k, r in open_rows.items() if r["status"] in statuses]
        if hits:
            return headline, [describe(k, rows[k]) for k in hits]
    if audit_state == "missing":
        return "PENDING_AUDIT", ["the evidence supports PASS; an independent audit must confirm it"]
    if audit_state != "pass":
        return "BLOCKED", [f"audit {audit_state}: {issue}" for issue in audit_issues] or [f"audit: {audit_state}"]
    waived = sorted(k for k in accepted if k in rows and rows[k]["status"] != "PASS")
    if waived:
        return "PASS_WITH_ACCEPTED_RISK", [f"accepted by {accepted[k]['decided_by']}: {describe(k, rows[k])}; "
                                           f"reason: {accepted[k]['reason']}" for k in waived]
    return "PASS", ["every criterion and gate is proven and the audit passed"]


def render_report(run: Path, verdict: dict, state: dict) -> str:
    manifest, results = state["manifest"], state["results"] if isinstance(state["results"], dict) else {}
    cand = manifest["candidate"]
    lines = [f"# Verification report: {manifest['lineage']} / {manifest['run_id']}", "",
             f"- **Verdict:** `{verdict['headline']}`; next action: `{verdict['next']}`",
             f"- **Candidate:** tree `{cand['tree']}`" + (f", commit `{cand['commit']}`" if cand.get("commit")
                                                        else " (uncommitted working tree)"),
             f"- **Spec:** `{manifest['spec']['sha256'][:16]}` from {', '.join(manifest['spec']['sources'])}",
             f"- **Kind:** {manifest['kind']}; prior run: {(manifest.get('prior') or {}).get('run_id', 'none')}",
             f"- **Criteria proven:** {verdict['counts']['proven']}/{verdict['counts']['criteria']}", "",
             "## Why", ""] + [f"- {reason}" for reason in verdict["reasons"] + verdict["next_reasons"]]
    lines += ["", "## Rows", "", "| Row | Obligation | Status | Reported | Checks | Notes |",
              "|---|---|---|---|---|---|"]
    for row_id, row in state["rows"].items():
        notes = "; ".join(([row["detail"]] if row.get("detail") else []) + row["reasons"]
                          + ([row["note"]] if row.get("note") else [])).replace("|", "\\|")
        quote = str(row.get("quote", "")).replace("|", "\\|")
        lines.append(f"| {row_id} | {quote} | {row['status']} | {row['reported'] or ''} | "
                     f"{', '.join(map(str, row.get('checks') or []))} | {notes} |")
    lines += ["", "## Checks", "", "| Check | Kind | Status | Counts | Command | Evidence |",
              "|---|---|---|---|---|---|"]
    for check in as_list(results.get("checks")):
        if isinstance(check, dict):
            cmd = str(check.get("cmd") or "").replace("|", "\\|")[:80]
            counts = check.get("counts") if isinstance(check.get("counts"), dict) else {}
            shown = " ".join(f"{k}={v}" for k, v in counts.items())
            lines.append(f"| {check.get('id')} | {check.get('kind')} | {check.get('status')} | {shown} | `{cmd}` | "
                         f"{', '.join(map(str, as_list(check.get('evidence'))))} |")
    if verdict["defects"]:
        lines += ["", "## Defects", ""]
        for defect in verdict["defects"]:
            lines += [f"### {defect['id']}: {defect.get('title') or defect['row']}", "",
                      f"- Expected: {defect.get('expected')}", f"- Observed: {defect.get('observed')}",
                      f"- Reproducer: `{defect.get('repro')}`", ""]
    limits = as_list(results.get("limits"))
    lines += ["", "## Limits", ""] + ([f"- {limit}" for limit in limits] or ["- none recorded"])
    lines += ["", "## Audit", "", f"- State: {verdict['audit']['state']}"] + [
        f"- {issue}" for issue in verdict["audit"]["issues"]]
    lines += ["", "This report proves acceptance evidence for one candidate. It does not approve a merge, "
              "release or deployment.", ""]
    return "\n".join(lines)


def cmd_check(args) -> int:
    run = Path(args.run).resolve()
    state = evaluate(run)
    for problem in state["problems"]:
        print(f"problem: {problem}")
    for row_id, row in state["rows"].items():
        if row["reasons"]:
            print(f"row {row_id}: {row['reported']} -> {row['status']}: {'; '.join(row['reasons'])}")
    bad = bool(state["problems"]) or any(row["reasons"] for row in state["rows"].values())
    if args.audit:
        audit_state, issues = evaluate_audit(run, state)
        print(f"audit: {audit_state}")
        for issue in issues:
            print(f"audit issue: {issue}")
        bad = bad or audit_state == "invalid"
    print("check: problems found" if bad else "check: ok")
    return 1 if bad else 0


def process_in(pid: int, run: Path) -> bool:
    """True when pid is alive and (where /proc exists) runs from inside this run directory.

    On Windows os.kill would terminate the process, so the check is skipped there."""
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OverflowError):
        return False
    cwd = Path(f"/proc/{pid}/cwd")
    if not Path("/proc").is_dir():
        return True
    try:
        return inside(os.readlink(cwd), run)
    except OSError:
        return False


def cmd_seal(args) -> int:
    run = Path(args.run).resolve()
    manifest_of(run)
    if (run / "verdict.json").exists():
        fail("this run already has a verdict; sealed evidence is immutable, so start a retry run instead")
    if (run / "seal.json").exists():
        print(f"already sealed; run `verdict {run}`", file=sys.stderr)
        return 0
    if not (run / "results.json").is_file():
        print("warning: results.json is missing; the verdict will be BLOCKED and a retry can follow",
              file=sys.stderr)
    results = read_json(run / "results.json", []) or {}
    for binding in (results.get("binding") or []) if isinstance(results, dict) else []:
        pid = binding.get("pid") if isinstance(binding, dict) else None
        if is_int(pid) and pid > 0 and process_in(pid, run):
            fail(f"service {binding.get('service')} (pid {pid}) is still running from this run; stop it, then seal")
    write_json(run / "seal.json", {"run_id": run.name, "sealed_at": now(), "files": sealed_files(run)})
    print(run / "seal.json")
    return 0


def cmd_verdict(args) -> int:
    run = Path(args.run).resolve()
    manifest = manifest_of(run)
    seal = read_json(run / "seal.json")
    if seal is None:
        fail("seal the run before computing its verdict")
    state = evaluate(run)
    if seal.get("files") != sealed_files(run):
        state["problems"].append("evidence changed after the seal")
    lineage_dir = run.parent
    ledger = load_ledger(lineage_dir, manifest["lineage"])
    tree = manifest["candidate"]["tree"]
    accepted = {a["row"]: a for a in ledger.get("acceptances", [])
                if isinstance(a, dict) and a.get("candidate_tree") == tree and a.get("decided_by")
                and a.get("reason") and state["rows"].get(a.get("row"), {}).get("status") in ACCEPTABLE}
    audit_state, audit_issues = evaluate_audit(run, state)
    headline, reasons = decide(state, audit_state, accepted, audit_issues)
    if headline in ("PASS", "PASS_WITH_ACCEPTED_RISK"):
        reasons += audit_issues
    this_round = current_round(ledger)
    fixes = sum(r["kind"] == "fix" for r in this_round)
    retries = sum(r["kind"] == "retry" and r["candidate_tree"] == tree for r in this_round)
    next_reasons = []
    if headline in ("PASS", "PASS_WITH_ACCEPTED_RISK"):
        drift = current_tree(Path(manifest["repo"]), manifest["candidate"]) != tree
        next_action = "stop" if drift else "continue"
        if drift:
            next_reasons.append("the repository no longer matches the verified candidate; verify the current code")
    elif headline == "PENDING_AUDIT":
        next_action = "audit"
    elif headline == "FAIL" and fixes < ledger["caps"]["fix"]:
        next_action = "fix"
        next_reasons.append(f"fix cycle {fixes + 1} of {ledger['caps']['fix']}")
    else:
        next_action = "stop"
        if headline == "FAIL":
            next_reasons.append(f"fix cap reached ({fixes}/{ledger['caps']['fix']}); a human decides")
    spec_questions = [k for k, r in state["rows"].items() if r["status"] == "NEEDS_SPEC_DECISION"]
    soft = [k for k, r in state["rows"].items() if r["status"] == "NOT_APPLICABLE" or r.get("note")]
    if soft and headline in ("PASS", "PASS_WITH_ACCEPTED_RISK", "PENDING_AUDIT"):
        next_reasons.append("not runtime-proven (static or not applicable): " + ", ".join(soft))
    for decision in ledger.get("decisions", []):
        next_reasons.append(f"human decision ({decision.get('run_id')}): {decision.get('decision')}")
    if spec_questions:
        next_reasons.append("the spec owner must answer: " + ", ".join(spec_questions))
    retry_allowed = (next_action == "stop" and headline in ("BLOCKED", "INCONCLUSIVE") and not spec_questions
                     and retries < ledger["caps"]["retry"])
    if retry_allowed:
        next_reasons.append("one retry of this candidate is allowed if the blocker is fixable without code changes")
    failed = [k for k, r in state["rows"].items() if r["status"] == "FAIL"]
    defects = []
    for row_id in failed:
        defect = state.get("defects", {}).get(row_id, {})
        defects.append({"id": f"DEF-{row_id}", "row": row_id, "title": defect.get("title"),
                        "expected": defect.get("expected"), "observed": defect.get("observed"),
                        "repro": str(run / defect["repro"]) if defect.get("repro") else None,
                        "evidence": [str(run / p) for p in defect.get("evidence") or []]})
    criteria = [r for r in state["rows"].values() if r["type"] == "criterion"]
    counts = {"criteria": len(criteria), "proven": sum(r["status"] == "PASS" for r in criteria)}
    for row in criteria:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    audit_file = run / "audit" / "audit.json"
    verdict = {
        "run_id": manifest["run_id"], "lineage": manifest["lineage"], "kind": manifest["kind"],
        "candidate": manifest["candidate"], "spec_sha256": manifest["spec"]["sha256"],
        "headline": headline, "reasons": reasons, "next": next_action, "next_reasons": next_reasons,
        "retry_allowed": retry_allowed, "counts": counts, "failed_rows": failed, "defects": defects,
        "spec_questions": [{"row": k, "question": state["rows"][k]["detail"]} for k in spec_questions],
        "problems": state["problems"], "audit": {"state": audit_state, "issues": audit_issues},
        "inputs": {"seal_sha256": sha256_file(run / "seal.json"),
                   "audit_sha256": sha256_file(audit_file) if audit_file.is_file() else None},
        "computed_at": now(),
    }
    write_json(run / "verdict.json", verdict)
    write_text(run / "report.md", render_report(run, verdict, state))
    save_ledger(lineage_dir, ledger)
    print(json.dumps({"headline": headline, "next": next_action, "retry_allowed": retry_allowed,
                      "report": str(run / "report.md")}))
    return EXIT_NEXT[next_action]


def cmd_accept(args) -> int:
    run = Path(args.run).resolve()
    manifest = manifest_of(run)
    verdict = read_json(run / "verdict.json")
    if verdict is None:
        fail("compute the verdict before recording an acceptance")
    state = evaluate(run)
    row = state["rows"].get(args.row)
    if row is None or row["status"] not in ACCEPTABLE:
        fail(f"{args.row} is not an open BLOCKED/NOT_RUN/INCONCLUSIVE row in this run; "
             "failures and spec questions cannot be accepted")
    lineage_dir = run.parent
    ledger = load_ledger(lineage_dir, manifest["lineage"])
    ledger["acceptances"].append({"row": args.row, "candidate_tree": manifest["candidate"]["tree"],
                                  "decided_by": args.by, "reason": args.reason, "run_id": manifest["run_id"],
                                  "recorded_at": now()})
    save_ledger(lineage_dir, ledger)
    print(f"accepted {args.row} for tree {manifest['candidate']['tree']}; run `verdict {run}` again")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--repo", required=True)
    freeze.add_argument("--spec", action="append", help="spec/ticket file; initial runs only")
    freeze.add_argument("--lineage", required=True)
    freeze.add_argument("--kind", choices=["initial", "fix", "retry"], default="initial")
    freeze.add_argument("--candidate", help="commit/branch/tree to verify; default: the working tree")
    freeze.add_argument("--base")
    freeze.add_argument("--overlay", action="append")
    freeze.add_argument("--note", action="append")
    freeze.add_argument("--depth", choices=["focused", "standard", "exhaustive"],
                        help="default: standard, or the prior run's depth for fix and retry runs")
    freeze.add_argument("--root")
    freeze.add_argument("--human-decision")
    export = sub.add_parser("export")
    export.add_argument("run")
    export.add_argument("--base", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("run")
    check.add_argument("--audit", action="store_true")
    for name in ("seal", "verdict"):
        sub.add_parser(name).add_argument("run")
    accept = sub.add_parser("accept")
    accept.add_argument("run")
    accept.add_argument("--row", required=True)
    accept.add_argument("--by", required=True, help="the human who decided, as they asked to be named")
    accept.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    return {"freeze": cmd_freeze, "export": cmd_export, "check": cmd_check, "seal": cmd_seal,
            "verdict": cmd_verdict, "accept": cmd_accept}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
