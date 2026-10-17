# Fork notes

This is Hann9f's fork of mattpocock/skills. It is upstream plus one change: the `verify-spec` skill, its call from `implement-spec` step 8, and one clause in `ask-matt`. Everything else tracks upstream.

## Install on a machine

```sh
python3 scripts/fork-sync.py          # clone or fast-forward, then link into every harness found
python3 scripts/fork-sync.py --check  # change nothing, exit 1 on drift
```

It needs Python 3.8 or newer. Without git it downloads the main branch tarball. Re-run it to update; it is safe to repeat. Older copies of a skill are moved to `~/.skill-sync-backup/`, never deleted.

## Take upstream changes

```sh
git fetch upstream
git merge upstream/main
```

A conflict can only land in the edited spots: `implement-spec` step 8 onward, one sentence in `ask-matt`, and the README and plugin manifest lines for `verify-spec`. Re-read `ask-matt` after the merge so the router stays accurate, then push `main`.
