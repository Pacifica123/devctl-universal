#!/usr/bin/env python3
"""
devctl v0.4 — проектно-независимый конвейер применения ИИ-патчей на чистом Python.

Базовый поток конвейера: применить патч -> выполнить проверки -> создать коммит -> отправить в remote.

Команды:
    python tools/devctl.py init --project ./project
    python tools/devctl.py status
    python tools/devctl.py inspect
    python tools/devctl.py plan
    python tools/devctl.py start

Инструмент намеренно использует только стандартную библиотеку Python.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEVCTL_VERSION = "0.4"
STATE_VERSION = 1
DEFAULT_PROJECT_DIR_NAME = "project"
DEFAULT_PATCHES_DIR_NAME = "patches"
DEFAULT_ARCHIVES_DIR_NAME = "archives"
LEGACY_ARCHIVES_DIR_ALIASES = ("arhives",)
PATCH_FILENAME_RE = re.compile(r"patch_(\d{8})_(\d{6})(?:_.*)?\.zip$", re.IGNORECASE)

BANNED_PATH_PARTS = {".git", ".devctl", "target", "node_modules"}
ARCHIVE_EXCLUDED_PARTS = {
    ".git",
    "target",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "logs",
    "tmp",
    "__pycache__",
}
ARCHIVE_EXCLUDED_SUFFIXES = (".db", ".sqlite", ".sqlite3")
RELEASE_DIR_NAME = "release"
RELEASE_ARCHIVE_PAYLOAD_SUFFIXES = (".zip",)
RELEASE_EXECUTABLE_PAYLOAD_SUFFIXES = (".exe",)
RELEASE_ZIP_PLACEHOLDER = "тут_был_zip_архив.txt"
RELEASE_EXE_PLACEHOLDER = "тут_был_экзешник.txt"
ARCHIVE_SIZE_WARNING_BYTES = 100 * 1024 * 1024
DANGEROUS_GIT_PATH_SUFFIXES = ARCHIVE_EXCLUDED_SUFFIXES + (".pyc", ".pyo")
DANGEROUS_GIT_PATH_PARTS = {"node_modules", "target", ".git", "__pycache__"}


class DevctlError(Exception):
    """Базовая ожидаемая ошибка devctl."""


class PreflightError(DevctlError):
    """Проверка окружения или Git не прошла до применения патча."""


class InvalidPatchError(DevctlError):
    """Архив патча или его манифест некорректен либо небезопасен."""


class CheckFailedError(DevctlError):
    """Одна из проверок из манифеста не прошла после применения патча."""


@dataclass
class CommandResult:
    args: list[str] | str
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


@dataclass
class CheckResult:
    name: str
    command: str
    cwd: str
    status: str
    returncode: int | None = None
    duration_seconds: float | None = None
    log_path: str | None = None
    error: str | None = None


@dataclass
class PatchCandidate:
    path: Path
    sha256: str | None = None
    manifest: dict[str, Any] | None = None
    manifest_error: str | None = None
    sort_key: tuple[int, float] = (0, 0.0)

    @property
    def patch_id(self) -> str | None:
        if isinstance(self.manifest, dict):
            value = self.manifest.get("patchId")
            if isinstance(value, str):
                return value
        return None

    @property
    def title(self) -> str | None:
        if isinstance(self.manifest, dict):
            value = self.manifest.get("title")
            if isinstance(value, str):
                return value
        return None


@dataclass
class Workspace:
    project_root: Path
    workspace_root: Path
    patches_dir: Path
    archives_dir: Path
    state_dir: Path
    state_file: Path


@dataclass
class RunContext:
    workspace: Workspace
    patch: PatchCandidate
    manifest: dict[str, Any]
    started_at: datetime
    status: str = "running"
    run_dir: Path | None = None
    logs_dir: Path | None = None
    report_path: Path | None = None
    pre_archive: Path | None = None
    post_archive: Path | None = None
    failed_archive: Path | None = None
    commit_sha: str | None = None
    push_result: str | None = None
    push_enabled: bool = True
    push_remote: str | None = None
    push_branch: str | None = None
    push_policy_note: str = "devctl default: push after successful checks and commit"
    applied_started: bool = False
    copied_files: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    check_results: list[CheckResult] = field(default_factory=list)
    git_branch: str | None = None
    git_head_before: str | None = None
    git_status_before: str = ""
    git_status_after_apply: str = ""
    git_status_after_checks: str = ""
    changes_introduced_by_checks: list[str] = field(default_factory=list)
    archive_size_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Encoding / printing helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def safe_decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def print_header(title: str) -> None:
    print(f"\n== {title} ==")


def rel_display(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except Exception:
        return str(path)


def slugify(value: str | None, fallback: str = "patch") -> str:
    text = (value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or fallback


def short_sha(value: str | None, length: int = 7) -> str:
    return (value or "unknown")[:length]


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run_command(
    args: list[str] | str,
    cwd: Path,
    *,
    timeout: int | None = None,
    shell: bool = False,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            args=args,
            cwd=cwd,
            returncode=completed.returncode,
            stdout=safe_decode(completed.stdout),
            stderr=safe_decode(completed.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = safe_decode(exc.stdout)
        stderr = safe_decode(exc.stderr)
        return CommandResult(args=args, cwd=cwd, returncode=124, stdout=stdout, stderr=stderr + "\nTIMEOUT")
    except FileNotFoundError as exc:
        return CommandResult(
            args=args,
            cwd=cwd,
            returncode=127,
            stdout="",
            stderr=f"Не удалось запустить команду или открыть рабочий каталог: {exc}",
        )


def git(project_root: Path, args: list[str], *, timeout: int | None = 120) -> CommandResult:
    return run_command(["git", *args], project_root, timeout=timeout)


def require_git(project_root: Path, args: list[str], *, timeout: int | None = 120) -> CommandResult:
    result = git(project_root, args, timeout=timeout)
    if result.returncode != 0:
        command = "git " + " ".join(args)
        raise PreflightError(f"{command} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def looks_like_project_root(path: Path) -> bool:
    """Проектно-независимое определение корня проекта.

    Репозиторий Git — самый сильный сигнал. Несколько типичных файлов сборки
    принимаются только как запасной вариант для экспериментов без Git и dry-run.
    """
    if (path / ".git").exists():
        return True
    markers = (
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "CMakeLists.txt",
        "pom.xml",
        "build.gradle",
        "Makefile",
        "README.md",
    )
    return any((path / marker).exists() for marker in markers)


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise DevctlError(f"Файл конфигурации не найден: {path}") from exc
    except Exception as exc:
        raise DevctlError(f"Не удалось прочитать JSON-конфигурацию {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DevctlError(f"Некорректная JSON-конфигурация {path}: корень должен быть объектом")
    return data


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def candidate_start_dirs() -> list[Path]:
    result: list[Path] = []
    try:
        result.append(Path.cwd().resolve())
    except Exception:
        pass
    try:
        result.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    # Preserve order while removing duplicates.
    unique: list[Path] = []
    seen: set[Path] = set()
    for item in result:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def find_workspace_config() -> Path | None:
    for start in candidate_start_dirs():
        for current in [start, *start.parents]:
            config = current / ".devctl" / "workspace.json"
            if config.is_file():
                return config
    return None


def resolve_workspace_path(workspace_root: Path, raw: Any, *, default: str, key: str) -> Path:
    value = raw if isinstance(raw, str) and raw.strip() else default
    rel = validate_relative_posix_path(value, allow_dot=True, kind=f"workspace.{key}")
    if rel == ".":
        return workspace_root.resolve()
    return (workspace_root / Path(*rel.split("/"))).resolve()


def discover_workspace_from_config(config_path: Path) -> Workspace:
    workspace_root = config_path.parent.parent.resolve()
    config = read_json_file(config_path)
    project_root = resolve_workspace_path(
        workspace_root,
        config.get("projectDir"),
        default=DEFAULT_PROJECT_DIR_NAME,
        key="projectDir",
    )
    patches_dir = resolve_workspace_path(
        workspace_root,
        config.get("patchesDir"),
        default=DEFAULT_PATCHES_DIR_NAME,
        key="patchesDir",
    )
    archives_dir = resolve_workspace_path(
        workspace_root,
        config.get("archivesDir"),
        default=DEFAULT_ARCHIVES_DIR_NAME,
        key="archivesDir",
    )
    state_dir = workspace_root / ".devctl"
    return Workspace(
        project_root=project_root,
        workspace_root=workspace_root,
        patches_dir=patches_dir,
        archives_dir=archives_dir,
        state_dir=state_dir,
        state_file=state_dir / "state.json",
    )


def find_project_root() -> Path:
    seen: set[Path] = set()
    for start in candidate_start_dirs():
        for current in [start, *start.parents]:
            if current in seen:
                continue
            seen.add(current)
            if looks_like_project_root(current):
                return current
    raise DevctlError(
        "Не удалось найти корень проекта. Запустите `devctl init --project ./your-project` "
        "из корня рабочей области или запускайте devctl из каталога Git/проекта."
    )


def discover_workspace() -> Workspace:
    config_path = find_workspace_config()
    if config_path:
        return discover_workspace_from_config(config_path)

    project_root = find_project_root()
    workspace_root = project_root.parent
    patches_dir = workspace_root / DEFAULT_PATCHES_DIR_NAME

    archives_dir = workspace_root / DEFAULT_ARCHIVES_DIR_NAME
    if not archives_dir.exists():
        for alias in LEGACY_ARCHIVES_DIR_ALIASES:
            legacy = workspace_root / alias
            if legacy.exists():
                archives_dir = legacy
                break

    state_dir = workspace_root / ".devctl"
    state_file = state_dir / "state.json"
    return Workspace(
        project_root=project_root,
        workspace_root=workspace_root,
        patches_dir=patches_dir,
        archives_dir=archives_dir,
        state_dir=state_dir,
        state_file=state_file,
    )


def validate_workspace_for_start(workspace: Workspace) -> None:
    if not workspace.patches_dir.is_dir():
        raise PreflightError(f"Каталог патчей отсутствует: {workspace.patches_dir}")
    if not workspace.archives_dir.exists():
        workspace.archives_dir.mkdir(parents=True, exist_ok=True)
    if not workspace.archives_dir.is_dir():
        raise PreflightError(f"Путь архивов не является каталогом: {workspace.archives_dir}")


# ---------------------------------------------------------------------------
# State registry
# ---------------------------------------------------------------------------


def load_state(workspace: Workspace) -> dict[str, Any]:
    if not workspace.state_file.exists():
        return {"version": STATE_VERSION, "runs": []}
    try:
        with workspace.state_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise DevctlError(f"Не удалось прочитать реестр состояния {workspace.state_file}: {exc}") from exc
    if not isinstance(data, dict):
        raise DevctlError(f"Некорректный реестр состояния {workspace.state_file}: корень должен быть объектом")
    if not isinstance(data.get("runs"), list):
        data["runs"] = []
    data.setdefault("version", STATE_VERSION)
    return data


def save_state(workspace: Workspace, state: dict[str, Any]) -> None:
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = workspace.state_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(workspace.state_file)


def append_run_state(workspace: Workspace, run: dict[str, Any]) -> None:
    state = load_state(workspace)
    runs = state.setdefault("runs", [])
    runs.append(run)
    save_state(workspace, state)


def find_state_run(state: dict[str, Any], patch_sha256: str | None, patch_id: str | None = None) -> dict[str, Any] | None:
    for run in reversed(state.get("runs", [])):
        if patch_sha256 and run.get("patchSha256") == patch_sha256 and run.get("status") == "applied":
            return run
        if patch_id and run.get("patchId") == patch_id and run.get("status") == "applied":
            return run
    return None


def latest_failed_run(state: dict[str, Any]) -> dict[str, Any] | None:
    for run in reversed(state.get("runs", [])):
        if run.get("status") in {"failed", "push_failed", "interrupted", "preflight_failed", "invalid_patch"}:
            return run
    return None


# ---------------------------------------------------------------------------
# Чтение и сортировка патчей
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_iso_datetime(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def timestamp_from_patch_filename(path: Path) -> float | None:
    match = PATCH_FILENAME_RE.match(path.name)
    if not match:
        return None
    raw = match.group(1) + match.group(2)
    try:
        parsed = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def read_manifest_from_zip(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            try:
                with zf.open("manifest.json", "r") as fh:
                    data = json.loads(safe_decode(fh.read()))
            except KeyError:
                return None, "manifest.json отсутствует"
    except zipfile.BadZipFile:
        return None, "это не корректный zip-файл"
    except Exception as exc:
        return None, f"не удалось прочитать manifest.json: {exc}"
    if not isinstance(data, dict):
        return None, "корень manifest.json должен быть объектом"
    return data, None


def candidate_sort_key(path: Path, manifest: dict[str, Any] | None) -> tuple[int, float]:
    if isinstance(manifest, dict):
        created = manifest.get("createdAt")
        if isinstance(created, str):
            parsed = parse_iso_datetime(created)
            if parsed is not None:
                return (3, parsed)
    by_name = timestamp_from_patch_filename(path)
    if by_name is not None:
        return (2, by_name)
    try:
        return (1, path.stat().st_mtime)
    except OSError:
        return (0, 0.0)


def list_patch_candidates(workspace: Workspace) -> list[PatchCandidate]:
    if not workspace.patches_dir.is_dir():
        return []
    candidates: list[PatchCandidate] = []
    for path in workspace.patches_dir.glob("*.zip"):
        manifest, error = read_manifest_from_zip(path)
        candidate = PatchCandidate(
            path=path,
            manifest=manifest,
            manifest_error=error,
            sort_key=candidate_sort_key(path, manifest),
        )
        try:
            candidate.sha256 = sha256_file(path)
        except Exception as exc:
            candidate.manifest_error = f"не удалось посчитать hash патча: {exc}"
        candidates.append(candidate)
    candidates.sort(key=lambda c: c.sort_key, reverse=True)
    return candidates


def find_latest_unapplied_patch(
    workspace: Workspace,
    state: dict[str, Any],
    candidates: list[PatchCandidate],
) -> PatchCandidate | None:
    for candidate in candidates:
        if candidate.sha256 and find_state_run(state, candidate.sha256, candidate.patch_id):
            continue
        if candidate.sha256 and patch_seen_in_git(workspace.project_root, candidate.sha256, candidate.patch_id):
            continue
        return candidate
    return None


# ---------------------------------------------------------------------------
# Manifest validation and path safety
# ---------------------------------------------------------------------------


def require_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise InvalidPatchError(f"manifest.{key} должен быть объектом")
    return value


def require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise InvalidPatchError(f"manifest.{key} должен быть списком")
    return value


def validate_relative_posix_path(raw: Any, *, allow_dot: bool = False, kind: str = "path") -> str:
    if not isinstance(raw, str):
        raise InvalidPatchError(f"{kind} должен быть строкой")
    value = raw.strip()
    if not value:
        raise InvalidPatchError(f"{kind} не должен быть пустым")
    if value == "." and allow_dot:
        return value
    if value == "." and not allow_dot:
        raise InvalidPatchError(f"{kind} не должен указывать на корень проекта")
    if "\\" in value:
        raise InvalidPatchError(f"{kind} должен использовать POSIX-разделители '/', получен backslash в {value!r}")
    if value.startswith("/"):
        raise InvalidPatchError(f"{kind} должен быть относительным, получен абсолютный путь {value!r}")
    if value.startswith("//"):
        raise InvalidPatchError(f"{kind} не должен быть UNC-подобным путём: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidPatchError(f"{kind} содержит небезопасный сегмент: {value!r}")
    if ":" in parts[0]:
        raise InvalidPatchError(f"{kind} не должен начинаться с сегмента, похожего на диск: {value!r}")
    return value


def safe_destination(project_root: Path, relative_posix: str, *, kind: str = "path") -> Path:
    rel = validate_relative_posix_path(relative_posix, kind=kind)
    project_resolved = project_root.resolve()
    destination = (project_resolved / Path(*rel.split("/"))).resolve()
    try:
        destination.relative_to(project_resolved)
    except ValueError as exc:
        raise InvalidPatchError(f"{kind} выходит за пределы корня проекта: {relative_posix!r}") from exc
    return destination


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("formatVersion") != 1:
        raise InvalidPatchError("manifest.formatVersion должен быть равен 1")
    for key in ("patchId", "title", "summary"):
        if not isinstance(manifest.get(key), str) or not manifest.get(key, "").strip():
            raise InvalidPatchError(f"manifest.{key} должен быть непустой строкой")
    apply = require_dict(manifest, "apply")
    files_root = apply.get("filesRoot", "files")
    validate_relative_posix_path(files_root, kind="apply.filesRoot")
    delete_entries = apply.get("delete", [])
    if not isinstance(delete_entries, list):
        raise InvalidPatchError("manifest.apply.delete должен быть списком")
    for index, entry in enumerate(delete_entries):
        if not isinstance(entry, dict):
            raise InvalidPatchError(f"manifest.apply.delete[{index}] должен быть объектом")
        path = validate_relative_posix_path(entry.get("path"), kind=f"manifest.apply.delete[{index}].path")
        parts = set(path.split("/"))
        if parts & BANNED_PATH_PARTS:
            raise InvalidPatchError(f"manifest.apply.delete[{index}].path указывает на запрещённый каталог: {path}")
        for bool_key in ("recursive", "required"):
            if bool_key in entry and not isinstance(entry.get(bool_key), bool):
                raise InvalidPatchError(f"manifest.apply.delete[{index}].{bool_key} должен быть boolean")
    checks = manifest.get("checks", [])
    if not isinstance(checks, list):
        raise InvalidPatchError("manifest.checks должен быть списком")
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise InvalidPatchError(f"manifest.checks[{index}] должен быть объектом")
        for key in ("name", "cwd", "command"):
            if not isinstance(check.get(key), str) or not check.get(key, "").strip():
                raise InvalidPatchError(f"manifest.checks[{index}].{key} должен быть непустой строкой")
        validate_relative_posix_path(check.get("cwd"), allow_dot=True, kind=f"manifest.checks[{index}].cwd")
        required = check.get("requiredCommands", [])
        if not isinstance(required, list) or any(not isinstance(item, str) or not item.strip() for item in required):
            raise InvalidPatchError(f"manifest.checks[{index}].requiredCommands должен быть списком строк")
        timeout = check.get("timeoutSeconds", 300)
        if not isinstance(timeout, int) or timeout <= 0:
            raise InvalidPatchError(f"manifest.checks[{index}].timeoutSeconds должен быть положительным целым числом")
    commit = manifest.get("commit", {"enabled": True})
    if not isinstance(commit, dict):
        raise InvalidPatchError("manifest.commit должен быть объектом")
    if commit.get("enabled", True):
        if not isinstance(commit.get("message"), str) or not commit.get("message", "").strip():
            raise InvalidPatchError("manifest.commit.message должен быть непустой строкой, когда commit включён")
    push = manifest.get("push", {"enabled": True})
    if not isinstance(push, dict):
        raise InvalidPatchError("manifest.push должен быть объектом")
    for section in ("setup", "services"):
        if section in manifest and not isinstance(manifest.get(section), list):
            raise InvalidPatchError(f"manifest.{section} зарезервирован и должен быть списком")
        if isinstance(manifest.get(section), list) and manifest.get(section):
            raise InvalidPatchError(
                f"manifest.{section} зарезервирован для будущей версии devctl; "
                f"v{DEVCTL_VERSION} не устанавливает зависимости автоматически и не запускает сервисы"
            )


# ---------------------------------------------------------------------------
# Git state and applied detection
# ---------------------------------------------------------------------------


def git_available() -> bool:
    return shutil.which("git") is not None


def git_branch(project_root: Path) -> str:
    result = require_git(project_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip()


def git_head(project_root: Path) -> str:
    result = require_git(project_root, ["rev-parse", "HEAD"])
    return result.stdout.strip()


def git_last_commit_summary(project_root: Path) -> str:
    result = git(project_root, ["log", "-1", "--pretty=%h %s"])
    if result.returncode != 0:
        return "неизвестно"
    return result.stdout.strip() or "неизвестно"


def git_status_porcelain(project_root: Path) -> str:
    result = git(project_root, ["status", "--porcelain"])
    if result.returncode != 0:
        return ""
    return result.stdout


def git_status_short(project_root: Path) -> str:
    result = git(project_root, ["status", "-sb"])
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip()
    return result.stdout.strip()


def fetch_remote(project_root: Path, remote: str) -> None:
    result = git(project_root, ["fetch", "--prune", remote], timeout=180)
    if result.returncode != 0:
        raise PreflightError(f"git fetch --prune {remote} завершился ошибкой: {result.stderr.strip() or result.stdout.strip()}")


def remote_ref_exists(project_root: Path, remote: str, branch: str) -> bool:
    result = git(project_root, ["rev-parse", "--verify", f"{remote}/{branch}"])
    return result.returncode == 0


def ahead_behind(project_root: Path, remote: str, branch: str) -> tuple[int | None, int | None, str | None]:
    ref = f"{remote}/{branch}"
    if not remote_ref_exists(project_root, remote, branch):
        return None, None, f"Remote-ссылка {ref} не найдена"
    result = git(project_root, ["rev-list", "--left-right", "--count", f"HEAD...{ref}"])
    if result.returncode != 0:
        return None, None, result.stderr.strip() or result.stdout.strip()
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None, None, f"Неожиданный вывод ahead/behind: {result.stdout!r}"
    return int(parts[0]), int(parts[1]), None


def workspace_git_config(workspace: Workspace) -> dict[str, Any]:
    config_path = workspace.state_dir / "workspace.json"
    if not config_path.is_file():
        return {}
    try:
        data = read_json_file(config_path)
    except DevctlError:
        return {}
    git_cfg = data.get("git")
    return git_cfg if isinstance(git_cfg, dict) else {}


def bool_from_config(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def effective_push_policy(
    workspace: Workspace,
    manifest: dict[str, Any],
    *,
    no_push: bool = False,
    current_branch: str | None = None,
) -> tuple[bool, str, str, str]:
    """Вернуть (enabled, remote, branch, note) для шага git push в devctl.

    Манифест патча может подсказать цель push, но не владеет политикой рабочего
    процесса. По умолчанию `devctl start` — это «волшебная кнопка»: зелёные
    проверки ведут к коммиту и push. `devctl start --no-push` нужен только для
    явно локальных/отладочных запусков.
    """
    git_cfg = workspace_git_config(workspace)
    push_cfg = manifest.get("push") if isinstance(manifest.get("push"), dict) else {}

    remote = push_cfg.get("remote") or git_cfg.get("remote") or "origin"
    branch = push_cfg.get("branch") or git_cfg.get("branch") or current_branch or "main"
    if not isinstance(remote, str) or not remote.strip():
        remote = "origin"
    if not isinstance(branch, str) or not branch.strip():
        branch = current_branch or "main"

    if no_push:
        return False, remote, branch, "отключено параметром CLI --no-push"

    if bool_from_config(git_cfg.get("enabled"), True) is False:
        return False, remote, branch, "отключено настройкой workspace git.enabled=false"

    if bool_from_config(git_cfg.get("autoPush"), True) is False:
        return False, remote, branch, "отключено настройкой workspace git.autoPush=false"

    if push_cfg.get("enabled") is False:
        return True, remote, branch, "manifest push.enabled=false проигнорирован; по умолчанию devctl делает commit+push после зелёных проверок"

    return True, remote, branch, "devctl по умолчанию: push после успешных проверок и коммита"


def validate_git_preflight(
    workspace: Workspace,
    manifest: dict[str, Any],
    ctx: RunContext | None = None,
    *,
    no_push: bool = False,
) -> None:
    if not git_available():
        raise PreflightError("команда git не найдена")
    if not (workspace.project_root / ".git").exists():
        raise PreflightError(f"Корень проекта не является Git-репозиторием: {workspace.project_root}")

    status = git_status_porcelain(workspace.project_root)
    if ctx:
        ctx.git_status_before = status
        try:
            ctx.git_branch = git_branch(workspace.project_root)
            ctx.git_head_before = git_head(workspace.project_root)
        except DevctlError:
            pass
    if status.strip():
        raise PreflightError(
            "Рабочее дерево Git не чистое. Перед запуском devctl start закоммитьте, спрячьте или отмените локальные изменения."
        )

    base = manifest.get("base") if isinstance(manifest.get("base"), dict) else {}
    expected_branch = base.get("branch") if isinstance(base.get("branch"), str) else None
    current_branch = git_branch(workspace.project_root)
    if expected_branch and current_branch != expected_branch:
        raise PreflightError(f"Патч ожидает ветку {expected_branch!r}, текущая ветка — {current_branch!r}")

    push_enabled, remote, branch, note = effective_push_policy(
        workspace, manifest, no_push=no_push, current_branch=current_branch
    )
    if ctx:
        ctx.push_enabled = push_enabled
        ctx.push_remote = remote
        ctx.push_branch = branch
        ctx.push_policy_note = note
        if "ignored" in note:
            ctx.warnings.append(note)

    if not push_enabled:
        return
    if not isinstance(remote, str) or not remote:
        raise PreflightError("push remote должен быть непустой строкой")
    if not isinstance(branch, str) or not branch:
        raise PreflightError("push branch должен быть непустой строкой")

    fetch_remote(workspace.project_root, remote)
    ahead, behind, error = ahead_behind(workspace.project_root, remote, branch)
    if error:
        raise PreflightError(error)
    if ahead and behind:
        raise PreflightError(f"Локальная ветка разошлась с {remote}/{branch}: ahead={ahead}, behind={behind}")
    if behind:
        raise PreflightError(f"Локальная ветка отстаёт от {remote}/{branch} на {behind} коммит(ов). Сначала синхронизируйте вручную.")
    if ahead:
        raise PreflightError(
            f"Локальная ветка опережает {remote}/{branch} на {ahead} коммит(ов). Выполните push/синхронизацию перед новым патчем."
        )


def patch_seen_in_git(project_root: Path, patch_sha256: str | None, patch_id: str | None, limit: int = 100) -> bool:
    if not patch_sha256 and not patch_id:
        return False
    if not (project_root / ".git").exists() or not git_available():
        return False
    result = git(project_root, ["log", f"-n{limit}", "--format=%B%x1e"])
    if result.returncode != 0:
        return False
    for message in result.stdout.split("\x1e"):
        if patch_sha256 and f"Patch-SHA256: {patch_sha256}" in message:
            return True
        if patch_id and f"Patch-Id: {patch_id}" in message:
            return True
    return False


def build_commit_message(manifest: dict[str, Any], patch_sha256: str) -> str:
    commit = manifest.get("commit") if isinstance(manifest.get("commit"), dict) else {}
    message = str(commit.get("message") or f"chore: применить патч {manifest.get('patchId')}").strip()
    trailers = [
        f"Patch-Id: {manifest.get('patchId')}",
        f"Patch-SHA256: {patch_sha256}",
        f"Devctl-Version: {DEVCTL_VERSION}",
    ]
    return message.rstrip() + "\n\n" + "\n".join(trailers) + "\n"


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


def validate_check_prerequisites(project_root: Path, manifest: dict[str, Any]) -> None:
    checks = manifest.get("checks", [])
    if not isinstance(checks, list):
        raise InvalidPatchError("manifest.checks должен быть списком")
    missing: list[str] = []
    bad_cwds: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        check_name = str(check.get("name", index))
        cwd_raw = validate_relative_posix_path(check.get("cwd", "."), allow_dot=True, kind=f"checks[{index}].cwd")
        cwd = project_root if cwd_raw == "." else safe_destination(project_root, cwd_raw, kind=f"checks[{index}].cwd")
        if not cwd.is_dir():
            bad_cwds.append(f"{check_name}: {cwd_raw}")
        for command in check.get("requiredCommands", []):
            command_name = command.strip()
            if not shutil.which(command_name):
                missing.append(f"{command_name} (required by {check_name})")
    if bad_cwds:
        raise PreflightError("Рабочий каталог проверки не существует до применения патча: " + ", ".join(bad_cwds))
    if missing:
        unique = sorted(set(missing))
        raise PreflightError("Отсутствуют обязательные команды: " + ", ".join(unique))


def validate_patch_files_root(candidate: PatchCandidate, manifest: dict[str, Any]) -> None:
    files_root = manifest.get("apply", {}).get("filesRoot", "files")
    files_root = validate_relative_posix_path(files_root, kind="apply.filesRoot")
    prefix = files_root.rstrip("/") + "/"
    try:
        with zipfile.ZipFile(candidate.path, "r") as zf:
            names = zf.namelist()
    except Exception as exc:
        raise InvalidPatchError(f"Не удалось проверить zip-архив патча: {exc}") from exc
    file_entries = [name for name in names if name != files_root and name.startswith(prefix) and not name.endswith("/")]
    delete_entries = manifest.get("apply", {}).get("delete", [])
    if not file_entries and not delete_entries:
        raise InvalidPatchError(f"В патче нет файлов внутри {files_root!r} и нет записей на удаление")
    for name in names:
        if "\\" in name:
            raise InvalidPatchError(f"Запись zip содержит backslash, что запрещено: {name!r}")
        if name.startswith("/") or name.startswith("//"):
            raise InvalidPatchError(f"Запись zip является абсолютной или UNC-подобной: {name!r}")
        if name.startswith(prefix) and not name.endswith("/"):
            relative = name[len(prefix) :]
            validate_relative_posix_path(relative, kind=f"zip entry {name!r}")


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


def should_exclude_from_archive(relative_posix: str, extra_excludes: Iterable[str] = ()) -> bool:
    if relative_posix == ".":
        return False
    name = Path(relative_posix).name
    parts = set(relative_posix.split("/"))
    if ".env.example" == name:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if parts & ARCHIVE_EXCLUDED_PARTS:
        return True
    lower = relative_posix.lower()
    if lower.endswith(ARCHIVE_EXCLUDED_SUFFIXES):
        return True
    for pattern in extra_excludes:
        if not pattern or pattern.startswith("!"):
            continue
        normalized = pattern.strip("/")
        if not normalized:
            continue
        if normalized.endswith("/"):
            normalized = normalized.strip("/")
            if normalized in parts or relative_posix.startswith(normalized + "/"):
                return True
        if fnmatch.fnmatch(relative_posix, normalized):
            return True
    return False


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise DevctlError(f"Не удалось создать уникальный путь для {path}")


def manifest_archive_excludes(manifest: dict[str, Any]) -> list[str]:
    archive = manifest.get("archive") if isinstance(manifest.get("archive"), dict) else {}
    excludes = archive.get("exclude", [])
    if isinstance(excludes, list):
        return [item for item in excludes if isinstance(item, str)]
    return []


def manifest_include_release_payloads(manifest: dict[str, Any]) -> bool:
    archive = manifest.get("archive") if isinstance(manifest.get("archive"), dict) else {}
    return bool(archive.get("includeReleasePayloads", False)) if isinstance(archive, dict) else False


def release_payload_omission_kind(relative_posix: str) -> str | None:
    parts = relative_posix.split("/")
    if not parts or parts[0] != RELEASE_DIR_NAME:
        return None
    lower = relative_posix.lower()
    if lower.endswith(RELEASE_ARCHIVE_PAYLOAD_SUFFIXES):
        return "zip"
    if lower.endswith(RELEASE_EXECUTABLE_PAYLOAD_SUFFIXES):
        return "exe"
    return None


def release_placeholder_path(relative_posix: str, kind: str) -> str:
    parent = relative_posix.rsplit("/", 1)[0] if "/" in relative_posix else ""
    placeholder_name = RELEASE_ZIP_PLACEHOLDER if kind == "zip" else RELEASE_EXE_PLACEHOLDER
    return f"{parent}/{placeholder_name}" if parent else placeholder_name


def human_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown size"
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size_bytes)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def release_placeholder_text(entries: list[tuple[str, str, int | None]]) -> str:
    lines = [
        "Этот файл создан devctl при сборке snapshot-архива проекта.",
        "",
        "Тяжелые release payload-файлы намеренно не попали в архив devctl,",
        "чтобы служебные pre/post/failed архивы не раздувались на много мегабайт.",
        "",
        "Исключенные файлы:",
    ]
    for kind, rel_path, size in entries:
        label = "release zip" if kind == "zip" else "Windows exe-файл"
        lines.append(f"- {rel_path} ({label}, {human_size(size)})")
    lines.extend(
        [
            "",
            "Это не удаляет исходные файлы из рабочей копии проекта.",
            "Для реальной поставки пересобери release локально или используй исходный каталог release/.",
            "",
        ]
    )
    return "\n".join(lines)


def create_project_archive(
    workspace: Workspace,
    destination: Path,
    *,
    manifest: dict[str, Any] | None = None,
    include_project_dir: bool | None = None,
) -> tuple[Path, int]:
    destination = unique_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    extra_excludes = manifest_archive_excludes(manifest or {})
    archive = manifest.get("archive") if manifest and isinstance(manifest.get("archive"), dict) else {}
    if include_project_dir is None:
        include_project_dir = bool(archive.get("includeProjectDir", True)) if isinstance(archive, dict) else True

    include_release_payloads = manifest_include_release_payloads(manifest or {})

    file_count = 0
    written_arcnames: set[str] = set()
    release_placeholders: dict[str, list[tuple[str, str, int | None]]] = {}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(workspace.project_root):
            root_path = Path(root)
            rel_root = root_path.relative_to(workspace.project_root).as_posix()
            # Prune excluded directories before walking into them.
            kept_dirs = []
            for directory in dirs:
                rel_dir = directory if rel_root == "." else f"{rel_root}/{directory}"
                if should_exclude_from_archive(rel_dir + "/", extra_excludes):
                    continue
                kept_dirs.append(directory)
            dirs[:] = kept_dirs
            for filename in files:
                file_path = root_path / filename
                rel_path = file_path.relative_to(workspace.project_root).as_posix()
                if should_exclude_from_archive(rel_path, extra_excludes):
                    continue

                omission_kind = None if include_release_payloads else release_payload_omission_kind(rel_path)
                if omission_kind:
                    try:
                        size_bytes = file_path.stat().st_size
                    except OSError:
                        size_bytes = None
                    placeholder = release_placeholder_path(rel_path, omission_kind)
                    release_placeholders.setdefault(placeholder, []).append((omission_kind, rel_path, size_bytes))
                    continue

                arcname = rel_path
                if include_project_dir:
                    arcname = f"{workspace.project_root.name}/{rel_path}"
                zf.write(file_path, arcname)
                written_arcnames.add(arcname)
                file_count += 1

        for placeholder_rel, entries in sorted(release_placeholders.items()):
            arcname = placeholder_rel
            if include_project_dir:
                arcname = f"{workspace.project_root.name}/{placeholder_rel}"
            if arcname in written_arcnames:
                continue
            zf.writestr(arcname, release_placeholder_text(entries))
            written_arcnames.add(arcname)
            file_count += 1
    return destination, file_count


def archive_name(project: str, timestamp: str, phase: str, slug: str, suffix: str = "") -> str:
    extra = f"_{suffix}" if suffix else ""
    return f"{phase}_{project}_{timestamp}_{slug}{extra}.zip"


def create_run_dir(workspace: Workspace, manifest: dict[str, Any] | None, patch_sha: str | None) -> Path:
    archive = manifest.get("archive") if isinstance(manifest, dict) and isinstance(manifest.get("archive"), dict) else {}
    slug = slugify(archive.get("nameSlug") if isinstance(archive, dict) else None or manifest.get("patchId") if isinstance(manifest, dict) else None)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = workspace.archives_dir / f"{timestamp}_{slug}_{short_sha(patch_sha)}"
    return unique_path(base)


# ---------------------------------------------------------------------------
# Safe apply
# ---------------------------------------------------------------------------


def safe_delete_path(project_root: Path, relative_posix: str, *, recursive: bool, required: bool) -> tuple[str, str]:
    rel = validate_relative_posix_path(relative_posix, kind="delete.path")
    parts = rel.split("/")
    if set(parts) & BANNED_PATH_PARTS:
        raise InvalidPatchError(f"Отказ удалить запрещённый путь: {rel}")
    target = safe_destination(project_root, rel, kind="delete.path")
    if target == project_root.resolve():
        raise InvalidPatchError("Отказ удалить корень проекта")
    if not target.exists():
        if required:
            raise InvalidPatchError(f"Обязательный путь для удаления не существует: {rel}")
        return rel, "missing"
    if target.is_dir():
        if not recursive:
            raise InvalidPatchError(f"Путь удаления является каталогом; требуется recursive=true: {rel}")
        shutil.rmtree(target)
        return rel, "deleted directory"
    target.unlink()
    return rel, "deleted file"


def apply_deletions(ctx: RunContext) -> None:
    entries = ctx.manifest.get("apply", {}).get("delete", [])
    for entry in entries:
        path = entry.get("path")
        recursive = bool(entry.get("recursive", False))
        required = bool(entry.get("required", False))
        rel, status = safe_delete_path(ctx.workspace.project_root, path, recursive=recursive, required=required)
        if status == "missing":
            ctx.warnings.append(f"Путь удаления уже отсутствует: {rel}")
        else:
            ctx.deleted_paths.append(rel)


def safe_copy_files(ctx: RunContext) -> None:
    project_root = ctx.workspace.project_root
    files_root = ctx.manifest.get("apply", {}).get("filesRoot", "files")
    files_root = validate_relative_posix_path(files_root, kind="apply.filesRoot")
    prefix = files_root.rstrip("/") + "/"
    with zipfile.ZipFile(ctx.patch.path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            if "\\" in name:
                raise InvalidPatchError(f"Запись zip содержит backslash: {name!r}")
            relative = name[len(prefix) :]
            rel = validate_relative_posix_path(relative, kind=f"zip entry {name!r}")
            parts = rel.split("/")
            if parts[0] == ".git" or ".git" in parts:
                raise InvalidPatchError(f"Отказ копировать путь .git: {rel}")
            if parts[-1] == ".env" or parts[-1].startswith(".env."):
                raise InvalidPatchError(f"Отказ копировать env-файл, похожий на секрет: {rel}")
            destination = safe_destination(project_root, rel, kind=f"zip entry {name!r}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            ctx.copied_files.append(rel)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def log_path_for_check(logs_dir: Path, index: int, name: str) -> Path:
    return logs_dir / f"check-{index + 1:02d}-{slugify(name)}.log"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def run_checks(ctx: RunContext) -> None:
    checks = ctx.manifest.get("checks", [])
    if not checks:
        ctx.warnings.append("В манифесте нет проверок; продолжаю, потому что checks=[] разрешён в v0.")
        return
    for index, check in enumerate(checks):
        name = str(check.get("name"))
        command = str(check.get("command"))
        cwd_raw = str(check.get("cwd", "."))
        cwd = ctx.workspace.project_root if cwd_raw == "." else safe_destination(ctx.workspace.project_root, cwd_raw, kind="check.cwd")
        timeout = int(check.get("timeoutSeconds", 300))
        log_path = log_path_for_check(ctx.logs_dir or ctx.workspace.archives_dir, index, name)
        start = time.monotonic()
        result = run_command(command, cwd, timeout=timeout, shell=True)
        duration = time.monotonic() - start
        log_text = []
        log_text.append(f"# Проверка: {name}\n")
        log_text.append(f"Команда: {command}\n")
        log_text.append(f"Рабочий каталог: {cwd}\n")
        log_text.append(f"Код возврата: {result.returncode}\n")
        log_text.append(f"Длительность, секунд: {duration:.2f}\n\n")
        log_text.append("## STDOUT\n")
        log_text.append(result.stdout or "")
        log_text.append("\n\n## STDERR\n")
        log_text.append(result.stderr or "")
        write_text(log_path, "".join(log_text))
        check_result = CheckResult(
            name=name,
            command=command,
            cwd=cwd_raw,
            status="успех" if result.returncode == 0 else "ошибка",
            returncode=result.returncode,
            duration_seconds=duration,
            log_path=rel_display(log_path, ctx.workspace.workspace_root),
        )
        if result.returncode == 124:
            check_result.error = "таймаут"
        elif result.returncode != 0:
            check_result.error = "ненулевой код возврата"
        ctx.check_results.append(check_result)
        if result.returncode != 0:
            raise CheckFailedError(f"Проверка не прошла: {name} (см. {log_path})")


def parse_status_lines(status_text: str) -> set[str]:
    return {line.strip() for line in status_text.splitlines() if line.strip()}


def new_changes_after_checks(after_apply: str, after_checks: str) -> list[str]:
    before = parse_status_lines(after_apply)
    after = parse_status_lines(after_checks)
    return sorted(after - before)


# ---------------------------------------------------------------------------
# Commit/push
# ---------------------------------------------------------------------------


def dangerous_git_changes(status_text: str) -> list[str]:
    dangerous: list[str] = []
    for line in status_text.splitlines():
        if not line.strip() or len(line) < 4:
            continue
        path_text = line[3:].strip()
        # Rename lines have "old -> new". Check both sides.
        candidates = [part.strip() for part in path_text.split(" -> ")]
        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            parts = set(normalized.split("/"))
            name = normalized.split("/")[-1]
            lower = normalized.lower()
            if name == ".env" or name.startswith(".env."):
                dangerous.append(normalized)
            elif parts & DANGEROUS_GIT_PATH_PARTS:
                dangerous.append(normalized)
            elif lower.endswith(DANGEROUS_GIT_PATH_SUFFIXES):
                dangerous.append(normalized)
    return sorted(set(dangerous))


def commit_and_push(ctx: RunContext) -> None:
    project_root = ctx.workspace.project_root
    commit_cfg = ctx.manifest.get("commit") if isinstance(ctx.manifest.get("commit"), dict) else {}

    if commit_cfg.get("enabled") is False:
        ctx.warnings.append("manifest.commit.enabled=false проигнорирован; по умолчанию devctl делает коммит после зелёных проверок")

    current_status = git_status_porcelain(project_root)
    dangerous = dangerous_git_changes(current_status)
    if dangerous:
        raise DevctlError(
            "Отказ коммитить опасные сгенерированные/локальные файлы: " + ", ".join(dangerous)
        )

    if not current_status.strip() and not commit_cfg.get("allowEmpty", False):
        ctx.warnings.append("После патча/проверок нет изменений Git; commit и push пропущены.")
        return

    add_result = git(project_root, ["add", "-A"], timeout=120)
    if add_result.returncode != 0:
        raise DevctlError(f"git add -A завершился ошибкой: {add_result.stderr.strip() or add_result.stdout.strip()}")

    message = build_commit_message(ctx.manifest, ctx.patch.sha256 or "")
    # subprocess.run is used directly here because git commit reads the message from stdin.
    completed = subprocess.run(
        ["git", "commit", "-F", "-"],
        input=message.encode("utf-8"),
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    commit_stdout = safe_decode(completed.stdout)
    commit_stderr = safe_decode(completed.stderr)
    if completed.returncode != 0:
        raise DevctlError(f"git commit завершился ошибкой: {commit_stderr.strip() or commit_stdout.strip()}")
    ctx.commit_sha = git_head(project_root)

    if not ctx.push_enabled:
        ctx.push_result = "пропущено: " + (ctx.push_policy_note or "push отключён")
        return

    remote = ctx.push_remote or "origin"
    branch = ctx.push_branch or git_branch(project_root)
    if not isinstance(remote, str) or not remote:
        raise DevctlError("push remote должен быть непустой строкой")
    if not isinstance(branch, str) or not branch:
        raise DevctlError("push branch должен быть непустой строкой")
    push_result = git(project_root, ["push", remote, f"HEAD:{branch}"], timeout=240)
    if push_result.returncode != 0:
        ctx.push_result = push_result.stderr.strip() or push_result.stdout.strip()
        ctx.status = "push_failed"
        raise DevctlError("PUSH_FAILED: " + ctx.push_result)
    ctx.push_result = push_result.stdout.strip() or "push выполнен"


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------


def copy_manifest_to_logs(ctx: RunContext) -> None:
    if not ctx.logs_dir:
        return
    manifest_path = ctx.logs_dir / "manifest.json"
    write_text(manifest_path, json.dumps(ctx.manifest, ensure_ascii=False, indent=2) + "\n")


def write_log(ctx: RunContext, name: str, text: str) -> None:
    if not ctx.logs_dir:
        return
    write_text(ctx.logs_dir / name, text)


def report_lines(ctx: RunContext, finished_at: datetime) -> list[str]:
    patch_id = ctx.manifest.get("patchId", "неизвестно") if isinstance(ctx.manifest, dict) else "неизвестно"
    title = ctx.manifest.get("title", "неизвестно") if isinstance(ctx.manifest, dict) else "неизвестно"
    lines: list[str] = []
    lines.append(f"# Отчёт запуска devctl — {ctx.status}\n")
    lines.append("\n")
    lines.append("## Патч\n\n")
    lines.append(f"- ID патча: `{patch_id}`\n")
    lines.append(f"- Название: {title}\n")
    lines.append(f"- Файл патча: `{ctx.patch.path.name}`\n")
    lines.append(f"- SHA-256 патча: `{ctx.patch.sha256 or 'неизвестно'}`\n")
    lines.append("\n## Время\n\n")
    lines.append(f"- Старт: `{ctx.started_at.isoformat(timespec='seconds')}`\n")
    lines.append(f"- Финиш: `{finished_at.isoformat(timespec='seconds')}`\n")
    lines.append("\n## Проект\n\n")
    lines.append(f"- Корень проекта: `{ctx.workspace.project_root}`\n")
    lines.append(f"- Корень рабочей области: `{ctx.workspace.workspace_root}`\n")
    lines.append(f"- Ветка: `{ctx.git_branch or 'неизвестно'}`\n")
    lines.append(f"- HEAD до запуска: `{ctx.git_head_before or 'неизвестно'}`\n")
    lines.append("\n## Сводка применения\n\n")
    lines.append(f"- Скопировано файлов: {len(ctx.copied_files)}\n")
    for path in ctx.copied_files[:200]:
        lines.append(f"  - `{path}`\n")
    if len(ctx.copied_files) > 200:
        lines.append(f"  - ... ещё {len(ctx.copied_files) - 200}\n")
    lines.append(f"- Удалено путей: {len(ctx.deleted_paths)}\n")
    for path in ctx.deleted_paths[:200]:
        lines.append(f"  - `{path}`\n")
    if len(ctx.deleted_paths) > 200:
        lines.append(f"  - ... ещё {len(ctx.deleted_paths) - 200}\n")
    lines.append("\n## Снимки статуса Git\n\n")
    lines.append("### Изменения после применения\n\n")
    lines.append("```text\n" + (ctx.git_status_after_apply or "<пусто>\n") + "```\n\n")
    lines.append("### Изменения после проверок\n\n")
    lines.append("```text\n" + (ctx.git_status_after_checks or "<пусто>\n") + "```\n\n")
    lines.append("### Новые изменения, внесённые проверками\n\n")
    if ctx.changes_introduced_by_checks:
        for line in ctx.changes_introduced_by_checks:
            lines.append(f"- `{line}`\n")
    else:
        lines.append("После проверок новых изменений не обнаружено.\n")
    lines.append("\n## Проверки\n\n")
    if ctx.check_results:
        lines.append("| Проверка | Результат | Код возврата | Лог |\n")
        lines.append("|---|---:|---:|---|\n")
        for result in ctx.check_results:
            lines.append(
                f"| {result.name} | {result.status} | {result.returncode if result.returncode is not None else ''} | `{result.log_path or ''}` |\n"
            )
    else:
        lines.append("Проверки не запускались.\n")
    lines.append("\n## Архивы\n\n")
    for label, path in (("Архив до применения", ctx.pre_archive), ("Архив после применения", ctx.post_archive), ("Архив ошибки", ctx.failed_archive)):
        if path:
            lines.append(f"- {label}: `{rel_display(path, ctx.workspace.workspace_root)}`\n")
    if ctx.archive_size_warnings:
        lines.append("\n### Предупреждения по архивам\n\n")
        for warning in ctx.archive_size_warnings:
            lines.append(f"- {warning}\n")
    lines.append("\n## Commit / push\n\n")
    lines.append("- Политика конвейера по умолчанию: `проверки -> commit -> push`\n")
    lines.append(f"- Push включён: `{ctx.push_enabled}`\n")
    lines.append(f"- Цель push: `{(ctx.push_remote or 'origin')}/{(ctx.push_branch or ctx.git_branch or 'current')}`\n")
    lines.append(f"- Примечание политики push: `{ctx.push_policy_note}`\n")
    lines.append(f"- SHA коммита: `{ctx.commit_sha or 'нет'}`\n")
    lines.append(f"- Результат push: `{ctx.push_result or 'нет'}`\n")
    lines.append("\n## Предупреждения\n\n")
    if ctx.warnings:
        for warning in ctx.warnings:
            lines.append(f"- {warning}\n")
    else:
        lines.append("Предупреждений нет.\n")
    lines.append("\n## Ошибки\n\n")
    if ctx.errors:
        for error in ctx.errors:
            lines.append(f"- {error}\n")
    else:
        lines.append("Ошибок нет.\n")
    if ctx.status in {"failed", "push_failed", "interrupted"}:
        lines.append("\n## Восстановление\n\n")
        if ctx.applied_started:
            lines.append("Рабочее дерево оставлено с изменениями для инспекции. Архив состояния ошибки должен существовать, если его удалось создать.\n\n")
            lines.append("```bash\n")
            lines.append("git status\n")
            lines.append("git diff\n")
            lines.append("# Осторожно: следующие команды откатывают локальные изменения.\n")
            lines.append("git reset --hard HEAD\n")
            lines.append("# Осторожно: удаляет untracked файлы/каталоги.\n")
            lines.append("git clean -fd\n")
            lines.append("```\n")
        elif ctx.status == "push_failed":
            lines.append("Коммит создан локально, но push не прошёл. Выполните `git status -sb` и push вручную после устранения причины.\n")
        else:
            lines.append("Патч не был применён до ошибки/прерывания. Проверьте логи и повторите после исправления причины.\n")
    lines.append("\n## Итоговый статус\n\n")
    lines.append(f"`{ctx.status}`\n")
    return lines


def write_report(ctx: RunContext) -> None:
    if not ctx.run_dir:
        return
    finished_at = now_utc()
    ctx.report_path = ctx.run_dir / "report.md"
    write_text(ctx.report_path, "".join(report_lines(ctx, finished_at)))


def update_state_from_context(ctx: RunContext) -> None:
    if ctx.status == "running":
        return
    record = {
        "patchId": ctx.manifest.get("patchId") if isinstance(ctx.manifest, dict) else None,
        "patchFile": ctx.patch.path.name,
        "patchSha256": ctx.patch.sha256,
        "status": ctx.status,
        "startedAt": ctx.started_at.isoformat(timespec="seconds"),
        "finishedAt": iso_now(),
        "commitSha": ctx.commit_sha,
        "archiveDir": rel_display(ctx.run_dir, ctx.workspace.workspace_root) if ctx.run_dir else None,
        "report": rel_display(ctx.report_path, ctx.workspace.workspace_root) if ctx.report_path else None,
    }
    append_run_state(ctx.workspace, record)


def warn_archive_size(ctx: RunContext, path: Path | None) -> None:
    if not path or not path.exists():
        return
    size = path.stat().st_size
    if size > ARCHIVE_SIZE_WARNING_BYTES:
        ctx.archive_size_warnings.append(
            f"Архив {rel_display(path, ctx.workspace.workspace_root)} большой: {size / (1024 * 1024):.1f} MiB"
        )


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------


def status_command() -> int:
    try:
        workspace = discover_workspace()
    except DevctlError as exc:
        print(f"[ОШИБКА] {exc}")
        return 2

    print_header("статус devctl")
    print(f"версия devctl: {DEVCTL_VERSION}")
    print(f"Корень проекта:       {workspace.project_root}")
    print(f"Корень рабочей области: {workspace.workspace_root}")
    print(f"Каталог патчей:        {workspace.patches_dir} {'[нет]' if not workspace.patches_dir.is_dir() else ''}")
    print(f"Каталог архивов:       {workspace.archives_dir} {'[нет]' if not workspace.archives_dir.is_dir() else ''}")

    print_header("git")
    if not git_available():
        print("git: не найден")
    elif not (workspace.project_root / ".git").exists():
        print("git: корень проекта не является репозиторием Git")
    else:
        print(git_status_short(workspace.project_root) or "неизвестно")
        print(f"Последний коммит: {git_last_commit_summary(workspace.project_root)}")
        status = git_status_porcelain(workspace.project_root)
        print("Рабочее дерево: чистое" if not status.strip() else "Рабочее дерево: есть изменения")
        if status.strip():
            print("Сводка изменений:")
            for line in status.splitlines()[:50]:
                print(f"  {line}")
            if len(status.splitlines()) > 50:
                print("  ...")
            dirty_lines = status.splitlines()
            if any("tools/" in line or "tools\\" in line for line in dirty_lines) and any(
                "docs/devctl/" in line or "docs\\devctl\\" in line for line in dirty_lines
            ):
                print("Подсказка: это похоже на состояние bootstrap/обновления devctl. Сделайте commit/push перед повторным start.")
        try:
            branch = git_branch(workspace.project_root)
            # Do not fetch in status; just inspect existing remote ref if present.
            ahead, behind, error = ahead_behind(workspace.project_root, "origin", branch)
            if error:
                print(f"Ahead/behind: недоступно ({error})")
            else:
                print(f"Ahead/behind origin/{branch}: ahead={ahead}, behind={behind}")
        except DevctlError as exc:
            print(f"Ahead/behind: недоступно ({exc})")

    print_header("патчи")
    state = {"version": STATE_VERSION, "runs": []}
    try:
        state = load_state(workspace)
    except DevctlError as exc:
        print(f"Реестр состояния: ошибка: {exc}")
    candidates = list_patch_candidates(workspace)
    if not candidates:
        print("Zip-файлы патчей не найдены.")
    else:
        latest = candidates[0]
        status_text = "ожидает применения"
        applied_run = find_state_run(state, latest.sha256, latest.patch_id)
        if applied_run:
            status_text = f"уже применён локально ({applied_run.get('commitSha') or 'без коммита'})"
        elif patch_seen_in_git(workspace.project_root, latest.sha256, latest.patch_id):
            status_text = "уже присутствует в трейлерах недавних Git-коммитов"
        elif latest.manifest_error:
            status_text = f"некорректный кандидат: {latest.manifest_error}"
        print(f"Последний кандидат: {latest.path.name}")
        print(f"ID патча:           {latest.patch_id or 'неизвестно'}")
        print(f"Название:           {latest.title or 'неизвестно'}")
        print(f"SHA-256:            {latest.sha256 or 'неизвестно'}")
        print(f"Статус:             {status_text}")
        print(f"Всего кандидатов:   {len(candidates)}")

    print_header("состояние")
    runs = state.get("runs", []) if isinstance(state, dict) else []
    print(f"Файл состояния: {workspace.state_file} {'[нет]' if not workspace.state_file.exists() else ''}")
    print(f"Записано запусков: {len(runs)}")
    failed = latest_failed_run(state)
    if failed:
        print(f"Последний неуспешный запуск: {failed.get('status')} / {failed.get('patchId')} / {failed.get('report')}")
    latest_archive = latest_archive_dir(workspace)
    if latest_archive:
        print(f"Последний каталог архивов: {latest_archive}")
    return 0


def latest_archive_dir(workspace: Workspace) -> str | None:
    if not workspace.archives_dir.is_dir():
        return None
    dirs = [path for path in workspace.archives_dir.iterdir() if path.is_dir()]
    if not dirs:
        return None
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return rel_display(dirs[0], workspace.workspace_root)


# ---------------------------------------------------------------------------
# Start command
# ---------------------------------------------------------------------------


def prepare_context(workspace: Workspace, state: dict[str, Any]) -> RunContext | None:
    candidates = list_patch_candidates(workspace)
    if not candidates:
        print("Zip-файлы патчей не найдены. Делать нечего.")
        return None
    patch = find_latest_unapplied_patch(workspace, state, candidates)
    if patch is None:
        latest = candidates[0]
        applied = find_state_run(state, latest.sha256, latest.patch_id)
        print("Неприменённых патчей не найдено. Делать нечего.")
        if applied:
            print(f"Последний патч уже применён: {latest.path.name} -> {applied.get('commitSha') or 'без коммита'}")
        else:
            print(f"Последний патч уже виден в недавней истории Git: {latest.path.name}")
        return None
    manifest = patch.manifest
    if patch.manifest_error or manifest is None:
        # Minimal context with synthetic manifest for diagnostic report.
        diagnostic = {
            "formatVersion": 1,
            "patchId": patch.path.stem,
            "title": "Некорректный патч",
            "summary": patch.manifest_error or "Не удалось прочитать manifest.json",
            "apply": {"filesRoot": "files", "delete": []},
            "checks": [],
            "commit": {"enabled": False, "message": "некорректный патч"},
            "push": {"enabled": False},
        }
        ctx = RunContext(workspace=workspace, patch=patch, manifest=diagnostic, started_at=now_utc())
        ctx.status = "invalid_patch"
        ctx.errors.append(patch.manifest_error or "Некорректный патч")
        ctx.run_dir = create_run_dir(workspace, diagnostic, patch.sha256)
        ctx.logs_dir = ctx.run_dir / "logs"
        ctx.logs_dir.mkdir(parents=True, exist_ok=True)
        write_report(ctx)
        update_state_from_context(ctx)
        print(f"Некорректный патч: {patch.path.name}")
        print(f"Отчёт: {ctx.report_path}")
        return None
    return RunContext(workspace=workspace, patch=patch, manifest=manifest, started_at=now_utc())


def start_command(args: argparse.Namespace) -> int:
    try:
        workspace = discover_workspace()
        validate_workspace_for_start(workspace)
        state = load_state(workspace)
        ctx = prepare_context(workspace, state)
        if ctx is None:
            return 0

        try:
            validate_manifest(ctx.manifest)
            validate_patch_files_root(ctx.patch, ctx.manifest)

            # Git/environment prerequisites are deliberately checked before creating a pre archive or applying patch.
            validate_git_preflight(workspace, ctx.manifest, ctx, no_push=args.no_push)
            validate_check_prerequisites(workspace.project_root, ctx.manifest)

            ctx.run_dir = create_run_dir(workspace, ctx.manifest, ctx.patch.sha256)
            ctx.logs_dir = ctx.run_dir / "logs"
            ctx.logs_dir.mkdir(parents=True, exist_ok=True)
            copy_manifest_to_logs(ctx)
            write_log(ctx, "git-status-before.log", ctx.git_status_before or git_status_porcelain(workspace.project_root))

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = slugify(
                (ctx.manifest.get("archive") if isinstance(ctx.manifest.get("archive"), dict) else {}).get("nameSlug")
                or ctx.manifest.get("patchId")
            )
            pre_name = archive_name(workspace.project_root.name, timestamp, "pre", f"before_{slug}")
            ctx.pre_archive, _ = create_project_archive(
                workspace,
                ctx.run_dir / pre_name,
                manifest=ctx.manifest,
            )
            warn_archive_size(ctx, ctx.pre_archive)

            ctx.applied_started = True
            apply_deletions(ctx)
            safe_copy_files(ctx)
            ctx.git_status_after_apply = git_status_porcelain(workspace.project_root)
            write_log(ctx, "git-status-after-apply.log", ctx.git_status_after_apply)

            run_checks(ctx)
            ctx.git_status_after_checks = git_status_porcelain(workspace.project_root)
            write_log(ctx, "git-status-after-checks.log", ctx.git_status_after_checks)
            ctx.changes_introduced_by_checks = new_changes_after_checks(
                ctx.git_status_after_apply,
                ctx.git_status_after_checks,
            )
            if ctx.changes_introduced_by_checks:
                ctx.warnings.append("Проверки внесли дополнительные изменения Git; см. раздел отчёта 'Новые изменения, внесённые проверками'.")

            try:
                commit_and_push(ctx)
            except DevctlError as exc:
                if str(exc).startswith("PUSH_FAILED") or ctx.status == "push_failed":
                    ctx.status = "push_failed"
                else:
                    ctx.status = "failed"
                ctx.errors.append(str(exc))
                failed_name = archive_name(workspace.project_root.name, timestamp, "failed", f"after_failed_{slug}")
                ctx.failed_archive, _ = create_project_archive(workspace, ctx.run_dir / failed_name, manifest=ctx.manifest)
                warn_archive_size(ctx, ctx.failed_archive)
                write_report(ctx)
                update_state_from_context(ctx)
                print(f"[ОШИБКА] {ctx.status}. Отчёт: {ctx.report_path}")
                return 1

            gitsha = short_sha(ctx.commit_sha or git_head(workspace.project_root))
            post_name = archive_name(workspace.project_root.name, timestamp, "post", f"after_{slug}", gitsha)
            ctx.post_archive, _ = create_project_archive(workspace, ctx.run_dir / post_name, manifest=ctx.manifest)
            warn_archive_size(ctx, ctx.post_archive)
            ctx.status = "applied"
            write_report(ctx)
            update_state_from_context(ctx)
            print(f"[OK] Патч применён: {ctx.manifest.get('patchId')}")
            if ctx.commit_sha:
                print(f"Коммит: {ctx.commit_sha}")
            if ctx.post_archive:
                print(f"Архив: {ctx.post_archive}")
            print(f"Отчёт: {ctx.report_path}")
            return 0

        except InvalidPatchError as exc:
            ctx.status = "invalid_patch"
            ctx.errors.append(str(exc))
            if not ctx.run_dir:
                ctx.run_dir = create_run_dir(workspace, ctx.manifest, ctx.patch.sha256)
                ctx.logs_dir = ctx.run_dir / "logs"
                ctx.logs_dir.mkdir(parents=True, exist_ok=True)
                copy_manifest_to_logs(ctx)
            write_report(ctx)
            update_state_from_context(ctx)
            print(f"[НЕКОРРЕКТНЫЙ ПАТЧ] {exc}")
            print(f"Отчёт: {ctx.report_path}")
            return 2

        except PreflightError as exc:
            ctx.status = "preflight_failed"
            ctx.errors.append(str(exc))
            if not ctx.run_dir:
                ctx.run_dir = create_run_dir(workspace, ctx.manifest, ctx.patch.sha256)
                ctx.logs_dir = ctx.run_dir / "logs"
                ctx.logs_dir.mkdir(parents=True, exist_ok=True)
                copy_manifest_to_logs(ctx)
                write_log(ctx, "git-status-before.log", ctx.git_status_before or git_status_porcelain(workspace.project_root))
            write_report(ctx)
            update_state_from_context(ctx)
            print(f"[ПРЕДПОЛЁТНАЯ ПРОВЕРКА НЕ ПРОШЛА] {exc}")
            print(f"Отчёт: {ctx.report_path}")
            return 2

        except CheckFailedError as exc:
            ctx.status = "failed"
            ctx.errors.append(str(exc))
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = slugify(
                (ctx.manifest.get("archive") if isinstance(ctx.manifest.get("archive"), dict) else {}).get("nameSlug")
                or ctx.manifest.get("patchId")
            )
            ctx.git_status_after_checks = git_status_porcelain(workspace.project_root)
            write_log(ctx, "git-status-after-checks.log", ctx.git_status_after_checks)
            ctx.changes_introduced_by_checks = new_changes_after_checks(
                ctx.git_status_after_apply,
                ctx.git_status_after_checks,
            )
            failed_name = archive_name(workspace.project_root.name, timestamp, "failed", f"after_failed_{slug}")
            ctx.failed_archive, _ = create_project_archive(workspace, ctx.run_dir / failed_name, manifest=ctx.manifest)
            warn_archive_size(ctx, ctx.failed_archive)
            write_report(ctx)
            update_state_from_context(ctx)
            print(f"[ПРОВЕРКА НЕ ПРОШЛА] {exc}")
            print(f"Отчёт: {ctx.report_path}")
            return 1

    except KeyboardInterrupt:
        print("\n[ПРЕРВАНО] devctl прерван пользователем.")
        # Лучшее возможное сохранение отчёта, если контекст есть в locals().
        ctx_obj = locals().get("ctx")
        if isinstance(ctx_obj, RunContext):
            ctx_obj.status = "interrupted"
            ctx_obj.errors.append("Прервано пользователем")
            if ctx_obj.applied_started and ctx_obj.run_dir:
                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    slug = slugify(ctx_obj.manifest.get("patchId"))
                    failed_name = archive_name(ctx_obj.workspace.project_root.name, timestamp, "failed", f"after_interrupted_{slug}")
                    ctx_obj.failed_archive, _ = create_project_archive(
                        ctx_obj.workspace,
                        ctx_obj.run_dir / failed_name,
                        manifest=ctx_obj.manifest,
                    )
                except Exception as exc:
                    ctx_obj.warnings.append(f"Не удалось создать архив состояния после прерывания: {exc}")
            try:
                write_report(ctx_obj)
                update_state_from_context(ctx_obj)
                print(f"Отчёт: {ctx_obj.report_path}")
            except Exception as exc:
                print(f"Не удалось записать отчёт о прерывании: {exc}")
        return 130
    except DevctlError as exc:
        print(f"[ОШИБКА] {exc}")
        return 2



# ---------------------------------------------------------------------------
# Init / inspect / plan commands
# ---------------------------------------------------------------------------


def posix_rel_or_dot(path: Path, base: Path) -> str:
    try:
        rel = path.resolve().relative_to(base.resolve())
        return rel.as_posix() or "."
    except Exception:
        return path.as_posix()


def init_command(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    project_path = Path(args.project).expanduser()
    if project_path.is_absolute():
        project_root = project_path.resolve()
    else:
        project_root = (workspace_root / project_path).resolve()

    patches_dir = (workspace_root / args.patches).resolve()
    archives_dir = (workspace_root / args.archives).resolve()
    state_dir = workspace_root / ".devctl"
    config_path = state_dir / "workspace.json"

    if config_path.exists() and not args.force:
        raise DevctlError(f"Конфигурация рабочей области уже существует: {config_path}. Используйте --force для перезаписи.")

    patches_dir.mkdir(parents=True, exist_ok=True)
    archives_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "version": 1,
        "projectDir": posix_rel_or_dot(project_root, workspace_root),
        "patchesDir": posix_rel_or_dot(patches_dir, workspace_root),
        "archivesDir": posix_rel_or_dot(archives_dir, workspace_root),
        "git": {
            "enabled": True,
            "autoCommit": True,
            "autoPush": True,
            "remote": "origin",
            "requireClean": True,
            "requireUpToDate": True,
        },
        "archive": {
            "exclude": sorted(ARCHIVE_EXCLUDED_PARTS) + list(ARCHIVE_EXCLUDED_SUFFIXES),
        },
        "checkProfiles": {
            "default": []
        },
    }
    write_json_file(config_path, config)
    if not (state_dir / "state.json").exists():
        write_json_file(state_dir / "state.json", {"version": STATE_VERSION, "runs": []})

    print_header("devctl init")
    print(f"Корень рабочей области: {workspace_root}")
    print(f"Корень проекта:        {project_root} {'[нет]' if not project_root.exists() else ''}")
    print(f"Каталог патчей:       {patches_dir}")
    print(f"Каталог архивов:      {archives_dir}")
    print(f"Конфигурация:         {config_path}")
    if not project_root.exists():
        print("Предупреждение: каталог проекта пока не существует. Создайте его перед запуском start.")
    return 0


def select_patch_for_readonly(workspace: Workspace, patch_arg: str | None) -> PatchCandidate | None:
    if patch_arg:
        path = Path(patch_arg).expanduser()
        if not path.is_absolute():
            candidates = [Path.cwd() / path, workspace.patches_dir / path]
            path = next((p for p in candidates if p.exists()), candidates[0])
        manifest, error = read_manifest_from_zip(path)
        candidate = PatchCandidate(path=path, manifest=manifest, manifest_error=error, sort_key=candidate_sort_key(path, manifest))
        try:
            candidate.sha256 = sha256_file(path)
        except Exception as exc:
            candidate.manifest_error = f"не удалось посчитать hash патча: {exc}"
        return candidate
    candidates = list_patch_candidates(workspace)
    return candidates[0] if candidates else None


def zip_files_under_root(path: Path, files_root: str) -> list[str]:
    prefix = files_root.rstrip("/") + "/"
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return sorted(name for name in zf.namelist() if name.startswith(prefix) and not name.endswith("/"))
    except Exception:
        return []


def inspect_command(args: argparse.Namespace, *, plan: bool = False) -> int:
    workspace = discover_workspace()
    patch = select_patch_for_readonly(workspace, args.patch)
    if not patch:
        print("Zip-файлы патчей не найдены.")
        return 0

    print_header("devctl plan" if plan else "devctl inspect")
    print(f"Файл патча: {patch.path}")
    print(f"SHA-256:    {patch.sha256 or 'неизвестно'}")
    if patch.manifest_error:
        print(f"Манифест:   НЕКОРРЕКТЕН — {patch.manifest_error}")
        return 2
    assert patch.manifest is not None
    manifest = patch.manifest
    print(f"ID патча:   {manifest.get('patchId', 'неизвестно')}")
    print(f"Название:   {manifest.get('title', 'неизвестно')}")
    print(f"Сводка:     {manifest.get('summary', '')}")

    try:
        validate_manifest(manifest)
        validate_patch_files_root(patch, manifest)
        print("Валидация: OK")
    except InvalidPatchError as exc:
        print(f"Валидация: НЕКОРРЕКТНО — {exc}")
        return 2

    apply_cfg = manifest.get("apply", {}) if isinstance(manifest.get("apply"), dict) else {}
    files_root = apply_cfg.get("filesRoot", "files")
    copied = zip_files_under_root(patch.path, files_root)
    deletes = apply_cfg.get("delete", []) if isinstance(apply_cfg.get("delete", []), list) else []
    checks = manifest.get("checks", []) if isinstance(manifest.get("checks", []), list) else []
    commit = manifest.get("commit", {}) if isinstance(manifest.get("commit"), dict) else {}
    push = manifest.get("push", {}) if isinstance(manifest.get("push"), dict) else {}

    print_header("применение")
    print(f"Корень файлов: {files_root}")
    print(f"Файлов к копированию: {len(copied)}")
    for name in copied[:80]:
        print(f"  + {name[len(str(files_root).rstrip('/') + '/'):]}")
    if len(copied) > 80:
        print(f"  ... ещё {len(copied) - 80}")
    print(f"Путей к удалению: {len(deletes)}")
    for entry in deletes[:80]:
        if isinstance(entry, dict):
            print(f"  - {entry.get('path')} recursive={entry.get('recursive', False)} required={entry.get('required', False)}")

    print_header("проверки")
    if checks:
        for check in checks:
            if isinstance(check, dict):
                print(f"  - {check.get('name')}: {check.get('command')}  [cwd={check.get('cwd')}]")
    else:
        print("Проверки не объявлены.")

    print_header("commit / push")
    try:
        current_branch = git_branch(workspace.project_root)
    except DevctlError:
        current_branch = None
    push_enabled, remote, branch, note = effective_push_policy(
        workspace, manifest, current_branch=current_branch
    )
    print("Политика конвейера по умолчанию: проверки -> commit -> push")
    print(f"Сообщение коммита: {commit.get('message', '')}")
    if commit.get("enabled") is False:
        print("Примечание commit: manifest commit.enabled=false будет проигнорирован командой start")
    print(f"Push включён:     {push_enabled}")
    print(f"Цель push:        {remote}/{branch}")
    print(f"Примечание push:  {note}")

    if plan:
        print_header("dry-run")
        print("Файлы не изменялись. Запустите `devctl start`, чтобы выполнить конвейер.")
    return 0

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"devctl v{DEVCTL_VERSION} — проектно-независимый конвейер ИИ-патчей",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="показать это сообщение и выйти")
    parser._positionals.title = "команды"
    parser._optionals.title = "параметры"
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{init,status,inspect,plan,start}")

    init = subparsers.add_parser("init", help="Создать .devctl/workspace.json, patches/ и archives/")
    init.add_argument("--workspace", default=None, help="Корень рабочей области. По умолчанию текущий каталог.")
    init.add_argument("--project", default=DEFAULT_PROJECT_DIR_NAME, help="Каталог проекта относительно рабочей области или абсолютный путь.")
    init.add_argument("--patches", default=DEFAULT_PATCHES_DIR_NAME, help="Каталог патчей относительно рабочей области.")
    init.add_argument("--archives", default=DEFAULT_ARCHIVES_DIR_NAME, help="Каталог архивов относительно рабочей области.")
    init.add_argument("--force", action="store_true", help="Перезаписать существующий .devctl/workspace.json")

    subparsers.add_parser("status", help="Показать состояние рабочей области/Git/патчей без изменений")
    inspect = subparsers.add_parser("inspect", help="Проверить zip-патч без изменения файлов")
    inspect.add_argument("patch", nargs="?", help="Путь/имя zip-патча. По умолчанию последний патч в patches/.")
    plan = subparsers.add_parser("plan", help="Показать dry-run-план zip-патча без изменения файлов")
    plan.add_argument("patch", nargs="?", help="Путь/имя zip-патча. По умолчанию последний патч в patches/.")
    start = subparsers.add_parser("start", help="Применить последний неприменённый патч, выполнить проверки, commit и push")
    start.add_argument("--no-push", action="store_true", help="Отладочный/локальный запуск: commit после зелёных проверок, но без git push")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return init_command(args)
        if args.command == "status":
            return status_command()
        if args.command == "inspect":
            return inspect_command(args)
        if args.command == "plan":
            return inspect_command(args, plan=True)
        if args.command == "start":
            return start_command(args)
    except DevctlError as exc:
        print(f"[ОШИБКА] {exc}")
        return 2
    parser.error(f"неизвестная команда: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
