#!/usr/bin/env python3
"""
Claude Usage Menu Bar Widget
Shows Claude's 5-hour rolling window and 7-day weekly quota in the macOS menu bar.

Reads the OAuth token from (first match wins):
  1. CLAUDE_OAUTH_TOKEN environment variable
  2. ~/.claude_menubar.json  {"oauth_token": "..."}
  3. ~/.claude/.credentials.json  (claudeAiOauth.accessToken)
  4. macOS Keychain ("Claude Code-credentials")

Requirements: pip install rumps pyobjc-framework-Cocoa
"""

import json
import os
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import rumps

MENUBAR_CONFIG = Path.home() / ".claude_menubar.json"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
POLL_INTERVAL = 300  # seconds (5 minutes)


def get_claude_code_version():
    """Detect installed Claude Code version for the User-Agent header."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Output is like "2.1.173 (Claude Code)"
            return result.stdout.strip().split()[0]
    except Exception:
        pass
    return "2.1.0"  # fallback


def get_access_token():
    """Read the OAuth access token. Checks multiple locations."""
    # 1. Environment variable
    token = os.environ.get("CLAUDE_OAUTH_TOKEN")
    if token:
        return token

    # 2. Dedicated menubar config file
    if MENUBAR_CONFIG.exists():
        try:
            data = json.loads(MENUBAR_CONFIG.read_text())
            token = data.get("oauth_token")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    # 3. Claude Code credentials file
    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text())
            token = data.get("claudeAiOauth", {}).get("accessToken")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    # 4. macOS Keychain
    try:
        user = os.environ.get("USER", "")
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-a", user, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
            try:
                data = json.loads(raw)
                # Nested: {"claudeAiOauth": {"accessToken": "..."}}
                for outer in ("claudeAiOauth", "oauth"):
                    if isinstance(data.get(outer), dict):
                        for key in ("accessToken", "access_token", "token"):
                            if key in data[outer]:
                                return data[outer][key]
                # Flat: {"accessToken": "..."}
                for key in ("accessToken", "access_token", "token"):
                    if key in data:
                        return data[key]
            except json.JSONDecodeError:
                return raw
    except Exception:
        pass

    return None


# Detect version once at startup
_CC_VERSION = get_claude_code_version()


def fetch_usage(token):
    """Fetch usage data from the OAuth usage endpoint."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{_CC_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def format_time_remaining(resets_at_str):
    """Return a human-readable string for time until reset."""
    if not resets_at_str:
        return "—"
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = resets_at - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "resetting..."
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except Exception:
        return "?"


def pct_used(utilization):
    """Round utilization (0–100) to an integer."""
    return min(100, max(0, round(utilization)))


def status_dot(utilization):
    """Colored indicator for usage level."""
    if utilization >= 90:
        return "🔴"
    if utilization >= 75:
        return "🟠"
    if utilization >= 50:
        return "🟡"
    return "🟢"


def bar(utilization, width=20):
    """Render a text progress bar. utilization is clamped to 0–100."""
    utilization = min(100, max(0, utilization))
    used = round((utilization / 100) * width)
    return "█" * used + "░" * (width - used)


class ClaudeUsageApp(rumps.App):
    def __init__(self):
        super().__init__("Claude Usage", quit_button=None)
        self.title = "…"
        self.usage_data = None
        self.last_error = None

        # Info rows get a no-op callback: callback-less items are disabled
        # and macOS dims them (even re-enabling is undone by menu validation
        # at popup time, since they have no action).
        self.menu = [
            rumps.MenuItem("5-Hour Window", callback=self._noop),
            rumps.MenuItem("Weekly Quota", callback=self._noop),
            None,
            rumps.MenuItem("Last Updated: never", callback=self._noop),
            rumps.MenuItem("Refresh Now", callback=self.refresh, key="r"),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        self.refresh(None)

    def _noop(self, _):
        pass

    @rumps.timer(POLL_INTERVAL)
    def auto_refresh(self, _):
        self.refresh(None)

    def refresh(self, _):
        token = get_access_token()
        if not token:
            self.title = "No token"
            self.last_error = (
                "No OAuth token found. Log in with: claude /login "
                "(select 'Claude account with subscription')"
            )
            self._update_menu_error()
            return

        try:
            self.usage_data = fetch_usage(token)
            self.last_error = None
            self._update_display()
        except urllib.error.HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            self.title = f"err:{e.code}"
            self._update_menu_error()
        except Exception as e:
            self.last_error = str(e)[:50]
            self.title = "err"
            self._update_menu_error()

    def _set_item_title(self, name, text):
        """Set a menu item's title in monospaced, full-contrast text.

        The info rows are non-clickable, so macOS dims them; an attributed
        title with an explicit color overrides that. Monospaced font keeps
        the two progress bars aligned (the menu font is proportional, so
        space-padding alone can't line them up).
        """
        item = self.menu[name]
        item.title = text
        try:
            from AppKit import (
                NSAttributedString,
                NSColor,
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
            )
            font = NSFont.monospacedSystemFontOfSize_weight_(
                NSFont.systemFontSize(), 0.0
            )
            attr = NSAttributedString.alloc().initWithString_attributes_(
                text,
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: NSColor.labelColor(),
                },
            )
            item._menuitem.setAttributedTitle_(attr)
        except Exception:
            pass

    def _update_display(self):
        d = self.usage_data
        five = d.get("five_hour", {})
        week = d.get("seven_day", {})

        five_util = five.get("utilization", 0)
        week_util = week.get("utilization", 0)

        five_used = pct_used(five_util)
        week_used = pct_used(week_util)

        self.title = f"5h:{five_used}%  7d:{week_used}%"

        five_reset = format_time_remaining(five.get("resets_at", ""))
        week_reset = format_time_remaining(week.get("resets_at", ""))

        self._set_item_title(
            "5-Hour Window",
            f"{status_dot(five_util)} 5-Hour Window:  {bar(five_util, 15)}  "
            f"{five_used}% used  (resets in {five_reset})",
        )
        self._set_item_title(
            "Weekly Quota",
            f"{status_dot(week_util)} Weekly Quota:   {bar(week_util, 15)}  "
            f"{week_used}% used  (resets in {week_reset})",
        )

        now = datetime.now().strftime("%H:%M")
        self._set_item_title("Last Updated: never", f"Last Updated: {now}")

    def _update_menu_error(self):
        err = self.last_error or "Unknown error"
        self._set_item_title("5-Hour Window", f"Error: {err}")
        self._set_item_title("Weekly Quota", "—")
        now = datetime.now().strftime("%H:%M")
        self._set_item_title(
            "Last Updated: never", f"Last Updated: {now} (error)"
        )


if __name__ == "__main__":
    # Hide the Python rocket icon from the Dock
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except ImportError:
        pass

    ClaudeUsageApp().run()
