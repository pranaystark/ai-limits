#!/bin/sh
set -eu

BIN_DIR="${HOME}/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/ai-limits"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DBUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/dbus-1/services"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
EXT_DIR="${HOME}/.local/share/gnome-shell/extensions/ai-limits@local"
ICON="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/org.local.AiLimits.svg"

if command -v gnome-extensions >/dev/null 2>&1; then
  gnome-extensions disable ai-limits@local 2>/dev/null || true
fi

rm -f "$BIN_DIR/ai-limits"
rm -f "$APP_DIR/org.local.AiLimits.desktop"
rm -f "$DBUS_DIR/org.local.AiLimits.service"
rm -f "$AUTOSTART_DIR/org.local.AiLimits.desktop"
rm -f "$AUTOSTART_DIR/ai-limits-autostart.desktop"
rm -f "$ICON"
rm -rf "$DATA_DIR"
rm -rf "$EXT_DIR"

echo "AI Limits removed for $USER."
echo "Cache left in place: ${XDG_CACHE_HOME:-$HOME/.cache}/ai-limits"
echo "Stop a running instance with:  killall ai_limits.py 2>/dev/null || true"
