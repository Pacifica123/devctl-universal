# devctl universal v0.4

Project-agnostic pure-Python AI patch conveyor.

`devctl` applies AI-generated patch archives to a project inside a workspace, runs declared checks, writes reports/logs, creates pre/post/failed snapshots, commits, pushes, and records state.

## Core idea

`devctl start` is the magic button:

```text
apply latest unapplied patch -> run checks -> commit -> push
```

Patch manifests describe the patch content and checks. They should not be used as the normal control surface for whether the conveyor commits or pushes. A manifest may still provide a commit message or push target, but the default workflow is owned by `devctl` and the workspace.

Use `devctl start --no-push` only for explicit local-only/debug runs.

## Workspace layout

```text
workspace/
  .devctl/
    workspace.json
    state.json
  project/
    .git/
    ... any project ...
  patches/
    YYYY-MM-DD-stageN-title.devctl.patch.zip
  archives/
    ... run artifacts ...
```

## Bootstrap

```bash
cd /path/to/workspace
python3 devctl.py init --project ./project
python3 devctl.py status
```

`init` creates `.devctl/workspace.json`, `patches/`, `archives/`, and an empty state registry.

## Read-only commands

```bash
python3 devctl.py status
python3 devctl.py inspect
python3 devctl.py inspect patches/2026-05-05-stage2-config-parser-patcher-v2.devctl.patch.zip
python3 devctl.py plan
```

`inspect` and `plan` never modify the project.

## Run conveyor

```bash
python3 devctl.py start
```

The v0.4 default flow:

1. discover workspace/project;
2. find the latest unapplied patch zip;
3. validate `manifest.json` and zip paths;
4. check Git preflight and push target;
5. create pre-archive;
6. apply deletions and file overlay;
7. run manifest checks;
8. commit after green checks;
9. push after successful commit;
10. create post/failed archive;
11. write report and update `.devctl/state.json`.

Local-only escape hatch:

```bash
python3 devctl.py start --no-push
```

## Git policy

Workspace defaults:

```json
{
  "git": {
    "enabled": true,
    "autoCommit": true,
    "autoPush": true,
    "remote": "origin",
    "requireClean": true,
    "requireUpToDate": true
  }
}
```

Policy priority:

1. `devctl start --no-push` disables only the push step for a deliberate local/debug run.
2. `.devctl/workspace.json` owns the default workflow policy.
3. `manifest.commit.message`, `manifest.push.remote`, and `manifest.push.branch` may provide metadata/target.
4. `manifest.commit.enabled=false` and `manifest.push.enabled=false` are ignored by normal `start`, with a warning, because the conveyor default is commit+push after checks.

## Patch zip format

```text
YYYY-MM-DD-stageN-title.devctl.patch.zip
  manifest.json
  files/
    path/inside/project.ext
  PATCH_SUMMARY.md      optional
  reports/              optional
```

See `docs/patch-manifest.example.json`.

## Notes

- Pure Python standard library only.
- Paths in manifest must be POSIX-style relative paths.
- Dangerous paths such as `.git`, `.devctl`, `node_modules`, `target`, `__pycache__`, and `*.pyc` are blocked for patch writes/deletions or commits.
- `devctl` is project-agnostic; the project is selected through `.devctl/workspace.json`.
