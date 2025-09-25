from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
import random
import subprocess
from typing import List, Sequence
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_fixed


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor SJTU sports reservation portal for available slots",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single availability check and exit",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Force browser into headful mode regardless of HEADLESS setting",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip sending notifications (useful for dry runs)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override log level configured via environment",
    )
    return parser.parse_args(argv)


@dataclass(slots=True)
class AppConfig:
    login_url: str
    reserve_url: str
    username: str
    password: str
    sel_user_input: str
    sel_pass_input: str
    sel_submit_btn: str
    sel_login_trigger: str | None
    sel_captcha_input: str | None
    sel_captcha_img: str | None
    slot_item_sel: str
    available_keyword: str
    today_container_sel: str | None
    venue_keyword: str | None
    venue_sub_keyword: str | None
    date_keyword: str | None
    check_every_min: int
    random_jitter_sec: int
    headless: bool
    user_data_dir: Path
    enable_email: bool
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_pass: str | None
    mail_from_name: str | None
    mail_from_addr: str | None
    mail_to_addrs: List[str]
    enable_tg: bool
    tg_bot_token: str | None
    tg_chat_id: str | None
    enable_desktop: bool
    db_path: Path
    log_level: str
    tesseract_cmd: str | None

    @classmethod
    def load(cls) -> "AppConfig":
        load_dotenv()

        mail_to = os.getenv("MAIL_TO_ADDRS", "")
        mail_list = [addr.strip() for addr in mail_to.split(",") if addr.strip()]

        return cls(
            login_url=os.environ["LOGIN_URL"],
            reserve_url=os.environ.get("RESERVE_URL", os.environ["LOGIN_URL"]),
            username=os.environ["USERNAME"],
            password=os.environ["PASSWORD"],
            sel_user_input=os.environ.get("SEL_USER_INPUT", "#username"),
            sel_pass_input=os.environ.get("SEL_PASS_INPUT", "#password"),
            sel_submit_btn=os.environ.get("SEL_SUBMIT_BTN", "button[type=submit]"),
            sel_login_trigger=os.environ.get("SEL_LOGIN_TRIGGER"),
            sel_captcha_input=os.environ.get("SEL_CAPTCHA_INPUT"),
            sel_captcha_img=os.environ.get("SEL_CAPTCHA_IMG"),
            slot_item_sel=os.environ.get("SLOT_ITEM_SEL", ".slot-card"),
            available_keyword=os.environ.get("AVAILABLE_KEYWORD", "可预约"),
            today_container_sel=os.environ.get("TODAY_CONTAINER_SEL"),
            venue_keyword=os.environ.get("VENUE_KEYWORD"),
            venue_sub_keyword=os.environ.get("VENUE_SUB_KEYWORD"),
            date_keyword=os.environ.get("DATE_KEYWORD"),
            check_every_min=_to_int(os.getenv("CHECK_EVERY_MIN"), 10),
            random_jitter_sec=_to_int(os.getenv("RANDOM_JITTER_SEC"), 30),
            headless=_to_bool(os.getenv("HEADLESS"), True),
            user_data_dir=Path(os.getenv("USER_DATA_DIR", "./user_data")).expanduser(),
            enable_email=_to_bool(os.getenv("ENABLE_EMAIL"), False),
            smtp_host=os.getenv("SMTP_HOST"),
            smtp_port=_to_int(os.getenv("SMTP_PORT"), 587),
            smtp_user=os.getenv("SMTP_USER"),
            smtp_pass=os.getenv("SMTP_PASS"),
            mail_from_name=os.getenv("MAIL_FROM_NAME"),
            mail_from_addr=os.getenv("MAIL_FROM_ADDR"),
            mail_to_addrs=mail_list,
            enable_tg=_to_bool(os.getenv("ENABLE_TG"), False),
            tg_bot_token=os.getenv("TG_BOT_TOKEN"),
            tg_chat_id=os.getenv("TG_CHAT_ID"),
            enable_desktop=_to_bool(os.getenv("ENABLE_DESKTOP"), False),
            db_path=Path(os.getenv("DB_PATH", "./state.db")).expanduser(),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            tesseract_cmd=os.getenv("TESSERACT_CMD"),
        )


@dataclass(slots=True)
class Slot:
    slot_id: str
    display_text: str


class SlotStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notified_slots (
                slot_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self._conn.commit()

    def is_notified(self, slot_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM notified_slots WHERE slot_id = ?", (slot_id,)
        )
        row = cur.fetchone()
        return row is not None

    def mark_notified_many(self, slot_ids: Sequence[str]) -> None:
        if not slot_ids:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO notified_slots(slot_id) VALUES (?)",
            ((slot_id,) for slot_id in slot_ids),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class BaseNotifier:
    name = "base"

    async def send(self, title: str, message: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class EmailNotifier(BaseNotifier):
    name = "email"

    def __init__(self, config: AppConfig) -> None:
        self._host = config.smtp_host
        self._port = config.smtp_port
        self._user = config.smtp_user
        self._password = config.smtp_pass
        self._from_name = config.mail_from_name or "AutoCheck"
        self._from_addr = config.mail_from_addr or (config.smtp_user or "")
        self._recipients = config.mail_to_addrs

    async def send(self, title: str, message: str) -> None:
        if not self._host or not self._recipients:
            logging.warning("Email notifier not configured properly");
            return

        await asyncio.to_thread(self._send_sync, title, message)

    def _send_sync(self, title: str, message: str) -> None:
        email = EmailMessage()
        email["Subject"] = title
        email["From"] = f"{self._from_name} <{self._from_addr}>"
        email["To"] = ", ".join(self._recipients)
        email.set_content(message)

        context = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
            try:
                smtp.starttls(context=context)
            except smtplib.SMTPException:
                logging.info("SMTP server did not accept STARTTLS; continuing without it")
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(email)


class TelegramNotifier(BaseNotifier):
    name = "telegram"

    def __init__(self, config: AppConfig) -> None:
        self._bot_token = config.tg_bot_token
        self._chat_id = config.tg_chat_id

    async def send(self, title: str, message: str) -> None:
        if not self._bot_token or not self._chat_id:
            logging.warning("Telegram notifier not configured properly");
            return

        payload = {
            "chat_id": self._chat_id,
            "text": f"{title}\n{message}",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        await asyncio.to_thread(self._post_message, payload)

    def _post_message(self, payload: dict[str, str]) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        data = urlparse.urlencode(payload).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlrequest.urlopen(req, timeout=15) as resp:
                resp.read()
        except urlerror.URLError as exc:
            logging.error("Failed to send Telegram message: %s", exc)


class DesktopNotifier(BaseNotifier):
    name = "desktop"

    async def send(self, title: str, message: str) -> None:
        if platform.system() == "Darwin":
            await asyncio.to_thread(_notify_macos, title, message)
            return

        try:
            from plyer import notification
        except ImportError:  # pragma: no cover - runtime safeguard
            logging.warning("plyer not available; desktop notifications disabled")
            return

        try:
            await asyncio.to_thread(
                notification.notify,
                title=title,
                message=message,
                timeout=10,
            )
        except NotImplementedError:
            logging.error(
                "Desktop notifications not supported on this platform without additional dependencies"
            )


def _notify_macos(title: str, message: str) -> None:
    script = (
        "display notification "
        + json.dumps(message, ensure_ascii=False)
        + " with title "
        + json.dumps(title, ensure_ascii=False)
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logging.error("Failed to send macOS notification: %s", exc)


def build_notifiers(config: AppConfig) -> List[BaseNotifier]:
    notifiers: List[BaseNotifier] = []
    if config.enable_email:
        notifiers.append(EmailNotifier(config))
    if config.enable_tg:
        notifiers.append(TelegramNotifier(config))
    if config.enable_desktop:
        notifiers.append(DesktopNotifier())
    return notifiers


async def _open_login_modal(page: Page, config: AppConfig) -> None:
    selector = config.sel_login_trigger or "#logoin .el-button"
    try:
        buttons = page.locator(selector)
    except Exception:
        logging.debug("Login trigger selector %s is invalid", selector)
        return

    try:
        count = await buttons.count()
    except Exception:
        logging.debug("Unable to evaluate login trigger selector %s", selector)
        return

    if count:
        logging.debug("Clicking login trigger (%s)", selector)
        try:
            await buttons.first.click()
            await page.wait_for_timeout(400)
        except Exception:
            logging.exception("Failed to click login trigger")


async def _maybe_click_campus_login(page: Page) -> None:
    candidates = [
        page.get_by_text("校内人员登录", exact=False),
        page.get_by_text("校内登录", exact=False),
        page.get_by_role("button", name="校内人员登录"),
        page.get_by_role("button", name="校内登录"),
        page.get_by_role("link", name="校内登录"),
    ]
    for locator in candidates:
        try:
            first_locator = locator.first
            if await locator.count() and await first_locator.is_visible():
                logging.debug("Clicking campus login option")
                await first_locator.click()
                try:
                    await page.wait_for_url(
                        lambda url: "jaccount" in url,
                        timeout=7000,
                    )
                except PlaywrightTimeoutError:
                    logging.debug("Campus login click did not navigate to jaccount promptly")
                return
        except PlaywrightTimeoutError:
            continue


async def _query_in_frames(page: Page, selector: str | None):
    if not selector:
        return None, None
    element = await page.query_selector(selector)
    if element:
        return element, page
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            element = await frame.query_selector(selector)
        except PlaywrightTimeoutError:
            element = None
        if element:
            return element, frame
    return None, None


async def _ensure_venue_context(page: Page, config: AppConfig) -> None:
    try:
        await page.wait_for_selector(".day-panel", timeout=3000)
        return
    except PlaywrightTimeoutError:
        pass

    logging.debug("Attempting to enter venue view")

    if config.venue_keyword:
        venue_selectors = [
            page.locator(
                ".el-card.iscard", has_text=config.venue_keyword
            ),
            page.locator(
                ".venue-card", has_text=config.venue_keyword
            ),
        ]
        for locator in venue_selectors:
            if await locator.count():
                logging.debug(
                    "Clicking venue card matching keyword '%s'", config.venue_keyword
                )
                await locator.first.click()
                await page.wait_for_timeout(800)
                break
        else:
            logging.warning("Primary venue keyword '%s' not found", config.venue_keyword)

    if config.venue_sub_keyword:
        try:
            await page.wait_for_selector(".el-tabs__item, .el-radio-button__inner", timeout=4000)
        except PlaywrightTimeoutError:
            logging.debug("Venue sub tabs not ready yet")
        sub_locators = [
            page.locator(".el-tabs__item", has_text=config.venue_sub_keyword),
            page.locator(
                ".el-radio-button__inner", has_text=config.venue_sub_keyword
            ),
            page.locator(
                ".el-card.iscard", has_text=config.venue_sub_keyword
            ),
        ]
        for locator in sub_locators:
            if await locator.count():
                logging.debug(
                    "Clicking sub venue option '%s'", config.venue_sub_keyword
                )
                await locator.first.click()
                await page.wait_for_timeout(600)
                break
        else:
            logging.warning(
                "Sub venue keyword '%s' not located after selecting primary venue",
                config.venue_sub_keyword,
            )

    if config.date_keyword:
        try:
            await page.wait_for_selector(".el-tabs__item", timeout=4000)
        except PlaywrightTimeoutError:
            logging.debug("Date tabs not ready yet")
        date_locators = [
            page.locator(".el-tabs__item", has_text=config.date_keyword),
            page.locator(
                ".el-radio-button__inner", has_text=config.date_keyword
            ),
        ]
        for locator in date_locators:
            if await locator.count():
                logging.debug("Clicking date tab '%s'", config.date_keyword)
                await locator.first.click()
                await page.wait_for_timeout(600)
                break
        else:
            logging.warning(
                "Date keyword '%s' not found among available tabs",
                config.date_keyword,
            )

    try:
        await page.wait_for_selector(".day-panel", timeout=3000)
        return
    except PlaywrightTimeoutError:
        logging.debug("Day panel still not visible; falling back to generic triggers")

    possible_triggers = [
        "进入预约",
        "前往预约",
        "立即预约",
        "选择场馆",
    ]
    for text in possible_triggers:
        locator = page.get_by_text(text, exact=False)
        if await locator.count():
            await locator.first.click()
            try:
                await page.wait_for_selector(".day-panel", timeout=5000)
            except PlaywrightTimeoutError:
                continue
            return

    venue_cards = page.locator(".venue-card, .gym-item, .card-wrapper, .el-card.iscard")
    if await venue_cards.count():
        await venue_cards.first.click()
        try:
            await page.wait_for_selector(".day-panel", timeout=5000)
        except PlaywrightTimeoutError:
            logging.debug("Venue selection fallback did not reveal day panel")


def _recognize_captcha(image_bytes: bytes) -> str:
    with Image.open(BytesIO(image_bytes)) as img:
        base = img.convert("L")
        if base.width and base.height:
            resize_factor = 2 if base.width < 150 else 1
            if resize_factor != 1:
                base = base.resize(
                    (base.width * resize_factor, base.height * resize_factor),
                    Image.LANCZOS,
                )
        base = ImageOps.autocontrast(base)

        variants = [base]
        variants.append(base.filter(ImageFilter.MedianFilter()))
        for threshold in (90, 110, 130, 150):
            variants.append(base.point(lambda x, t=threshold: 255 if x > t else 0))

        config = (
            "--psm 7 --oem 3 -c tessedit_char_whitelist="
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        )

        for variant in variants:
            text = pytesseract.image_to_string(variant, lang="eng", config=config)
            cleaned = re.sub(r"[^0-9A-Za-z]", "", text)
            if len(cleaned) >= 4:
                return cleaned[:4]

        fallback = pytesseract.image_to_string(base, lang="eng")
        return re.sub(r"\s+", "", fallback)


async def _maybe_fill_captcha(page: Page, config: AppConfig) -> None:
    if not config.sel_captcha_input:
        return

    captcha_input, input_owner = await _query_in_frames(page, config.sel_captcha_input)
    if not captcha_input:
        return

    if not config.sel_captcha_img:
        logging.warning("Captcha input selector provided but SEL_CAPTCHA_IMG missing")
        return

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        captcha_img, _ = await _query_in_frames(page, config.sel_captcha_img)
        if not captcha_img:
            logging.warning("Captcha image selector %s not found", config.sel_captcha_img)
            return

        try:
            code = _recognize_captcha(await captcha_img.screenshot())
        except Exception:
            logging.exception("Failed to process captcha image")
            code = ""

        if code:
            await captcha_input.fill(code)
            logging.debug("Filled captcha using OCR attempt %d", attempt)
            return

        logging.debug("Captcha OCR attempt %d returned empty result; refreshing", attempt)
        try:
            await captcha_img.click(force=True)
        except Exception:
            logging.exception("Failed to refresh captcha")
            return
        await (input_owner or page).wait_for_timeout(600)

    logging.warning("Unable to recognize captcha after %d attempts", max_attempts)


async def ensure_login(page: Page, config: AppConfig) -> None:
    await page.goto(config.reserve_url, wait_until="domcontentloaded")

    target_host = urlparse.urlparse(config.reserve_url).netloc or urlparse.urlparse(config.login_url).netloc

    await _open_login_modal(page, config)
    await _maybe_click_campus_login(page)

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        if not config.sel_user_input or not config.sel_pass_input or not config.sel_submit_btn:
            raise RuntimeError("Login selectors are not fully configured")

        user_input, user_frame = await _query_in_frames(page, config.sel_user_input)
        pass_input, _ = await _query_in_frames(page, config.sel_pass_input)

        if not user_input or not pass_input:
            if target_host and target_host in page.url and "jaccount" not in page.url:
                logging.info("Session already authenticated; skipping login")
                return
            logging.error("Login form not located (attempt %d)", attempt)
            break

        logging.debug("Filling login form (attempt %d)", attempt)
        await user_input.fill(config.username)
        await pass_input.fill(config.password)
        await _maybe_fill_captcha(page, config)

        submit_btn, frame = await _query_in_frames(page, config.sel_submit_btn)
        if not submit_btn:
            logging.error("Submit button not found (attempt %d)", attempt)
            break

        await submit_btn.click()
        await (frame or page).wait_for_load_state("networkidle")

        try:
            await page.wait_for_url(
                lambda current: target_host in current and "jaccount" not in current,
                timeout=30000,
            )
            await page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            logging.warning("Login attempt %d did not reach target site; refreshing captcha", attempt)
        else:
            logging.info("Login succeeded on attempt %d", attempt)
            await page.goto(config.reserve_url, wait_until="networkidle")
            return

        if attempt < max_attempts:
            await page.goto(config.reserve_url, wait_until="domcontentloaded")
            await _open_login_modal(page, config)
            await _maybe_click_campus_login(page)

    raise RuntimeError("Unable to authenticate with JAccount after multiple attempts")


async def extract_today_slots(page: Page, config: AppConfig) -> List[Slot]:
    await page.goto(config.reserve_url, wait_until="networkidle")

    await _ensure_venue_context(page, config)

    try:
        raw_slots = await page.evaluate(
            """
(() => {
  const trim = (str) => (str || '').replace(/\s+/g, ' ').trim();
  const columns = Array.from(document.querySelectorAll('.topsiteStyle'), el => trim(el.textContent));
  const rows = Array.from(document.querySelectorAll('.leftUl li'), el => trim(el.textContent));
  if (!columns.length || !rows.length) {
    return [];
  }
  const seats = Array.from(document.querySelectorAll('.tables .seat .inner-seat'));
  const results = [];
  seats.forEach((seat, index) => {
    const className = seat.className || '';
    if (!className.includes('unselected-seat')) {
      return;
    }
    const columnIndex = index % columns.length;
    const rowIndex = Math.floor(index / columns.length);
    const columnName = columns[columnIndex] || `列${columnIndex + 1}`;
    const rowName = rows[rowIndex] || `行${rowIndex + 1}`;
    const dataId = seat.getAttribute('data-id');
    const labelParts = [rowName, columnName].filter(Boolean);
    const label = labelParts.join(' ');
    const slotId = (dataId && dataId.trim()) || `${rowName}#${columnName}`;
    results.push({ id: slotId, text: label });
  });
  return results;
})()
            """
        )
    except Exception:
        logging.exception("Failed to evaluate seat availability script")
        return []

    slots: List[Slot] = []
    for item in raw_slots or []:
        slot_id = (item.get("id") or "").strip()
        text = (item.get("text") or "").strip()
        if not slot_id or not text:
            continue
        slots.append(Slot(slot_id=slot_id, display_text=text))
    if not slots:
        logging.debug("No unselected seats detected after evaluation")
    return slots


class PlaywrightSession:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._playwright = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Page:
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._config.user_data_dir),
            headless=self._config.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        self._playwright = playwright
        self._context = context
        return page

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def ensure_storage(config: AppConfig) -> None:
    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)


async def notify_all(notifiers: Sequence[BaseNotifier], title: str, message: str) -> None:
    for notifier in notifiers:
        try:
            await notifier.send(title, message)
        except Exception:  # pragma: no cover - defensive logging
            logging.exception("Failed to send notification via %s", notifier.name)


async def _perform_check(
    config: AppConfig, store: SlotStore, notifiers: Sequence[BaseNotifier]
) -> List[Slot]:
    async with PlaywrightSession(config) as page:
        await ensure_login(page, config)
        slots = await extract_today_slots(page, config)

    new_slots = [slot for slot in slots if not store.is_notified(slot.slot_id)]
    if not new_slots:
        logging.info("No new slots found")
        return []

    store.mark_notified_many([slot.slot_id for slot in new_slots])

    title = "新的可预约体育场地"
    body_lines = [slot.display_text for slot in new_slots]
    body = "\n".join(body_lines)
    if notifiers:
        await notify_all(notifiers, title, body)
    else:
        logging.warning("No notifier configured; new slots will not trigger alerts")
    logging.info("Discovered %d new slots", len(new_slots))
    return new_slots


_retry_logger = logging.getLogger("autocheck.retry")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(5),
    reraise=True,
    before_sleep=before_sleep_log(_retry_logger, logging.WARNING),
)
async def check_once(
    config: AppConfig, store: SlotStore, notifiers: Sequence[BaseNotifier]
) -> List[Slot]:
    return await _perform_check(config, store, notifiers)


async def run_scheduler(
    config: AppConfig, store: SlotStore, notifiers: Sequence[BaseNotifier]
) -> None:
    interval_sec = max(config.check_every_min, 1) * 60
    jitter_max = max(config.random_jitter_sec, 0)
    logging.info(
        "Starting auto check loop with interval=%ss jitter<=%ss",
        interval_sec,
        jitter_max,
    )

    while True:
        try:
            await check_once(config, store, notifiers)
        except Exception:  # pragma: no cover - defensive logging
            logging.error("Check failed after retries", exc_info=True)

        jitter = random.randint(0, jitter_max) if jitter_max else 0
        sleep_seconds = interval_sec + jitter
        logging.info("Next check in %s seconds", sleep_seconds)
        await asyncio.sleep(sleep_seconds)


async def main_async(args: argparse.Namespace) -> None:
    config = AppConfig.load()
    if args.log_level:
        config.log_level = args.log_level
    if args.headful:
        config.headless = False

    if config.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd

    ensure_storage(config)
    setup_logging(config.log_level)
    store = SlotStore(config.db_path)

    notifiers = [] if args.no_notify else build_notifiers(config)
    if args.no_notify:
        logging.warning("Notifications disabled via CLI flag; results will log only")

    try:
        if args.once:
            await check_once(config, store, notifiers)
        else:
            await run_scheduler(config, store, notifiers)
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("Received interrupt signal. Exiting.")


if __name__ == "__main__":
    main()
