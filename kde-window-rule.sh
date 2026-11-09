#!/usr/bin/env bash
# KDE Plasma helper: pin the Powermon overlay to a screen position, keep it below other
# windows and hide it from the taskbar / pager / Alt-Tab.
#
#   ./kde-window-rule.sh X Y        # position in LOGICAL pixels, e.g. ./kde-window-rule.sh 1694 600
#   ./kde-window-rule.sh --remove
#
# Top-right corner:  X = (screen width / scale) - 420 - margin.   Find width and scale with:
#   kscreen-doctor -o      (e.g. 2560x1440 at scale 1.2 -> 2133 logical; X = 2133-420-20 = 1693)
# Existing KWin rules are preserved. Only the "powermon-overlay" rule is added/replaced.
set -euo pipefail
f=kwinrulesrc; g=powermon-overlay
command -v kwriteconfig6 >/dev/null || { echo "Needs KDE Plasma 6 (kwriteconfig6)." >&2; exit 1; }

current_rules() { kreadconfig6 --file "$f" --group General --key rules 2>/dev/null || true; }

reload() { gdbus call --session --dest org.kde.KWin --object-path /KWin \
             --method org.kde.KWin.reconfigure >/dev/null 2>&1 || echo "(restart KWin or re-login to apply)"; }

set_rules() {   # $1 = comma separated rules list
  local n; n=$(awk -F, '{print NF}' <<<"$1"); [[ -z "$1" ]] && n=0
  kwriteconfig6 --file "$f" --group General --key rules "$1"
  kwriteconfig6 --file "$f" --group General --key count "$n"
}

if [[ "${1:-}" == "--remove" ]]; then
  new=$(tr ',' '\n' <<<"$(current_rules)" | grep -vx "$g" | paste -sd, || true)
  set_rules "$new"
  python3 - "${XDG_CONFIG_HOME:-$HOME/.config}/$f" "$g" <<'PY'
import re, sys
path, grp = sys.argv[1:3]
try:
    txt = open(path).read()
except OSError:
    sys.exit(0)
txt = re.sub(r"^\[" + re.escape(grp) + r"\]\n(?:(?!\[).*\n?)*", "", txt, flags=re.M)
open(path, "w").write(txt)
PY
  reload; echo "Removed rule '$g'"; exit 0
fi

[[ $# -eq 2 && $1 =~ ^[0-9]+$ && $2 =~ ^[0-9]+$ ]] || { sed -n '2,11p' "$0"; exit 1; }
rules=$(current_rules)
if ! tr ',' '\n' <<<"$rules" | grep -qx "$g"; then
  set_rules "${rules:+$rules,}$g"
fi

w() { kwriteconfig6 --file "$f" --group "$g" --key "$@"; }
w Description "Powermon overlay"
w title powermon;       w titlematch 1
w wmclass powermon;     w wmclassmatch 1
w position "$1,$2";   w positionrule 2
w below true;         w belowrule 2
w skiptaskbar true;   w skiptaskbarrule 2
w skippager true;     w skippagerrule 2
w skipswitcher true;  w skipswitcherrule 2
w noborder true;      w noborderrule 2
reload
echo "Rule '$g' set: position $1,$2. Restart powermon to apply."
