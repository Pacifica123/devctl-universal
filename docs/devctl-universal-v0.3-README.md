# devctl universal v0.4

Проектно-независимый конвейер применения ИИ-патчей на чистом Python.

`devctl` применяет архивы патчей к проекту внутри рабочей области, запускает объявленные проверки, пишет отчёты и логи, создаёт снимки состояния до/после/при ошибке, делает commit, выполняет push и записывает результат в реестр состояния.

## Главная идея

`devctl start` — это «волшебная кнопка»:

```text
применить последний неприменённый патч -> выполнить проверки -> commit -> push
```

Манифест патча описывает содержимое патча и проверки. Он не должен быть обычной поверхностью управления тем, будет ли конвейер делать commit или push. Манифест может подсказать commit-сообщение или цель push, но базовым процессом управляют сам `devctl` и конфигурация рабочей области.

`devctl start --no-push` используйте только для явных локальных или отладочных запусков.

## Структура рабочей области

```text
workspace/
  .devctl/
    workspace.json
    state.json
  project/
    .git/
    ... любой проект ...
  patches/
    patch_YYYYMMDD_HHMMSS_stageN_title.zip
  archives/
    ... артефакты запусков ...
```

## Bootstrap

```bash
cd /path/to/workspace
python3 devctl.py init --project ./project
python3 devctl.py status
```

`init` создаёт `.devctl/workspace.json`, каталоги `patches/`, `archives/` и пустой реестр состояния.

## Команды только для чтения

```bash
python3 devctl.py status
python3 devctl.py inspect
python3 devctl.py inspect patches/patch_20260505_120000_stage2_config_parser.zip
python3 devctl.py plan
```

`inspect` и `plan` никогда не изменяют проект.

## Запуск конвейера

```bash
python3 devctl.py start
```

Поток v0.4:

1. обнаружить рабочую область и проект;
2. найти последний неприменённый zip-патч;
3. проверить `manifest.json` и безопасность путей внутри zip;
4. выполнить предзапусковые проверки Git и push-цели;
5. создать pre-архив проекта;
6. применить удаления и наложить файлы из `files/`;
7. выполнить проверки из манифеста;
8. создать commit после зелёных проверок;
9. выполнить push после успешного commit;
10. создать post-архив или failed-архив;
11. записать отчёт и обновить `.devctl/state.json`.

Локальный аварийный/отладочный режим:

```bash
python3 devctl.py start --no-push
```

## Политика Git

Типовые настройки рабочей области:

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

Приоритет политики:

1. `devctl start --no-push` отключает только шаг push для осознанного локального/отладочного запуска.
2. `.devctl/workspace.json` владеет политикой рабочего процесса по умолчанию.
3. `manifest.commit.message`, `manifest.push.remote` и `manifest.push.branch` могут дать метаданные и цель.
4. `manifest.commit.enabled=false` и `manifest.push.enabled=false` игнорируются обычным `start` с предупреждением, потому что конвейер по умолчанию делает commit+push после зелёных проверок.

## Формат zip-патча

```text
patch_YYYYMMDD_HHMMSS_stageN_title.zip
  manifest.json
  files/
    path/inside/project.ext
  PATCH_SUMMARY.md      # опционально
  reports/              # опционально
```

См. `docs/patch-manifest.example.json`.

## Предохранители и ограничения

- Используется только стандартная библиотека Python.
- Пути в манифесте должны быть относительными POSIX-путями.
- Опасные пути вроде `.git`, `.devctl`, `node_modules`, `target`, `__pycache__` и `*.pyc` блокируются для рискованных операций записи/commit. Deletion-only cleanup уже tracked generated/cache файлов разрешается, потому что удаляет мусор из Git, а не добавляет его.
- `.env` и `.env.*` не копируются из патчей и исключаются из архивов.
- `devctl` проектно-независим: проект выбирается через `.devctl/workspace.json`.
- Снимки `pre/post/failed` нужны не для релиза, а для диагностики и воспроизводимости.

## Практическая философия

`devctl` полезен как дисциплина разработки с ИИ:

- ИИ не «правит всё подряд», а упаковывает изменения в патч.
- Человек или автомат запускает один понятный конвейер.
- Каждый запуск оставляет проверяемый след: hash патча, отчёт, логи, Git-коммит и архив состояния.
- Ошибка не прячется: рабочее дерево остаётся для анализа, а отчёт показывает, где оборвался процесс.
