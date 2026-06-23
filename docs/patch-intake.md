# Patch Intake v0.1

Patch Intake добавляет входной шлюз перед обычным `plan/start`.

Новый цикл:

```text
общий склад patch.zip -> devctl inbox grab -> workspace/patches/ -> devctl plan -> devctl start
```

Функция не применяет патчи автоматически. Она только доставляет валидный `patch.zip` в правильный workspace.

## Глобальный config

Пользовательский config хранится вне workspace:

- Linux/macOS: `~/.config/devctl/config.json`
- Windows: `%APPDATA%/devctl/config.json`

Минимальный формат:

```json
{
  "version": 1,
  "patchInboxDirs": ["D:/PatchInbox"],
  "workspaces": [
    {"id": "devctl", "name": "devctl universal", "path": "D:/projects/devctl-workspace"}
  ]
}
```

## Команды

```bash
devctl workspace register . --id devctl --name "devctl universal"
devctl inbox init --path "D:/PatchInbox"
devctl inbox scan
devctl inbox grab
```

`inbox init` создаёт подпапки `incoming/`, `imported/`, `rejected/`, `duplicate/`.

`inbox scan` ничего не меняет: читает zip-кандидаты, проверяет `manifest.json`, считает SHA-256 и показывает предполагаемый target workspace.

`inbox grab` копирует самый свежий валидный патч в `workspace/patches/`, проверяет SHA-256 копии, переносит оригинал в `imported/` и записывает событие в `~/.config/devctl/inbox_index.json`.

## Target в manifest.json

Поле `target` необязательное. Старые патчи без него остаются валидными.

```json
"target": {
  "projectId": "devctl",
  "workspaceId": "devctl",
  "projectName": "devctl universal",
  "expectedFiles": ["devctl.py", "README.md"]
}
```

`projectId`/`workspaceId` не считаются абсолютной истиной: devctl дополнительно сверяет `expectedFiles` и файлы из payload с зарегистрированным workspace. Если уверенность низкая, CLI требует `--workspace <id>` или ручной выбор, а GUI показывает выбор workspace.

## Безопасность

- zip без `manifest.json` не импортируется;
- имя файла не используется как единственный сигнал target;
- дубликаты блокируются через SHA-256 индекс;
- существующий файл в `workspace/patches/` не перезаписывается;
- `inbox grab` никогда не запускает `plan` или `start` автоматически.
