#!/usr/bin/env bash
set -euo pipefail

echo "==> Building Clio Native Bar Binary..."
mkdir -p bin
swiftc -O src/ui/ClioBar.swift -o bin/clio-bar

echo "==> Packaging Clio.app..."
APP_BUNDLE="Clio.app"
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources/src"

cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/Clio"
cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/clio-bar"
cp AppIcon.icns "${APP_BUNDLE}/Contents/Resources/"

rsync -av --delete --exclude="__pycache__" --exclude="*.pyc" src/ "${APP_BUNDLE}/Contents/Resources/src/"

echo "==> Ad-hoc code signing Clio.app..."
xattr -cr "${APP_BUNDLE}"
codesign --force --deep --sign - "${APP_BUNDLE}"

echo "==> Build complete: ${APP_BUNDLE}"
