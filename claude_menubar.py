#!/usr/bin/env python3
"""
Claude Usage Menu Bar Widget
Shows Claude's 5-hour rolling window and 7-day weekly quota in the macOS menu bar.

Reads OAuth credentials from (first match wins):
  1. CLAUDE_OAUTH_TOKEN environment variable (static, no auto-refresh)
  2. ~/.claude_menubar.json  {"oauth_token": "..."}  (static, no auto-refresh)
  3. ~/.claude/.credentials.json  (claudeAiOauth.accessToken)
  4. macOS Keychain ("Claude Code-credentials")

For sources 3 and 4 the stored refresh token is used to renew the access
token when it expires, and the rotated credentials are written back so
Claude Code stays in sync.

Requirements: pip install rumps pyobjc-framework-Cocoa
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import rumps

MENUBAR_CONFIG = Path.home() / ".claude_menubar.json"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Claude Code's public OAuth client id (same one the CLI uses for /login)
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
POLL_INTERVAL = 300  # seconds (5 minutes)
EXPIRY_MARGIN = 60  # refresh this many seconds before expiresAt


class NoTokenError(Exception):
    pass


class ReauthRequiredError(Exception):
    pass


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


def get_static_token():
    """Tokens supplied directly by the user; these can't be auto-refreshed."""
    token = os.environ.get("CLAUDE_OAUTH_TOKEN")
    if token:
        return token

    if MENUBAR_CONFIG.exists():
        try:
            data = json.loads(MENUBAR_CONFIG.read_text())
            token = data.get("oauth_token")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    return None


def _extract_oauth(data):
    """Find the dict holding accessToken inside a credentials JSON blob."""
    if not isinstance(data, dict):
        return None
    for outer in ("claudeAiOauth", "oauth"):
        if isinstance(data.get(outer), dict) and data[outer].get("accessToken"):
            return data[outer]
    if data.get("accessToken"):
        return data
    return None


def _file_read():
    if CREDENTIALS_PATH.exists():
        try:
            return json.loads(CREDENTIALS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _file_write(data):
    CREDENTIALS_PATH.write_text(json.dumps(data))
    os.chmod(CREDENTIALS_PATH, 0o600)


def _keychain_read():
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", ""), "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def _keychain_write(data):
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", ""),
         "-w", json.dumps(data)],
        capture_output=True, text=True, timeout=5,
    )


def load_credentials():
    """Return (container, oauth, write_fn) from the first available store.

    container is the full parsed JSON (what gets written back), oauth is
    the nested dict holding accessToken/refreshToken/expiresAt.
    """
    for read_fn, write_fn in ((_file_read, _file_write),
                              (_keychain_read, _keychain_write)):
        container = read_fn()
        oauth = _extract_oauth(container)
        if oauth:
            return container, oauth, write_fn
    return None, None, None


def token_expired(oauth):
    expires_at = oauth.get("expiresAt")
    if not expires_at:
        return False
    return time.time() >= expires_at / 1000 - EXPIRY_MARGIN


def refresh_credentials(container, oauth, write_fn):
    """Exchange the refresh token for a new access token and persist it.

    Refresh tokens rotate: the response carries a replacement, and the old
    one stops working. Persisting the rotated credentials back to the same
    store is what keeps Claude Code's login valid.
    """
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"claude-code/{_CC_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise ReauthRequiredError(
                "refresh token rejected — run: claude /login"
            ) from e
        raise

    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + tok["expires_in"]) * 1000)
    write_fn(container)


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


def get_usage():
    """Fetch usage, refreshing the access token when needed."""
    token = get_static_token()
    if token:
        return fetch_usage(token)

    container, oauth, write_fn = load_credentials()
    if not oauth:
        raise NoTokenError()

    if token_expired(oauth) and oauth.get("refreshToken"):
        try:
            refresh_credentials(container, oauth, write_fn)
        except ReauthRequiredError:
            raise
        except Exception:
            # Transient failure (e.g. the endpoint rate-limits refresh
            # attempts while the token is still valid). The stored token
            # may still work — fall through and try it; a 401 below
            # retries the refresh and surfaces the real error.
            pass

    try:
        return fetch_usage(oauth["accessToken"])
    except urllib.error.HTTPError as e:
        # Token may have been revoked before its expiry timestamp;
        # try one refresh and retry.
        if e.code == 401 and oauth.get("refreshToken"):
            refresh_credentials(container, oauth, write_fn)
            return fetch_usage(oauth["accessToken"])
        raise


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
        try:
            self.usage_data = get_usage()
            self.last_error = None
            self._update_display()
        except NoTokenError:
            self.title = "No token"
            self.last_error = (
                "No OAuth token found. Log in with: claude /login "
                "(select 'Claude account with subscription')"
            )
            self._update_menu_error()
        except ReauthRequiredError as e:
            self.title = "re-auth"
            self.last_error = str(e)
            self._update_menu_error()
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
