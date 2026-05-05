# zapret2-gui Stage 0 workspace

This workspace is the bootstrap state for the Linux MVP of `zapret2-gui`.

## Layout

```text
workspace/
  .devctl/
    workspace.json      # active devctl config
    state.json          # devctl run history
  workspace.json        # human-readable mirror of the active config
  devctl.py             # project-agnostic patch conveyor
  project/              # Git repository for zapret2-gui sources/docs
  patches/              # incoming patch zips
  archives/             # source anchors, devctl snapshots, run reports
```

## Stage 0 rule

Future project changes should be delivered as patch zips in `patches/` and applied with:

```bash
python3 devctl.py plan
python3 devctl.py start
```

Every patch proposal should explicitly answer: how does this relate to the real source anchor in `archives/zapret-main.zip`?

## Current bootstrap decisions

- `archives/zapret-main.zip` is the mandatory product/source anchor.
- `archives/docs.zip` and `archives/reference/*` are supplemental references.
- `project/` is already initialized as a Git repository on branch `main`.
- `devctl start` is intended as the magic button: apply patch, run checks, commit, and push.
- Use `python3 devctl.py start --no-push` only for explicit local/debug runs.
- `.devctl/workspace.json` owns workspace-level Git policy (`autoCommit`, `autoPush`, remote, branch/up-to-date rules).

## Useful commands

```bash
python3 devctl.py status
python3 devctl.py inspect
python3 devctl.py plan
python3 devctl.py start
cd project && git status -sb
```

See `project/docs/STAGE0_EXECUTION_REPORT.md` and `project/docs/STAGE0_POSTDESIGN.md` for the executed Stage 0 design decisions.
