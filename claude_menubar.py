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
import sys
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

# At login, launchd starts this app with a minimal PATH (typically just
# /usr/bin:/bin:/usr/sbin:/sbin), so `claude` — installed under a user dir —
# isn't found. We probe these locations explicitly so the User-Agent carries
# a real claude-code version instead of the fallback (the usage endpoint
# rate-limits unknown/old User-Agents more aggressively).
CLAUDE_BIN_CANDIDATES = [
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".claude" / "local" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
    Path.home() / ".npm-global" / "bin" / "claude",
    Path.home() / "node_modules" / ".bin" / "claude",
]

# Startup self-heal: a fresh login often has a stale access token, and the
# very first request can come back 429/401 before the network is fully up.
# Retry a few times so the meter paints quickly instead of waiting a full
# POLL_INTERVAL on whatever transient error happened at login.
STARTUP_RETRIES = 4
STARTUP_RETRY_INTERVAL = 4  # seconds between startup retries

# Fallback refresh path. When our own OAuth refresh is rate limited (or the
# stored refresh token is stale), we let Claude Code refresh instead.
# Launching `claude` makes it renew its OAuth token at startup and write the
# rotated credentials back to the Keychain — no prompt is sent, so no usage
# is consumed. We feed EOF on stdin so the session exits right after init,
# and cap it with a timeout so a hang can never wedge the menu bar.
CLAUDE_REFRESH_ARGS = []      # bare `claude`; the refresh happens at startup
CLAUDE_REFRESH_TIMEOUT = 20   # seconds (safety cap; it normally exits in ~1-3s)


class NoTokenError(Exception):
    pass


class ReauthRequiredError(Exception):
    pass


class RateLimitedError(Exception):
    """Server returned 429. retry_after is seconds to wait, if provided."""

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        msg = (f"rate limited (retry in ~{round(retry_after / 60)}m)"
               if retry_after else "rate limited")
        super().__init__(msg)


def _parse_retry_after(headers):
    """Pull an integer seconds value out of a Retry-After header."""
    if not headers:
        return None
    val = headers.get("retry-after")
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _find_claude_bin():
    """Locate the claude CLI even under launchd's minimal PATH."""
    from shutil import which

    found = which("claude")
    if found:
        return found
    for candidate in CLAUDE_BIN_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return None


def get_claude_code_version():
    """Detect installed Claude Code version for the User-Agent header."""
    claude_bin = _find_claude_bin()
    if claude_bin:
        try:
            result = subprocess.run(
                [claude_bin, "--version"],
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
    """Return (container, oauth, write_fn) for the freshest available store.

    container is the full parsed JSON (what gets written back), oauth is
    the nested dict holding accessToken/refreshToken/expiresAt.

    Both the legacy file and the Keychain can hold credentials at once. If
    `claude` recently refreshed in one store, the other may carry a stale
    token whose refresh token has already been rotated away. We pick the
    store with the latest expiresAt so we always use the most recent token.
    """
    best = (None, None, None)
    best_expiry = -1
    for read_fn, write_fn in ((_file_read, _file_write),
                              (_keychain_read, _keychain_write)):
        container = read_fn()
        oauth = _extract_oauth(container)
        if not oauth:
            continue
        expiry = oauth.get("expiresAt") or 0
        if expiry > best_expiry:
            best_expiry = expiry
            best = (container, oauth, write_fn)
    return best


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
        if e.code == 429:
            # The token endpoint itself is rate limited. Hammering it is
            # what gets us banned, so surface this and let the caller wait.
            raise RateLimitedError(_parse_retry_after(e.headers)) from e
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


def refresh_via_claude(oauth_before):
    """Let Claude Code refresh the token, as a fallback to our own refresh.

    Launching `claude` makes it renew its OAuth token at startup and write
    the rotated credentials back to the Keychain — without sending a prompt,
    so it spends no usage. Claude Code holds the current (un-rotated) refresh
    token and isn't subject to the same rate limit our direct refresh hit, so
    this often succeeds when `refresh_credentials` is banned.

    Returns True only if the stored access token actually advanced, i.e. a
    real refresh happened. (If the token wasn't due, `claude` is a no-op and
    this returns False — harmless, since we only call it when expired.)
    """
    claude_bin = _find_claude_bin()
    if not claude_bin:
        return False
    before = (oauth_before or {}).get("expiresAt") or 0
    try:
        subprocess.run(
            [claude_bin, *CLAUDE_REFRESH_ARGS],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CLAUDE_REFRESH_TIMEOUT,
        )
    except Exception:
        # A non-zero exit or a timeout doesn't mean failure: the refresh
        # happens during startup, before the input loop, so the token may
        # already be renewed. Fall through and judge by the stored value.
        pass
    _, oauth_after, _ = load_credentials()
    after = (oauth_after or {}).get("expiresAt") or 0
    return after > before


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
    """Fetch usage, refreshing the access token when needed.

    The usage endpoint answers an *expired* token with a long 429 ban
    (observed Retry-After ~1h) rather than a clean 401, so the cardinal
    rule is: never call it with a token we already know is expired. We
    refresh first, and we refresh at most once per call. If a refresh is
    itself rate limited we raise RateLimitedError and let the app back off,
    rather than falling through to a doomed request that deepens the ban.
    """
    token = get_static_token()
    if token:
        return fetch_usage(token)

    container, oauth, write_fn = load_credentials()
    if not oauth:
        raise NoTokenError()

    if token_expired(oauth):
        # Try our own cheap refresh first.
        direct_error = None
        if oauth.get("refreshToken"):
            try:
                refresh_credentials(container, oauth, write_fn)
            except (RateLimitedError, ReauthRequiredError) as e:
                direct_error = e
        else:
            direct_error = ReauthRequiredError(
                "access token expired and no refresh token"
            )

        # If that failed (rate limited, or our stored refresh token is
        # stale), let Claude Code refresh for us — no usage spent — then
        # reload the credentials it just wrote.
        if direct_error is not None:
            if refresh_via_claude(oauth):
                container, oauth, write_fn = load_credentials()
            else:
                raise direct_error

    try:
        return fetch_usage(oauth["accessToken"])
    except urllib.error.HTTPError as e:
        if e.code == 401 and oauth.get("refreshToken"):
            # Token was revoked before its expiry timestamp; refresh once.
            refresh_credentials(container, oauth, write_fn)
            return fetch_usage(oauth["accessToken"])
        if e.code == 429:
            raise RateLimitedError(_parse_retry_after(e.headers)) from e
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
        self._startup_attempt = 0
        self._startup_timer = None
        self._cooldown_until = 0  # epoch secs; skip requests until then

        # Info rows get a no-op callback: callback-less items are disabled
        # and macOS dims them (even re-enabling is undone by menu validation
        # at popup time, since they have no action).
        self._reauth_item = rumps.MenuItem(
            "Re-authenticate...", callback=self._launch_login, key="l",
        )
        self.menu = [
            rumps.MenuItem("5-Hour Window", callback=self._noop),
            rumps.MenuItem("Weekly Quota", callback=self._noop),
            None,
            rumps.MenuItem("Last Updated: never", callback=self._noop),
            rumps.MenuItem("Refresh Now", callback=self.manual_refresh, key="r"),
            self._reauth_item,
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self._set_reauth_visible(False)

        self.refresh(None)
        # If the first attempt failed (stale token, network not up yet at
        # login), retry quickly a few times instead of waiting 5 minutes.
        if self.last_error is not None:
            self._startup_timer = rumps.Timer(
                self._startup_retry, STARTUP_RETRY_INTERVAL
            )
            self._startup_timer.start()

    def _noop(self, _):
        pass

    def _set_reauth_visible(self, visible):
        try:
            self._reauth_item._menuitem.setHidden_(not visible)
        except Exception:
            pass

    def _launch_login(self, _):
        """Open Terminal and run `claude /login` so the user can re-authenticate."""
        claude_bin = _find_claude_bin() or "claude"
        script = (
            f'tell application "Terminal"\n'
            f'  activate\n'
            f'  do script "{claude_bin} /login"\n'
            f'end tell'
        )
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        # Poll for fresh credentials after the user completes the browser flow.
        self._login_poll_count = 0
        if getattr(self, "_login_timer", None):
            self._login_timer.stop()
        self._login_timer = rumps.Timer(self._poll_after_login, 5)
        self._login_timer.start()

    def _poll_after_login(self, sender):
        """Check periodically whether login wrote fresh credentials."""
        self._login_poll_count += 1
        self.refresh(None)
        if self.last_error is None or self._login_poll_count >= 24:
            sender.stop()

    def _startup_retry(self, sender):
        self._startup_attempt += 1
        self.refresh(None)
        in_cooldown = time.time() < self._cooldown_until
        # Stop the rapid startup retries on success, on a rate-limit cooldown
        # (retrying fast would only deepen the ban — the 5-min poll takes over
        # once the cooldown expires), or once attempts are exhausted.
        if (self.last_error is None or in_cooldown
                or self._startup_attempt >= STARTUP_RETRIES):
            sender.stop()

    @rumps.timer(POLL_INTERVAL)
    def auto_refresh(self, _):
        self.refresh(None)

    def manual_refresh(self, _):
        """Refresh Now / Cmd-R: clear any cooldown and try immediately.

        An explicit user action overrides the rate-limit backoff — handy
        right after running `claude`, which may have just written a fresh
        token that the cooldown would otherwise make us ignore.
        """
        self._cooldown_until = 0
        self.refresh(None)

    def refresh(self, _):
        # Honor an active rate-limit cooldown: hitting the endpoint again
        # before Retry-After elapses only extends the ban.
        if time.time() < self._cooldown_until:
            mins = max(1, round((self._cooldown_until - time.time()) / 60))
            self.title = "rate-limited"
            self.last_error = f"Rate limited — auto-retry in ~{mins}m"
            self._update_menu_error()
            return
        try:
            self.usage_data = get_usage()
            self.last_error = None
            self._cooldown_until = 0
            self._set_reauth_visible(False)
            self._update_display()
        except NoTokenError:
            self.title = "No token"
            self.last_error = (
                "No OAuth token found — click Re-authenticate"
            )
            self._set_reauth_visible(True)
            self._update_menu_error()
        except ReauthRequiredError as e:
            self.title = "re-auth"
            self.last_error = str(e)
            self._set_reauth_visible(True)
            self._update_menu_error()
        except RateLimitedError as e:
            # Wait out the server's Retry-After (default 15m if unspecified),
            # capped so a huge value can't wedge the app for hours.
            wait = min(e.retry_after or 900, 3600)
            self._cooldown_until = time.time() + wait
            mins = max(1, round(wait / 60))
            self.title = "rate-limited"
            self.last_error = f"Rate limited — auto-retry in ~{mins}m"
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


def run_diagnostic():
    """Print exactly what the app sees, without starting the menu bar.

    Run with:  python3 claude_menubar.py --check
    Tokens themselves are never printed.
    """
    print("=== claude-usage-menubar diagnostic ===")
    print(f"python:          {sys.executable}")
    print(f"claude binary:   {_find_claude_bin() or 'NOT FOUND on PATH or known locations'}")
    print(f"User-Agent:      claude-code/{_CC_VERSION}")

    if get_static_token():
        print("credentials:     static token (env/config) — no auto-refresh")
        oauth = None
        container = write_fn = None
    else:
        container, oauth, write_fn = load_credentials()
        if not oauth:
            print("credentials:     NONE found in file or Keychain")
            print("\nRun: claude /login  (select 'Claude account with subscription')")
            return
        src = "file" if _extract_oauth(_file_read()) is oauth else "keychain"
        print(f"credentials:     loaded from {src} (freshest of the two)")
        exp = oauth.get("expiresAt")
        if exp:
            secs = exp / 1000 - time.time()
            print(f"expiresAt:       {datetime.fromtimestamp(exp/1000)} "
                  f"({secs/3600:+.1f}h from now)")
        else:
            print("expiresAt:       (missing)")
        print(f"token_expired:   {token_expired(oauth)}")
        print(f"has refresh tok: {bool(oauth.get('refreshToken'))}")

    token = get_static_token() or (oauth or {}).get("accessToken")
    print("\n--- raw usage request ---")
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{_CC_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"HTTP {resp.status} OK")
            print(resp.read().decode()[:400])
            return
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}")
        print(f"retry-after:     {e.headers.get('retry-after')}")
        print(f"x-ratelimit:     {e.headers.get('anthropic-ratelimit-unified-status')}")
        body = e.read().decode()[:600]
        print(f"body:            {body}")
    except Exception as e:
        print(f"request failed: {e}")
        return

    if oauth and oauth.get("refreshToken"):
        print("\n--- attempting token refresh ---")
        try:
            refresh_credentials(container, oauth, write_fn)
            print("refresh: OK (new token written)")
            with urllib.request.urlopen(
                urllib.request.Request(
                    USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {oauth['accessToken']}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "User-Agent": f"claude-code/{_CC_VERSION}",
                    },
                ),
                timeout=10,
            ) as resp:
                print(f"retry usage: HTTP {resp.status} OK — self-heal works")
        except RateLimitedError as e:
            print(f"refresh rate limited: {e}")
            print("  -> the token endpoint is banned right now; the app will "
                  "wait out the cooldown and refresh automatically. No manual "
                  "`claude` run needed once the window clears.")
        except urllib.error.HTTPError as e:
            print(f"refresh/retry HTTP {e.code} {e.reason}")
            print(f"retry-after: {e.headers.get('retry-after')}")
            print(f"body: {e.read().decode()[:600]}")
        except ReauthRequiredError as e:
            print(f"refresh rejected: {e}")
        except Exception as e:
            print(f"refresh failed: {e}")


def test_claude_refresh():
    """Exercise the Claude-Code fallback refresh in isolation.

    Run with:  python3 claude_menubar.py --claude-refresh
    Note: if your token is still fresh, `claude` won't refresh it (it only
    renews when due), so 'advanced: False' here just means 'nothing to do',
    not that the mechanism is broken.
    """
    claude_bin = _find_claude_bin()
    print(f"claude binary:   {claude_bin or 'NOT FOUND'}")
    if not claude_bin:
        return
    _, oauth, _ = load_credentials()
    before = (oauth or {}).get("expiresAt") or 0
    print(f"expiresAt before: {datetime.fromtimestamp(before/1000) if before else '—'}")
    t0 = time.time()
    advanced = refresh_via_claude(oauth)
    print(f"claude ran in:   {time.time() - t0:.1f}s")
    _, oauth2, _ = load_credentials()
    after = (oauth2 or {}).get("expiresAt") or 0
    print(f"expiresAt after: {datetime.fromtimestamp(after/1000) if after else '—'}")
    print(f"token advanced:  {advanced}")


def test_expired_refresh():
    """Prove the claude fallback against an actually-expired token.

    Run with:  python3 claude_menubar.py --test-expired-refresh
    Safe and reversible: it only rewrites the `expiresAt` *timestamp*, never
    the token itself. If `claude` refreshes (rotating to a new token), we keep
    it. If it doesn't, we restore the original timestamp. Worst case, your
    real token still works and `claude /login` would reset everything anyway.
    """
    if get_static_token():
        print("Using a static token (env/config); nothing to refresh.")
        return
    container, oauth, write_fn = load_credentials()
    if not oauth:
        print("No OAuth credentials found.")
        return
    orig = oauth.get("expiresAt")
    print(f"real expiresAt:   {datetime.fromtimestamp(orig/1000) if orig else '—'}")

    # Temporarily backdate the timestamp so claude considers it due.
    oauth["expiresAt"] = int((time.time() - 3600) * 1000)
    write_fn(container)
    print("marked token as expired (timestamp only); launching claude...")

    t0 = time.time()
    advanced = refresh_via_claude(oauth)
    print(f"claude ran in:    {time.time() - t0:.1f}s")

    _, oauth2, _ = load_credentials()
    new_exp = (oauth2 or {}).get("expiresAt")
    if advanced:
        print(f"new expiresAt:    {datetime.fromtimestamp(new_exp/1000)}")
        print("RESULT: claude refreshed the expired token — the fallback WORKS.")
    else:
        oauth["expiresAt"] = orig
        write_fn(container)
        print("RESULT: claude did NOT refresh; restored original timestamp.")
        print("        The bare `claude` invocation skips refresh when "
              "non-interactive. Consider adjusting CLAUDE_REFRESH_ARGS.")


if __name__ == "__main__":
    if "--check" in sys.argv:
        run_diagnostic()
        sys.exit(0)
    if "--claude-refresh" in sys.argv:
        test_claude_refresh()
        sys.exit(0)
    if "--test-expired-refresh" in sys.argv:
        test_expired_refresh()
        sys.exit(0)

    # Hide the Python rocket icon from the Dock
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except ImportError:
        pass

    ClaudeUsageApp().run()
