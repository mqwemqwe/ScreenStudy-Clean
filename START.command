#!/bin/bash
set -euo pipefail
TARGET="$HOME/Applications/ScreenStudy.app"
if [ -d "$TARGET" ]; then
  open "$TARGET"
else
  echo "Сначала откройте INSTALL.command."
  read -r -p "Нажмите Enter… " || true
fi
