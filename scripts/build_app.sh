#!/usr/bin/env bash
set -euo pipefail

echo "==> Building Clio Native Bar & Probe Binaries..."
mkdir -p bin
swiftc -O src/ui/ClioBar.swift -o bin/clio-bar
swiftc -O tools/clio-probe.swift -o bin/clio-probe

echo "==> Packaging Clio.app on Desktop..."
APP_BUNDLE="/Users/minhnguyen/Desktop/Clio.app"
rm -rf "${APP_BUNDLE}" Clio.app
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources/src"

cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/Clio"
cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/clio-bar"
cp bin/clio-probe "${APP_BUNDLE}/Contents/MacOS/clio-probe"
cp src/ui/Info.plist "${APP_BUNDLE}/Contents/Info.plist"
cp AppIcon.icns "${APP_BUNDLE}/Contents/Resources/"
cp assets/logo.png "${APP_BUNDLE}/Contents/Resources/"

rsync -av --delete --exclude="__pycache__" --exclude="*.pyc" src/ "${APP_BUNDLE}/Contents/Resources/src/"

echo "==> Ad-hoc code signing Clio.app on Desktop..."
xattr -cr "${APP_BUNDLE}"
codesign --force --deep --sign - "${APP_BUNDLE}"

echo "==> Setting Desktop icon visual cover..."
swift -e 'import AppKit
guard let img = NSImage(contentsOfFile: "AppIcon.icns") ?? NSImage(contentsOfFile: "assets/logo.png") else { exit(1) }
_ = NSWorkspace.shared.setIcon(img, forFile: "/Users/minhnguyen/Desktop/Clio.app", options: [])
'
touch "${APP_BUNDLE}"

# Ensure project directory is kept clean (no Clio.app inside repo)
rm -rf Clio.app

echo "==> Packaging dist/Clio-macOS.zip..."
mkdir -p dist
(cd /Users/minhnguyen/Desktop && zip -r -FS "/Users/minhnguyen/Desktop/Coding/imitate/dist/Clio-macOS.zip" "Clio.app")

echo "==> Build complete! Only /Users/minhnguyen/Desktop/Clio.app is kept."
