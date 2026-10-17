---
name: verify-spec
description: Acceptance proof that a built branch meets its accepted spec, taken from the running product. Use when implement-spec reaches its verification step, or when the user asks whether a branch meets its spec.
---

# Verify Spec

If your prompt names you the **verifier** or the **auditor** of a run, read [references/verifier.md](references/verifier.md) or [references/auditor.md](references/auditor.md) in full and do only that role. Otherwise you are the **coordinator**: follow the steps below.

This skill proves, criterion by criterion, that the **candidate** (the exact code under test) does what the accepted spec promises in the running product. A fresh verifier gathers the evidence and a second fresh agent audits it. The coordinator freezes inputs, dispatches both, and acts on the **verdict**, which `scripts/verify_check.py` computes. Below, `vc` means `python3 <this skill's directory>/scripts/verify_check.py`.

## Coordinator steps

1. **Gather inputs.**
   - The repository.
   - Every spec source as a file: the spec and its tickets. Save tracker issues to files outside the repository first, with their bodies and comments.
   - A **lineage** name: the spec's issue number or file name, so any later session finds the same loop history.
   - The candidate: `--candidate <branch or commit>`; omit it to verify the working tree as it is now.
   - The regression base, `--base`: usually the merge-base with main.
   - Gitignored files the product needs to run, `--overlay`: for example a `.env` with sandbox credentials.
   - Optionally `--depth`: `focused`, `standard` (the default), or `exhaustive`.
   - `--note` for constraints the verifier must respect: the safe environment, live systems it must leave alone, and setup pointers.

   Ask the user only for what you cannot find. If the spec or the candidate still cannot be identified, stop and report what is missing.
2. **Freeze**: `vc freeze --repo R --lineage L --spec F [--spec F2...]` plus the options from step 1. It prints the **run** directory. A fix or retry run takes `--kind fix` or `--kind retry` in place of `--spec`: it reuses the round's frozen spec and inherits the base, overlay, depth, and notes. Exit 6 means the loop rules refused the run: report the printed reason and stop.
3. **Export**: `vc export RUN`, plus `vc export RUN --base` when a base is set.
4. **Verify**: start a fresh subagent with no history from this context, on the model the active policy (the user, then the calling workflow, then `AGENTS.md`) names for delegated work. Prompt it with exactly this, filling in the role, `RUN`, and the directory:

   Before each T3 `delegate_task` or `task_status` call, use [delegation-recovery](../../../.codex/skills/delegation-recovery/SKILL.md) to validate and persist the exact role prompt or task ID; use it again after schema errors or uncertain results.

   > You are the verify-spec {verifier|auditor} for run `RUN`. Call the Skill tool with `verify-spec` (skill directory: `<this skill's directory>`) and follow its instructions for your role.

   Add nothing about how the candidate was built or why it should work. Wait for the agent. On a harness that resumes you when a child finishes, end your turn and continue at the next step. A run directory without `verdict.json` is still in flight. *Done when* the role's file exists: `RUN/results.json` for the verifier, `RUN/audit/audit.json` for the auditor. A role that returns without it has failed: go to step 2 once with `--kind retry`. Without subagents, take the verifier role in place only if this context neither built nor reviewed the candidate. Hand the user the prompt for any role you cannot take, including the auditor of evidence you gathered, and wait for its file.
5. **Seal and compute**: `vc seal RUN`, then `vc verdict RUN`. It prints the headline and `next`. Exits 3, 4, and 5 mean audit, fix, and stop; they are not errors.
6. **Audit** when `next` is `audit`: dispatch the auditor exactly as in step 4, then run `vc verdict RUN` again. A FAIL skips the audit and goes straight to a fix.
7. **Act on `next`**. Relay any `spec_questions` to the spec owner as soon as they appear. When the loop ends, report the headline, the counts, the reasons, and `RUN/report.md`.

| `next` | Verdict | Coordinator |
|---|---|---|
| `continue` | `PASS`, or `PASS_WITH_ACCEPTED_RISK` | Done. The calling workflow proceeds. |
| `fix` | `FAIL` with fix runs left | Give `RUN/report.md` and the `defects` in `verdict.json` to the calling workflow's implementer. When the user invoked this directly, report the defects to them and stop. After the fix lands, go to step 2 with `--kind fix`. If the implementer cannot reproduce a defect, it changes nothing, and that question goes to the user. |
| `stop` | Anything else, including `FAIL` at the cap and a repository that drifted after the run | Report each reason. If `retry_allowed` is true and you removed the cause without changing code (restarted a missing local service, or answered audit findings), go to step 2 once with `--kind retry`, passing the reasons as notes. After a drift, verify the current code as a new `--kind initial` run. |

## Human decisions

Only the user changes the loop. A new round after a stop takes `vc freeze ... --spec F --human-decision "<what the user decided>"`. This applies to a changed spec, a raised cap, and a different approach. For a `BLOCKED`, `NOT_RUN`, or `INCONCLUSIVE` row the user explicitly accepts as a risk, run `vc accept RUN --row ID --by "<name>" --reason "<why>"` and then `vc verdict RUN`. That accepts this candidate only, and failures and spec questions cannot be accepted. One round allows at most two fix runs and one retry per candidate.
