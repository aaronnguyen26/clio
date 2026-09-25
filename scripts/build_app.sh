#!/usr/bin/env bash
set -euo pipefail

# 1. Resolve code signing identity: auto-detect Apple Development identity with ad-hoc (-) fallback
SIGN_IDENTITY=$(security find-identity -p codesigning -v 2>/dev/null | grep "Apple Development" | head -n 1 | sed -E 's/.*"([^"]+)".*/\1/' || true)
if [ -z "${SIGN_IDENTITY}" ]; then
    echo "==> Warning: No Apple Development certificate found in keychain, falling back to ad-hoc (-)"
    SIGN_IDENTITY="-"
else
    echo "==> Found Apple Development signing identity: ${SIGN_IDENTITY}"
fi

# Terminate any previous Clio or src.main processes
killall Clio 2>/dev/null || true
pkill -f "src.main" 2>/dev/null || true

echo "==> Building Clio Native Binaries (Bar, Probe, Synthesizer)..."
mkdir -p bin
swiftc -O src/ui/ClioBar.swift -o bin/clio-bar
swiftc -O tools/clio-probe.swift -o bin/clio-probe
if [ -f "tools/clio-synthesizer.swift" ]; then
    echo "==> Compiling tools/clio-synthesizer.swift -> bin/clio-synthesizer..."
    swiftc -O tools/clio-synthesizer.swift -o bin/clio-synthesizer
fi
if [ -f "tools/clio-recorder.swift" ]; then
    echo "==> Compiling tools/clio-recorder.swift -> bin/clio-recorder..."
    swiftc -O tools/clio-recorder.swift -o bin/clio-recorder
fi

echo "==> Packaging Clio.app in clean staging directory..."
STAGE_DIR="/tmp/Clio_build_staging"
APP_BUNDLE="${STAGE_DIR}/Clio.app"
DESKTOP_BUNDLE="/Users/minhnguyen/Desktop/Clio.app"

rm -rf "${STAGE_DIR}" Clio.app
mkdir -p "${STAGE_DIR}"
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources/src"

cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/Clio"
cp bin/clio-bar "${APP_BUNDLE}/Contents/MacOS/clio-bar"
cp bin/clio-probe "${APP_BUNDLE}/Contents/MacOS/clio-probe"
if [ -f "bin/clio-synthesizer" ]; then
    cp bin/clio-synthesizer "${APP_BUNDLE}/Contents/MacOS/clio-synthesizer"
fi
if [ -f "bin/clio-recorder" ]; then
    cp bin/clio-recorder "${APP_BUNDLE}/Contents/MacOS/clio-recorder"
fi

cp src/ui/Info.plist "${APP_BUNDLE}/Contents/Info.plist"
cp AppIcon.icns "${APP_BUNDLE}/Contents/Resources/"
cp assets/logo.png "${APP_BUNDLE}/Contents/Resources/"

# Sync Python source while strictly excluding any bytecode caches
rsync -av --delete --exclude="__pycache__" --exclude="*.pyc" src/ "${APP_BUNDLE}/Contents/Resources/src/"

# Clean all bytecode caches and resource forks to guarantee 100% clean sealed resources
echo "==> Purging all bytecode caches and detritus..."
rm -f "${APP_BUNDLE}/Icon"$'\r' 2>/dev/null || true
find "${APP_BUNDLE}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${APP_BUNDLE}" -name "*.pyc" -delete 2>/dev/null || true
find src/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find src/ -name "*.pyc" -delete 2>/dev/null || true

# Strip quarantine and extended attributes
echo "==> Cleaning extended attributes..."
xattr -cr "${APP_BUNDLE}"

# Deep code sign with resolved identity
echo "==> Deep code signing Clio.app with identity '${SIGN_IDENTITY}'..."
codesign --force --deep --sign "${SIGN_IDENTITY}" "${APP_BUNDLE}"

# Verify codesign immediately
echo "==> Verifying code signature..."
codesign --verify --verbose "${APP_BUNDLE}"
echo "==> Designated Requirement:"
codesign -d -r- "${APP_BUNDLE}"

# Packaging dist/Clio-macOS.zip directly from clean staging
echo "==> Packaging dist/Clio-macOS.zip..."
mkdir -p dist
(cd "${STAGE_DIR}" && zip -r -FS "/Users/minhnguyen/Desktop/Coding/imitate/dist/Clio-macOS.zip" "Clio.app")

# Deploy to Desktop
echo "==> Deploying to Desktop..."
[ -d "${DESKTOP_BUNDLE}" ] && chmod -R u+rwX "${DESKTOP_BUNDLE}" 2>/dev/null || true
rm -rf "${DESKTOP_BUNDLE}"
cp -a "${APP_BUNDLE}" "${DESKTOP_BUNDLE}"

# Force LaunchServices re-registration
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "${LSREGISTER}" ]; then
    echo "==> Forcing LaunchServices re-registration..."
    "${LSREGISTER}" -f "${DESKTOP_BUNDLE}"
fi

# Clean staging
rm -rf "${STAGE_DIR}" Clio.app

echo "==> Build complete! Only /Users/minhnguyen/Desktop/Clio.app is kept and verified."
