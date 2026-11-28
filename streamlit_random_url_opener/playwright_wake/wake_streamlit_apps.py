#!/usr/bin/env python3
"""Visit Streamlit Cloud apps sequentially, waking sleepers and checking rendered content."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
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
RECHECK_TIMEOUT_MS = 30_000

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
  const componentErrors = [...document.querySelectorAll(
    '[data-testid="stAlert"], [data-testid="stNotification"], [role="alert"], '
    + '[data-testid="stMarkdownContainer"], [data-testid="stElementContainer"], .element-container'
  )].filter(e => visible(e) && /Your app is having trouble loading the .+? component\./i.test(e.innerText.replace(/\s+/g, ' ')));
  errors.push(...componentErrors);
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

# A custom component is eligible only when its actual iframe is visible inside
# the real Streamlit main area and uses Streamlit's same-origin component route.
COMPONENT_HOST_JS = r"""e => {
  const visible = n => {
    const s = getComputedStyle(n), r = n.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const root = e.closest('[data-testid="stMainBlockContainer"], [data-testid="stAppViewBlockContainer"], '
    + '.main .block-container, .stMain .block-container, .stMainBlockContainer');
  const src = new URL(e.getAttribute('src') || 'about:blank', document.baseURI);
  return e.matches('iframe[data-testid="stCustomComponentV1"]') && root && visible(root) && visible(e)
    && src.origin === location.origin && /^\/(?:~\/\+\/)?component\//.test(src.pathname);
}"""

COMPONENT_STATE_JS = r"""() => {
  const visible = e => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const alerts = [...document.querySelectorAll('[role="alert"]')].filter(e => visible(e)
    && /\b(?:error|failed|unable|timed out|timeout)\b/i.test(e.innerText));
  if (alerts.length) return {state:'error', detail:alerts.map(e => e.innerText).join(' ').slice(0,400)};
  const busy = [...document.querySelectorAll('[aria-busy="true"], [role="progressbar"], [role="status"]')]
    .some(e => visible(e) && (e.matches('[aria-busy="true"], [role="progressbar"]')
      || /loading|connecting|please wait/i.test(e.innerText)));
  const usable = [...document.querySelectorAll('button, input:not([type="hidden"]), select, textarea')]
    .some(e => visible(e) && !e.disabled && (e.tagName !== 'BUTTON' || e.innerText.trim()));
  return {state:!busy && usable ? 'ready' : 'loading',
    detail:!busy && usable ? 'Visible UI in a Streamlit custom component' : 'Custom component has no ready UI'};
}"""


@dataclass(frozen=True)
class AppUrl:
    name: str
    url: str


@dataclass(frozen=True)
class RouteCheck:
    url: str
    status: str
    detail: str


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
    checks: tuple[RouteCheck, ...] = ()

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
    parser.add_argument("--recheck-first", type=int, choices=range(6), default=0,
                        help="On a full sweep, revisit transient failures among the first N configured URLs.")
    parser.add_argument("--only-url", action="append", default=[], help="Check only these registered URLs; repeatable.")
    parser.add_argument("--report-file", type=Path, help="JSON progress/results including unvisited URLs.")
    parser.add_argument("--max-delay-seconds", type=int, default=0)
    parser.add_argument("--navigation-timeout-ms", type=int, default=90_000)
    parser.add_argument("--sleep-detection-timeout-ms", type=int, default=8_000,
                        help="Compatibility option: minimum shell observation, within the total budget.")
    parser.add_argument("--post-click-wait-ms", type=int, default=15_000,
                        help="Minimum ready observation after wake click, within the total budget.")
    parser.add_argument("--wake-timeout-ms", type=int, default=180_000,
                        help="Total budget for all navigation, wake and public URL recovery checks.")
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
            if (isinstance(state, dict) and state.get("state") == "loading"
                    and getattr(frame, "parent_frame", None) is not None):
                # Empty/about:blank placeholders and arbitrary child frames never qualify.
                parts = urlsplit(frame.url)
                if (parts.scheme == "https" and (parts.hostname or "").endswith(".streamlit.app")
                        and re.match(r"^/(?:~/\+/)?component/", parts.path)
                        and frame.frame_element().evaluate(COMPONENT_HOST_JS)):
                    state = frame.evaluate(COMPONENT_STATE_JS)
            if isinstance(state, dict):
                states.append((state, frame))
        except Exception as exc:
            # The shell can replace a frame between enumeration and evaluation.
            if re.search(r"frame (?:was |has been )?detached|execution context was destroyed", str(exc), re.I):
                continue
            return {"state": "error", "detail": "Frame inspection failed: " + trim(str(exc))}, frame
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


def wake_app(context: Any, app: AppUrl, args: argparse.Namespace, timeout_error: type[Exception],
             *, public_only: bool = False) -> WakeResult:
    started = time.monotonic()
    budget_ms = min(RECHECK_TIMEOUT_MS, args.wake_timeout_ms) if public_only else args.wake_timeout_ms
    deadline = started + budget_ms / 1000
    clicked = False
    detail = "No attempt completed"
    final_status = "timeout"
    checked_url = app.url
    attempts = 0
    checks: list[RouteCheck] = []
    routes = (app.url,) if public_only else (app.url, body_url(app.url), app.url)
    for attempt, url in enumerate(routes, 1):
        if remaining_ms(deadline) <= 0:
            break
        attempts = attempt
        checked_url = url
        phase_deadline = deadline
        if attempt < len(routes):
            # Reserve time for the final public visit even if the direct body route hangs.
            phase_ms = min(60_000, args.wake_timeout_ms // 3)
            if attempt == 1:
                phase_ms = min(max(phase_ms, args.sleep_detection_timeout_ms),
                               args.wake_timeout_ms // 2)
            phase_deadline = min(deadline, time.monotonic() + phase_ms / 1000)
        page = context.new_page()
        client_error = False
        try:
            response = None
            try:
                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=max(1, min(args.navigation_timeout_ms, remaining_ms(phase_deadline))))
            except timeout_error:
                # Navigation can time out after useful DOM has already appeared.
                pass
            if response is not None and response.status >= 400:
                state, detail = "http_error", f"HTTP {response.status}"
                client_error = response.status < 500
            else:
                state, detail, did_click = observe_app(page, phase_deadline, args)
                clicked = clicked or did_click
        except Exception as exc:
            state = "timeout" if isinstance(exc, timeout_error) else "error"
            detail = str(exc)
        finally:
            page.close()

        checks.append(RouteCheck(url, state, trim(detail)))
        if state == "ready":
            if url == app.url:
                final_status = "woke" if clicked else "awake"
                break
            # A reachable body can wake the backend while its public shell is broken.
            # Only a fresh visit through the registered URL proves user-facing recovery.
            final_status = "body_only"
            detail = "App body rendered; public URL recovery has not been confirmed"
            continue
        if final_status != "body_only":
            final_status = state
        if client_error and url == app.url:
            break
    return WakeResult(app.name, app.url, final_status, trim(detail), attempts, clicked,
                      round(time.monotonic() - started, 3), checked_url, tuple(checks))


def write_report(path: Path | None, apps: list[AppUrl], results: list[WakeResult], *, complete: bool,
                 first_visits: dict[str, WakeResult] | None = None) -> None:
    if path is None:
        return
    checked = {result.url for result in results}
    records = [dict(asdict(result), ok=result.ok) for result in results]
    for record in records:
        if first_visits and record["url"] in first_visits:
            first = first_visits[record["url"]]
            record["first_visit"] = dict(asdict(first), ok=first.ok)
    data = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "total": len(apps),
        "checked": len(results),
        "successful": sum(result.ok for result in results),
        "results": records,
        "unvisited": [asdict(app) for app in apps if app.url not in checked],
        "meaning": "Rendered app content was observed; this is not a guarantee of continuous availability or correct conversion.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def transient_failure(result: WakeResult) -> bool:
    if result.status == "http_error":
        return re.fullmatch(r"HTTP (?:5[0-9]{2}|408|429)", result.detail) is not None
    return result.status in {"timeout", "body_only"}


def run_checks(args, apps, recheck_urls, visit) -> int:
    results: list[WakeResult] = []
    first_visits: dict[str, WakeResult] = {}
    for index, app in enumerate(apps, 1):
        if args.max_delay_seconds:
            time.sleep(random.randint(0, args.max_delay_seconds))
        print(f"[{index}/{len(apps)}] opening {app.name}", flush=True)
        result = visit(app, public_only=False)
        results.append(result)
        write_report(args.report_file, apps, results, complete=False)
        print(f"{result.status.upper()}: {result.name} | {result.detail} | {result.elapsed_seconds}s", flush=True)
        if len(result.checks) > 1:
            for check in result.checks:
                print(f"  {check.url} | {check.status} | {check.detail}", flush=True)

    for index, app in enumerate(apps):
        first = results[index]
        if app.url in recheck_urls and transient_failure(first):
            print(f"Rechecking public URL after the full sweep: {app.name}", flush=True)
            result = visit(app, public_only=True)
            first_visits[app.url] = first
            results[index] = result
            write_report(args.report_file, apps, results, complete=False, first_visits=first_visits)
            print(f"{first.status} -> {result.status}: {app.url} | {result.detail}", flush=True)

    write_report(args.report_file, apps, results, complete=True, first_visits=first_visits)
    failed = [result for result in results if not result.ok]
    recovered = [result for result in results if result.url in first_visits and result.ok]
    summary = f"{len(results) - len(failed)}/{len(apps)} apps rendered successfully."
    print(f"Summary: {summary}")
    for result in failed:
        print(f"- {result.status}: {result.name} | {result.url} | {result.detail}")
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_file).open("a", encoding="utf-8") as stream:
            final_successes = len(results) - len(failed)
            initial_successes = final_successes - len(recovered)
            print(f"### Streamlit sweep\n\nInitial {initial_successes}/{len(apps)} → "
                  f"Final {final_successes}/{len(apps)} apps rendered successfully.\n", file=stream)
            print(f"Rechecked {len(first_visits)} · Recovered {len(recovered)} · Unresolved {len(failed)}\n", file=stream)
            for result in recovered + failed:
                status = result.status
                if result.url in first_visits:
                    status = f"{first_visits[result.url].status} → {status}"
                print(f"- [{result.name}]({result.url}) — {status}", file=stream)
    return 1 if failed else 0


def run_browser(args: argparse.Namespace, apps: list[AppUrl], recheck_urls: set[str]) -> int:
    write_report(args.report_file, apps, [], complete=False)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install playwright_wake/requirements.txt and run playwright install chromium.") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headful)
        try:
            def visit(app, *, public_only):
                context = browser.new_context(viewport={"width": 1365, "height": 900})
                context.set_default_timeout(10_000)
                try:
                    return wake_app(context, app, args, PlaywrightTimeoutError, public_only=public_only)
                except Exception as exc:
                    return WakeResult(app.name, app.url, "error", trim(str(exc)))
                finally:
                    context.close()
            return run_checks(args, apps, recheck_urls, visit)
        finally:
            browser.close()


def trim(text: str, max_length: int = 400) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= max_length else cleaned[:max_length] + "..."


def main() -> int:
    args = parse_args()
    configured = load_urls(args.urls_file)
    recheck_urls = {app.url for app in configured[:args.recheck_first]} if not (args.only_url or args.limit) else set()
    apps = select_apps(configured, args)
    if args.dry_run:
        print(f"Dry run OK: {len(apps)} active URLs loaded from {args.urls_file}")
        for index, app in enumerate(apps, 1):
            print(f"{index:02d}. {app.name} | {app.url}")
        return 0
    return run_browser(args, apps, recheck_urls)


if __name__ == "__main__":
    sys.exit(main())
