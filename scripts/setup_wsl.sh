#!/usr/bin/env bash
# Create the repo virtualenv and install Python deps plus Playwright's Chromium.
# Safe to re-run. Pass --dev to also install test dependencies.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

REQ=requirements.txt
[[ "${1:-}" == "--dev" ]] && REQ=requirements-dev.txt

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r "$REQ"

# System libraries need root; the browser itself must install as this user
# so it lands in ~/.cache/ms-playwright where the scripts look for it.
if [[ $EUID -eq 0 ]]; then
    .venv/bin/playwright install-deps chromium
else
    sudo "$REPO_ROOT/.venv/bin/playwright" install-deps chromium
fi
.venv/bin/playwright install chromium

echo "OK: .venv ready at $REPO_ROOT/.venv"
