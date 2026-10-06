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

# Folders the agents write into. Headless runs may not mkdir, so they must exist.
mkdir -p state/finance_reports state/retirement state/hyvee_tmp state/logs

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

# Monarch MCP server (community, replaces Monarch's paused official MCP).
# Pinned to the commit in use and installed from its uv.lock, because the
# newest resolvable mcp release is a different major version than it targets.
MONARCH_MCP_COMMIT="ca6c1598c99a0ff1f7b7effbb2c9f518224eab44"
if command -v uv >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
    if [[ ! -x vendor/monarch-mcp-server/.venv/bin/monarch-mcp-server ]]; then
        if [[ ! -d vendor/monarch-mcp-server/.git ]]; then
            git clone --quiet https://github.com/robcerda/monarch-mcp-server.git vendor/monarch-mcp-server
        fi
        git -C vendor/monarch-mcp-server checkout --quiet "$MONARCH_MCP_COMMIT"
        (cd vendor/monarch-mcp-server &&
            UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --frozen --python 3.12 --quiet)
    fi
else
    echo "warning: uv/git not found; skipping monarch-mcp-server" >&2
fi

echo "OK: .venv ready at $REPO_ROOT/.venv"
