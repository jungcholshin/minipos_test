#!/bin/bash
# Сборка MiniPOS.app на Mac. Запуск из папки проекта:  bash build_mac.sh
# Результат: dist/MiniPOS.app — перетащите в «Программы» и открывайте двойным кликом.
set -e
cd "$(dirname "$0")"

PY=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || true)
if [ -z "$PY" ]; then echo "Нужен Python 3.10+ (python.org)"; exit 1; fi

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt pywebview pyinstaller

.venv/bin/pyinstaller --noconfirm --clean --windowed --name MiniPOS \
  --osx-bundle-identifier kz.mizonyalyan.minipos \
  --add-data "app/static:app/static" \
  --collect-submodules uvicorn --collect-submodules app \
  desktop.py

# Перенести уже существующие данные (база, настройки) в папку программы, если там пусто
DATA="$HOME/Documents/MiniPOS"
mkdir -p "$DATA"
for f in minipos.db settings.env; do
  if [ -f "$f" ] && [ ! -f "$DATA/$f" ]; then cp "$f" "$DATA/$f"; echo "Скопирован $f → $DATA"; fi
done

echo ""
echo "Готово: $(pwd)/dist/MiniPOS.app"
echo "Данные программы: $DATA"
open dist
