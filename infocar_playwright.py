#!/usr/bin/env python3
"""
InfoCar Playwright Login Helper
===============================
Opens a browser window once so the user can log into info-kierowca.pl, then hands the
session cookies to the bot. It does NOT keep a page open afterwards: the site's Angular
app calls /bknd/auth/api/v1/jwt/logout after frontendInactivitySeconds (600s) without
real mouse/keyboard events, which killed the bot's session server-side.
"""

import os
import time
import json
import logging
import re
from typing import Dict, Optional, List, Any

logger = logging.getLogger("InfoCarBot")

try:
    from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class PlaywrightSessionManager:
    """One-time interactive login and cookie extraction via Playwright."""

    BASE_URL = "https://info-kierowca.pl"
    USER_DATA_DIR = os.path.abspath("./browser_data")
    STATE_FILE = "storage_state.json"

    def __init__(self, config_path: str = "config.json", headless: bool = True):
        self.config_path = config_path
        self.headless = headless

    @staticmethod
    def is_available() -> bool:
        """Returns True if Playwright library is installed."""
        return PLAYWRIGHT_AVAILABLE

    @staticmethod
    def build_cookie_string(cookies_list: List[Dict[str, Any]]) -> str:
        """Converts Playwright cookie list back into a single HTTP Cookie header string."""
        parts = []
        pudojt = None
        pudojtmd = None
        consent = None

        for c in cookies_list:
            name = c.get("name")
            val = c.get("value")
            if name == "__Secure-PUDOJT":
                pudojt = val
            elif name == "__Secure-PUDOJTMD":
                pudojtmd = val
            elif name == "CookieScriptConsent":
                consent = val

        if consent:
            parts.append(f"CookieScriptConsent={consent}")
        if pudojt:
            parts.append(f"__Secure-PUDOJT={pudojt}")
        if pudojtmd:
            parts.append(f"__Secure-PUDOJTMD={pudojtmd}")

        return "; ".join(parts)

    def is_valid_token_str(self, cookie_str: str) -> bool:
        """Validates that cookie string contains a valid non-empty __Secure-PUDOJT JWT token."""
        if not cookie_str or "__Secure-PUDOJT=" not in cookie_str:
            return False
        pudojt_match = re.search(r'__Secure-PUDOJT=([^;]+)', cookie_str)
        if not pudojt_match:
            return False
        val = pudojt_match.group(1).lower()
        if "no%20token" in val or "no token" in val or len(val) < 30:
            return False
        return True

    @staticmethod
    def parse_cookie_string(cookie_str: str, domain: str = "info-kierowca.pl") -> List[Dict[str, Any]]:
        """Parses an HTTP Cookie header string into a list of Playwright cookie dictionaries."""
        cookies = []
        if not cookie_str:
            return cookies
        clean_str = cookie_str.strip()
        if clean_str.lower().startswith("cookie:"):
            clean_str = clean_str[7:].strip()
        parts = clean_str.split(";")
        for p in parts:
            p = p.strip()
            if not p or "=" not in p:
                continue
            name, val = p.split("=", 1)
            name = name.strip()
            val = val.strip()
            cookies.append({
                "name": name,
                "value": val,
                "domain": domain,
                "path": "/",
                "secure": True if name.startswith("__Secure-") else False
            })
        return cookies

    def _get_saved_token(self) -> Optional[str]:
        """Loads saved auth_token from config.json or storage_state.json."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                token = cfg.get("auth_token") or cfg.get("token")
                if token and self.is_valid_token_str(token):
                    return token
            except Exception:
                pass
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                cookies = state.get("cookies", [])
                token_str = self.build_cookie_string(cookies)
                if self.is_valid_token_str(token_str):
                    return token_str
            except Exception:
                pass
        return None

    def is_logged_in(self, context: BrowserContext) -> bool:
        """Checks if current browser context holds a valid non-expired __Secure-PUDOJT cookie."""
        try:
            cookies = context.cookies()
            cookie_str = self.build_cookie_string(cookies)
            return self.is_valid_token_str(cookie_str)
        except Exception:
            return False

    def interactive_login(self, timeout_secs: int = 300) -> Optional[str]:
        """
        Opens a visible browser window using persistent storage state so the user can log into InfoCar once.
        Saves session cookies to browser_data directory, storage_state.json, and config.json.
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright is not installed. Install it with: pip install playwright && playwright install chromium")
            return None

        logger.info("==================================================")
        logger.info("Opening browser window for initial interactive login...")
        logger.info("Please complete login on the InfoCar page.")
        logger.info("Your session will be saved permanently in ./browser_data.")
        logger.info("==================================================")

        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.USER_DATA_DIR,
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled']
                )
            except Exception as e:
                logger.error(f"Could not launch browser window: {e}")
                return None

            page = context.pages[0] if context.pages else context.new_page()

            try:
                page.goto(f"{self.BASE_URL}/login")
            except Exception:
                pass

            start_time = time.time()
            auth_cookie = None

            while time.time() - start_time < timeout_secs:
                time.sleep(2)
                try:
                    if "/reservation" in page.url or self.is_logged_in(context):
                        logger.info("Login detected! Capturing session state & cookies...")
                        page.wait_for_timeout(2000)
                        cookies = context.cookies()
                        context.storage_state(path=self.STATE_FILE)
                        auth_cookie = self.build_cookie_string(cookies)
                        if self.is_valid_token_str(auth_cookie):
                            self._save_token_to_config(auth_cookie)
                            logger.info("Session state saved successfully in ./browser_data!")
                            context.close()
                            return auth_cookie
                except Exception:
                    break

            logger.error("Interactive login timed out or browser was closed.")
            try:
                context.close()
            except Exception:
                pass
            return None

    def _save_token_to_config(self, token: str):
        """Saves updated auth_token to config.json safely.

        Also stamps session_started_at: the server caps a session at ~1h from login and
        exposes no claim for it, so login time is the only way the bot can see the cap coming.
        """
        if not self.is_valid_token_str(token):
            return

        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["auth_token"] = token
                cfg["session_started_at"] = time.time()
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                logger.info(f"Updated auth_token saved to '{self.config_path}'.")
            except Exception as e:
                logger.warning(f"Failed to update token in '{self.config_path}': {e}")
