#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/pavelsvyados/Python/Bybit scanner"
BRANCH="main"

cd "$PROJECT_DIR"

if [ ! -d ".git" ]; then
  echo "Ошибка: $PROJECT_DIR не является Git-репозиторием."
  echo "Сначала выполни первичную настройку remote/clone."
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Pull остановлен: в папке есть незакоммиченные изменения."
  echo "Проверь их командой:"
  echo "  git status"
  echo ""
  echo "Если это только локальные логи, убедись, что они в .gitignore."
  exit 2
fi

echo "Подтягиваю последние изменения из GitHub..."
git fetch origin "$BRANCH"
git pull --ff-only origin "$BRANCH"
echo "Готово."

