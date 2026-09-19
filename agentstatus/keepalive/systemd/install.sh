#!/usr/bin/env bash
# Writes (or updates) the agent-status-keepalive systemd --user unit for
# THIS checkout of the repository.
#
# This script never runs as root, never touches system-level systemd, never
# calls `systemctl enable/start/daemon-reload` itself, and never writes any
# secret. It only renders the unit template with paths resolved from this
# checkout and from your own current PATH, and tells you the exact commands
# to run afterward.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Do not run this as root: systemd --user units belong to your own user session, not the system." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/agent-status-tui"

if [ ! -x "$ENTRYPOINT" ]; then
    echo "Could not find the agent-status-tui entrypoint at: $ENTRYPOINT" >&2
    exit 1
fi

resolve() {
    command -v "$1" 2>/dev/null || echo "$1"
}

CLAUDE_BIN="$(resolve claude)"
CODEX_BIN="$(resolve codex)"
GROK_BIN="$(resolve grok)"

for pair in "claude:$CLAUDE_BIN" "codex:$CODEX_BIN" "grok:$GROK_BIN"; do
    name="${pair%%:*}"
    bin="${pair#*:}"
    case "$bin" in
        /*) ;;  # resolved to an absolute path on this PATH right now -- fine
        *)
            echo "Warning: could not resolve '$name' on PATH; the unit will use the bare" >&2
            echo "  command name '$bin', which systemd --user's own (much narrower) PATH" >&2
            echo "  may not find. If the service fails to start on '$name', add its real" >&2
            echo "  install directory to PATH in ~/.config/agent-status-tui/keepalive.env" >&2
            echo "  (see keepalive.env.example next to this script)." >&2
            ;;
    esac
done

PATH_FALLBACK="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

TEMPLATE="$SCRIPT_DIR/agent-status-keepalive.service.in"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/agent-status-keepalive.service"

mkdir -p "$UNIT_DIR"
sed \
    -e "s#@AGENT_STATUS_TUI@#$ENTRYPOINT#g" \
    -e "s#@CLAUDE_BIN@#$CLAUDE_BIN#g" \
    -e "s#@CODEX_BIN@#$CODEX_BIN#g" \
    -e "s#@GROK_BIN@#$GROK_BIN#g" \
    -e "s#@PATH_FALLBACK@#$PATH_FALLBACK#g" \
    "$TEMPLATE" > "$UNIT_PATH"

echo "Wrote $UNIT_PATH"
echo
echo "Nothing was enabled or started. Run these yourself, one at a time:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now agent-status-keepalive.service"
echo "  systemctl --user status agent-status-keepalive.service"
echo
echo "Logs:"
echo "  journalctl --user -u agent-status-keepalive.service -f"
echo
echo "Later:"
echo "  systemctl --user restart agent-status-keepalive.service"
echo "  systemctl --user stop agent-status-keepalive.service"
