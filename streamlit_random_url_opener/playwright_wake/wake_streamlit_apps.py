#!/usr/bin/env python3
"""Visit Streamlit Cloud apps sequentially, waking sleepers and checking rendered content."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import re
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SLEEP_TEXT = "This app has gone to sleep due to inactivity"
WAKE_BUTTON_RE = re.compile(r"^Yes,\s*get this app back up!?$", re.IGNORECASE)
DEFAULT_URLS_FILE = Path(__file__).with_name("urls.jsonc")

# HTTP 200, the Cloud shell and "waking up" are not evidence of a ready app.
PAGE_STATE_JS = r"""() => {
  const visible = e => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const text = document.body ? document.body.innerText : '';
  const wake = [...document.querySelectorAll('button')].some(e =>
    visible(e) && /^Yes,\s*get this app back up!?$/i.test(e.innerText.trim()));
  const sleep = text.includes('This app has gone to sleep due to inactivity');
  const errors = [...document.querySelectorAll(
    '[data-testid="stException"], .stException, [data-testid="stConnectionError"]'
  )].filter(visible);
  const disconnected = [...document.querySelectorAll(
    '[role="dialog"], [role="alert"], [data-testid="stModal"], [data-testid="stDialog"], '
    + '[data-testid="stStatusWidget"], .stStatusWidget'
  )].filter(e => visible(e) && /Connection error|Connection lost|Unable to (?:re)?connect|Could not connect|Disconnected/i.test(e.innerText));
  errors.push(...disconnected);
  if (errors.length) return {state:'error', detail:errors.map(e => e.innerText).join(' ').slice(0,400)};
  if (wake || sleep) return {state:'sleep', wake, detail:'Cloud inactivity screen'};
  if (text.includes('Your app is waking up')) return {state:'loading', detail:'Cloud is waking the app'};
  const busy = [...document.querySelectorAll(
    '[data-testid="stStatusWidget"], .stStatusWidget, [data-testid="stSpinner"]'
  )].some(e => visible(e) && (e.matches('[data-testid="stSpinner"]') || /running|connecting/i.test(e.innerText)));
  if (busy) return {state:'loading', detail:'Streamlit is running or connecting'};
  const roots = [...document.querySelectorAll(
    '[data-testid="stMainBlockContainer"], [data-testid="stAppViewBlockContainer"], '
    + '.main .block-container, .stMain .block-container, .stMainBlockContainer'
  )];
  const rendered = roots.some(root => visible(root) && [...root.querySelectorAll(
    '[data-testid="stElementContainer"], .element-container, h1, h2, '
    + '[data-testid="stMarkdownContainer"], input, textarea, select, button, canvas, img'
  )].some(e => visible(e) && (e.innerText.trim() || e.matches('input, textarea, select, canvas, img')
    || e.querySelector('input, textarea, select, canvas, img'))));
  return {state:rendered ? 'ready' : 'loading',
    detail:rendered ? 'Visible Streamlit app content' : 'No rendered Streamlit app content'};
}"""


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
    attempts: int = 0
    wake_clicked: bool = False
    elapsed_seconds: float = 0
    checked_url: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"awake", "woke"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls-file", type=Path, default=DEFAULT_URLS_FILE)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--randomize-order", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only-url", action="append", default=[], help="Check only these registered URLs; repeatable.")
    parser.add_argument("--report-file", type=Path, help="JSON progress/results including unvisited URLs.")
    parser.add_argument("--max-delay-seconds", type=int, default=0)
    parser.add_argument("--navigation-timeout-ms", type=int, default=90_000)
    parser.add_argument("--sleep-detection-timeout-ms", type=int, default=8_000,
                        help="Compatibility option: minimum shell observation, within the total budget.")
    parser.add_argument("--post-click-wait-ms", type=int, default=15_000,
                        help="Minimum ready observation after wake click, within the total budget.")
    parser.add_argument("--wake-timeout-ms", type=int, default=180_000,
                        help="TOTAL per-app budget including navigation, wake and one retry.")
    args = parser.parse_args()
    if not 0 <= args.max_delay_seconds <= 300:
        parser.error("--max-delay-seconds must be between 0 and 300")
    if args.limit < 0:
        parser.error("--limit must not be negative")
    for name in ("navigation_timeout_ms", "wake_timeout_ms"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("sleep_detection_timeout_ms", "post_click_wait_ms"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must not be negative")
    return args


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.streamlit\.app", host)
            or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment):
        raise ValueError("Use HTTPS *.streamlit.app without credentials, fragment or a custom port")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def load_urls(path: Path) -> list[AppUrl]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonc":
            # Only whole-line comments: do not strip // inside quoted URLs.
            text = "\n".join("" if line.lstrip().startswith("//") else line for line in text.splitlines())
        raw_items = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read URLs: {path}: {exc}") from exc
    if not isinstance(raw_items, list) or not raw_items:
        raise SystemExit("URLs file must contain a non-empty JSON array.")
    apps: list[AppUrl] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("url"), str):
            raise SystemExit(f"URL entry #{index} needs string name and url.")
        name = item["name"].strip()
        try:
            url = canonical_url(item["url"].strip())
        except ValueError as exc:
            raise SystemExit(f"Invalid URL entry #{index}: {exc}") from exc
        if not name or url in seen:
            raise SystemExit(f"URL entry #{index} has an empty name or duplicate URL: {url}")
        seen.add(url)
        apps.append(AppUrl(name, url))
    return apps


def select_apps(apps: list[AppUrl], args: argparse.Namespace) -> list[AppUrl]:
    if args.only_url:
        try:
            selected = {canonical_url(url) for url in args.only_url}
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        unknown = selected - {app.url for app in apps}
        if unknown:
            raise SystemExit(f"--only-url must be in the active list: {sorted(unknown)}")
        apps = [app for app in apps if app.url in selected]
    if args.randomize_order:
        random.shuffle(apps)
    return apps[:args.limit] if args.limit else apps


def body_url(url: str) -> str:
    parts = urlsplit(url)
    # Observed Cloud body route, used only as a fallback for the same app.
    if parts.path in ("", "/"):
        return urlunsplit((parts.scheme, parts.netloc, "/~/+/", parts.query, ""))
    return url


def remaining_ms(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


def inspect_page(page: Any) -> tuple[dict[str, Any], Any]:
    states: list[tuple[dict[str, Any], Any]] = []
    for frame in page.frames:
        try:
            state = frame.evaluate(PAGE_STATE_JS)
            if isinstance(state, dict):
                states.append((state, frame))
        except Exception:
            # Frames may detach while Cloud replaces its shell with the app.
            continue
    for wanted in ("error", "sleep", "ready", "loading"):
        for state, frame in states:
            if state.get("state") == wanted:
                return state, frame
    return {"state": "loading", "detail": "No accessible app frame yet"}, page


def observe_app(page: Any, deadline: float, args: argparse.Namespace) -> tuple[str, str, bool]:
    clicked = False
    ready_since: float | None = None
    not_before = time.monotonic()
    last = "No rendered Streamlit app content"
    while remaining_ms(deadline) > 0:
        state, frame = inspect_page(page)
        last = str(state.get("detail", state.get("state", "loading")))
        if state.get("state") == "error":
            return "error", last, clicked
        if state.get("state") == "sleep" and state.get("wake") and not clicked:
            button = frame.get_by_role("button", name=WAKE_BUTTON_RE).first
            button.click(timeout=max(1, min(10_000, remaining_ms(deadline))))
            clicked = True
            not_before = time.monotonic() + args.post_click_wait_ms / 1000
        if state.get("state") == "ready":
            if ready_since is None:
                ready_since = time.monotonic()
            if time.monotonic() - ready_since >= 1 and time.monotonic() >= not_before:
                return "ready", last, clicked
        else:
            ready_since = None
        page.wait_for_timeout(min(500, remaining_ms(deadline)))
    return "timeout", last, clicked


def wake_app(context: Any, app: AppUrl, args: argparse.Namespace, timeout_error: type[Exception]) -> WakeResult:
    started = time.monotonic()
    deadline = started + args.wake_timeout_ms / 1000
    clicked = False
    detail = "No attempt completed"
    final_status = "timeout"
    checked_url = app.url
    attempts = 0
    for attempt, url in enumerate((app.url, body_url(app.url)), 1):
        if remaining_ms(deadline) <= 0:
            break
        attempts = attempt
        checked_url = url
        phase_deadline = deadline
        if attempt == 1:
            phase_seconds = max(8, args.sleep_detection_timeout_ms / 1000,
                                min(60, args.wake_timeout_ms / 2000))
            phase_deadline = min(deadline, time.monotonic() + phase_seconds)
        page = context.new_page()
        try:
            response = None
            try:
                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=max(1, min(args.navigation_timeout_ms, remaining_ms(phase_deadline))))
            except timeout_error:
                # Navigation can time out after useful DOM has already appeared.
                pass
            if response is not None and response.status >= 400:
                final_status, detail = "http_error", f"HTTP {response.status}"
                if response.status < 500:
                    break
                continue
            state, detail, did_click = observe_app(page, phase_deadline, args)
            clicked = clicked or did_click
            if state == "ready":
                return WakeResult(app.name, app.url, "woke" if clicked else "awake", trim(detail),
                                  attempts, clicked, round(time.monotonic() - started, 3), url)
            final_status = state
        except Exception as exc:
            final_status = "timeout" if isinstance(exc, timeout_error) else "error"
            detail = trim(str(exc))
        finally:
            page.close()
    return WakeResult(app.name, app.url, final_status, trim(detail), attempts, clicked,
                      round(time.monotonic() - started, 3), checked_url)


def write_report(path: Path | None, apps: list[AppUrl], results: list[WakeResult], *, complete: bool) -> None:
    if path is None:
        return
    checked = {result.url for result in results}
    data = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "total": len(apps),
        "checked": len(results),
        "successful": sum(result.ok for result in results),
        "results": [dict(asdict(result), ok=result.ok) for result in results],
        "unvisited": [asdict(app) for app in apps if app.url not in checked],
        "meaning": "Rendered app content was observed; this is not a guarantee of continuous availability or correct conversion.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def run_browser(args: argparse.Namespace, apps: list[AppUrl]) -> int:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install playwright_wake/requirements.txt and run playwright install chromium.") from exc
    results: list[WakeResult] = []
    write_report(args.report_file, apps, results, complete=False)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headful)
        try:
            for index, app in enumerate(apps, 1):
                if args.max_delay_seconds:
                    time.sleep(random.randint(0, args.max_delay_seconds))
                print(f"[{index}/{len(apps)}] opening {app.name}", flush=True)
                context = browser.new_context(viewport={"width": 1365, "height": 900})
                context.set_default_timeout(10_000)
                try:
                    result = wake_app(context, app, args, PlaywrightTimeoutError)
                except Exception as exc:
                    result = WakeResult(app.name, app.url, "error", trim(str(exc)))
                finally:
                    context.close()
                results.append(result)
                write_report(args.report_file, apps, results, complete=False)
                print(f"{result.status.upper()}: {result.name} | {result.detail} | {result.elapsed_seconds}s", flush=True)
        finally:
            browser.close()
    write_report(args.report_file, apps, results, complete=True)
    failed = [result for result in results if not result.ok]
    print(f"Summary: {len(results) - len(failed)}/{len(apps)} apps rendered successfully.")
    for result in failed:
        print(f"- {result.status}: {result.name} | {result.url} | {result.detail}")
    return 1 if failed else 0


def trim(text: str, max_length: int = 400) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= max_length else cleaned[:max_length] + "..."


def main() -> int:
    args = parse_args()
    apps = select_apps(load_urls(args.urls_file), args)
    if args.dry_run:
        print(f"Dry run OK: {len(apps)} active URLs loaded from {args.urls_file}")
        for index, app in enumerate(apps, 1):
            print(f"{index:02d}. {app.name} | {app.url}")
        return 0
    return run_browser(args, apps)


if __name__ == "__main__":
    sys.exit(main())
