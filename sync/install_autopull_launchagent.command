#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/pavelsvyados/Python/Bybit scanner"
PLIST="$HOME/Library/LaunchAgents/com.pavel.crypto-signal-collector.autopull.plist"
PULL_SCRIPT="$PROJECT_DIR/sync/pull_latest.command"

if [ ! -f "$PULL_SCRIPT" ]; then
  echo "Не найден pull-скрипт: $PULL_SCRIPT"
  echo "Сначала положи файлы репозитория в $PROJECT_DIR"
  exit 1
fi

chmod +x "$PULL_SCRIPT"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.pavel.crypto-signal-collector.autopull</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PULL_SCRIPT</string>
  </array>

  <key>StartInterval</key>
  <integer>300</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$PROJECT_DIR/sync/autopull.out.log</string>

  <key>StandardErrorPath</key>
  <string>$PROJECT_DIR/sync/autopull.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Автоподтягивание установлено."
echo "Интервал: каждые 5 минут."
echo "Лог:"
echo "  $PROJECT_DIR/sync/autopull.out.log"
echo "  $PROJECT_DIR/sync/autopull.err.log"

