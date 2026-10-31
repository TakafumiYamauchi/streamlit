"""Deterministic failure/recovery tests; no remote apps or browser installation needed."""
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "streamlit_random_url_opener/playwright_wake/wake_streamlit_apps.py"
spec = importlib.util.spec_from_file_location("wake_under_test", SOURCE)
wake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wake
spec.loader.exec_module(wake)


class Clock:
    def __init__(self):
        self.now = 0.0

    def advance(self, milliseconds):
        self.now += milliseconds / 1000


class BrowserTimeout(Exception):
    pass


class Frame:
    def __init__(self, states, clock, after_click=None):
        self.states = list(states)
        self.clock = clock
        self.after_click = after_click
        self.clicks = 0

    def evaluate(self, expression):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"state": state, "detail": "observed " + state, "wake": state == "sleep"}

    def get_by_role(self, role, name):
        if role != "button" or not name.fullmatch("Yes, get this app back up!"):
            raise AssertionError("Only the Cloud wake button may be clicked")
        return SimpleNamespace(first=self)

    def click(self, timeout):
        self.clicks += 1
        if self.after_click:
            self.states = list(self.after_click)


class Page:
    def __init__(self, clock, frames, status=200, navigation_timeout=False):
        self.clock, self.frames, self.status = clock, frames, status
        self.navigation_timeout = navigation_timeout
        self.closed = False
        self.url = None

    def goto(self, url, wait_until, timeout):
        self.url = url
        if self.navigation_timeout:
            self.clock.advance(timeout)
            raise BrowserTimeout("slow navigation")
        self.clock.advance(100)
        return SimpleNamespace(status=self.status)

    def wait_for_timeout(self, milliseconds):
        self.clock.advance(milliseconds)

    def close(self):
        self.closed = True


class Context:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = []

    def new_page(self):
        page = self.pages.pop(0)
        self.created.append(page)
        return page


def args(**updates):
    defaults = dict(wake_timeout_ms=180_000, navigation_timeout_ms=90_000,
                    sleep_detection_timeout_ms=8_000, post_click_wait_ms=0,
                    only_url=[], randomize_order=False, limit=0)
    return SimpleNamespace(**(defaults | updates))


class URLTests(unittest.TestCase):
    def load(self, text, suffix=".jsonc"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("urls" + suffix)
            path.write_text(text, encoding="utf-8")
            return wake.load_urls(path)

    def test_commented_language_is_never_loaded(self):
        apps = self.load('[\n// {"name":"disabled","url":"https://fr.streamlit.app/"},\n'
                         '{"name":"active","url":"https://ja.streamlit.app/"}\n]')
        self.assertEqual([app.name for app in apps], ["active"])

    def test_json_remains_strict_and_inline_comments_are_rejected(self):
        for suffix, content in [(".json", '// comment\n[]'),
                                (".jsonc", '[{"name":"x","url":"https://a.streamlit.app/"} // inline\n]')]:
            with self.subTest(suffix=suffix), self.assertRaises(SystemExit):
                self.load(content, suffix)

    def test_standard_json_and_double_slash_in_url_survive(self):
        apps = self.load('[{"name":"x","url":"https://a.streamlit.app/"}]', '.json')
        self.assertEqual(apps[0].url, 'https://a.streamlit.app/')

    def test_duplicate_missing_or_non_cloud_targets_fail(self):
        invalid = [[], {}, [None], [{"name": "", "url": "https://a.streamlit.app/"}],
                   [{"name": "x", "url": "https://a.streamlit.app.evil.test/"}],
                   [{"name": "x", "url": "http://a.streamlit.app/"}],
                   [{"name": "x", "url": "https://user:pass@a.streamlit.app/"}],
                   [{"name": "x", "url": "https://a.streamlit.app:bad/"}],
                   [{"name": "x", "url": "https://a.streamlit.app"},
                    {"name": "y", "url": "https://a.streamlit.app/"}]]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(SystemExit):
                self.load(json.dumps(item))

    def test_only_url_cannot_reactivate_a_commented_target(self):
        apps = [wake.AppUrl('ja', 'https://ja.streamlit.app/')]
        self.assertEqual(wake.select_apps(apps, args(only_url=['https://ja.streamlit.app'])), apps)
        with self.assertRaises(SystemExit):
            wake.select_apps(apps, args(only_url=['https://fr.streamlit.app/']))


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer = patch.object(wake.time, 'monotonic', side_effect=lambda: self.clock.now)
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.app = wake.AppUrl('test', 'https://test.streamlit.app/')

    def page(self, states=('ready',), **options):
        return Page(self.clock, [Frame(states, self.clock)], **options)

    def check(self, pages, **options):
        context = Context(pages)
        result = wake.wake_app(context, self.app, args(**options), BrowserTimeout)
        self.assertTrue(all(page.closed for page in context.created))
        return result, context

    def test_404_is_failure_even_if_page_contains_ready_looking_content(self):
        result, context = self.check([self.page(status=404)])
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'http_error')
        self.assertEqual(len(context.created), 1)

    def test_http_200_spinner_is_not_success_and_has_total_budget(self):
        result, context = self.check([self.page(('loading',)), self.page(('loading',))])
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'timeout')
        self.assertEqual(result.attempts, 2)
        self.assertLessEqual(self.clock.now, 180.001)
        self.assertEqual(context.created[1].url, 'https://test.streamlit.app/~/+/')

    def test_ready_app_inside_frame_works_despite_loading_outer_shell(self):
        page = Page(self.clock, [Frame(['loading'], self.clock), Frame(['ready'], self.clock)])
        result, _ = self.check([page])
        self.assertTrue(result.ok)
        self.assertLess(result.elapsed_seconds, 3)
        self.assertEqual(result.attempts, 1)

    def test_app_exception_does_not_get_hidden_by_another_ready_frame(self):
        page = Page(self.clock, [Frame(['ready'], self.clock), Frame(['error'], self.clock)])
        result, _ = self.check([page, self.page(['error'])])
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'error')

    def test_click_alone_or_wake_progress_does_not_mean_success(self):
        frame = Frame(['sleep'], self.clock, after_click=['loading'])
        page = Page(self.clock, [frame])
        result, _ = self.check([page, self.page(['loading'])])
        self.assertEqual(frame.clicks, 1)
        self.assertTrue(result.wake_clicked)
        self.assertFalse(result.ok)

    def test_wake_then_rendered_content_is_success(self):
        frame = Frame(['sleep'], self.clock, after_click=['loading', 'ready'])
        result, _ = self.check([Page(self.clock, [frame])], post_click_wait_ms=2000)
        self.assertEqual(result.status, 'woke')
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.elapsed_seconds, 2)

    def test_navigation_timeout_can_recover_on_the_single_fallback(self):
        result, _ = self.check([self.page(['loading'], navigation_timeout=True), self.page()])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertLessEqual(result.elapsed_seconds, 180)

    def test_two_navigation_timeouts_do_not_double_the_budget(self):
        result, _ = self.check([self.page(['loading'], navigation_timeout=True), self.page(['loading'], navigation_timeout=True)])
        self.assertFalse(result.ok)
        self.assertLessEqual(result.elapsed_seconds, 180.001)

    def test_slow_navigation_does_not_hide_already_rendered_app(self):
        result, _ = self.check([self.page(['loading'], navigation_timeout=True), self.page(['ready'], navigation_timeout=True)])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_transient_ready_content_must_remain_ready(self):
        result, _ = self.check([self.page(['ready', 'loading']), self.page(['loading'])])
        self.assertFalse(result.ok)

    def test_server_error_retry_does_not_prevent_a_successful_fallback(self):
        result, _ = self.check([self.page(status=503), self.page()])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)


class ReportTests(unittest.TestCase):
    def test_partial_report_names_unvisited_and_final_report_records_failures(self):
        apps = [wake.AppUrl('a', 'https://a.streamlit.app/'), wake.AppUrl('b', 'https://b.streamlit.app/')]
        results = [wake.WakeResult('a', apps[0].url, 'awake', 'visible')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            wake.write_report(path, apps, results, complete=False)
            partial = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(partial['complete'])
            self.assertEqual(partial['unvisited'], [{'name': 'b', 'url': apps[1].url}])
            self.assertEqual(partial['successful'], 1)
            results.append(wake.WakeResult('b', apps[1].url, 'timeout', 'spinner'))
            wake.write_report(path, apps, results, complete=True)
            final = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(final['complete'])
            self.assertEqual(final['checked'], 2)
            self.assertEqual(final['successful'], 1)
            self.assertFalse(final['results'][1]['ok'])
            self.assertFalse(path.with_name(path.name + '.tmp').exists())


if __name__ == '__main__':
    unittest.main()
