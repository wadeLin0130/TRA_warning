#!/bin/bash
# Quick Tunnel wrapper — starts cloudflared, captures the trycloudflare.com URL,
# writes it to ~/Desktop/tra-url.txt so it's easy to share with colleagues.

URL_FILE="$HOME/Desktop/tra-url.txt"
LOG_FILE="$(dirname "$0")/logs/tunnel-error.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting cloudflared quick tunnel..." >> "$LOG_FILE"

# Run cloudflared and tee stderr so we can parse the URL while still logging it
/opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:8000 2>&1 | while IFS= read -r line; do
    echo "$line" >> "$LOG_FILE"

    # Parse URL from cloudflared output
    if [[ "$line" =~ https://[a-z0-9-]+\.trycloudflare\.com ]]; then
        URL="${BASH_REMATCH[0]}"
        echo "$URL" > "$URL_FILE"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Tunnel URL: $URL" >> "$LOG_FILE"
        # macOS notification
        osascript -e "display notification \"$URL\" with title \"台鐵 App Tunnel 已啟動\" subtitle \"分享給同事使用\"" 2>/dev/null || true
    fi
done
