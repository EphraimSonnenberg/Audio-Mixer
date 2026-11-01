#!/bin/sh
# Copies the plugin into OpenDeck's plugin folder. Restart OpenDeck (or use its Plugins tab) afterwards.
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="${XDG_CONFIG_HOME:-$HOME/.config}/opendeck/plugins"
mkdir -p "$dest"
rm -rf "$dest/io.github.lawsonf.audiomixer.sdPlugin"
cp -r "$here/io.github.lawsonf.audiomixer.sdPlugin" "$dest/"
rm -rf "$dest/io.github.lawsonf.audiomixer.sdPlugin/__pycache__"
chmod +x "$dest/io.github.lawsonf.audiomixer.sdPlugin/plugin.py"
echo "Installed to $dest/io.github.lawsonf.audiomixer.sdPlugin"
