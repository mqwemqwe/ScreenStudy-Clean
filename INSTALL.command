#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
pause_on_error() {
  result=$?
  if [ "$result" -ne 0 ]; then
    echo ""
    echo "Установка остановлена. Скопируйте текст ошибки выше или сделайте скриншот."
    read -r -p "Нажмите Enter, чтобы закрыть окно… " || true
  fi
}
trap pause_on_error EXIT
if [ "$(uname -s)" != "Darwin" ]; then
  echo "Этот установщик предназначен для macOS."
  exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
  echo "Нужен нативный терминал Apple Silicon. Отключите запуск Terminal через Rosetta."
  exit 1
fi
MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if [ "$MAJOR" -lt 14 ]; then
  echo "Нужна macOS 14 или новее."
  exit 1
fi
PYTHON_BIN=""
for candidate in "/opt/homebrew/bin/python3.13" "/opt/homebrew/bin/python3.12" "/opt/homebrew/bin/python3.11" "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" "/usr/local/bin/python3" "$(command -v python3 || true)"; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys,platform; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) and platform.machine()=="arm64" else 1)' 2>/dev/null; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "Установите Python 3.13 для macOS (universal2) с python.org."
  echo "Затем снова откройте INSTALL.command. Нужен Python 3.10–3.13, без Rosetta."
  open "https://www.python.org/downloads/macos/"
  exit 1
fi
echo "ScreenStudy · установка для Apple Silicon"
echo "Загрузка компонентов и сборка приложения займут несколько минут."
SUPPORT="$HOME/Library/Application Support/ScreenStudy"
mkdir -p "$SUPPORT" "$HOME/Applications"
chmod 700 "$SUPPORT"
VENV="$SUPPORT/build-venv"
if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys,platform; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) and platform.machine()=="arm64" else 1)'; then
  mv "$VENV" "$SUPPORT/build-venv-backup-$(date +%Y%m%d-%H%M%S)"
fi
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install --only-binary=:all: -r "$ROOT/requirements.txt" 'pyinstaller>=6.12,<7'
cd "$ROOT"
echo "Проверка основных компонентов…"
"$VENV/bin/python" -m unittest discover -s tests -p 'test_core.py' -q
echo "Проверка доступности API macOS…"
"$VENV/bin/python" mac_preflight.py
BUILD_DIR="$(mktemp -d "$SUPPORT/build.XXXXXX")"
"$VENV/bin/python" -m PyInstaller --noconfirm --distpath "$BUILD_DIR/dist" --workpath "$BUILD_DIR/work" "$ROOT/ScreenStudy.spec"
TARGET="$HOME/Applications/ScreenStudy.app"
if [ -d "$TARGET" ]; then
  BACKUP="$SUPPORT/ScreenStudy-backup-$(date +%Y%m%d-%H%M%S).app"
  mv "$TARGET" "$BACKUP"
  echo "Предыдущая версия сохранена: $BACKUP"
fi
ditto "$BUILD_DIR/dist/ScreenStudy.app" "$TARGET"
echo "Готово. ScreenStudy установлен в $TARGET"
echo "При первом запуске выберите ИИ в настройках и разрешите запись экрана."
open "$TARGET"
read -r -p "Нажмите Enter, чтобы закрыть установщик… " || true
