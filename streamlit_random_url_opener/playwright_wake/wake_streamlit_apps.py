#!/usr/bin/env python3
"""Open Streamlit apps in Chromium and click the sleep wake-up button if needed."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SLEEP_TEXT = "This app has gone to sleep due to inactivity"
WAKE_PROGRESS_TEXT = "Your app is waking up"
WAKE_BUTTON_RE = re.compile(r"Yes,\s*get this app back up!?", re.IGNORECASE)
DEFAULT_URLS_FILE = Path(__file__).with_name("urls.json")


@dataclass(frozen=True)
class AppUrl:
    name: str
    url: str


@dataclass(frozen=True)
class WakeResult:
    name: str
    url: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in {"awake", "woke", "opened"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls-file", type=Path, default=DEFAULT_URLS_FILE)
    parser.add_argument("--headful", action="store_true", help="Run Chromium with a visible window.")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs without opening pages.")
    parser.add_argument("--randomize-order", action="store_true", help="Visit URLs in random order.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N URLs after shuffling.")
    parser.add_argument(
        "--max-delay-seconds",
        type=int,
        default=0,
        help="Sleep 0..N seconds before each URL to avoid opening everything at once.",
    )
    parser.add_argument("--navigation-timeout-ms", type=int, default=90_000)
    parser.add_argument(
        "--sleep-detection-timeout-ms",
        type=int,
        default=8_000,
        help="Wait this long after navigation for Streamlit's sleep screen to render.",
    )
    parser.add_argument(
        "--post-click-wait-ms",
        type=int,
        default=15_000,
        help="Keep the browser open this long after clicking the wake button.",
    )
    parser.add_argument("--wake-timeout-ms", type=int, default=180_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    apps = load_urls(args.urls_file)

    if args.randomize_order:
        random.shuffle(apps)
    if args.limit:
        if args.limit < 1:
            raise SystemExit("--limit must be 1 or greater when provided.")
        apps = apps[: args.limit]

    if args.dry_run:
        print(f"Dry run OK: {len(apps)} URLs loaded from {args.urls_file}")
        for index, app in enumerate(apps, start=1):
            print(f"{index:02d}. {app.name} | {app.url}")
        return 0

    validate_delay(args.max_delay_seconds)
    return run_browser(args, apps)


def load_urls(path: Path) -> list[AppUrl]:
    try:
        raw_items = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"URLs file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"URLs file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(raw_items, list) or not raw_items:
        raise SystemExit("URLs file must contain a non-empty JSON array.")

    apps: list[AppUrl] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"URL entry #{index} must be an object.")
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            raise SystemExit(f"URL entry #{index} needs both name and url.")
        if not url.startswith(("http://", "https://")):
            raise SystemExit(f"URL entry #{index} must start with http:// or https://")
        if url in seen_urls:
            raise SystemExit(f"Duplicate URL found: {url}")
        seen_urls.add(url)
        apps.append(AppUrl(name=name, url=url))

    return apps


def validate_delay(max_delay_seconds: int) -> None:
    if max_delay_seconds < 0:
        raise SystemExit("--max-delay-seconds must be 0 or greater.")
    if max_delay_seconds > 300:
        raise SystemExit("--max-delay-seconds is capped at 300 to keep the job bounded.")


def run_browser(args: argparse.Namespace, apps: list[AppUrl]) -> int:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Run: pip install -r playwright_wake/requirements.txt"
        ) from exc

    results: list[WakeResult] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headful)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                viewport={"width": 1365, "height": 900},
            )
            context.set_default_timeout(15_000)

            for index, app in enumerate(apps, start=1):
                if args.max_delay_seconds:
                    delay = random.randint(0, args.max_delay_seconds)
                    print(f"[{index}/{len(apps)}] waiting {delay}s before {app.name}")
                    time.sleep(delay)
                else:
                    print(f"[{index}/{len(apps)}] opening {app.name}")

                try:
                    result = wake_app(context, app, args, PlaywrightTimeoutError)
                except Exception as exc:  # noqa: BLE001 - report and continue with remaining apps.
                    result = WakeResult(app.name, app.url, "error", trim(str(exc)))

                results.append(result)
                print(f"{result.status.upper()}: {result.name} | {result.detail}")
        finally:
            browser.close()

    ok_count = sum(result.ok for result in results)
    print(f"Summary: {ok_count}/{len(results)} apps opened or woke successfully.")

    failed = [result for result in results if not result.ok]
    if failed:
        print("Failures:")
        for result in failed:
            print(f"- {result.status}: {result.name} | {result.url} | {result.detail}")
        return 1

    return 0


def wake_app(context: Any, app: AppUrl, args: argparse.Namespace, timeout_error: type[Exception]) -> WakeResult:
    page = context.new_page()
    try:
        response = page.goto(
            app.url,
            wait_until="domcontentloaded",
            timeout=args.navigation_timeout_ms,
        )
        status_code = response.status if response else "no-response"

        if not wait_for_sleep_screen(page, args.sleep_detection_timeout_ms, timeout_error):
            wait_for_streamlit_page(page, timeout_error)
            if wait_for_sleep_screen(page, 1_000, timeout_error):
                return click_wake_button(page, app, status_code, args, timeout_error)
            return WakeResult(app.name, app.url, "awake", f"HTTP {status_code}")

        return click_wake_button(page, app, status_code, args, timeout_error)
    except timeout_error as exc:
        return WakeResult(app.name, app.url, "timeout", trim(str(exc)))
    finally:
        page.close()


def click_wake_button(
    page: Any,
    app: AppUrl,
    status_code: int | str,
    args: argparse.Namespace,
    timeout_error: type[Exception],
) -> WakeResult:
    button = page.get_by_role("button", name=WAKE_BUTTON_RE).first
    try:
        button.click(timeout=10_000)
    except timeout_error:
        fallback = page.get_by_text(WAKE_BUTTON_RE).first
        fallback.click(timeout=10_000)

    wait_for_wake_request(page, args.wake_timeout_ms, timeout_error)
    if args.post_click_wait_ms:
        page.wait_for_timeout(args.post_click_wait_ms)
    return WakeResult(app.name, app.url, "woke", f"clicked wake button after HTTP {status_code}")


def wait_for_sleep_screen(page: Any, timeout_ms: int, timeout_error: type[Exception]) -> bool:
    try:
        page.wait_for_function(
            f"""
            () => {{
              const text = document.body ? document.body.innerText : "";
              const hasSleepText = text.includes({json.dumps(SLEEP_TEXT)});
              const buttons = Array.from(document.querySelectorAll("button"));
              const hasWakeButton = buttons.some((button) => /Yes,\\s*get this app back up!?/i.test(button.innerText));
              return hasSleepText || hasWakeButton;
            }}
            """,
            timeout=timeout_ms,
        )
    except timeout_error:
        return False
    return True


def page_contains_sleep_message(page: Any) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except Exception:  # noqa: BLE001 - missing/slow body means no reliable sleep marker.
        return False
    return SLEEP_TEXT in body_text


def wait_for_wake_request(page: Any, timeout_ms: int, timeout_error: type[Exception]) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except timeout_error:
        pass

    try:
        page.wait_for_function(
            f"""
            () => {{
              const text = document.body ? document.body.innerText : "";
              return !text.includes({json.dumps(SLEEP_TEXT)})
                || text.includes({json.dumps(WAKE_PROGRESS_TEXT)});
            }}
            """,
            timeout=timeout_ms,
        )
    except timeout_error:
        # Streamlit may still be booting while the page text remains stale. A reload usually
        # confirms whether the wake request was accepted.
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        if page_contains_sleep_message(page):
            raise


def wait_for_streamlit_page(page: Any, timeout_error: type[Exception]) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except timeout_error:
        pass


def trim(text: str, max_length: int = 240) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= max_length else cleaned[:max_length] + "..."


if __name__ == "__main__":
    sys.exit(main())
