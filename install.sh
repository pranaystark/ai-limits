#!/bin/sh
# Install AI Limits for the current user. No root required.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BIN_DIR="${HOME}/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/ai-limits"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DBUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/dbus-1/services"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
EXT_DIR="${HOME}/.local/share/gnome-shell/extensions/ai-limits@local"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

die() { echo "install: $*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

echo "AI Limits installer"
echo "  source: $ROOT"
echo "  prefix: $DATA_DIR"
echo

need_cmd python3
need_cmd install

python3 - <<'PY' || die "Python GI / GTK 4 / Adwaita / GdkPixbuf / cairo not available"
import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, Gtk  # noqa: F401
print("  python", __import__("sys").version.split()[0], "gtk", Gtk.get_major_version(), Gtk.get_minor_version())
PY

mkdir -p "$BIN_DIR" "$DATA_DIR" "$DATA_DIR/icons" "$APP_DIR" "$DBUS_DIR" "$AUTOSTART_DIR" "$ICON_DIR"

install -m 644 "$ROOT/ai_limits.py" "$DATA_DIR/ai_limits.py"
install -m 644 "$ROOT/style.css" "$DATA_DIR/style.css"
install -m 644 "$ROOT/icons/antigravity.svg" "$DATA_DIR/icons/antigravity.svg"
install -m 644 "$ROOT/icons/grok.svg" "$DATA_DIR/icons/grok.svg"
if [ -f "$ROOT/icons/grok.png" ]; then
  install -m 644 "$ROOT/icons/grok.png" "$DATA_DIR/icons/grok.png"
fi
install -m 644 "$ROOT/icons/antigravity.svg" "$ICON_DIR/org.local.AiLimits.svg"

cat > "$BIN_DIR/ai-limits" <<EOF
#!/bin/sh
exec python3 "$DATA_DIR/ai_limits.py" "\$@"
EOF
chmod 755 "$BIN_DIR/ai-limits"

cat > "$APP_DIR/org.local.AiLimits.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AI Limits
Comment=AGY and Grok remaining usage
Exec=$BIN_DIR/ai-limits
Icon=org.local.AiLimits
Terminal=false
Categories=Utility;Monitor;
StartupWMClass=org.local.AiLimits
DBusActivatable=true
X-GNOME-UsesNotifications=false
EOF

cat > "$DBUS_DIR/org.local.AiLimits.service" <<EOF
[D-BUS Service]
Name=org.local.AiLimits
Exec=$BIN_DIR/ai-limits --hidden
EOF

cat > "$AUTOSTART_DIR/org.local.AiLimits.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AI Limits
Comment=Show AGY and Grok remaining usage in the tray
Exec=$BIN_DIR/ai-limits --hidden
Icon=org.local.AiLimits
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

if [ -d "$ROOT/gnome-extension" ]; then
  mkdir -p "$EXT_DIR"
  install -m 644 "$ROOT/gnome-extension/metadata.json" "$EXT_DIR/metadata.json"
  install -m 644 "$ROOT/gnome-extension/extension.js" "$EXT_DIR/extension.js"
  install -m 644 "$ROOT/gnome-extension/stylesheet.css" "$EXT_DIR/stylesheet.css"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "Note: $BIN_DIR is not on PATH. Add this to ~/.profile and re-login:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

if command -v gnome-extensions >/dev/null 2>&1; then
  gnome-extensions enable ai-limits@local 2>/dev/null || true
fi

echo
echo "Installed."
echo "  command:    $BIN_DIR/ai-limits"
echo "  autostart:  $AUTOSTART_DIR/org.local.AiLimits.desktop"
echo "  extension:  $EXT_DIR"
echo
echo "Next:"
echo "  1. Confirm agy and omp are on PATH and signed in."
echo "  2. Start the tray:  ai-limits --hidden"
echo "  3. GNOME panel chip (next to Vitals): log out and back in once."
echo "  4. Click a tray chip for the glance. Pie-corner refresh is per provider."
