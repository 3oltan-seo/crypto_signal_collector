#!/bin/zsh
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.pavel.crypto-signal-collector.autopull.plist"

if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Автоподтягивание удалено."
else
  echo "LaunchAgent не найден, удалять нечего."
fi

