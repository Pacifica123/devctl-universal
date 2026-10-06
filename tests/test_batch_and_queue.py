"""devctl 0.9.0: очередь по базе, env-шаблоны, cwd после наложения, реестр машины и пачки патчей.

Запуск: python -B -m unittest discover -s tests -v
Каждый тест строит настоящие workspace через `devctl init`/`start` в своём HOME,
поэтому пользовательский конфиг (~/.config/devctl) не затрагивается.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

DEVCTL = Path(__file__).resolve().parents[1] / "devctl.py"


class DevctlCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def devctl(self, *args: str, workspace: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"))
        env.pop("DEVCTL_WORKSPACE", None)
        env.pop("APPDATA", None)
        prefix = ["-w", str(workspace)] if workspace else []
        result = subprocess.run(
            [sys.executable, "-B", str(DEVCTL), *prefix, *args],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=300, cwd=str(self.root),
        )
        if check and result.returncode != 0:
            raise AssertionError(f"devctl {' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result

    def git(self, cwd: Path, *args: str) -> str:
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"))
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env).stdout.strip()

    def make_workspace(self, name: str, *, remote: bool = False) -> Path:
        ws = self.root / name
        self.devctl("init", "--workspace", str(ws), "--project", "project", "--create-project", "--git-init", "--branch", "main")
        project = ws / "project"
        for key, value in (("user.email", "dev@example.invalid"), ("user.name", "dev"), ("core.autocrlf", "false")):
            self.git(project, "config", key, value)
        (project / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        self.git(project, "add", "-A")
        self.git(project, "commit", "-m", "init")
        if remote:
            bare = self.root / f"{name}.git"
            self.git(self.root, "init", "--bare", "-b", "main", str(bare))
            self.git(project, "remote", "add", "origin", str(bare))
            self.git(project, "push", "origin", "HEAD:main")
            self.git(project, "branch", "--set-upstream-to=origin/main", "main")
        else:
            cfg_path = ws / ".devctl" / "workspace.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.setdefault("git", {})["autoPush"] = False
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return ws


def patch_bytes(patch_id: str, files: dict[str, str], *, base: dict | None = None, checks: list[dict] | None = None) -> bytes:
    manifest = {
        "formatVersion": 1,
        "patchId": patch_id,
        "title": f"Patch {patch_id}",
        "summary": f"Summary of {patch_id}.",
        "apply": {"filesRoot": "files", "delete": []},
        "checks": checks or [],
        "commit": {"message": f"feat: {patch_id}"},
    }
    if base is not None:
        manifest["base"] = base
    path = Path(tempfile.mkdtemp()) / "p.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, text in files.items():
            archive.writestr(f"files/{name}", text)
    return path.read_bytes()


def write_patch(ws: Path, name: str, data: bytes) -> Path:
    path = ws / "patches" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_batch(path: Path, batch: dict, patches: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("batch.json", json.dumps(batch, ensure_ascii=False))
        for name, data in patches.items():
            archive.writestr(f"patches/{name}", data)
    return path


class QueueAndSafetyTests(DevctlCase):
    def test_after_dependency_applies_first_even_when_dropped_later(self) -> None:
        ws = self.make_workspace("ws")
        second = write_patch(ws, "patch_20260101_000002_b.zip", patch_bytes("b", {"b.txt": "b\n"}, base={"after": "a"}))
        time.sleep(1.1)
        first = write_patch(ws, "patch_20260101_000001_a.zip", patch_bytes("a", {"a.txt": "a\n"}))
        os.utime(first, (time.time() - 100, time.time() - 100))  # a старше по mtime, b новее
        os.utime(second, None)
        self.devctl("start", "--no-push", workspace=ws)
        self.devctl("start", "--no-push", workspace=ws)
        log = self.git(ws / "project", "log", "--format=%s")
        self.assertEqual(log.splitlines()[:2], ["feat: b", "feat: a"])

    def test_missing_dependency_and_expected_head_block_start(self) -> None:
        ws = self.make_workspace("ws")
        write_patch(ws, "patch_20260101_000001_c.zip", patch_bytes("c", {"c.txt": "c\n"}, base={"after": ["absent"]}))
        result = self.devctl("start", "--no-push", workspace=ws, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("absent", result.stdout)
        (ws / "patches" / "patch_20260101_000001_c.zip").unlink()

        write_patch(ws, "patch_20260101_000002_d.zip", patch_bytes("d", {"d.txt": "d\n"}, base={"expectedHead": "0" * 40}))
        result = self.devctl("start", "--no-push", workspace=ws, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("HEAD", result.stdout)
        head = self.git(ws / "project", "rev-parse", "HEAD")
        (ws / "patches" / "patch_20260101_000002_d.zip").unlink()
        write_patch(ws, "patch_20260101_000003_e.zip", patch_bytes("e", {"e.txt": "e\n"}, base={"expectedHead": head[:10]}))
        self.devctl("start", "--no-push", workspace=ws)

    def test_env_templates_are_allowed_and_secrets_are_not(self) -> None:
        ws = self.make_workspace("ws")
        write_patch(ws, "patch_20260101_000001_env.zip", patch_bytes("env", {"backend/.env.example": "PORT=8080\n"}))
        self.devctl("start", "--no-push", workspace=ws)
        tracked = self.git(ws / "project", "ls-files")
        self.assertIn("backend/.env.example", tracked)

        write_patch(ws, "patch_20260101_000002_secret.zip", patch_bytes("secret", {"backend/.env.local": "TOKEN=x\n"}))
        result = self.devctl("start", "--no-push", workspace=ws, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((ws / "project" / "backend" / ".env.local").exists())

    def test_check_cwd_may_be_created_by_the_patch(self) -> None:
        ws = self.make_workspace("ws")
        check = {"name": "new dir", "cwd": "newpkg", "command": f'"{sys.executable}" -c "import os; assert os.path.exists(\'mod.py\')"'}
        write_patch(ws, "patch_20260101_000001_new.zip", patch_bytes("new", {"newpkg/mod.py": "X = 1\n"}, checks=[check]))
        self.devctl("start", "--no-push", workspace=ws)

        bad = {"name": "missing dir", "cwd": "nowhere", "command": "true"}
        write_patch(ws, "patch_20260101_000002_bad.zip", patch_bytes("bad", {"x.txt": "x\n"}, checks=[bad]))
        result = self.devctl("start", "--no-push", workspace=ws, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("nowhere", result.stdout)


class WorkspaceRegistryTests(DevctlCase):
    def test_scan_registers_and_list_for_agent_reports_heads(self) -> None:
        alpha = self.make_workspace("alpha")
        self.make_workspace("beta")
        (self.root / "alpha" / "UserTestSpace" / "copy" / ".devctl").mkdir(parents=True)
        (self.root / "alpha" / "UserTestSpace" / "copy" / ".devctl" / "workspace.json").write_text("{}", encoding="utf-8")
        found = json.loads(self.devctl("workspace", "scan", str(self.root), "--register", "--json").stdout)
        paths = sorted(Path(item["path"]).name for item in found["workspaces"])
        self.assertEqual(paths, ["alpha", "beta"])  # копии в UserTestSpace не считаются
        listing = json.loads(self.devctl("workspace", "list", "--json").stdout)
        heads = {item["id"]: item["head"] for item in listing["workspaces"]}
        self.assertEqual(heads["alpha"], self.git(alpha / "project", "rev-parse", "HEAD"))
        text = self.devctl("workspace", "list", "--for-agent").stdout
        self.assertIn("## alpha", text)
        self.assertIn("batch_*.zip", text)


class BatchTests(DevctlCase):
    def setUp(self) -> None:
        super().setUp()
        self.web = self.make_workspace("web", remote=True)
        self.lib = self.make_workspace("lib", remote=True)
        self.devctl("workspace", "scan", str(self.root), "--register")
        self.inbox = self.root / "inbox"
        self.devctl("inbox", "init", "--path", str(self.inbox))

    def heads(self) -> tuple[str, str]:
        return (self.git(self.web / "project", "rev-parse", "HEAD"), self.git(self.lib / "project", "rev-parse", "HEAD"))

    def remote_head(self, name: str) -> str:
        return self.git(self.root / f"{name}.git", "rev-parse", "main")

    def make_batch(self, *, broken: bool = False) -> Path:
        web_head, lib_head = self.heads()
        failing = [{"name": "fails", "cwd": ".", "command": f'"{sys.executable}" -c "raise SystemExit(3)"'}] if broken else None
        patches = {
            "patch_1_web-a.zip": patch_bytes("web-a", {"a.txt": "a\n"}),
            "patch_2_lib-x.zip": patch_bytes("lib-x", {"x.txt": "x\n"}),
            "patch_3_web-b.zip": patch_bytes("web-b", {"b.txt": "b\n"}, base={"after": "web-a"}, checks=failing),
        }
        batch = {
            "formatVersion": 1, "batchId": "demo-batch", "title": "Demo", "requiresDevctl": ">=0.9.0",
            "push": "after-all",
            "items": [
                {"patch": "patches/patch_1_web-a.zip", "workspace": "web", "expectedHead": web_head},
                {"patch": "patches/patch_2_lib-x.zip", "workspace": "lib", "expectedHead": lib_head[:12]},
                {"patch": "patches/patch_3_web-b.zip", "workspace": "web"},
            ],
        }
        return write_batch(self.inbox / "incoming" / "batch_20260101_000000_demo.zip", batch, patches)

    def test_plan_changes_nothing_and_start_pushes_after_all(self) -> None:
        self.make_batch()
        before = self.heads()
        plan = json.loads(self.devctl("batch", "plan", "--json").stdout)
        self.assertTrue(plan["ok"], plan["problems"])
        self.assertEqual(self.heads(), before)
        self.assertEqual(list((self.web / "patches").glob("*.zip")), [])
        scan = json.loads(self.devctl("inbox", "scan", "--json").stdout)
        self.assertEqual(scan["patches"]["count"], 0)  # пачка не выглядит битым патчем

        self.devctl("batch", "start")
        log = self.git(self.web / "project", "log", "--format=%s")
        self.assertEqual(log.splitlines()[:2], ["feat: web-b", "feat: web-a"])
        self.assertEqual(self.remote_head("web"), self.heads()[0])
        self.assertEqual(self.remote_head("lib"), self.heads()[1])
        message = self.git(self.lib / "project", "log", "-1", "--format=%B")
        self.assertIn("Devctl-Batch: demo-batch", message)
        status = json.loads(self.devctl("batch", "status", "--json").stdout)
        self.assertEqual(status["batch"]["status"], "applied")
        self.assertTrue((self.inbox / "imported" / "batch_20260101_000000_demo.zip").exists())

    def test_stale_base_blocks_whole_batch(self) -> None:
        batch_path = self.make_batch()
        (self.lib / "project" / "late.txt").write_text("late\n", encoding="utf-8")
        self.git(self.lib / "project", "add", "-A")
        self.git(self.lib / "project", "commit", "-m", "late")
        self.git(self.lib / "project", "push", "origin", "HEAD:main")  # кто-то продвинул lib после сборки пачки
        before = self.heads()
        result = self.devctl("batch", "start", str(batch_path), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("пачка собрана против", result.stdout)
        self.assertEqual(self.heads(), before)

    def test_failure_stops_without_push_and_reset_restores_heads(self) -> None:
        batch_path = self.make_batch(broken=True)
        before = self.heads()
        result = self.devctl("batch", "start", str(batch_path), check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.remote_head("web"), before[0])
        self.assertEqual(self.remote_head("lib"), before[1])
        self.assertNotEqual(self.heads(), before)  # web-a и lib-x закоммичены локально

        # Повторный запуск: применённые пропускаются, локальные коммиты этой пачки не мешают предполётной проверке.
        again = self.devctl("batch", "start", str(batch_path), check=False)
        self.assertEqual(again.returncode, 1)
        self.assertNotIn("[ПРОБЛЕМА]", again.stdout)
        self.assertEqual(self.remote_head("web"), before[0])

        plan_only = self.devctl("batch", "reset")
        self.assertIn("--yes", plan_only.stdout)
        self.devctl("batch", "reset", "--yes")
        self.assertEqual(self.heads(), before)
        status = json.loads(self.devctl("batch", "status", "--json").stdout)
        self.assertEqual(status["batch"]["status"], "reverted")
        # После отката та же (исправленная) пачка применяется заново.
        batch_path.unlink()
        fixed = self.make_batch()
        self.devctl("batch", "start", str(fixed))
        self.assertEqual(self.remote_head("web"), self.heads()[0])

    def test_requires_newer_devctl_is_refused(self) -> None:
        batch = {"formatVersion": 1, "batchId": "future", "requiresDevctl": ">=99.0.0",
                 "items": [{"patch": "patches/p.zip", "workspace": "web"}]}
        path = write_batch(self.root / "future.zip", batch, {"p.zip": patch_bytes("f", {"f.txt": "f\n"})})
        result = self.devctl("batch", "plan", str(path), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("99.0.0", result.stdout)


if __name__ == "__main__":
    unittest.main()
