# devctl как релизная CLI-утилита

Цель v0.5+ — пользоваться `devctl` как обычной командой, а не как локальным `python devctl.py` из конкретной папки. v0.6.0 добавляет `reset` и User Test Space, v0.6.1 — автоочистку Python bytecode/cache внутри `start`, а v0.6.3 — корректный commit deletion-only cleanup для tracked bytecode.

## Установка для пользователя

Из каталога с актуальным `devctl.py`:

```bash
python3 devctl.py self install --with-completions
```

Или короче:

```bash
./install.sh
```

По умолчанию команда `devctl` ставится в `~/.local/bin/devctl`, управляемая копия ядра — в `~/.local/share/devctl/devctl.py`, а метаданные установки — в `~/.local/share/devctl/install.json`. Метаданные запоминают исходный `devctl.py`, Git-корень источника и список установленных completion-оболочек.

Если `~/.local/bin` ещё не в `PATH`, добавь в профиль оболочки:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Проверка:

```bash
devctl --version
devctl self info
```

## Обновление установленной утилиты

После применения патча к репозиторию devctl обнови установленную копию:

```bash
cd /path/to/devctl-repo
devctl self update
```

Если установка уже записала `~/.local/share/devctl/install.json`, то `devctl self update` можно запускать из любого каталога: он возьмёт `sourcePath` из метаданных и автоматически перезапишет ранее установленные completion-файлы.

Для первого перехода со старой v0.5 без метаданных или для нестандартной раскладки можно явно указать источник:

```bash
devctl self update --source /path/to/devctl.py --with-completions
```

Если нужно перед копированием подтянуть исходный Git-репозиторий:

```bash
devctl self update --pull-source
```

`--pull-source` выполняет `git fetch --all --prune` и `git pull --ff-only` в Git-репозитории источника, поэтому не создаёт merge-коммитов и падает на конфликтных/не fast-forward обновлениях.

## Работа с разными workspace

Команды `status`, `inspect`, `plan` и `start` теперь можно запускать из любого каталога, явно указав workspace:

```bash
devctl -w ~/workspaces/my-product status
devctl -w ~/workspaces/my-product plan
devctl -w ~/workspaces/my-product start
devctl -w ~/workspaces/my-product reset
```

То же самое можно закрепить переменной окружения:

```bash
export DEVCTL_WORKSPACE="$HOME/workspaces/my-product"
devctl status
```

`--workspace` принимает:

- корень workspace, где есть `.devctl/workspace.json`;
- путь к самому `.devctl/workspace.json`;
- корень Git/проектного каталога без devctl-конфига — тогда `patches/` и `archives/` ищутся рядом с проектом.

## Shell completion

Показать completion-скрипт:

```bash
devctl completion bash
devctl completion zsh
devctl completion fish
```

Поставить completion-файлы в пользовательские каталоги:

```bash
devctl self install-completions --shell auto
```

Пути по умолчанию:

- Bash: `~/.local/share/bash-completion/completions/devctl`
- Zsh: `~/.local/share/zsh/site-functions/_devctl`
- Fish: `~/.config/fish/completions/devctl.fish`

Факт наличия файла ещё не означает, что shell его загрузил. `devctl self info` теперь печатает подсказки по активации. Для zsh на Arch/KDE обычно нужно добавить пользовательский каталог в `fpath` до `compinit`:

```zsh
fpath=("$HOME/.local/share/zsh/site-functions" $fpath)
autoload -Uz compinit && compinit
```

После изменения `.zshrc` открой новый терминал или выполни эти строки в текущей сессии. Проверка генератора без shell-интеграции:

```bash
devctl __complete --position 1 bash -- devctl -
devctl __complete --position 1 bash -- devctl st
```

Completion не хранит вручную продублированный список команд: shell вызывает `devctl __complete`, а тот строит подсказки по текущему argparse-парсеру. Поэтому новые подкоманды и флаги будут появляться в completion после обновления devctl; начиная с v0.5.1 `self update` также освежает ранее установленные completion-файлы.

## Удаление пользовательской установки

```bash
devctl self uninstall --with-completions
```

Если файлы были изменены вручную и devctl отказывается их трогать, можно явно разрешить:

```bash
devctl self uninstall --with-completions --force
```

## v0.6.0: reset и User Test Space

`devctl reset` выполняет из корня workspace аварийный откат `project/` через `git reset --hard <target>` и `git clean -fd`/`-fdx`. По умолчанию команда также пытается удалить последний failed patch.zip из `patches/`, если такой запуск записан в `.devctl/state.json`. Для сохранения архива патча используй `--keep-patch`; для явного удаления — `--delete-patch <name.zip>`.

`devctl start` при failed checks или ошибке до создания локального commit теперь сначала сохраняет failed-архив, затем автоматически откатывает рабочее дерево. При `push_failed` auto-reset намеренно не выполняется, потому что локальный commit уже создан и его нужно разбирать отдельно.

После успешного `start` свежий post-архив разворачивается в `UserTestSpace/<version>/project`. Это пространство предназначено для ручных запусков вроде `cargo run`, `npm install`, сборок GUI и других действий, создающих тяжёлые/грязные файлы. Старые папки UTS не удаляются автоматически.


## v0.6.1: автоочистка Python bytecode/cache

`devctl start` теперь мягко игнорирует Python bytecode/cache, случайно попавший в patch payload: `__pycache__/`, `*.pyc`, `*.pyo`. Такие записи не копируются в `project/`, но фиксируются в отчёте как проигнорированные.

После этапа применения и после проверок `start` дополнительно удаляет `__pycache__/`, `*.pyc`, `*.pyo` из `project/` перед вычислением Git status и перед commit. Это закрывает частый случай, когда Python-команды проверок создают локальный bytecode и devctl раньше отказывался коммитить из-за опасных сгенерированных файлов.


## Workspace upgrade

`devctl init --upgrade` безопасно актуализирует существующий workspace после появления новых служебных папок и полей конфигурации. Команда создаёт недостающие `patches/`, `archives/`, `UserTestSpace/`, `.devctl/state.json` и дополняет `.devctl/workspace.json`, но не перезаписывает пользовательские пути и не трогает содержимое `project/`.

## v0.6.3: tracked bytecode cleanup не блокирует commit

`devctl start` теперь разрешает deletion-only изменения для уже tracked generated/cache путей, если автоочистка удалила старый `__pycache__/`, `*.pyc` или `*.pyo` из репозитория. Это устраняет конфликт, когда devctl сам очищал legacy bytecode, а затем commit-guard отказывался коммитить `D backend/tests/__pycache__/...pyc`.

Строгая защита сохранена: добавления, модификации, переименования, копирования и untracked generated/cache пути (`A`, `M`, `R`, `C`, `??`) по-прежнему блокируются перед `git add -A`. В отчёте такое разрешённое удаление отображается как warning, чтобы пользователь видел, что вместе с патчем очищен ранее tracked мусор.
