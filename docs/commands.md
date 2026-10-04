# Справочник команд devctl 0.8.0

Общий вид: `devctl [-w WORKSPACE] <команда> [флаги]`. Без установки — `python3 devctl.py …`.

## Выбор workspace

По порядку:

1. `-w/--workspace <путь>` или переменная `DEVCTL_WORKSPACE`. Путь может указывать на корень workspace,
   на `.devctl/workspace.json`, на каталог `.devctl` или на каталог проекта без конфигурации.
2. Ближайший `.devctl/workspace.json` от текущего каталога вверх, затем от каталога, в котором лежит
   сам `devctl.py`.
3. Если конфигурации нет — ближайший каталог, похожий на проект (`.git` либо `pyproject.toml`,
   `package.json`, `Cargo.toml`, `go.mod`, `CMakeLists.txt`, `pom.xml`, `build.gradle`, `Makefile`,
   `README.md`). Тогда `patches/`, `archives/`, `UserTestSpace/` и `.devctl/` ищутся рядом с ним,
   в родительском каталоге.

## Коды возврата

| Код | Значение |
| --- | --- |
| 0 | успех; для `start` также «применять нечего» |
| 1 | `start`: проверка не прошла, commit или push завершился ошибкой |
| 2 | некорректный патч, непройденная предполётная проверка, ошибка workspace или аргументов |
| 130 | `start` прерван с клавиатуры |

Флаг `--json` есть у большинства команд. У `start`, `reset` и `init` он добавляет JSON-строку к обычному
выводу, у остальных — заменяет его.

## init

```bash
devctl init --project project --create-project --git-init --branch main
devctl init --project . --git-init                 # проект совпадает с корнем workspace
devctl init --remote-url git@github.com:me/app.git # клонировать или подтянуть проект
devctl init --upgrade                              # дополнить старый workspace
```

Создаёт `patches/`, `archives/`, `UserTestSpace/`, `.devctl/workspace.json` и пустой `.devctl/state.json`.

| Флаг | Назначение |
| --- | --- |
| `--workspace DIR` | корень workspace; по умолчанию текущий каталог |
| `--project DIR` | каталог проекта внутри workspace: относительный путь или абсолютный путь, лежащий в workspace |
| `--patches`, `--archives`, `--uts` | имена служебных каталогов |
| `--create-project` | создать каталог проекта, если его нет |
| `--git-init` | инициализировать Git-репозиторий в проекте |
| `--branch NAME` | основная ветка; попадает в `git.branch` конфигурации |
| `--remote-url URL` | origin: проект будет клонирован или синхронизирован через fetch/pull |
| `--force` | перезаписать существующий `.devctl/workspace.json` |
| `--upgrade` | добавить недостающие каталоги и поля, не трогая пути, проект, патчи и архивы |

## status, inspect, plan

Ничего не меняют.

- `devctl status` — пути workspace, состояние Git (ветка, последний коммит, чистота дерева,
  ahead/behind), последний патч-кандидат и его статус, число записей в журнале, последний неуспешный
  запуск. Подсказывает, когда конфигурации нужен `init --upgrade`.
- `devctl inspect [патч]` — манифест, список файлов, удаления, проверки, цель push.
- `devctl plan [патч]` — то же в виде плана применения.

`патч` — путь или имя файла в `patches/`. По умолчанию берётся последний неприменённый.

## start

```bash
devctl start
devctl start --no-push
```

Применяет **один** патч — последний неприменённый. Порядок кандидатов: время изменения файла в
`patches/`, затем `createdAt` из манифеста, затем метка в имени `patch_YYYYMMDD_HHMMSS_*`. Применённым
считается патч, чей SHA-256 или `patchId` есть в журнале `.devctl/state.json` либо в трейлерах последних
100 коммитов.

| Флаг | Назначение |
| --- | --- |
| `--no-push` | commit после зелёных проверок, но без `git push` |
| `--keep-failed-patch` | не удалять patch.zip после упавших проверок или частичного применения |
| `--json` | финальная JSON-строка: отчёт, архив, коммит, результат push |

Шаги, статусы и содержимое каталога запуска — в [pipeline.md](pipeline.md).

## reset

```bash
devctl reset
devctl reset --target HEAD~1 --keep-patch
```

Выполняет в проекте `git reset --hard <target>` и `git clean -fd`, затем удаляет из `patches/` файл
последнего неуспешного запуска, если он известен журналу.

| Флаг | Назначение |
| --- | --- |
| `--target REF` | цель `reset --hard`; по умолчанию `HEAD` |
| `--clean-mode fd\|fdx` | режим `git clean`; по умолчанию `fd` |
| `--keep-patch` | не удалять патч |
| `--delete-patch NAME.zip` | удалить указанный файл внутри `patches/` |

`fdx` удаляет и игнорируемые файлы. Если проект совпадает с корнем workspace, это сотрёт `patches/`,
`archives/` и `UserTestSpace/`.

## sync

```bash
devctl sync
devctl sync --discard-local
```

Подтягивает проект из remote и освежает артефакты: `project → archives → UserTestSpace`. Если проект
ещё не репозиторий — клонирует его (нужен `--remote-url`). После синхронизации создаёт каталог
`archives/<время>_workspace-sync_<sha>/` с post-снимком и `sync-report.json` и разворачивает снимок в
`UserTestSpace`.

| Флаг | Назначение |
| --- | --- |
| `--remote NAME` | имя remote; по умолчанию `origin`. При клонировании remote всегда `origin` |
| `--remote-url URL` | адрес remote, если проект ещё не привязан |
| `--branch NAME` | ветка-источник; по умолчанию `git.branch`, HEAD remote, текущая ветка или `main`. При клонировании — HEAD remote или `main` |
| `--discard-local` | считать remote источником истины: `reset --hard <remote>/<branch>` + `git clean` |
| `--clean-mode fd\|fdx` | режим `git clean` для `--discard-local` |
| `--no-archive` | не создавать снимок (тогда нужен и `--no-uts`) |
| `--no-uts` | не разворачивать снимок в `UserTestSpace` |

Без `--discard-local` используется только fast-forward.

## zip

```bash
devctl zip
devctl zip --level full --with-final
devctl zip --budget-kb 800 --output ~/Desktop/
devctl zip --dry-run
```

Собирает эволюционный архив workspace. Ничего, кроме самого архива, не создаёт и не меняет; Git нужен
только для чтения истории и не обязателен. Команде нужен инициализированный workspace: если у
найденного workspace нет `.devctl/workspace.json`, она откажется работать, чтобы не просматривать
случайный каталог. Поиск workspace общий для всех команд (см. выше): `python3 /путь/devctl.py zip`,
запущенный вне любого workspace, найдёт конфигурацию рядом с самим `devctl.py`, если она там есть.

| Флаг | Назначение |
| --- | --- |
| `--level brief\|normal\|full\|max` | бюджет текста: 256 КиБ, 512 КиБ (по умолчанию), 2 МиБ, без ограничения |
| `--budget-kb N` | точный бюджет в КиБ вместо уровня; `0` — без ограничения |
| `--output PATH` | файл или каталог результата |
| `--with-final` | приложить текстовые файлы итогового состояния проекта в `final/` (сверх бюджета; файлы больше 512 КиБ пропускаются) |
| `--dry-run` | посчитать и показать статистику, архив не писать |
| `--quiet` | не печатать ход работы |
| `--json` | JSON со статистикой и путём архива |

По умолчанию архив `<имя-workspace>_evolution_<YYYYMMDD_HHMMSS>.zip` (имя — в нижнем регистре)
кладётся в корень workspace. Если проект совпадает с корнем workspace — в `archives/`, чтобы рабочее
дерево осталось чистым. Прежние эволюционные архивы при повторном запуске пропускаются; они узнаются
по содержимому, даже если файл переименован.

Подробности — в [evolution-archive.md](evolution-archive.md).

## workspace, inbox

```bash
devctl workspace register . --id myapp --name "My App"
devctl inbox init --path ~/PatchInbox
devctl inbox scan
devctl inbox grab
devctl inbox grab --all --dry-run
devctl inbox grab --workspace myapp
```

Приём патчей из общего склада в `patches/` нужного workspace. Сам патч при этом не применяется.
Подробности — в [patch-intake.md](patch-intake.md).

## self, completion

```bash
devctl self install --with-completions
devctl self update [--pull-source] [--source /path/to/devctl.py]
devctl self info
devctl self install-completions --shell auto
devctl self uninstall --with-completions
devctl completion bash|zsh|fish
```

Подробности — в [release-cli.md](release-cli.md).
