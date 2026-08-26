#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_ID="io.github.forge.Desktop"

INSTALL_ROOT="${FORGE_INSTALL_ROOT:-$HOME/.local/share/forge-ai}"
BIN_DIR="${FORGE_BIN_DIR:-$HOME/.local/bin}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/metainfo"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$INSTALL_ROOT/venv"
LAUNCHER="$BIN_DIR/forge-desktop"
DESKTOP_FILE="$APPLICATIONS_DIR/$APP_ID.desktop"
ICON_FILE="$ICONS_DIR/$APP_ID.svg"
METAINFO_FILE="$METAINFO_DIR/$APP_ID.metainfo.xml"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing python3. Install Python 3.11+ and try again." >&2
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
PY
then
  echo "Missing GTK Python bindings (PyGObject/libadwaita)." >&2
  echo "Install python-gobject + GTK4 + libadwaita packages and retry." >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT" "$BIN_DIR" "$APPLICATIONS_DIR" "$ICONS_DIR" "$METAINFO_DIR"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR"
fi

PIP_LOG="$INSTALL_ROOT/pip-install.log"
if "$VENV_DIR/bin/python" - <<'PY' >/dev/null 2>&1
import setuptools.backends.legacy  # noqa: F401
PY
then
  if "$VENV_DIR/bin/python" -m pip install --no-build-isolation -e "$ROOT_DIR" >"$PIP_LOG" 2>&1; then
    echo "Editable package install: ok"
  else
    echo "Warning: editable package install failed; writing source-tree launcher anyway." >&2
    echo "Pip log: $PIP_LOG" >&2
    echo "The launcher will use PYTHONPATH=$ROOT_DIR." >&2
  fi
else
  echo "Editable package install skipped: setuptools legacy backend is unavailable in the venv." >&2
  echo "The launcher will use PYTHONPATH=$ROOT_DIR." >&2
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env sh
set -eu
export PYTHONPATH="$ROOT_DIR:\${PYTHONPATH:-}"
exec "$VENV_DIR/bin/python" -m desktop.gtk.app "\$@"
EOF
chmod 755 "$LAUNCHER"

sed "s|@EXEC@|$LAUNCHER|g" \
  "$ROOT_DIR/packaging/linux/$APP_ID.desktop.in" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"

install -m 644 "$ROOT_DIR/desktop/gtk/assets/$APP_ID.svg" "$ICON_FILE"
install -m 644 "$ROOT_DIR/packaging/linux/$APP_ID.metainfo.xml" "$METAINFO_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "Forge Desktop installed."
echo "Launcher: $LAUNCHER"
echo "Desktop file: $DESKTOP_FILE"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "Note: $BIN_DIR is not on PATH for this shell."
    echo "Add this to your shell config, then restart the terminal:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
echo "Run from any directory: forge-desktop"
