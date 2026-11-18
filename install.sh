#!/bin/bash
# Install the built plugin into Decky and restart the loader. Run with: sudo ./install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/home/deck/homebrew/plugins/decky-scoff
[ -f "$SRC/dist/index.js" ] || { echo "build first: npm run build"; exit 1; }
rm -rf "$DEST"; mkdir -p "$DEST/dist"
cp "$SRC"/{plugin.json,package.json,main.py,LICENSE,README.md} "$DEST/"
cp "$SRC/dist/index.js" "$DEST/dist/"
chown -R root:root "$DEST"
systemctl restart plugin_loader
echo "installed to $DEST and restarted plugin_loader"
