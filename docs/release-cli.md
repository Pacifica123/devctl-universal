# devctl как релизная CLI-утилита

Цель v0.5 — пользоваться `devctl` как обычной Linux-командой, а не как локальным `python3 devctl.py` из конкретной папки.

## Установка для пользователя

Из каталога с актуальным `devctl.py`:

```bash
python3 devctl.py self install --with-completions
```

Или короче:

```bash
./install.sh
```

По умолчанию команда `devctl` ставится в `~/.local/bin/devctl`, а управляемая копия ядра — в `~/.local/share/devctl/devctl.py`.

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

После применения патча к репозиторию devctl обнови установленную копию одной командой из каталога свежего исходника:

```bash
python3 devctl.py self update --with-completions
```

Если команда `devctl` уже указывает на свежий файл, можно выполнить:

```bash
devctl self update --with-completions
```

Для нестандартной раскладки:

```bash
devctl self update --source /path/to/devctl.py
```

## Работа с разными workspace

Команды `status`, `inspect`, `plan` и `start` теперь можно запускать из любого каталога, явно указав workspace:

```bash
devctl -w ~/workspaces/my-product status
devctl -w ~/workspaces/my-product plan
devctl -w ~/workspaces/my-product start
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

На Arch/KDE bash completion обычно подхватывается после нового shell-сеанса, если установлен пакет `bash-completion`. Для zsh может потребоваться добавить пользовательский каталог в `fpath` до `compinit`, например:

```zsh
fpath=("$HOME/.local/share/zsh/site-functions" $fpath)
autoload -Uz compinit && compinit
```

Completion не хранит вручную продублированный список команд: shell вызывает `devctl __complete`, а тот строит подсказки по текущему argparse-парсеру. Поэтому новые подкоманды и флаги будут появляться в completion после обновления devctl.

## Удаление пользовательской установки

```bash
devctl self uninstall --with-completions
```

Если файлы были изменены вручную и devctl отказывается их трогать, можно явно разрешить:

```bash
devctl self uninstall --with-completions --force
```
