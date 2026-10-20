"""devctl 0.10.0: workspace в пачке называется репозиторием, а не id реестра конкретной машины.

Запуск: python -B -m unittest discover -s tests -v
Две «машины» — два HOME со своими реестрами и разными id одного и того же репозитория.
Remote записан как https://github.com/…, а git ходит в локальный bare-репозиторий через insteadOf
из GIT_CONFIG_GLOBAL, поэтому сеть не нужна.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_batch_and_queue import DEVCTL, DevctlCase, patch_bytes, write_batch  # noqa: E402

REPO_URL = "https://github.com/Example/Shared-Repo.git"


def load_devctl_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("devctl_under_test", DEVCTL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RepoKeyTests(unittest.TestCase):
    def test_remote_urls_of_one_repo_give_one_key(self) -> None:
        devctl = load_devctl_module()
        key = "github.com/example/shared-repo"
        for url in (
            "https://github.com/Example/Shared-Repo.git",
            "https://token@github.com/Example/Shared-Repo",
            "git@github.com:Example/Shared-Repo.git",
            "ssh://git@github.com:22/Example/Shared-Repo.git/",
        ):
            self.assertEqual(devctl.repo_key_from_url(url), key, url)
        for url in ("/srv/git/shared.git", "file:///srv/git/shared.git", "C:\\git\\shared.git", "../shared"):
            self.assertIsNone(devctl.repo_key_from_url(url), url)
        self.assertEqual(devctl.normalize_repo_ref("Example/Shared-Repo"), key)
        self.assertEqual(devctl.normalize_repo_ref("github.com/Example/Shared-Repo"), key)
        self.assertIsNone(devctl.normalize_repo_ref("p2p-ecosystem"))
        self.assertEqual(devctl.repo_short(key), "example/shared-repo")


class RepoIdentityBatchTests(DevctlCase):
    def setUp(self) -> None:
        super().setUp()
        self.bare = self.root / "shared.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(self.bare))
        self.machines: dict[str, Path] = {}

    def use_machine(self, name: str) -> None:
        home = self.root / f"home-{name}"
        if not home.exists():
            home.mkdir()
            (home / ".gitconfig").write_text(
                f'[url "{self.bare.as_posix()}"]\n\tinsteadOf = {REPO_URL}\n', encoding="utf-8"
            )
        self.home = home

    def clone_workspace(self, machine: str, ws_name: str, ws_id: str) -> Path:
        """Workspace на «машине» machine с локальным id ws_id и origin = REPO_URL."""
        self.use_machine(machine)
        ws = self.root / machine / ws_name
        self.devctl("init", "--workspace", str(ws), "--project", "project", "--create-project", "--git-init", "--branch", "main")
        project = ws / "project"
        for key, value in (("user.email", "dev@example.invalid"), ("user.name", "dev"), ("core.autocrlf", "false")):
            self.git(project, "config", key, value)
        self.git(project, "remote", "add", "origin", REPO_URL)
        if self.git(self.root, "--git-dir", str(self.bare), "for-each-ref", "refs/heads/main"):
            self.git(project, "fetch", "origin")
            self.git(project, "reset", "--hard", "origin/main")
        else:
            (project / "README.md").write_text("# shared\n", encoding="utf-8")
            self.git(project, "add", "-A")
            self.git(project, "commit", "-m", "init")
            self.git(project, "push", "origin", "HEAD:main")
        self.git(project, "branch", "--set-upstream-to=origin/main", "main")
        self.devctl("workspace", "register", str(ws), "--id", ws_id)
        return ws

    def head(self, ws: Path) -> str:
        return self.git(ws / "project", "rev-parse", "HEAD")

    def make_batch(self, path: Path, item: dict) -> Path:
        batch = {"formatVersion": 1, "batchId": "shared-batch", "requiresDevctl": ">=0.10.0", "push": "after-all",
                 "items": [{"patch": "patches/patch_1_a.zip", **item}]}
        return write_batch(path, batch, {"patch_1_a.zip": patch_bytes("a", {"a.txt": "a\n"})})

    def test_same_batch_plans_on_two_machines_with_different_ids(self) -> None:
        laptop = self.clone_workspace("laptop", "P2P-ECOSYSTEM", "p2p-ecosystem")
        pc = self.clone_workspace("pc", "P2PKANBAN_ECOSYSTEM", "p2pkanban_ecosystem")
        batch = self.make_batch(self.root / "batch.zip", {"repo": "Example/Shared-Repo", "expectedHead": self.head(laptop)})

        for machine, ws, ws_id in (("laptop", laptop, "p2p-ecosystem"), ("pc", pc, "p2pkanban_ecosystem")):
            self.use_machine(machine)
            plan = json.loads(self.devctl("batch", "plan", str(batch), "--json").stdout)
            self.assertTrue(plan["ok"], plan["problems"])
            step = plan["steps"][0]
            self.assertEqual(step["workspace"], ws_id)
            self.assertEqual(Path(step["workspacePath"]).name, ws.name)
            self.assertIn("example/shared-repo", step["resolvedBy"])

        self.use_machine("pc")
        self.devctl("batch", "start", str(batch))
        self.assertEqual(self.git(self.root, "--git-dir", str(self.bare), "rev-parse", "main"), self.head(pc))

        # Ноутбук догоняет remote; та же пачка там уже применена и только помечается.
        self.use_machine("laptop")
        self.git(laptop / "project", "pull", "--ff-only", "origin", "main")
        plan = json.loads(self.devctl("batch", "plan", str(batch), "--json").stdout)
        self.assertEqual(plan["steps"][0]["status"], "already_applied")

    def test_workspace_field_with_owner_repo_and_list_for_agent(self) -> None:
        ws = self.clone_workspace("laptop", "WS", "local-alias")
        batch = self.make_batch(self.root / "batch.zip", {"workspace": "github.com/example/shared-repo"})
        plan = json.loads(self.devctl("batch", "plan", str(batch), "--json").stdout)
        self.assertTrue(plan["ok"], plan["problems"])
        self.assertEqual(plan["steps"][0]["workspace"], "local-alias")

        text = self.devctl("workspace", "list", "--for-agent").stdout
        self.assertIn("## example/shared-repo", text)
        self.assertIn("id на этой машине: local-alias", text)
        listing = json.loads(self.devctl("workspace", "list", "--json").stdout)
        self.assertEqual(listing["workspaces"][0]["repo"], "example/shared-repo")
        self.assertEqual(self.head(ws), listing["workspaces"][0]["head"])

    def test_old_local_id_still_works_and_mismatch_is_reported(self) -> None:
        self.clone_workspace("laptop", "WS", "p2p-ecosystem")
        old_style = self.make_batch(self.root / "old.zip", {"workspace": "p2p-ecosystem"})
        self.assertTrue(json.loads(self.devctl("batch", "plan", str(old_style), "--json").stdout)["ok"])

        other_machine = self.make_batch(self.root / "pc.zip", {"workspace": "p2pkanban_ecosystem"})
        result = self.devctl("batch", "plan", str(other_machine), "--json", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("p2pkanban_ecosystem", json.loads(result.stdout)["problems"][0])

        wrong = self.make_batch(self.root / "wrong.zip", {"repo": "Example/Other", "workspace": "p2p-ecosystem"})
        problems = json.loads(self.devctl("batch", "plan", str(wrong), "--json", check=False).stdout)["problems"]
        self.assertIn("example/other", problems[0])

    def test_two_clones_of_one_repo_need_a_local_id(self) -> None:
        self.clone_workspace("laptop", "first", "first")
        self.clone_workspace("laptop", "second", "second")
        ambiguous = self.make_batch(self.root / "a.zip", {"repo": "Example/Shared-Repo"})
        problems = json.loads(self.devctl("batch", "plan", str(ambiguous), "--json", check=False).stdout)["problems"]
        self.assertIn("несколько раз", problems[0])
        exact = self.make_batch(self.root / "b.zip", {"repo": "Example/Shared-Repo", "workspace": "second"})
        plan = json.loads(self.devctl("batch", "plan", str(exact), "--json").stdout)
        self.assertEqual(plan["steps"][0]["workspace"], "second")

    def test_old_devctl_refuses_repo_batches_honestly(self) -> None:
        self.clone_workspace("laptop", "WS", "ws")
        batch = write_batch(self.root / "bad.zip", {"formatVersion": 1, "batchId": "x", "items": [
            {"patch": "patches/p.zip"}]}, {"p.zip": patch_bytes("p", {"p.txt": "p\n"})})
        result = self.devctl("batch", "plan", str(batch), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repo", result.stdout + result.stderr)

    def test_inbox_routes_patch_by_target_repo(self) -> None:
        self.clone_workspace("laptop", "WS", "laptop-id")
        inbox = self.root / "inbox"
        self.devctl("inbox", "init", "--path", str(inbox))
        manifest_target = {"repo": "Example/Shared-Repo", "expectedFiles": ["README.md"]}
        data = patch_bytes("routed", {"README.md": "# routed\n"})
        import io
        import zipfile
        source = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(source.read("manifest.json"))
        manifest["target"] = manifest_target
        target_zip = inbox / "incoming" / "patch_20260101_000000_routed.zip"
        with zipfile.ZipFile(target_zip, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("files/README.md", "# routed\n")
        scan = json.loads(self.devctl("inbox", "scan", "--json").stdout)
        detected = scan["patches"]["items"][0]["target"]
        self.assertEqual(detected["confidence"], "high")
        self.assertEqual(detected["workspaceId"], "laptop-id")


if __name__ == "__main__":
    unittest.main()
