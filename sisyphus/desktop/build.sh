#!/bin/bash
# Build Sisyphus.app: compile the Swift package, wrap the binary in an app
# bundle, and bake in where the daemon lives and which Python runs it.
#
#   ./build.sh                 → desktop/build/Sisyphus.app
#   ./build.sh --install       → also copies it to ~/Applications
#
# SISYPHUS_PYTHON overrides the interpreter (default: the one the launchd
# agent used, Homebrew's python3.14).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"                       # .../sisyphus — the daemon's cwd
python="${SISYPHUS_PYTHON:-/opt/homebrew/opt/python@3.14/bin/python3.14}"
out="$here/build/Sisyphus.app"

[ -x "$python" ] || { echo "no Python at $python — set SISYPHUS_PYTHON" >&2; exit 1; }

swift build -c release --package-path "$here"
bin="$(swift build -c release --package-path "$here" --show-bin-path)/Sisyphus"

rm -rf "$out"
mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
cp "$bin" "$out/Contents/MacOS/Sisyphus"

# The icon is drawn in code (Icon/make_icon.swift) — the same shapes as the
# sidebar's mark — and packed into an .icns.
iconset="$here/build/AppIcon.iconset"
rm -rf "$iconset"
swift "$here/Icon/make_icon.swift" "$iconset"
iconutil -c icns "$iconset" -o "$out/Contents/Resources/AppIcon.icns"

version="$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo dev)"
cat > "$out/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Sisyphus</string>
    <key>CFBundleDisplayName</key><string>Sisyphus</string>
    <key>CFBundleIdentifier</key><string>com.sisyphus.app</string>
    <key>CFBundleExecutable</key><string>Sisyphus</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>2.0</string>
    <key>CFBundleVersion</key><string>$version</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleIconName</key><string>AppIcon</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
    <key>NSHumanReadableCopyright</key><string>© 2026 onelenyk</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
    <!-- the UI is plain http on loopback -->
    <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
    <key>SisyphusRoot</key><string>$root</string>
    <key>SisyphusPython</key><string>$python</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough for this machine to run it; not for distribution.
codesign --force --sign - "$out" >/dev/null

echo "built $out"
if [ "${1:-}" = "--install" ]; then
    mkdir -p "$HOME/Applications"
    rm -rf "$HOME/Applications/Sisyphus.app"
    cp -R "$out" "$HOME/Applications/"
    echo "installed ~/Applications/Sisyphus.app"
fi
