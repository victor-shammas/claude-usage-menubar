#!/bin/bash
# Install script for claude-usage-menubar
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.claude-usage-menubar"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
PYTHON="$(which python3)"

echo "claude-usage-menubar installer"
echo "==============================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi

echo "Using Python: $PYTHON"
echo ""

# Install dependencies
echo "Installing dependencies..."
# Use $PYTHON -m pip so packages land in the same interpreter the LaunchAgent runs
if "$PYTHON" -m pip install --user --break-system-packages -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null; then
    true
elif "$PYTHON" -m pip install --user -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null; then
    true
elif "$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null; then
    true
else
    echo "Error: Failed to install dependencies. Try manually:"
    echo "  pip3 install rumps pyobjc-framework-Cocoa"
    exit 1
fi

echo ""

# Check Claude Code auth
if ! security find-generic-password -s "Claude Code-credentials" -a "$USER" &>/dev/null; then
    if [ ! -f "$HOME/.claude/.credentials.json" ]; then
        echo "Warning: No Claude Code credentials found."
        echo "Run 'claude /login' and select 'Claude account with subscription'."
        echo "The menu bar app will show 'No token' until you do."
        echo ""
    fi
fi

# Set up LaunchAgent
echo "Setting up auto-start..."

# Unload existing if present
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Generate plist with correct paths
cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/claude_menubar.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude_menubar.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude_menubar.err</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_DST"

echo ""
echo "Done! The menu bar widget should appear shortly."
echo ""
echo "To uninstall:"
echo "  launchctl unload ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo "  rm ~/Library/LaunchAgents/$PLIST_NAME.plist"
