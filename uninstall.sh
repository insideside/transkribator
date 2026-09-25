#!/bin/bash
# Удаление ярлыков Транскрибатора на macOS/Linux. Саму папку приложения (с моделями и историей) удалите вручную.
cd "$(dirname "$0")"
rm -rf "$HOME/Applications/Транскрибатор.app" "$HOME/Library/Application Support/Übersicht/widgets/transkribator.widget" \
       "$HOME/.local/share/applications/transkribator.desktop"
echo "Ярлыки и виджет удалены."
echo "Чтобы удалить приложение полностью (модели ~6 ГБ, история и загруженные записи), удалите папку:"
echo "  $(pwd)"
