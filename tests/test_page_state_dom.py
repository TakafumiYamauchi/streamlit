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

    def test_component_loading_failure_is_error_not_rendered_markdown(self):
        warning = '<div data-testid="stMarkdownContainer">Your app is having trouble loading the <code>mobile_streamlit_bridge.esperanto_mobile_pwa</code> component.</div>'
        for markup in (MAIN + warning,
                       '<div data-testid="stMainBlockContainer"><div data-testid="stElementContainer">' + warning + '</div></div>'):
            with self.subTest(markup=markup):
                state = self.state(markup)
                self.assertEqual(state["state"], "error")
                self.assertIn("mobile_streamlit_bridge.esperanto_mobile_pwa", state["detail"])

    def test_hidden_component_warning_does_not_block_a_ready_app(self):
        warning = '<div data-testid="stAlert" style="display:none">Your app is having trouble loading the example component.</div>'
        self.assertEqual(self.state(MAIN + warning)["state"], "ready")

    def test_sleep_and_waking_screens_are_not_success(self):
        sleeping = self.state('<p>This app has gone to sleep due to inactivity</p><button>Yes, get this app back up!</button>')
        self.assertEqual(sleeping["state"], "sleep")
        self.assertTrue(sleeping["wake"])
        self.assertEqual(self.state('<p>Your app is waking up</p>')["state"], "loading")

    def test_ready_app_in_iframe_is_recognized(self):
        self.page.set_content('<p>Cloud shell</p><iframe style="width:900px;height:600px" srcdoc="' + html.escape(MAIN, quote=True) + '"></iframe>')
        self.page.frame_locator('iframe').locator('textarea').wait_for()
        self.assertEqual(wake.inspect_page(self.page)[0]["state"], "ready")

    def component_fixture(self, contents, *, testid="stCustomComponentV1", in_main=True):
        origin = "https://fixture.streamlit.app"
        component_path = "/~/+/component/example.quiz/index.html"
        frame = '<iframe style="width:900px;height:600px" data-testid="' + testid + '" src="' + component_path + '"></iframe>'
        markup = '<div data-testid="stMainBlockContainer">' + frame + '</div>' if in_main else frame
        self.context.route(origin + "/", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
        self.context.route(origin + component_path, lambda route: route.fulfill(status=200, content_type="text/html", body=contents))
        self.page.goto(origin + "/")

    def test_custom_component_with_real_controls_is_ready(self):
        self.component_fixture('<h1>Quiz</h1><button>Start quiz</button>')
        self.page.frame_locator('iframe').get_by_role('button').wait_for()
        state = wake.inspect_page(self.page)[0]
        self.assertEqual(state["state"], "ready")
        self.assertIn("custom component", state["detail"])

    def test_blank_custom_component_is_not_ready(self):
        self.component_fixture('')
        self.assertEqual(wake.inspect_page(self.page)[0]["state"], "loading")
        self.page.set_content('<div data-testid="stMainBlockContainer"><iframe data-testid="stCustomComponentV1" src="about:blank"></iframe></div>')
        self.assertEqual(wake.inspect_page(self.page)[0]["state"], "loading")

    def test_unrelated_or_outside_main_iframe_does_not_qualify(self):
        for options in ({"testid": "not-a-streamlit-component"}, {"in_main": False}):
            with self.subTest(options=options):
                self.component_fixture('<button>Unrelated button</button>', **options)
                self.assertEqual(wake.inspect_page(self.page)[0]["state"], "loading")

    def test_custom_component_error_or_loading_does_not_count_as_ready(self):
        for status, markup in (("error", '<div role="alert">Failed to load quiz</div><button>Retry</button>'),
                               ("loading", '<div role="status">Loading...</div><button>Quiz</button>'),
                               ("loading", '<div>Service status: OK</div>')):
            with self.subTest(status=status, markup=markup):
                self.component_fixture(markup)
                self.assertEqual(wake.inspect_page(self.page)[0]["state"], status)


if __name__ == "__main__":
    unittest.main()
