#!/usr/bin/env bash
# Install the systemd user units from systemd/ into ~/.config/systemd/user/,
# substituting this checkout's path for @REPO_ROOT@.
#
# Usage:
#   scripts/install_systemd.sh                       install only
#   scripts/install_systemd.sh assistant-scheduler.timer ...
#                                                    install, then enable --now the named units
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"

for unit in "$REPO_ROOT"/systemd/*.service "$REPO_ROOT"/systemd/*.timer; do
    [[ -e "$unit" ]] || continue
    sed "s#@REPO_ROOT@#$REPO_ROOT#g" "$unit" >"$DEST/$(basename "$unit")"
done
systemctl --user daemon-reload
echo "Installed units into $DEST"

if [[ $# -gt 0 ]]; then
    systemctl --user enable --now "$@"
fi
