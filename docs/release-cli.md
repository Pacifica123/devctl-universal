# Установка, обновление, автодополнение

`devctl` — один файл. Его можно запускать на месте (`python3 devctl.py …`) или поставить как обычную
команду пользователя.

## Установка

Из каталога с актуальным `devctl.py`:

```bash
python3 devctl.py self install --with-completions
# то же самое:
./install.sh
```

| Что | Куда |
| --- | --- |
| команда `devctl` | `~/.local/bin/devctl` |
| управляемая копия | `~/.local/share/devctl/devctl.py` |
| метаданные установки | `~/.local/share/devctl/install.json` |

Каталоги меняются флагами `--bin-dir` и `--app-dir`. Если `~/.local/bin` нет в `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Проверка:

```bash
devctl --version
devctl self info
```

Метаданные запоминают исходный `devctl.py`, корень его Git-репозитория и оболочки, для которых
поставлено автодополнение.

## Обновление

Установленная копия не обновляется сама: после того как патч изменил `devctl.py` в репозитории,
выполните

```bash
devctl self update
```

Источник берётся из `install.json`, заодно перезаписываются установленные completion-файлы. Если в
текущем каталоге лежит свой `devctl.py`, источником станет он, а не записанный в метаданных.

```bash
devctl self update --source /path/to/devctl.py --with-completions   # другой источник
devctl self update --pull-source                                    # сначала подтянуть репозиторий источника
```

`--pull-source` выполняет в репозитории источника `git fetch --all --prune` и `git pull --ff-only`:
merge-коммитов не создаёт и останавливается, если fast-forward невозможен.

Пока `self update` не выполнен, команда `devctl` остаётся прежней версии. Новую версию из репозитория
можно запустить напрямую: `python3 devctl.py …`.

## Автодополнение

```bash
devctl completion bash|zsh|fish            # напечатать скрипт
devctl self install-completions --shell auto   # поставить для bash, zsh и fish
```

| Оболочка | Файл |
| --- | --- |
| Bash | `~/.local/share/bash-completion/completions/devctl` |
| Zsh | `~/.local/share/zsh/site-functions/_devctl` |
| Fish | `~/.config/fish/completions/devctl.fish` |

Наличие файла ещё не значит, что оболочка его загрузила; `devctl self info` печатает подсказки по
активации. Для zsh пользовательский каталог нужно добавить в `fpath` до `compinit`:

```zsh
fpath=("$HOME/.local/share/zsh/site-functions" $fpath)
autoload -Uz compinit && compinit
```

Список команд в скрипте не зашит: оболочка вызывает `devctl __complete`, а тот строит подсказки по
действующему разбору аргументов. Новые команды и флаги — например `zip` — появляются в автодополнении
сразу после `self update`. Проверка без оболочки:

```bash
devctl __complete --position 1 bash -- devctl z
```

## Удаление

```bash
devctl self uninstall --with-completions
devctl self uninstall --with-completions --force   # если файлы меняли вручную
```

## Работа с несколькими workspace

```bash
devctl -w ~/spaces/my-product status
export DEVCTL_WORKSPACE="$HOME/spaces/my-product"
devctl plan
```

Что принимает `-w` — в [commands.md](commands.md#выбор-workspace).
