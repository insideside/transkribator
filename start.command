#!/bin/bash
# Запуск Транскрибатора на macOS/Linux: двойной клик в Finder или ./start.command
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Приложение ещё не установлено — запускаю установку…"
  bash install.sh || { read -r -p "Нажмите Enter, чтобы закрыть"; exit 1; }
fi
exec .venv/bin/python run.py
