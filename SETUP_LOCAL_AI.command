#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
OLLAMA="$(command -v ollama || true)"
if [ -z "$OLLAMA" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  OLLAMA="/Applications/Ollama.app/Contents/Resources/ollama"
fi
if [ -z "$OLLAMA" ]; then
  echo "Установите Ollama для macOS, откройте приложение и снова запустите этот файл."
  open "https://ollama.com/download/mac"
  read -r -p "Нажмите Enter… " || true
  exit 1
fi
open -a Ollama || true
echo "Загружаю qwen3-vl:4b. Для этого нужны интернет и несколько ГБ свободного места."
ready=0
for attempt in {1..15}; do
  if "$OLLAMA" list >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" -eq 0 ]; then
  echo "Ollama не запущена. Откройте её в Программах и повторите."
  read -r -p "Нажмите Enter… " || true
  exit 1
fi
if "$OLLAMA" pull qwen3-vl:4b; then
  echo "Готово. В ScreenStudy выберите Ollama и модель qwen3-vl:4b."
else
  echo "Загрузка не завершилась. Проверьте интернет, обновите Ollama и повторите запуск."
fi
read -r -p "Нажмите Enter… " || true
