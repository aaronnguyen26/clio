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

echo "==> Installing to /Applications & ~/Applications for Spotlight Indexing..."
rm -rf /Applications/Clio.app
cp -R "${APP_BUNDLE}" /Applications/Clio.app
mkdir -p ~/Applications
rm -rf ~/Applications/Clio.app
cp -R "${APP_BUNDLE}" ~/Applications/Clio.app
rm -rf /Users/minhnguyen/Desktop/Clio.app
cp -R "${APP_BUNDLE}" /Users/minhnguyen/Desktop/Clio.app

# Clean attributes & register in macOS LaunchServices database
xattr -cr /Applications/Clio.app ~/Applications/Clio.app /Users/minhnguyen/Desktop/Clio.app
codesign --force --deep --sign - /Applications/Clio.app
codesign --force --deep --sign - ~/Applications/Clio.app
codesign --force --deep --sign - /Users/minhnguyen/Desktop/Clio.app

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/Clio.app || true
mdimport /Applications/Clio.app || true
touch /Applications/Clio.app || true

echo "==> Packaging dist/Clio-macOS.zip..."
mkdir -p dist
zip -r -FS dist/Clio-macOS.zip "${APP_BUNDLE}"

echo "==> Build and Spotlight registration complete!"
