#!/usr/bin/env bash
# Install / remove Linux Powermon for the current user.
#   ./install.sh [--autostart] [--system-helper]
#   ./install.sh --uninstall
#
# --system-helper also installs the root-run energy-saver helper to the root-owned
# /usr/local/bin (asks for sudo). Recommended: pkexec then never runs a user-writable file.
set -euo pipefail
BIN="$HOME/.local/bin/powermon"
APP="$HOME/.local/share/applications/powermon.desktop"
AUTO="$HOME/.config/autostart/powermon.desktop"
here="$(cd "$(dirname "$0")" && pwd)"

[[ "$(uname -s)" == "Linux" ]] || { echo "Linux Powermon only supports Linux." >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN" "$(dirname "$BIN")/powermon-saver" "$APP" "$AUTO"
  [[ -e /usr/local/bin/powermon-saver ]] && echo "Also remove the root helper: sudo rm /usr/local/bin/powermon-saver"
  echo "Removed $BIN, $APP and $AUTO"; exit 0
fi

autostart=0; syshelper=0; extra=()
for a in "$@"; do
  if [[ "$a" == "--autostart" ]]; then autostart=1
  elif [[ "$a" == "--system-helper" ]]; then syshelper=1
  else extra+=("$a"); fi
done

mkdir -p "$(dirname "$BIN")" "$(dirname "$APP")"
install -m 0755 "$here/powermon.py" "$BIN"
install -m 0755 "$here/powermon-saver" "$(dirname "$BIN")/powermon-saver"
exec_line="$BIN"
for a in "${extra[@]+"${extra[@]}"}"; do exec_line+=" \"$a\""; done
sed "s|@EXEC@|${exec_line//|/\\|}|" "$here/powermon.desktop" > "$APP"
if [[ $syshelper -eq 1 ]]; then
  sudo install -m 0755 -o root -g root "$here/powermon-saver" /usr/local/bin/powermon-saver
  echo "Root-owned helper: /usr/local/bin/powermon-saver"
fi
echo "Installed: $BIN"
echo "Menu entry: $APP  (Exec: $exec_line)"
if [[ $autostart -eq 1 ]]; then
  mkdir -p "$(dirname "$AUTO")"; cp "$APP" "$AUTO"; echo "Autostart: $AUTO"
fi
python3 -c "import PySide6, psutil" 2>/dev/null || echo "WARNING: run: pip install --user -r requirements.txt"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to PATH to run 'powermon'.";; esac
