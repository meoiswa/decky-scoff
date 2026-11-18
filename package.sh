#!/bin/bash
# Build ScOFF and package it as out/ScOFF.zip, in the layout Decky's "Install Plugin from URL/ZIP" expects:
# a single top-level folder containing plugin.json.
set -euo pipefail
cd "$(dirname "$0")"
npm run build
rm -rf out && mkdir -p out/decky-scoff/dist
cp plugin.json package.json main.py LICENSE README.md out/decky-scoff/
cp dist/index.js out/decky-scoff/dist/
(cd out && python3 -I -c 'import shutil; shutil.make_archive("ScOFF", "zip", ".", "decky-scoff")')
echo "created out/ScOFF.zip"
