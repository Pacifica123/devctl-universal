# devctl — конвейер ИИ-патчей

`devctl` — один файл на чистом Python (`devctl.py`, только стандартная библиотека), который превращает
разработку с нейросетью в повторяемый поток: изменения приходят zip-патчами, каждый патч проверяется,
применяется, коммитится и оставляет после себя полный след — отчёт, логи, снимки проекта и копию для
ручного тестирования.

Версия: **0.8.0**. Журнал изменений — в [docs/CHANGELOG.md](docs/CHANGELOG.md).

## Что он делает

```text
patch.zip в patches/
   │  devctl plan     посмотреть, что будет сделано, ничего не меняя
   ▼
devctl start
   проверка манифеста и путей → предполётная проверка Git → снимок «до» →
   удаления и наложение файлов → проверки из манифеста → commit → push →
   снимок «после» → копия в UserTestSpace → report.md → запись в журнал
```

Если проверка не прошла, `start` сохраняет снимок сломанного состояния, откатывает проект
(`git reset --hard HEAD` + `git clean -fd`) и убирает плохой патч из `patches/`. Откату нужен хотя бы
один коммит в проекте.

`devctl zip` (новое в 0.8.0) собирает всю историю workspace — коммиты, патчи, запуски, копии проекта и
посторонние материалы вокруг него — в один небольшой архив, который можно целиком отдать нейросети.

## Быстрый старт

Нужны Python 3.9+ и Git. Версия 0.8.0 проверена на Python 3.11, 3.12 и 3.13.

```bash
python3 devctl.py self install --with-completions   # или ./install.sh
devctl --version

mkdir my-space && cd my-space
devctl init --project project --create-project --git-init --branch main
# положить patch_YYYYMMDD_HHMMSS_slug.zip в patches/
devctl plan
devctl start --no-push      # без --no-push после коммита выполняется git push
devctl zip                  # история workspace одним архивом
```

Без установки те же команды работают как `python3 devctl.py <команда>`.

## Рабочая область

```text
workspace/
  .devctl/
    workspace.json      # конфигурация: где проект, патчи, архивы, политика push
    state.json          # журнал запусков
  project/              # Git-репозиторий проекта (имя и место задаёт projectDir)
  patches/              # входящие patch.zip
  archives/             # по каталогу на запуск: report.md, logs/, снимки pre/post/failed
  UserTestSpace/        # распакованные post-снимки для ручного тестирования
  …                     # любые другие каталоги и файлы: devctl их не трогает, но `devctl zip` учитывает
```

Проект может совпадать с корнем workspace (`"projectDir": "."`) — так устроен сам этот репозиторий:
`patches/`, `archives/`, `UserTestSpace/` и `.devctl/state.json` тогда должны быть в `.gitignore`.

Workspace ищется от текущего каталога вверх по `.devctl/workspace.json`. Явно его задают `-w <путь>`
или переменная `DEVCTL_WORKSPACE`.

## Команды

| Команда | Что делает | Меняет файлы |
| --- | --- | --- |
| `devctl init` | создаёт workspace; `--upgrade` дополняет старый | да |
| `devctl status` | состояние workspace, Git и очереди патчей | нет |
| `devctl inspect [патч]` | разбор патча: манифест, файлы, проверки | нет |
| `devctl plan [патч]` | план применения | нет |
| `devctl start` | применяет последний неприменённый патч | да |
| `devctl reset` | откатывает проект и убирает последний упавший патч | да |
| `devctl sync` | подтягивает проект из remote, делает свежий снимок и копию UTS | да |
| `devctl zip` | эволюционный архив workspace | только сам архив |
| `devctl workspace register`, `devctl inbox …` | приём патчей из общего склада | да |
| `devctl self …`, `devctl completion …` | установка утилиты и автодополнение | да |

Все флаги и коды возврата — в [docs/commands.md](docs/commands.md).

## Патч

```text
patch_YYYYMMDD_HHMMSS_slug.zip
  manifest.json         # обязателен
  files/                # накладывается поверх проекта
  PATCH_SUMMARY.md      # необязателен; попадает в эволюционный архив
```

Манифест описывает содержимое и проверки. Политику — коммитить ли, пушить ли — задаёт workspace:
`commit.enabled=false` и `push.enabled=false` в манифесте игнорируются с предупреждением.
Формат и правила — в [docs/patch-format.md](docs/patch-format.md), пример —
[docs/patch-manifest.example.json](docs/patch-manifest.example.json).

## Эволюционный архив

```bash
devctl zip                    # ≈ 512 КиБ текста, входит в окно 200 тыс. токенов
devctl zip --level brief      # ≈ 256 КиБ
devctl zip --level full       # ≈ 2 МиБ, для моделей с окном 1 млн токенов
devctl zip --level max        # без ограничения: все диффы и тексты целиком
```

В корне workspace появляется `<имя>_evolution_<время>.zip` с `README.md`, `TIMELINE.md` и `steps/`.
Одинаковые деревья файлов — коммит, копия в `UserTestSpace`, pre/post-снимок, ручная копия вроде
`stables/v1` — считаются одним состоянием; каждое состояние описано отличием от предыдущего; заметки и
прочие материалы стоят в той же хронологии по времени изменения. При разработке 0.8.0 на
синтетическом workspace в 2,1 ГиБ (110 патчей, 357 копий проекта) архив уровня `normal` занял около
215 КиБ и собирался 8 секунд.
Секреты из содержимого `devctl zip` не вычищает: архив повторяет то, что лежит в workspace.

Устройство, формат и ограничения — в [docs/evolution-archive.md](docs/evolution-archive.md).

## Предохранители

- Пути в патче — только относительные POSIX-пути внутри проекта. Записать что-либо в `.git` или
  принести файл `.env` / `.env.*` патч не может; удалить `.git`, `.devctl`, `node_modules`, `target` —
  тоже.
- `start` требует чистое рабочее дерево и, если push включён, совпадение локальной ветки с remote.
- Python bytecode (`__pycache__/`, `*.pyc`, `*.pyo`) из патча не копируется и удаляется после проверок.
- Перед коммитом `start` просматривает `git status` и останавливается, если видит там
  сгенерированные или локальные файлы: `node_modules`, `target`, базы `*.db`/`*.sqlite`, каталоги с
  именами `patches`, `archives`, `UserTestSpace`. Проверка смотрит на строки `git status`, поэтому
  такие файлы внутри целиком нового каталога она не замечает — см. [docs/pipeline.md](docs/pipeline.md).
- Снимки `pre`/`post`/`failed` не содержат `.git`, зависимостей, сборочных каталогов, `.env` и `.env.*`.

## Документация

- [docs/commands.md](docs/commands.md) — справочник команд.
- [docs/pipeline.md](docs/pipeline.md) — как работает `start`: шаги, статусы, каталог запуска, откат.
- [docs/patch-format.md](docs/patch-format.md) — формат патча и манифеста.
- [docs/configuration.md](docs/configuration.md) — `.devctl/workspace.json`, `state.json`, глобальный конфиг.
- [docs/evolution-archive.md](docs/evolution-archive.md) — `devctl zip`.
- [docs/patch-intake.md](docs/patch-intake.md) — приём патчей из общего склада.
- [docs/release-cli.md](docs/release-cli.md) — установка, обновление, автодополнение.
- [docs/CHANGELOG.md](docs/CHANGELOG.md) — история версий.

## Проверка самого devctl

```bash
python -B -m unittest discover -s tests -v
```

Тесты используют только стандартную библиотеку и Git; они строят временный workspace через настоящий
`devctl start` и проверяют на нём `devctl zip`.
