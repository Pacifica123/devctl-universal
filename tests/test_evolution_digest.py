"""`devctl zip`: эволюционный архив строится по настоящему workspace.

Запуск: python -B -m unittest discover -s tests -v
Тесты используют только стандартную библиотеку и реальный конвейер `devctl start`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

DEVCTL = Path(__file__).resolve().parents[1] / "devctl.py"

APP_V1 = '''"""Demo application."""


def greet(name):
    return f"hello {name}"


def main():
    print(greet("world"))
'''
APP_V2 = APP_V1.replace('    print(greet("world"))\n', '    print(greet("world"))\n    print(farewell("world"))\n').replace(
    "def main():", 'def farewell(name):\n    return f"bye {name}"\n\n\ndef main():'
)
APP_V4 = APP_V2.replace('return f"hello {name}"', 'return f"hello, {name}!"')
GUIDE = "# Guide\n\n## Install\n\nRun the installer.\n\n## Use\n\nCall main().\n"


def hashes(seed: str) -> str:
    rows = {f"file{index}.bin": f"{abs(hash((seed, index))):064x}"[:64] for index in range(12)}
    return json.dumps({"version": 1, "files": rows}, indent=2) + "\n"


def index_states(files: dict[str, str]) -> list[dict[str, object]]:
    index = json.loads(files["index.json"])
    return [dict(zip(index["stateColumns"], row)) for row in index["states"]]


def run_devctl(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("DEVCTL_WORKSPACE", None)
    result = subprocess.run(
        [sys.executable, "-B", str(DEVCTL), "-w", str(workspace), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=300,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"devctl {' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def make_patch(
    workspace: Path, stamp: str, slug: str, files: dict[str, str], *, delete: tuple[str, ...] = (), check: str | None = None,
    summary_md: str = "", into: str = "patches",
) -> Path:
    manifest = {
        "formatVersion": 1,
        "patchId": f"demo-{slug}",
        "title": f"Demo patch {slug}",
        "summary": f"Summary of {slug}: what changed and why.",
        "apply": {"filesRoot": "files", "delete": [{"path": path} for path in delete]},
        "checks": [{"name": "check", "cwd": ".", "command": check, "timeoutSeconds": 60}] if check else [],
        "commit": {"message": f"feat: {slug}"},
        "archive": {"nameSlug": slug},
    }
    path = workspace / into / f"patch_{stamp}_{slug}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        if summary_md:
            archive.writestr("PATCH_SUMMARY.md", summary_md)
        for name, text in files.items():
            archive.writestr(f"files/{name}", text)
    return path


def build_workspace(root: Path) -> dict[str, float]:
    """Workspace с историей: три применённых патча, один упавший, копии и посторонние материалы."""
    marks: dict[str, float] = {}
    run_devctl(root, "init", "--workspace", str(root), "--project", "project", "--create-project", "--git-init", "--branch", "main")
    project = root / "project"
    for key, value in (("user.email", "dev@example.invalid"), ("user.name", "dev"), ("core.autocrlf", "false")):
        subprocess.run(["git", "-C", str(project), "config", key, value], check=True)

    vendor = {f"lib/vendor/module_{index:02d}.py": f"VALUE_{index} = {index}\n" for index in range(30)}
    make_patch(root, "20260101_100000", "initial", {
        "app.py": APP_V1, "README.md": "# Demo\n\nFirst version.\n", "docs/guide.md": GUIDE, "data/table.json": hashes("a"), **vendor,
    })
    run_devctl(root, "start", "--no-push")
    time.sleep(1.1)

    make_patch(root, "20260102_100000", "farewell", {
        "app.py": APP_V2, "docs/manual.md": GUIDE, "data/table.json": hashes("b"),
    }, delete=("docs/guide.md",))
    run_devctl(root, "start", "--no-push")
    uts_copy = sorted((root / "UserTestSpace").glob("*farewell*/project"))[0]
    shutil.copytree(project, root / "stables" / "v1", ignore=shutil.ignore_patterns(".git"))
    for path in (root / "stables" / "v1").rglob("*"):
        if path.is_file():  # перенос на другую ОС: те же файлы с CRLF
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    time.sleep(1.1)
    marks["note"] = time.time()
    (root / "IDEAS.md").write_text("# Ideas\n\nTry a plugin system next.\n", encoding="utf-8")
    time.sleep(1.1)

    make_patch(root, "20260103_100000", "broken", {"app.py": APP_V2 + "\nBROKEN = True\n"},
               check=f'"{sys.executable}" -c "import sys; print(\'boom from check\'); sys.exit(3)"')
    failed = run_devctl(root, "start", "--no-push", check=False)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    time.sleep(1.1)

    make_patch(root, "20260104_100000", "polish", {"app.py": APP_V4}, summary_md="# Polish\n\nGreeting punctuation fixed.\n")
    run_devctl(root, "start", "--no-push")

    # Ручное тестирование в копии: правка файла, мусор сборки и новый файл.
    (uts_copy / "app.py").write_text(APP_V2 + "\nDEBUG = True\n", encoding="utf-8")
    (uts_copy / "build").mkdir()
    (uts_copy / "build" / "out.bin").write_bytes(b"\0" * 2048)
    (uts_copy / "local_notes.txt").write_text("tested manually\n", encoding="utf-8")

    make_patch(root, "20260105_100000", "never-applied", {"extra.py": "X = 1\n"}, into="patches-attic")
    (root / "refs").mkdir()
    with zipfile.ZipFile(root / "refs" / "other-project.zip", "w") as archive:
        archive.writestr("other/src/main.c", "int main(void) { return 0; }\n")
        archive.writestr("other/README", "unrelated\n")
    materials = root / "materials" / "course"
    for part in range(3):
        (materials / f"part{part}").mkdir(parents=True)
        for index in range(25):
            (materials / f"part{part}" / f"lesson_{index:02d}.pdf").write_bytes(b"%PDF-" + bytes([index]) * 64)
    (project / "README.md").write_text("# Demo\n\nFirst version.\n\nUncommitted line.\n", encoding="utf-8")
    return marks


def read_archive(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


@unittest.skipUnless(shutil.which("git"), "git is required for the pipeline")
class EvolutionDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="devctl-evo-")
        cls.root = Path(cls.tmp.name) / "demo-space"
        cls.root.mkdir()
        cls.marks = build_workspace(cls.root)
        result = run_devctl(cls.root, "zip", "--level", "max", "--json")
        cls.payload = json.loads(result.stdout.strip().splitlines()[-1])
        cls.archive_path = Path(cls.payload["archive"])
        cls.files = read_archive(cls.archive_path)
        cls.timeline = cls.files["TIMELINE.md"]
        cls.steps = {name: text for name, text in cls.files.items() if name.startswith("steps/")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def step(self, needle: str) -> str:
        matches = [text for name, text in sorted(self.steps.items()) if needle in name]
        self.assertTrue(matches, f"no step file with {needle!r}: {sorted(self.steps)}")
        return matches[0]

    def test_archive_layout_and_location(self) -> None:
        self.assertTrue(self.payload["ok"])
        self.assertEqual(self.archive_path.parent, self.root)
        self.assertIn("_evolution_", self.archive_path.name)
        self.assertLessEqual({"README.md", "TIMELINE.md", "index.json"}, set(self.files))
        index = json.loads(self.files["index.json"])
        self.assertEqual(index["devctlEvolution"], 1)
        self.assertEqual(index["devctlVersion"], self.payload["version"])
        self.assertEqual(len(index["states"]), 4)  # три патча и незакоммиченное дерево
        self.assertIn("Как читать", self.files["README.md"])

    def test_timeline_is_chronological_across_all_sections(self) -> None:
        order = [self.timeline.index(marker) for marker in (
            "ПАТЧ · demo-initial", "ПАТЧ · demo-farewell", "ФАЙЛ · `IDEAS.md`", "ЗАПУСК БЕЗ РЕЗУЛЬТАТА · demo-broken",
            "ПАТЧ · demo-polish", "РАБОЧЕЕ ДЕРЕВО",
        )]
        self.assertEqual(order, sorted(order), self.timeline)
        numbers = [int(line.split("**")[1]) for line in self.timeline.splitlines() if line.startswith("- **")]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertIn("Summary of farewell", self.timeline)

    def test_identical_trees_collapse_into_one_state(self) -> None:
        farewell = next(state for state in index_states(self.files) if state["patches"] == ["demo-farewell"])
        self.assertEqual(farewell["snapshotZips"], 3)  # свой post-архив и pre-архивы двух следующих запусков
        self.assertEqual(farewell["otherCopies"], ["stables/v1"])  # копия с CRLF — то же состояние
        dirty = farewell["dirtyCopies"]  # копия UserTestSpace, в которой правили руками, и снимок упавшего патча
        self.assertEqual(len(dirty), 2, dirty)
        self.assertTrue(any("failed_project" in path for path in dirty), dirty)
        self.assertEqual(farewell["utsCopies"], 0)
        polish = next(state for state in index_states(self.files) if state["patches"] == ["demo-polish"])
        self.assertEqual((polish["utsCopies"], polish["snapshotZips"]), (1, 1))
        self.assertIn("КОПИЯ ПРОЕКТА · `stables/v1`", self.timeline)
        self.assertIn("точная копия", self.timeline)

    def test_dirty_copy_is_described_by_its_local_changes_only(self) -> None:
        text = self.step("farewell")
        self.assertIn("Копии этого состояния с локальными изменениями", text)
        self.assertIn("+DEBUG = True", text)
        self.assertIn("local_notes.txt", text)
        self.assertNotIn("out.bin", text)  # build/ не входит в снимки devctl
        self.assertIn("не сравнивалось по правилам снимков: 1 файлов", text)

    def test_changes_are_folded_by_meaning(self) -> None:
        initial = self.step("initial")
        self.assertIn("A+ lib/ — массовое добавление: 30 файлов", initial)
        self.assertIn("def greet(name):", initial)
        farewell = self.step("farewell")
        self.assertIn("R docs/guide.md → docs/manual.md", farewell)
        self.assertIn("строк с изменившимися только хэшами: 12", farewell)
        self.assertIn("+def farewell(name):", farewell)
        self.assertNotIn("# Guide", farewell)  # переименование не повторяет содержимое
        polish = self.step("polish")
        self.assertIn("Greeting punctuation fixed.", polish)
        self.assertIn('+    return f"hello, {name}!"', polish)
        self.assertIn("- патч: `patches/patch_20260104_100000_polish.zip`", polish)
        self.assertNotIn("связь:", polish)  # связь по трейлеру коммита — обычная, отдельно не оговаривается

    def test_failed_run_unlinked_patch_and_other_materials(self) -> None:
        broken = self.step("broken")
        self.assertIn("boom from check", broken)
        self.assertIn("ошибка", broken)
        self.assertIn("ПАТЧ БЕЗ СЛЕДА ПРИМЕНЕНИЯ · demo-never-applied", self.timeline)
        self.assertIn("`refs/other-project.zip`", self.timeline)
        self.assertIn("zip: файлов 2", self.timeline)
        self.assertIn("КАТАЛОГ · `materials/course/` · 75 файлов", self.timeline)
        self.assertIn("Try a plugin system next.", self.step("ideas"))
        self.assertIn("+Uncommitted line.", self.step("worktree"))

    def test_budget_shrinks_text_and_marks_omissions(self) -> None:
        small = json.loads(run_devctl(self.root, "zip", "--budget-kb", "6", "--dry-run", "--json").stdout.strip().splitlines()[-1])
        full = self.payload["stats"]
        self.assertTrue(small["dryRun"])
        self.assertIsNone(small["archive"])
        self.assertLess(small["stats"]["textBytes"], full["textBytes"])
        self.assertLess(small["stats"]["detail"], full["detail"])
        self.assertGreater(small["stats"]["itemsCut"] + small["stats"]["itemsHidden"], 0)
        self.assertEqual(full["itemsCut"] + full["itemsHidden"], 0)
        self.assertEqual(len(list(self.root.glob("*_evolution_*.zip"))), 1)  # dry-run ничего не пишет

    def test_previous_archives_are_not_swallowed(self) -> None:
        result = run_devctl(self.root, "zip", "--level", "brief", "--with-final", "--output", str(self.root / "out") + os.sep, "--json")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        files = read_archive(Path(payload["archive"]))
        self.assertIn("Прежние эволюционные архивы в workspace (1) пропущены", files["README.md"])
        self.assertNotIn("_evolution_", files["TIMELINE.md"])
        self.assertEqual(files["final/app.py"], APP_V4)
        self.assertEqual(payload["stats"]["events"], self.payload["stats"]["events"])


@unittest.skipUnless(shutil.which("git"), "git is required for the pipeline")
class EvolutionLayoutsTest(unittest.TestCase):
    def test_history_survives_without_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devctl-evo-") as tmp:
            root = Path(tmp) / "moved-space"
            root.mkdir()
            build_workspace(root)
            shutil.rmtree(root / "project" / ".git")  # проект перенесён на другую машину без истории
            payload = json.loads(run_devctl(root, "zip", "--level", "max", "--json").stdout.strip().splitlines()[-1])
            files = read_archive(Path(payload["archive"]))
            self.assertIn("Git-истории нет", files["README.md"])
            timeline = files["TIMELINE.md"]
            order = [timeline.index(marker) for marker in (
                "ПАТЧ · demo-initial", "ПАТЧ · demo-farewell", "ФАЙЛ · `IDEAS.md`", "ЗАПУСК БЕЗ РЕЗУЛЬТАТА · demo-broken",
                "ПАТЧ · demo-polish", "РАБОЧЕЕ ДЕРЕВО",
            )]
            self.assertEqual(order, sorted(order), timeline)
            self.assertNotIn("СОСТОЯНИЕ НЕ СОХРАНИЛОСЬ", timeline)  # у каждого применённого патча нашлось состояние
            self.assertEqual([state["kind"] for state in index_states(files)], ["snapshot", "snapshot", "snapshot", "worktree"])
            joined = "\n".join(text for name, text in files.items() if name.startswith("steps/"))
            self.assertIn("+def farewell(name):", joined)
            self.assertIn("совпадение содержимого", joined)  # патч привязан без трейлеров
            self.assertIn("failed_project", joined)  # откатанный патч виден как копия с локальными изменениями
            self.assertIn("+BROKEN = True", joined)

    def test_history_survives_without_git_and_without_run_archives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devctl-evo-") as tmp:
            root = Path(tmp) / "pruned-space"
            root.mkdir()
            build_workspace(root)
            shutil.rmtree(root / "project" / ".git")
            for run_dir in (root / "archives").iterdir():  # владелец вычистил archives/
                shutil.rmtree(run_dir)
            edited = sorted((root / "UserTestSpace").glob("*farewell*/project"))[0] / "app.py"
            later = time.time() + 3600  # в копии работали руками заметно позже её создания
            os.utime(edited, (later, later))
            payload = json.loads(run_devctl(root, "zip", "--level", "max", "--json").stdout.strip().splitlines()[-1])
            files = read_archive(Path(payload["archive"]))
            timeline = files["TIMELINE.md"]
            states = index_states(files)
            # Цепочку держат чистые копии UserTestSpace; копия с правками звеном не становится.
            self.assertEqual([state["kind"] for state in states], ["snapshot", "snapshot", "worktree"])
            self.assertIn("ПАТЧ · demo-initial", timeline)
            self.assertIn("ПАТЧ · demo-polish", timeline)
            self.assertIn("ПАТЧ ПРИМЕНЁН, СОСТОЯНИЕ НЕ СОХРАНИЛОСЬ · demo-farewell", timeline)
            self.assertIn("ЗАПУСК БЕЗ РЕЗУЛЬТАТА · demo-broken", timeline)
            joined = "\n".join(text for name, text in files.items() if name.startswith("steps/"))
            self.assertIn("каталог запуска удалён", joined)
            self.assertIn("+DEBUG = True", joined)

    def test_zip_requires_an_initialized_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devctl-evo-") as tmp:
            project = Path(tmp) / "somewhere" / "repo"
            project.mkdir(parents=True)
            (project / "README.md").write_text("# plain project\n", encoding="utf-8")
            result = run_devctl(project, "zip", check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(".devctl/workspace.json", result.stdout)
            self.assertEqual(list(Path(tmp).rglob("*_evolution_*.zip")), [])

    def test_workspace_that_is_its_own_project_stays_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devctl-evo-") as tmp:
            root = Path(tmp) / "selfhosted"
            root.mkdir()
            run_devctl(root, "init", "--workspace", str(root), "--project", ".", "--git-init", "--branch", "main")
            for key, value in (("user.email", "dev@example.invalid"), ("user.name", "dev")):
                subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
            (root / ".gitignore").write_text("patches/\narchives/\nUserTestSpace/\n.devctl/state.json\nscratch/\n", encoding="utf-8")
            (root / "tool.py").write_text("print('tool')\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "initial"], check=True)
            make_patch(root, "20260101_100000", "docs", {"README.md": "# Tool\n"})
            run_devctl(root, "start", "--no-push")
            (root / "scratch").mkdir()
            (root / "scratch" / "todo.txt").write_text("ship it\n", encoding="utf-8")
            payload = json.loads(run_devctl(root, "zip", "--json").stdout.strip().splitlines()[-1])
            archive = Path(payload["archive"])
            self.assertEqual(archive.parent, root / "archives")
            status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout, "")
            files = read_archive(archive)
            self.assertIn("ПАТЧ · demo-docs", files["TIMELINE.md"])
            self.assertIn("`scratch/todo.txt`", files["TIMELINE.md"])
            self.assertNotIn("ФАЙЛ · `tool.py`", files["TIMELINE.md"])  # файлы проекта не считаются посторонними
            self.assertEqual(json.loads(files["index.json"])["stats"]["eventKinds"]["step"], 2)


if __name__ == "__main__":
    unittest.main()
