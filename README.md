# devctl universal v0.3

Project-agnostic pure-Python AI patch conveyor.

`devctl` applies AI-generated `patch.zip` archives to any project inside a workspace, runs declared checks, writes reports/logs, creates pre/post/failed snapshots, commits, pushes, and records state.

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
    patch_YYYYMMDD_HHMMSS_slug.zip
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
python3 devctl.py inspect patches/patch_20260505_120000_demo.zip
python3 devctl.py plan
```

`inspect` and `plan` never modify the project.

## Run conveyor

```bash
python3 devctl.py start
```

The current v0.3 flow:

1. discover workspace/project;
2. find the latest unapplied patch zip;
3. validate `manifest.json` and zip paths;
4. check Git preflight;
5. create pre-archive;
6. apply deletions and file overlay;
7. run manifest checks;
8. commit/push according to manifest;
9. create post/failed archive;
10. write report and update `.devctl/state.json`.

## Patch zip format

```text
patch_YYYYMMDD_HHMMSS_slug.zip
  manifest.json
  files/
    path/inside/project.ext
  README.patch.md   optional
```

See `docs/patch-manifest.example.json`.

## Notes

- Pure Python standard library only.
- Paths in manifest must be POSIX-style relative paths.
- Dangerous paths such as `.git`, `.devctl`, `node_modules`, and `target` are blocked for patch writes/deletions.
- `devctl` no longer knows anything about `p2p_planner`; the project is selected through `.devctl/workspace.json`.
