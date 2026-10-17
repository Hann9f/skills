---
"mattpocock-skills": minor
---

New `verify-spec` skill: independent acceptance proof for a built branch. A fresh verifier checks every criterion of the accepted spec against the running product, a separate auditor challenges the evidence, and a script computes the verdict and enforces the fix and retry limits. `implement-spec` now calls it after `code-review` and before the PR goes ready or any ticket closes, and `ask-matt` routes to it.
