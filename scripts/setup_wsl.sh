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

# Home Assistant MCP server. ha-mcp needs Python >= 3.13, newer than Ubuntu
# 24.04's 3.12, so it gets its own uv-managed venv. Pinned to the version in use.
HA_MCP_VERSION="8.3.0"
if command -v uv >/dev/null 2>&1; then
    if [[ ! -x vendor/ha-mcp/.venv/bin/ha-mcp-web ]]; then
        mkdir -p vendor/ha-mcp
        uv venv --python 3.13 vendor/ha-mcp/.venv
        VIRTUAL_ENV="$REPO_ROOT/vendor/ha-mcp/.venv" uv pip install --quiet "ha-mcp==$HA_MCP_VERSION"
    fi
else
    echo "warning: uv not found; skipping ha-mcp (install uv, then re-run this script)" >&2
fi

echo "OK: .venv ready at $REPO_ROOT/.venv"
