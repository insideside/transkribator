#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Транскрибатор — установка на macOS (и Linux)
#  Запуск:  bash install.sh            — обычная установка
#           bash install.sh --all-models  — скачать обе модели (точную и быструю)
#           bash install.sh --model large-v3-turbo
#           bash install.sh --no-shortcut --no-widget
#           bash install.sh --ci        — для автотестов: маленькая модель, без ярлыков и вопросов
# ─────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

MODEL_ARGS=()
SHORTCUT=1; WIDGET=ask; CI=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-models) MODEL_ARGS=(--all) ;;
    --model) MODEL_ARGS=(--whisper "$2"); shift ;;
    --no-shortcut) SHORTCUT=0 ;;
    --no-widget) WIDGET=no ;;
    --ci) CI=1; SHORTCUT=0; WIDGET=no; MODEL_ARGS=(--whisper tiny) ;;
    -h|--help) sed -n '3,9p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1"; exit 2 ;;
  esac
  shift
done

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$1"; exit 1; }

OS="$(uname -s)"; ARCH="$(uname -m)"
bold "Транскрибатор — установка ($OS $ARCH)"
echo "Папка приложения: $ROOT"
if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
  echo "Распознавание: Whisper на GPU Apple Silicon (MLX)"
elif [[ "$OS" == "Darwin" ]]; then
  echo "Распознавание: faster-whisper на процессоре (Intel Mac — будет заметно медленнее, чем на Apple Silicon)"
else
  echo "Распознавание: faster-whisper (видеокарта NVIDIA, если есть, иначе процессор)"
fi
echo "Нужно ~6 ГБ свободного места и интернет только на время установки."

# файлы из скачанного архива macOS помечает «карантином» — снимаем, чтобы запускались скрипты
if [[ "$OS" == "Darwin" ]]; then xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true; fi

# ── 1. uv (менеджер Python; сам скачает нужную версию Python) ──
bold "1/4  Менеджер окружения uv"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Устанавливаю uv (https://docs.astral.sh/uv/)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || fail "Не удалось установить uv. Проверьте интернет."
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "uv: $(uv --version)"

# ── 2. Python и зависимости ──
bold "2/4  Python и библиотеки"
EXTRA=()
if [[ "$OS" == "Linux" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  echo "Найдена видеокарта NVIDIA — ставлю библиотеки CUDA"
  EXTRA=(--extra cuda)
fi
uv sync --frozen --no-dev ${EXTRA[@]+"${EXTRA[@]}"} || fail "Не удалось установить зависимости."
PY="$ROOT/.venv/bin/python"

# ── 3. Модели ──
bold "3/4  Модели (распознавание и разделение по голосам)"
"$PY" -m app.models ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} || fail "Не удалось скачать модели. Запустите установку ещё раз — скачанное сохранится."

bold "Проверка"
if [[ $CI == 1 ]]; then
  "$PY" -m app.selftest --model tiny || fail "Самопроверка не прошла."
else
  "$PY" -m app.selftest || fail "Самопроверка не прошла — см. сообщения выше и data/logs/app.log"
fi

# ── 4. Ярлык и виджет ──
bold "4/4  Ярлык"
chmod +x "$ROOT/start.command" 2>/dev/null || true
if [[ $SHORTCUT == 1 && "$OS" == "Darwin" ]]; then
  APP="$HOME/Applications/Транскрибатор.app"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp "$ROOT/assets/icon.icns" "$APP/Contents/Resources/AppIcon.icns"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Транскрибатор</string>
  <key>CFBundleDisplayName</key><string>Транскрибатор</string>
  <key>CFBundleIdentifier</key><string>local.transkribator</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
  # лаунчер: запускает сервер (иконка в Dock, пока работает; «Завершить» в Dock — остановить)
  cat > "$APP/Contents/MacOS/launcher" <<LAUNCH
#!/bin/bash
exec "$PY" "$ROOT/run.py"
LAUNCH
  chmod +x "$APP/Contents/MacOS/launcher"
  touch "$APP"
  echo "Приложение: $APP (можно перетащить в Dock)"
elif [[ $SHORTCUT == 1 && "$OS" == "Linux" ]]; then
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/transkribator.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Транскрибатор
Exec="$PY" "$ROOT/run.py"
Icon=$ROOT/assets/icon.png
Terminal=true
Categories=AudioVideo;Office;
DESK
  echo "Ярлык добавлен в меню приложений"
fi

UBER="$HOME/Library/Application Support/Übersicht/widgets"
if [[ "$OS" == "Darwin" && $WIDGET != no && -d "/Applications/Übersicht.app" ]]; then
  if [[ $WIDGET == ask ]]; then
    read -r -p "Найден Übersicht. Поставить виджет на рабочий стол? [Y/n] " ans || ans=n
    [[ "$ans" =~ ^[Nn] ]] && WIDGET=no
  fi
  if [[ $WIDGET != no ]]; then
    "$ROOT/macos-widget/install.sh"
  fi
fi

bold "Готово!"
if [[ "$OS" == "Darwin" && $SHORTCUT == 1 ]]; then
  echo "Запуск: «Транскрибатор» в Программах (Launchpad / Spotlight) или двойной клик по start.command"
else
  echo "Запуск: $ROOT/start.command  (или: $PY $ROOT/run.py)"
fi
echo "Откроется в браузере: http://127.0.0.1:8770"
