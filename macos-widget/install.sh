#!/usr/bin/env bash
# Ставит виджет «Транскрибатор» в Übersicht (https://tracesof.net/uebersicht/).
# Шаблон transkribator.widget/index.jsx копируется с подстановкой пути к папке приложения.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"
DEST="$HOME/Library/Application Support/Übersicht/widgets/transkribator.widget"

if [[ ! -d /Applications/Übersicht.app && ! -d "$HOME/Applications/Übersicht.app" ]]; then
  echo "⚠️  Übersicht не найден. Установите его: brew install --cask ubersicht (или https://tracesof.net/uebersicht/)"
fi

rm -rf "$DEST"   # в т.ч. старый симлинк
mkdir -p "$DEST"
# экранируем для sed: путь может содержать пробелы, кириллицу, &, /
ESC=$(printf '%s' "$PROJ" | sed 's/[&/\]/\\&/g')
sed "s/__PROJECT_DIR__/$ESC/" "$HERE/transkribator.widget/index.jsx" > "$DEST/index.jsx"
echo "✅ Виджет установлен: $DEST"
echo "   Übersicht подхватит его сам (или меню Übersicht → Refresh All)."
