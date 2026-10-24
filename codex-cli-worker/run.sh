#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/codex-home /config/codex_tasks
export CODEX_HOME=/data/codex-home
export HOME=/data
export PYTHONDONTWRITEBYTECODE=1
# bashio may reset the image PATH; select the trusted AppArmor wrapper explicitly.
export PATH="/opt/codex-sandbox:${PATH}"

token_file=/data/worker_api_token
legacy_token="$(python3 -c 'import json, pathlib; p=pathlib.Path("/data/options.json"); data=json.loads(p.read_text()) if p.exists() else {}; print(data.get("api_token") or "")')"

if [ ! -s "${token_file}" ]; then
    if [ -n "${legacy_token}" ]; then
        printf '%s' "${legacy_token}" > "${token_file}"
        bashio::log.info "Migrated worker API token from app options into private app storage."
    else
        python3 -c 'import pathlib, secrets; pathlib.Path("/data/worker_api_token").write_text(secrets.token_urlsafe(32), encoding="utf-8")'
        bashio::log.info "Generated a random worker API token in private app storage."
    fi
    chmod 600 "${token_file}"
fi

if [ -n "${legacy_token}" ]; then
    bashio::addon.option 'api_token'
    bashio::log.info "Removed legacy worker API token from app options."
fi

# Readiness is per app start, not a persistent socket left from a previous process.
python3 -c 'from pathlib import Path; Path("/run/codex-remote-verification-ready").unlink(missing_ok=True)'

# The enabled preference and pairing identity live in persistent app data.
# Restore only an explicitly enabled remote connection; keep the worker usable
# when the remote service or network is temporarily unavailable.
if python3 - <<'REMOTE_ENABLED'
import json
import sys
from pathlib import Path

try:
    settings = json.loads(Path("/data/codex-home/app-server-daemon/settings.json").read_text())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if isinstance(settings, dict) and settings.get("remoteControlEnabled") is True else 1)
REMOTE_ENABLED
then
    start_codex_remote_control() {
        local attempt
        # The worker must register MCP and create its socket before the daemon loads config.
        for ready_attempt in $(seq 1 120); do
            [ -f /run/codex-remote-verification-ready ] && break
            sleep 1
        done
        [ -f /run/codex-remote-verification-ready ] || return 1
        for attempt in 1 2 3 4 5 6; do
            # Match the worker's environment boundary for Codex children.
            if env -u SUPERVISOR_TOKEN -u HASSIO_TOKEN -u HA_TOKEN -u HA_URL \
                -u HA_VERIFICATION_CAPABILITY -u HA_DATA_CAPABILITY -u HA_DATA_SOCKET \
                /usr/local/bin/codex remote-control start --json >/dev/null; then
                bashio::log.info "Codex remote control daemon started."
                return 0
            fi
            if [ "$attempt" -lt 6 ]; then
                sleep 10
            fi
        done
        return 1
    }
    (start_codex_remote_control || bashio::log.warning "Remote control startup failed; worker remains available.") &
fi

echo "Codex CLI Worker starting"
python3 /server.py
