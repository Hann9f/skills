## What it does

`verify-spec` proves, criterion by criterion, that a built branch does what its accepted [spec](https://www.aihero.dev/ai-coding-dictionary/spec) promised, by exercising the running product. A fresh verifier [subagent](https://www.aihero.dev/ai-coding-dictionary/subagent) starts the candidate, checks every obligation through its real interface (an HTTP request, then the rows it should have written), and records evidence. A second fresh agent, the auditor, tries to knock that evidence down. A script, not an agent, computes the verdict.

The verdict is code. No agent writes `PASS`: the script reads the sealed evidence, the audit and the loop history, and either lets the work continue, sends the failures back for a fix, or stops. That is what makes the result safe to act on when the agent that built the code is the one asking.

## When to reach for it

Type `/verify-spec`, or the agent reaches for it automatically when [implement-spec](https://aihero.dev/skills-implement-spec) gets to its verification step or when you ask whether a branch meets its spec.

| Your situation | Reach for |
| --- | --- |
| A branch is built and you want proof it meets the spec, from the running product | `verify-spec` |
| You want the diff read against standards and the spec | [code-review](https://aihero.dev/skills-code-review) |
| Something is broken and you do not know why | [diagnosing-bugs](https://aihero.dev/skills-diagnosing-bugs) |
| You are building one slice and want tests first | [tdd](https://aihero.dev/skills-tdd) |

## Prerequisites

An accepted spec you can hand over as files (the spec and its tickets), a candidate to test (a branch, a commit, or the working tree as it is), and a way to run the product locally with disposable data. Python 3 runs the checker script. If the product needs a gitignored file to start, such as a `.env` with sandbox credentials, name it so the skill copies it in. The skill never touches production or real customer data.

## Candidate, evidence, verdict

The **candidate** is an immutable ID: the commit, or for uncommitted work a tree ID computed from the working files. The verifier tests an exported copy, never your working directory, and the script re-checks after the run that the copy is unchanged.

The evidence moves one way, and nothing is edited after it is hashed:

| Stage | Written by | File |
| --- | --- | --- |
| Frozen inputs | the coordinator | `manifest.json`, `spec.md` |
| Observations | the verifier | `results.json`, probes, raw evidence |
| Seal | the script | `seal.json` |
| Challenge | the auditor | `audit/audit.json` |
| Verdict and report | the script | `verdict.json`, `report.md` |

Each check carries a proof requirement, so a green command is not enough. A suite that ran zero tests, a skipped test, a mocked dependency, a status-code-only assertion, or a service that cannot be tied to the candidate is demoted to `BLOCKED`. The verifier also breaks the product on purpose (a red control) and shows its own probe goes red, then green when restored.

## The loop

The verdict names a next step, and the script enforces the limits.

| Next | Meaning |
| --- | --- |
| `continue` | Every criterion is proven and the audit passed. The calling workflow proceeds. |
| `fix` | A criterion failed. The implementer fixes only the reported defects, then verification runs again on the new candidate. |
| `stop` | Anything else: a blocker, an ambiguous spec, an audit finding, a repository that changed after the run, or the limits reached. |

A round allows at most two fix runs and one retry per candidate. After that only you can start a new round, and you record what you decided. A failure can never be accepted as a risk, and neither can a spec question. A blocked or unrun row can be, by your explicit decision, for that candidate only.

## Common questions

**Why not just trust `code-review`?**

`code-review` reads a diff and has never run the product. It finds a missing audit row only if the reviewer notices the missing line. `verify-spec` sends a request and looks in the database. The two answer different questions, and `implement-spec` runs both, review first.

**The audit blocked a run whose checks all passed. Is that a bug?**

No, it is the design working. The script can verify that evidence exists, that it hashes, and that a probe really ran. It cannot tell that a criterion cites a check that never exercises its quote. The auditor reads the probes against the spec and flags exactly that. The fix is a retry that maps each criterion to the checks that test it.

**Does it need subagents?**

Two fresh contexts, yes: one to verify and one to audit. With no subagent support, it hands you the prompt for each role to run in a fresh session and carries on from the file that session writes. A context that built the candidate never audits its own evidence.

**It cannot commit on my machine. Can it still verify?**

Yes. Uncommitted work is identified by a tree ID and tested from an export of it, so no commit is needed.

**A mandatory test suite skips tests. Does that fail the run?**

Only if the candidate skips more than the base does, shown by running the same suite on the base. A deliberate placeholder skip that the base also has is not a regression, and no criterion may lean on it as proof.

## It's working if

- The report lists every user story and ticket checkbox as its own row, with the spec text it came from.
- A broken build comes back `FAIL` with a reproducer per defect, and the fixed build comes back `PASS` on the next run.
- The verdict never changes after you read it: you can re-run the script and get the same headline.
- A run that stops tells you which row blocked it and who owns the next move.

## Where it fits

A chain step, after [code-review](https://aihero.dev/skills-code-review) and before the pull request goes ready: `implement-spec → code-review → verify-spec → pull request`. It also stands alone on any built branch.

Its neighbours are [implement-spec](https://aihero.dev/skills-implement-spec), which calls it, and [tdd](https://aihero.dev/skills-tdd), whose tests it deliberately does not trust as acceptance proof. [ask-matt](https://aihero.dev/skills-ask-matt) is the router over the whole set when you are not sure which flow you are in.
