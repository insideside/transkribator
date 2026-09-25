#!/bin/bash
# Сборка помощника записи системного звука для macOS (universal: Apple Silicon + Intel, macOS 14.2+).
# Результат: bin/macos/TranskribatorAudio.app (готовая сборка лежит в репозитории; пересобирать нужно
# только при изменении main.swift). Нужны Xcode Command Line Tools: xcode-select --install
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/bin/macos/TranskribatorAudio.app"
TMP="$(mktemp -d)"
for arch in arm64 x86_64; do
  swiftc -O -target "$arch-apple-macos14.2" -o "$TMP/audiotap-$arch" "$HERE/main.swift"
done
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
lipo -create -output "$APP/Contents/MacOS/audiotap" "$TMP/audiotap-arm64" "$TMP/audiotap-x86_64"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/assets/icon.icns" "$APP/Contents/Resources/AppIcon.icns"
# ad-hoc подпись: без неё macOS не запомнит выданное разрешение
codesign --force --sign - --identifier local.transkribator.audiotap "$APP"
rm -rf "$TMP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Format" || true
echo "Готово: $APP"
