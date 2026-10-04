# Конфигурация

У devctl три места с настройками и состоянием: конфигурация workspace, журнал запусков и
пользовательский конфиг для приёма патчей.

## `.devctl/workspace.json`

Создаётся командой `devctl init`. Пример — [workspace.example.json](workspace.example.json).

```json
{
  "version": 1,
  "projectDir": "project",
  "patchesDir": "patches",
  "archivesDir": "archives",
  "userTestSpaceDir": "UserTestSpace",
  "git": {"enabled": true, "autoPush": true, "remote": "origin", "branch": "main"},
  "archive": {"exclude": ["UserTestSpace", "__pycache__", "*.pyc"]}
}
```

### Ключи, которые читает devctl

| Ключ | Действие | По умолчанию |
| --- | --- | --- |
| `projectDir` | каталог проекта относительно корня workspace; `.` — проект совпадает с workspace | `project` |
| `patchesDir` | каталог входящих патчей | `patches` |
| `archivesDir` | каталог запусков | `archives` |
| `userTestSpaceDir` | каталог копий для ручного тестирования | `UserTestSpace` |
| `git.autoPush` | `false` — `start` коммитит, но не пушит | `true` |
| `git.enabled` | `false` — то же, что `autoPush: false`: отключает только push | `true` |
| `git.remote` | remote для push, если его не задал манифест | `origin` |
| `git.branch` | ветка для push, если её не задал манифест, и ветка по умолчанию для `sync` | текущая ветка |
| `archive.exclude` | исключения для снимков `devctl sync` и для сравнения деревьев в `devctl zip` | список из `init` |
| `id`, `workspaceId`, `name`, `projectName` | необязательные; из них `devctl workspace register` берёт идентификатор и имя, если не заданы `--id` и `--name` | имя каталога workspace |

Пути — относительные POSIX-пути внутри workspace; абсолютный путь в `projectDir` не принимается.

### Ключи, которые записывает `init`, но код не читает

`git.autoCommit`, `git.requireClean`, `git.requireUpToDate`, `checkProfiles`. Их значения ни на что не
влияют: `start` всегда требует чистое рабочее дерево, всегда коммитит после зелёных проверок и, когда
push включён, всегда требует совпадения локальной ветки с remote. Отключить эти требования
конфигурацией нельзя.

Незнакомые ключи devctl не трогает; `init --upgrade` сохраняет их как есть.

### `init --upgrade`

Добавляет недостающие `version`, `projectDir`, `patchesDir`, `archivesDir`, `userTestSpaceDir`, разделы
`git`, `archive`, `checkProfiles`, обязательные исключения (`UserTestSpace`, `__pycache__`,
`.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.pyc`, `*.pyo`) и создаёт отсутствующие каталоги и
`state.json`. Существующие значения не переписывает. О том, что обновление нужно, сообщает
`devctl status`.

## `.devctl/state.json`

Журнал запусков; правится только самим devctl.

```json
{
  "version": 1,
  "runs": [
    {
      "patchId": "2026-05-05-demo-change",
      "patchFile": "patch_20260505_120000_demo.zip",
      "patchSha256": "…",
      "status": "applied",
      "startedAt": "2026-05-05T12:00:01+00:00",
      "finishedAt": "2026-05-05T12:00:09+00:00",
      "commitSha": "…",
      "archiveDir": "archives/20260505_150001_demo-change_ab12cd3",
      "report": "archives/20260505_150001_demo-change_ab12cd3/report.md",
      "utsProjectDir": "UserTestSpace/project_20260505_150009_after_demo-change_1a2b3c4/project"
    }
  ]
}
```

Запись также хранит сведения об автоматическом откате (`autoResetPerformed`, `autoResetTarget`,
`autoResetCleanMode`, `autoResetError`), удалении плохого патча (`badPatchDeleted`,
`badPatchDeleteError`), ошибке копии UTS (`utsError`) и очистке bytecode.

Если проект хранится в Git вместе с `.devctl/workspace.json`, добавьте `.devctl/state.json` в
`.gitignore`: журнал — локальное состояние машины.

## Пользовательский конфиг

Не привязан к workspace.

| ОС | Каталог |
| --- | --- |
| Linux, macOS | `~/.config/devctl/` |
| Windows | `%APPDATA%\devctl\` |

- `config.json` — склады патчей и зарегистрированные workspace:

  ```json
  {
    "version": 1,
    "patchInboxDirs": ["/home/me/PatchInbox"],
    "workspaces": [{"id": "myapp", "name": "My App", "path": "/home/me/spaces/myapp"}]
  }
  ```

- `inbox_index.json` — что и куда было импортировано (по SHA-256), чтобы не принять патч дважды.

Команды, которые их меняют, описаны в [patch-intake.md](patch-intake.md).

## Переменные окружения

| Переменная | Действие |
| --- | --- |
| `DEVCTL_WORKSPACE` | workspace по умолчанию, как `-w` |
| `XDG_DATA_HOME` | база для управляемой копии devctl и completion-файлов bash и zsh; по умолчанию `~/.local/share` |
| `XDG_CONFIG_HOME` | база для completion-файла fish; по умолчанию `~/.config` |

Каталоги установки (`~/.local/bin`, `~/.local/share/devctl`) описаны в [release-cli.md](release-cli.md).
