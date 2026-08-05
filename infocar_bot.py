#!/usr/bin/env python3
"""
InfoCar Practical Driving Exam Checker & Auto-Book Bot (Friday Version)
=======================================================================
Monitors info-kierowca.pl for available practical exam dates, notifies via Telegram,
and optionally auto-books the earliest slot matching your criteria.
"""

import os
import sys
import time
import json
import base64
import random
import logging
import argparse
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import requests
except ImportError:
    print("Error: 'requests' package is required. Install it using: pip install requests")
    sys.exit(1)

from infocar_playwright import PlaywrightSessionManager


class LogColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"


class ColoredFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG: LogColor.BLUE + "%(asctime)s [%(levelname)s] %(message)s" + LogColor.RESET,
        logging.INFO: LogColor.GREEN + "%(asctime)s [%(levelname)s] %(message)s" + LogColor.RESET,
        logging.WARNING: LogColor.YELLOW + "%(asctime)s [%(levelname)s] %(message)s" + LogColor.RESET,
        logging.ERROR: LogColor.RED + "%(asctime)s [%(levelname)s] %(message)s" + LogColor.RESET,
        logging.CRITICAL: LogColor.BOLD + LogColor.RED + "%(asctime)s [%(levelname)s] %(message)s" + LogColor.RESET,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, "%(asctime)s [%(levelname)s] %(message)s")
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("InfoCarBot")
    logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler("infocar_bot.log", encoding="utf-8")
    file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger

logger = setup_logger()

WORD_CENTERS: Dict[str, str] = {}
if os.path.exists("word_centers.json"):
    try:
        with open("word_centers.json", "r", encoding="utf-8") as f:
            WORD_CENTERS = json.load(f)
    except Exception:
        pass


# The server refuses to extend a session past this many seconds from login, whatever
# jwt/refresh returns, and publishes no claim for it. Measured at 59-60 min across two
# sessions with different clients and refresh cadences, so re-login is the only cure.
SESSION_CAP_SECS = 3600
CAP_WARN_BEFORE = 300


class InfoCarClient:
    BASE_URL = "https://info-kierowca.pl"
    DOMAIN = "info-kierowca.pl"

    def __init__(self, cookie_str: str):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/reservation",
            "Content-Type": "application/json"
        })
        # Cookies live in the session jar, never in a static Cookie header: the server
        # rotates __Secure-PUDOJT on every jwt/refresh and requests must pick that up.
        for c in PlaywrightSessionManager.parse_cookie_string(cookie_str, self.DOMAIN):
            self.session.cookies.set(c["name"], c["value"], domain=self.DOMAIN, path="/")

    def cookie_string(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)

    def jwt_expires_at(self) -> Optional[datetime]:
        """Reads the JWT expiry from the __Secure-PUDOJTMD metadata cookie."""
        md = self.session.cookies.get("__Secure-PUDOJTMD")
        if not md:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(md + "=" * (-len(md) % 4)))
            return datetime.fromtimestamp(data["expires"])
        except Exception:
            return None

    def save_cookies(self, config_path: str = "config.json"):
        if not os.path.exists(config_path):
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            cookie_str = self.cookie_string()
            if cfg_data.get("auth_token") != cookie_str:
                cfg_data["auth_token"] = cookie_str
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Could not save cookies to '{config_path}': {e}")

    def refresh_jwt(self, config_path: str = "config.json", attempts: int = 3) -> bool:
        """Extends the session. Must run more often than the 900s JWT TTL.

        A False return is NOT proof the session is gone. The JWT already in the jar stays
        valid until its own expiry, and the server refuses to extend any session past
        SESSION_CAP_SECS from login no matter what. Callers must probe a real endpoint
        before deciding the bot is logged out.
        """
        before = self.jwt_expires_at()

        for attempt in range(1, attempts + 1):
            status = None
            try:
                status = self.session.get(f"{self.BASE_URL}/bknd/auth/api/v1/jwt/refresh", timeout=10).status_code
            except Exception as e:
                logger.warning(f"Network error refreshing JWT (attempt {attempt}/{attempts}): {e}")

            if status in (200, 204):
                break
            if status is not None and status < 500:
                # A 401/404 from the gateway is a definitive rejection - retrying cannot help.
                logger.error(f"JWT refresh rejected (HTTP {status}).")
                return False
            if status is not None:
                logger.warning(f"JWT refresh failed (HTTP {status}, attempt {attempt}/{attempts}).")
            if attempt < attempts:
                time.sleep(2 * attempt)
        else:
            # Server error or network failure every time: could be a transient gateway blip
            # or the 1h session cap. The caller tells them apart by probing the API.
            logger.error("JWT refresh did not succeed. The current token may still be usable.")
            return False

        expiry = self.jwt_expires_at()
        if expiry and expiry <= datetime.now():
            logger.error("JWT refresh returned OK but the token was already expired.")
            return False
        if before and expiry and expiry <= before:
            # The server accepted the call without issuing a new token - the session will die at `expiry`.
            logger.warning(f"JWT refresh did not extend the session (still expires {expiry.strftime('%H:%M:%S')}).")

        self.save_cookies(config_path)
        logger.info(f"Session refreshed (JWT valid until {expiry.strftime('%H:%M:%S') if expiry else 'unknown'}).")
        return True

    def check_logged_user(self) -> Optional[Any]:
        """Returns the user dict when logged in, False when the gateway says otherwise,
        and None when the gateway could not be reached at all. Callers deciding whether to
        give up must require False: an unreachable server is not a logged-out session."""
        url = f"{self.BASE_URL}/bknd/Users/api/v1/Users/logged/display"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code == 200:
                return res.json()
            # An expired/invalidated JWT gets 404 from the gateway, not 401.
            logger.error(f"Session is not valid (HTTP {res.status_code}).")
            return False
        except Exception as e:
            logger.error(f"Network error checking logged user: {e}")
            return None

    def fetch_pkk_profile(self) -> Optional[str]:
        url = f"{self.BASE_URL}/bknd/status/api/v1/pkk/get_profiles_for_reservation"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code == 200:
                profiles = res.json()
                if isinstance(profiles, list) and len(profiles) > 0:
                    pkk = profiles[0].get("pkkNumber")
                    if pkk:
                        logger.info(f"Auto-detected PKK profile number: {pkk}")
                        return pkk
            logger.warning(f"Failed to fetch PKK profile automatically (HTTP {res.status_code}).")
        except Exception as e:
            logger.error(f"Error fetching PKK profile: {e}")
        return None

    def fetch_schedule(self, pkk: str, org_id: int, category_enum: int = 5, start_date: Optional[str] = None) -> Tuple[bool, List[Dict[str, Any]]]:
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-%d")

        url = f"{self.BASE_URL}/bknd/exam/api/v1/Schedules/user/OneCenterExam"
        payload = {
            "startDate": start_date,
            "organizationId": [org_id],
            "category": category_enum,
            "profileNumber": pkk,
            "profileType": "Pkk"
        }

        try:
            res = self.session.post(url, json=payload, timeout=15)
            if res.status_code == 401:
                logger.error("Session token expired while fetching schedule (HTTP 401).")
                return False, []

            if res.status_code != 200:
                logger.error(f"Failed to fetch schedule. HTTP {res.status_code}: {res.text[:200]}")
                return False, []

            data = res.json()
            days = data.get("examCollectionForDay", [])
            practical_slots = []

            for day in days:
                exam_date = day.get("date")
                collections = day.get("examCollections", [])
                for item in collections:
                    exam_type = item.get("examType")
                    practice_id = item.get("practiceId")
                    practice_dt = item.get("practiceDateTime")
                    places = item.get("placePracticeAmount", 0)

                    if exam_type in ("Practical", "TheoreticalAndPractical") or practice_id or places > 0:
                        slot_info = {
                            "date": exam_date,
                            "time": practice_dt.split("T")[1][:5] if practice_dt and "T" in practice_dt else "N/A",
                            "full_datetime": practice_dt or f"{exam_date}T00:00:00",
                            "practice_id": practice_id,
                            "places": places,
                            "org_id": org_id,
                            "org_name": item.get("organizationName") or WORD_CENTERS.get(str(org_id), f"WORD ID {org_id}"),
                            "category": item.get("category") or "B",
                            "amount": item.get("amount")
                        }
                        practical_slots.append(slot_info)

            return True, practical_slots

        except Exception as e:
            logger.error(f"Error requesting schedule: {e}")
            return False, []

    def book_exam(self, pkk: str, org_id: int, exam_date: str, practice_id: str, category: str = "B") -> Tuple[bool, Optional[str]]:
        logger.info(f"Initiating booking for {exam_date} (ID: {practice_id})...")

        create_url = f"{self.BASE_URL}/bknd/exam/api/v1/Reservations/create"
        payload = {
            "profileNumber": pkk,
            "organizationId": org_id,
            "examDate": exam_date,
            "practiceExamId": practice_id,
            "profileType": "Pkk",
            "examType": "Practice",
            "language": "Polish",
            "category": category
        }

        try:
            res = self.session.post(create_url, json=payload, timeout=15)
            if res.status_code != 200:
                err_msg = f"Reservation create failed (HTTP {res.status_code}): {res.text[:200]}"
                logger.error(err_msg)
                return False, err_msg

            res_data = res.json()
            reservation_id = res_data.get("id")
            if not reservation_id:
                err_msg = f"Reservation ID missing in response: {res_data}"
                logger.error(err_msg)
                return False, err_msg

            logger.info(f"Reservation created with ID: {reservation_id}. Confirming...")

            confirm_url = f"{self.BASE_URL}/bknd/exam/api/v1/Reservations/confirm/{reservation_id}"
            headers = {
                "Accept": "text/event-stream",
                "Referer": f"{self.BASE_URL}/reservation?id={reservation_id}"
            }
            try:
                # SSE endpoint: it may hold the connection open. A timeout here is not a failure -
                # the reservation state below decides the outcome.
                c_res = self.session.get(confirm_url, headers=headers, timeout=15, stream=True)
                logger.info(f"Confirmation response status: {c_res.status_code}")
                c_res.close()
            except Exception as e:
                logger.warning(f"Confirm stream ended early ({e}). Checking reservation state anyway...")

            state_url = f"{self.BASE_URL}/bknd/exam/api/v1/Reservations/{reservation_id}"
            state_headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{self.BASE_URL}/reservation?id={reservation_id}"
            }
            # "Created" is the pre-confirmation state; only PlaceReserved/Accepted means the slot is ours.
            status_name = None
            for attempt in range(4):
                s_res = self.session.get(state_url, headers=state_headers, timeout=10)
                if s_res.status_code == 200:
                    status_name = s_res.json().get("status")
                    logger.info(f"Reservation state: {status_name}")
                    if status_name in ("PlaceReserved", "Accepted"):
                        logger.info(f"🎉 Booking successfully confirmed! Reservation ID: {reservation_id}")
                        return True, reservation_id
                time.sleep(3)

            return False, f"Reservation {reservation_id} was not confirmed (last state: {status_name}). Book manually NOW."

        except Exception as e:
            err_msg = f"Exception during booking: {e}"
            logger.error(err_msg)
            return False, err_msg


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.warning("Telegram Bot Token or Chat ID not configured. Notifications disabled.")

    def send_message(self, message: str) -> bool:
        if not self.enabled:
            logger.info(f"[Telegram Notification Skipped]:\n{message}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }

        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                logger.info("Telegram message sent successfully.")
                return True
            else:
                logger.error(f"Failed to send Telegram message (HTTP {res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return False


def parse_org_ids(cli_value: Optional[str], cfg: Dict[str, Any]) -> List[int]:
    if cli_value:
        return [int(v.strip()) for v in cli_value.split(",") if v.strip()]
    # Every config key takes a single ID or a list of them: putting a list under the
    # singular `organization_id` is the obvious thing to try, and it used to crash.
    value = cfg.get("organization_ids") or cfg.get("organization_id") or cfg.get("org_id") or 43
    return [int(v) for v in (value if isinstance(value, list) else [value])]


def load_config(config_path: str) -> Dict[str, Any]:
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                logger.info(f"Loaded config from '{config_path}'")
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read config file '{config_path}': {e}")
    return {}


def list_words():
    print("\n================ Known WORD Centers ================")
    if not WORD_CENTERS:
        print("No word_centers.json found. You can specify any integer organizationId (e.g., 43 for PORD Gdańsk).")
    else:
        for org_id, name in sorted(WORD_CENTERS.items(), key=lambda x: int(x[0])):
            print(f"  ID {org_id:>3} : {name}")
    print("===================================================\n")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="InfoCar Practical Exam Auto-Booker & Telegram Notifier",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("--config", "-cfg", default="config.json", help="Path to JSON configuration file")
    parser.add_argument("--token", "-t", help="InfoCar Authorization Token or Cookie string")
    parser.add_argument("--pkk", "-p", help="PKK Profile Number")
    parser.add_argument("--org-id", "-o", help="WORD Organization ID(s), comma-separated (e.g. 43,42,73)")
    parser.add_argument("--all-words", "-a", action="store_true", help="Check every known WORD center instead of --org-id")
    parser.add_argument("--days", "-d", type=int, help="Maximum number of days ahead to look for exams (e.g. 7)")
    parser.add_argument("--auto-book", "-b", action="store_true", help="Enable auto-booking if a slot is found")
    parser.add_argument("--min-interval", type=int, help="Minimum fetch sleep interval in seconds (default: 240)")
    parser.add_argument("--max-interval", type=int, help="Maximum fetch sleep interval in seconds (default: 360)")
    parser.add_argument("--telegram-token", help="Telegram Bot Token")
    parser.add_argument("--telegram-chat-id", help="Telegram Chat ID")
    parser.add_argument("--check-once", action="store_true", help="Perform a single check and exit")
    parser.add_argument("--test-telegram", action="store_true", help="Send a test Telegram message and exit")
    parser.add_argument("--list-words", action="store_true", help="List all known WORD centers and exit")
    parser.add_argument("--login", action="store_true", help="Open Playwright browser window to log in once and save persistent session")
    parser.add_argument("--headful", action="store_true", help="Run Playwright browser with visible GUI window")

    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.list_words:
        list_words()
        sys.exit(0)

    pw_manager = PlaywrightSessionManager(config_path=args.config, headless=not args.headful)

    if args.login:
        if not pw_manager.is_available():
            logger.critical("Playwright is required for --login. Please run: pip install playwright && playwright install chromium")
            sys.exit(1)
        token = pw_manager.interactive_login()
        if token:
            logger.info("Interactive login completed successfully! Session saved in ./browser_data.")
            sys.exit(0)
        else:
            logger.error("Interactive login failed or was cancelled.")
            sys.exit(1)

    cfg = load_config(args.config)

    auth_token = args.token or cfg.get("auth_token") or cfg.get("token")
    pkk = args.pkk or cfg.get("pkk")
    if args.all_words:
        if not WORD_CENTERS:
            logger.critical("No word_centers.json found; cannot use --all-words.")
            sys.exit(1)
        org_ids = sorted(int(k) for k in WORD_CENTERS.keys())
    else:
        org_ids = parse_org_ids(args.org_id, cfg)
    max_days = args.days if args.days is not None else cfg.get("max_days", 7)
    auto_book = args.auto_book or cfg.get("auto_book", False)
    min_interval = args.min_interval or cfg.get("min_interval", 240)
    max_interval = args.max_interval or cfg.get("max_interval", 360)
    tg_token = args.telegram_token or cfg.get("telegram_bot_token") or cfg.get("telegram_token")
    tg_chat_id = args.telegram_chat_id or cfg.get("telegram_chat_id")

    notifier = TelegramNotifier(tg_token, tg_chat_id)

    if args.test_telegram:
        logger.info("Sending test message to Telegram...")
        success = notifier.send_message("🚗 <b>InfoCar Bot Test</b>\nTelegram notifications are working properly!")
        sys.exit(0 if success else 1)

    client = InfoCarClient(auth_token) if auth_token else None
    user_info = client.check_logged_user() if client else None

    if not user_info and pw_manager.is_available():
        logger.warning("Session check failed. Trying the saved browser session state...")
        saved_token = pw_manager._get_saved_token()
        if saved_token:
            client = InfoCarClient(saved_token)
            user_info = client.check_logged_user()

    if not user_info and pw_manager.is_available():
        logger.info("No active session found. Launching browser window for interactive login...")
        active_token = pw_manager.interactive_login()
        if active_token:
            client = InfoCarClient(active_token)
            user_info = client.check_logged_user()

    if not user_info or not client:
        logger.critical("Failed to authenticate session. Please run 'python infocar_bot.py --login' to log in.")
        notifier.send_message("⚠️ <b>InfoCar Bot Error</b>\nFailed to authenticate. Run <code>python infocar_bot.py --login</code> to log in.")
        sys.exit(1)

    display_name = user_info.get("displayName", "User")
    logger.info(f"Authenticated successfully as: {display_name}")
    client.save_cookies(args.config)

    if not pkk:
        logger.info("PKK number not provided. Fetching from user profile...")
        pkk = client.fetch_pkk_profile()
        if not pkk:
            logger.critical("Unable to retrieve PKK profile number automatically. Please specify --pkk.")
            sys.exit(1)

    word_names = ", ".join(WORD_CENTERS.get(str(oid), f"WORD ID {oid}") for oid in org_ids)
    logger.info("==================================================")
    logger.info("InfoCar Practical Exam Checker Started")
    logger.info(f"  User:             {display_name}")
    logger.info(f"  PKK Profile:      {pkk}")
    logger.info(f"  Target WORD(s):   {word_names} (IDs: {', '.join(map(str, org_ids))})")
    logger.info(f"  Search Window:    Next {max_days} days (<= {(date.today() + timedelta(days=max_days)).strftime('%Y-%m-%d')})")
    logger.info(f"  Auto-Book Mode:   {'ENABLED ⚡' if auto_book else 'DISABLED 🔔 (Notify only)'}")
    logger.info(f"  Interval Jitter:  {min_interval}s to {max_interval}s")
    logger.info("==================================================")

    notifier.send_message(
        f"🚀 <b>InfoCar Exam Bot Started</b>\n\n"
        f"👤 <b>User:</b> {display_name}\n"
        f"🏢 <b>Target Center(s):</b> {word_names}\n"
        f"📅 <b>Window:</b> Within next {max_days} days\n"
        f"⚡ <b>Auto-Book:</b> {'ENABLED' if auto_book else 'DISABLED'}\n"
        f"⏱ <b>Interval:</b> {min_interval // 60}-{max_interval // 60} minutes"
    )

    consecutive_errors = 0
    booked_successfully = False
    cap_warned = False
    session_started_at = load_config(args.config).get("session_started_at", time.time())

    while not booked_successfully:
        # A failed refresh is not a logout: the token in the jar is good until its own expiry.
        # Only the gateway's own verdict ends the run - `is False`, never a falsy None, or an
        # outage that outlasts the refresh retries would look identical to being logged out.
        if not client.refresh_jwt(config_path=args.config):
            if client.check_logged_user() is False:
                logger.critical("Session is logged out. Stopping bot execution.")
                notifier.send_message(
                    "🛑 <b>InfoCar Session Expired</b>\n"
                    "Run <code>python infocar_bot.py --login</code> to log in again and restart the bot."
                )
                sys.exit(1)
            expiry = client.jwt_expires_at()
            logger.warning(
                "Refresh failed but the session still answers - carrying on with the current token"
                + (f", which expires {expiry.strftime('%H:%M:%S')}." if expiry else ".")
            )

        session_age = time.time() - session_started_at
        if not cap_warned and session_age > SESSION_CAP_SECS - CAP_WARN_BEFORE:
            cap_warned = True
            logger.warning(f"Session is {session_age / 60:.0f} min old; the {SESSION_CAP_SECS // 60} min server cap is close.")
            notifier.send_message(
                f"⏳ <b>InfoCar Re-Login Needed Soon</b>\n\n"
                f"This session is {session_age / 60:.0f} minutes old and the server caps sessions at "
                f"{SESSION_CAP_SECS // 60} minutes.\n"
                f"Run <code>python infocar_bot.py --login</code> and restart to avoid a monitoring gap."
            )

        today = date.today()
        cutoff_date = today + timedelta(days=max_days)

        logger.info(f"Checking for practical exam slots between {today.strftime('%Y-%m-%d')} and {cutoff_date.strftime('%Y-%m-%d')}...")

        all_matching_slots = []
        fetch_success = True

        for org_idx, org_id in enumerate(org_ids):
            current_chunk_start = today

            while current_chunk_start <= cutoff_date:
                chunk_start_str = current_chunk_start.strftime("%Y-%m-%d")
                logger.info(f"  Fetching schedule chunk for org {org_id} starting from {chunk_start_str}...")

                success, slots = client.fetch_schedule(pkk, org_id, category_enum=5, start_date=chunk_start_str)

                if not success:
                    fetch_success = False
                    consecutive_errors += 1
                    logger.warning(f"Fetch failed for org={org_id}, start_date={chunk_start_str} (Error count: {consecutive_errors}).")
                    break
                else:
                    consecutive_errors = 0
                    for s in slots:
                        try:
                            slot_date = datetime.strptime(s["date"], "%Y-%m-%d").date()
                            if today <= slot_date <= cutoff_date:
                                if not any(existing["org_id"] == s["org_id"] and existing["practice_id"] == s["practice_id"] and existing["date"] == s["date"] for existing in all_matching_slots):
                                    all_matching_slots.append(s)
                        except Exception:
                            pass

                    current_chunk_start += timedelta(days=20)

                    if current_chunk_start < cutoff_date:
                        sub_sleep = random.uniform(1, 5)
                        logger.info(f"  Sleeping {sub_sleep:.1f}s before fetching next date chunk...")
                        time.sleep(sub_sleep)

            if not fetch_success:
                break

            if org_idx < len(org_ids) - 1:
                org_sleep = random.uniform(1, 5)
                logger.info(f"  Sleeping {org_sleep:.1f}s before checking next center...")
                time.sleep(org_sleep)

        if fetch_success:
            if all_matching_slots:
                all_matching_slots.sort(key=lambda x: x["full_datetime"])
                earliest = all_matching_slots[0]

                logger.info(f"🎯 FOUND {len(all_matching_slots)} MATCHING SLOT(S) ACROSS SEARCH WINDOW!")
                for idx, slot in enumerate(all_matching_slots, 1):
                    logger.info(f"   [{idx}] {slot['org_name']}: {slot['date']} at {slot['time']} (ID: {slot['practice_id']})")

                msg_lines = [
                    "🎯 <b>PRACTICAL EXAM SLOT FOUND!</b>\n",
                    f"🏢 <b>Center:</b> {earliest['org_name']}",
                    f"📅 <b>Date:</b> <b>{earliest['date']}</b>",
                    f"⏰ <b>Time:</b> <b>{earliest['time']}</b>",
                    f"🆔 <b>Exam ID:</b> <code>{earliest['practice_id']}</code>\n"
                ]

                if auto_book:
                    msg_lines.append("⚡ <b>Attempting automatic booking...</b>")
                    notifier.send_message("\n".join(msg_lines))

                    book_ok, result_info = client.book_exam(
                        pkk=pkk,
                        org_id=earliest["org_id"],
                        exam_date=earliest["date"],
                        practice_id=earliest["practice_id"],
                        category="B"
                    )

                    if book_ok:
                        booked_successfully = True
                        success_msg = (
                            f"🎉 <b>EXAM BOOKED SUCCESSFULLY!</b> 🎉\n\n"
                            f"📅 <b>Date:</b> {earliest['date']}\n"
                            f"⏰ <b>Time:</b> {earliest['time']}\n"
                            f"🏢 <b>Center:</b> {earliest['org_name']}\n"
                            f"🆔 <b>Reservation ID:</b> <code>{result_info}</code>"
                        )
                        logger.info("Booking succeeded! Exiting script.")
                        notifier.send_message(success_msg)
                        break
                    else:
                        fail_msg = (
                            f"❌ <b>AUTO-BOOKING FAILED!</b>\n\n"
                            f"Error: {result_info}\n"
                            f"Please try booking manually immediately via InfoCar website!"
                        )
                        logger.error(fail_msg)
                        notifier.send_message(fail_msg)
                else:
                    msg_lines.append("⚠️ <i>Auto-booking disabled. Book manually on InfoCar!</i>")
                    notifier.send_message("\n".join(msg_lines))
            else:
                logger.info(f"No practical exam slots found within next {max_days} days.")

        if args.check_once:
            logger.info("Single check complete (--check-once). Exiting.")
            break

        # Never sleep past the 900s JWT TTL - the refresh at the top of the loop is the only keep-alive.
        sleep_secs = min(random.uniform(min_interval, max_interval), 600)
        next_run_time = datetime.now() + timedelta(seconds=sleep_secs)
        logger.info(f"Sleeping for {sleep_secs:.1f}s ({sleep_secs / 60:.1f} mins). Next check at {next_run_time.strftime('%H:%M:%S')}...\n")

        try:
            time.sleep(sleep_secs)
        except KeyboardInterrupt:
            logger.info("\nBot stopped by user (Ctrl+C). Exiting cleanly.")
            sys.exit(0)


if __name__ == "__main__":
    main()
