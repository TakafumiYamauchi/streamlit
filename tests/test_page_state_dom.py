"""Actual readiness JavaScript against local HTML fixtures; no external requests."""
from pathlib import Path
import html
import importlib.util
import os
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "streamlit_random_url_opener/playwright_wake/wake_streamlit_apps.py"
spec = importlib.util.spec_from_file_location("wake_dom_under_test", SOURCE)
wake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wake
spec.loader.exec_module(wake)

MAIN = '<div data-testid="stMainBlockContainer"><div data-testid="stElementContainer"><h1>App title</h1><textarea>Input</textarea></div></div>'


@unittest.skipUnless(os.environ.get("RUN_BROWSER_FIXTURES") == "1", "Set RUN_BROWSER_FIXTURES=1 after installing Chromium")
class DOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        # Fixtures must not contact any external server even if accidentally edited.
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def state(self, markup):
        self.page.set_content(markup)
        return wake.inspect_page(self.page)[0]

    def test_visible_main_content_is_ready(self):
        self.assertEqual(self.state(MAIN)["state"], "ready")

    def test_cloud_header_or_empty_app_container_is_not_ready(self):
        for markup in ('<header><button>Deploy</button></header>',
                       '<div data-testid="stAppViewContainer"><div data-testid="stMainBlockContainer"></div></div>',
                       '<div>HTTP 200 - please wait</div>'):
            with self.subTest(markup=markup):
                self.assertEqual(self.state(markup)["state"], "loading")

    def test_hidden_content_is_not_ready(self):
        self.assertEqual(self.state('<div style="display:none">' + MAIN + '</div>')["state"], "loading")

    def test_live_spinner_and_running_status_override_early_content(self):
        for busy in ('<div data-testid="stSpinner">Loading data</div>',
                     '<div data-testid="stStatusWidget">Running...</div>',
                     '<div class="stStatusWidget">Connecting...</div>'):
            with self.subTest(busy=busy):
                self.assertEqual(self.state(MAIN + busy)["state"], "loading")

    def test_idle_status_does_not_block_ready_content(self):
        self.assertEqual(self.state(MAIN + '<div data-testid="stStatusWidget">Connected</div>')["state"], "ready")

    def test_exception_overrides_visible_content(self):
        state = self.state(MAIN + '<div data-testid="stException">ModuleNotFoundError: missing</div>')
        self.assertEqual(state["state"], "error")
        self.assertIn("ModuleNotFoundError", state["detail"])

    def test_connection_dialog_overrides_stale_app_content(self):
        for markup in ('<div role="dialog">Connection error: unable to reconnect</div>',
                       '<div data-testid="stStatusWidget">Disconnected</div>'):
            with self.subTest(markup=markup):
                self.assertEqual(self.state(MAIN + markup)["state"], "error")

    def test_sleep_and_waking_screens_are_not_success(self):
        sleeping = self.state('<p>This app has gone to sleep due to inactivity</p><button>Yes, get this app back up!</button>')
        self.assertEqual(sleeping["state"], "sleep")
        self.assertTrue(sleeping["wake"])
        self.assertEqual(self.state('<p>Your app is waking up</p>')["state"], "loading")

    def test_ready_app_in_iframe_is_recognized(self):
        self.page.set_content('<p>Cloud shell</p><iframe style="width:900px;height:600px" srcdoc="' + html.escape(MAIN, quote=True) + '"></iframe>')
        self.page.frame_locator('iframe').locator('textarea').wait_for()
        self.assertEqual(wake.inspect_page(self.page)[0]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
