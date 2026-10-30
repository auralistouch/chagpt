"""Optional browser regression checks: python3 tests/browser_refresh.py.

Requires Playwright and Chromium. All exchange requests are intercepted.
"""
import json
from pathlib import Path
import shutil
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from market import demo_candles
from strategy import analyze
from playwright.sync_api import sync_playwright, expect


class RefreshBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
        data, now = demo_candles()
        cls.fixture = analyze(data, now, demo=False)
        # Force an observation direction to check that disconnects remove it.
        cls.fixture.update(action='UP',source='币安现货',pair='BTC / USDT',quote='USDT',data_age_seconds=0)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.clock.install()
        self.errors=[]
        self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')
        self.calls=[]

    def tearDown(self):
        self.assertEqual(self.errors,[])
        self.page.close()

    def route(self, plans):
        def reply(route):
            self.calls.append(route.request.url)
            outcome=plans[min(len(self.calls)-1,len(plans)-1)]
            if outcome=='abort': route.abort('failed')
            elif outcome=='500': route.fulfill(status=500,content_type='application/json',body=json.dumps({'error':'本机程序处理请求失败'}))
            else: route.fulfill(status=200,content_type='application/json',body=json.dumps(self.fixture))
        self.page.route('**/api/signals?**',reply)

    def tick(self, milliseconds, expected_calls):
        self.page.clock.run_for(milliseconds)
        self.page.wait_for_function('() => !document.getElementById("refresh").disabled')
        self.assertEqual(len(self.calls),expected_calls)

    def test_auto_disconnect_recovers_without_manual_click_and_clears_direction(self):
        self.route(['ok','abort','ok'])
        self.page.check('#auto')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        self.assertIn('观察看涨',self.page.locator('#action').inner_text())
        self.tick(30000,2)
        expect(self.page.locator('#error')).to_contain_text('浏览器未能连接本机程序')
        expect(self.page.locator('#action')).to_contain_text('暂停信号')
        expect(self.page.locator('#results')).to_be_visible()
        expect(self.page.locator('#refresh-status')).to_contain_text('5 秒后自动重试')
        self.tick(5000,3)
        expect(self.page.locator('#error')).to_be_hidden()
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')

    def test_backoff_and_unchecking_cancels_retries(self):
        self.route(['abort'])
        self.page.check('#auto')
        expect(self.page.locator('#refresh-status')).to_contain_text('5 秒后自动重试')
        self.tick(5000,2)
        expect(self.page.locator('#refresh-status')).to_contain_text('10 秒后自动重试')
        self.tick(10000,3)
        expect(self.page.locator('#refresh-status')).to_contain_text('20 秒后自动重试')
        self.page.uncheck('#auto')
        self.tick(120000,3)

    def test_slow_request_does_not_start_overlapping_refreshes(self):
        fixture=json.dumps(self.fixture)
        self.page.evaluate('''(fixture) => {
          window.requestCount=0;
          window.fetch=() => { window.requestCount++; return new Promise(resolve => {
            window.finishRequest=()=>resolve(new Response(fixture,{headers:{'Content-Type':'application/json'}}));
          }); };
        }''',fixture)
        self.page.check('#auto')
        self.page.clock.run_for(90000)
        self.assertEqual(self.page.evaluate('window.requestCount'),1)
        self.page.evaluate('window.finishRequest()')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        self.page.clock.run_for(29000)
        self.assertEqual(self.page.evaluate('window.requestCount'),1)
        self.page.clock.run_for(1000)
        self.assertEqual(self.page.evaluate('window.requestCount'),2)

    def test_server_error_remains_readable_and_retries(self):
        self.route(['500','ok'])
        self.page.check('#auto')
        expect(self.page.locator('#error')).to_have_text('本机程序处理请求失败')
        self.tick(5000,2)
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')


if __name__=='__main__': unittest.main(verbosity=2)
