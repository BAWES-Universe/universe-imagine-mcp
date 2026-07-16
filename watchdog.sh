#!/bin/bash
# Watchdog for imagine-mcp server.
# Checks every 2 minutes if the server is running, restarts if dead.
# Adjust MCP_DIR to match your install path.

MCP_DIR="$(cd "$(dirname "$0")" && pwd)"
MCP_LOG="$MCP_DIR/imagine_mcp.log"

cd "$MCP_DIR" || exit 1

if ! pgrep -f "designer_mcp.py" > /dev/null 2>&1; then
    echo "[$(date)] Imagine MCP not running, starting..." >> "$MCP_LOG"
    source bin/activate
    export $(grep -v '^#' .env | xargs)
    nohup python3 designer_mcp.py >> "$MCP_LOG" 2>&1 &
    echo "[$(date)] Started PID $!" >> "$MCP_LOG"
fi
