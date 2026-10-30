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
        self.fixture = json.loads(json.dumps(type(self).fixture))
        self.page = self.browser.new_page()
        self.page.clock.install()
        self.errors=[]
        self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')
        self.assertEqual(self.page.locator('#mode').input_value(), 'live')
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

    def mock_alert_apis(self):
        self.page.evaluate('''() => {
          window.sentNotifications=[]; window.permissionRequests=0; window.tones=0;
          window.permissionDecision='granted';
          window.Notification=class {
            static permission='default';
            static requestPermission() {
              window.permissionRequests++;
              this.permission=window.permissionDecision;
              return Promise.resolve(this.permission);
            }
            constructor(title,options) {
              this.record={title,...options,closed:false};
              window.sentNotifications.push(this.record);
            }
            close() { this.record.closed=true; }
          };
          window.AudioContext=class {
            constructor() { this.state='suspended';this.currentTime=0; }
            resume() { this.state='running';return Promise.resolve(); }
            createOscillator() {
              return {frequency:{},connect(){},start(){window.tones++;},stop(){}};
            }
            createGain() {
              return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};
            }
          };
        }''')

    def test_alerts_are_opt_in_and_deduplicate_refreshes_with_ten_minute_cooldown(self):
        self.mock_alert_apis(); self.route(['ok'])
        self.page.click('#refresh')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        self.assertEqual(self.page.evaluate('window.tones'),0)
        self.assertEqual(self.page.evaluate('window.permissionRequests'),0)
        self.page.check('#desktop-alerts')
        self.page.click('#enable-alerts')
        expect(self.page.locator('#alert-status')).to_contain_text('已请求提醒')
        self.assertEqual(self.page.evaluate('window.permissionRequests'),1)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),1)
        self.assertEqual(self.page.evaluate('window.tones'),3)
        self.tick(30000,3)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),1)
        self.fixture['signal_time']+=60000
        self.tick(30000,4)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),1)
        self.fixture['signal_time']+=540000
        self.tick(30000,5)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),2)

    def test_demo_wait_and_expired_data_do_not_alert_but_explicit_test_works(self):
        self.mock_alert_apis()
        self.fixture['action']='WAIT'; self.route(['ok'])
        self.page.check('#desktop-alerts'); self.page.click('#enable-alerts')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        for overrides in [dict(action='WAIT'),dict(action='UP',demo=True),dict(action='UP',data_age_seconds=90),dict(action='UP',data_age_seconds=100),dict(action='UP',data_age_seconds=30,request_duration_seconds=70)]:
            candidate={**self.fixture,**overrides}
            self.page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',candidate)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),0)
        self.assertEqual(self.page.evaluate('window.tones'),0)
        self.page.click('#test-alerts')
        self.assertIn('测试提醒',self.page.evaluate('window.sentNotifications[0].title'))
        self.assertIsNone(self.page.evaluate("localStorage.getItem('btc-observer-last-alert-v1')"))
        self.page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',{**self.fixture,'action':'UP'})
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),2)

    def test_notification_denial_keeps_sound_and_disable_stops_alerts(self):
        self.mock_alert_apis(); self.fixture['action']='WAIT'; self.route(['ok'])
        self.page.evaluate("window.permissionDecision='denied'")
        self.page.check('#desktop-alerts'); self.page.click('#enable-alerts')
        expect(self.page.locator('#alert-status')).to_contain_text('桌面通知被拒绝')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        self.page.click('#test-alerts')
        self.assertEqual(self.page.evaluate('window.tones'),3)
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),0)
        self.page.click('#enable-alerts')
        self.page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',{**self.fixture,'action':'UP'})
        self.assertEqual(self.page.evaluate('window.tones'),3)
        expect(self.page.locator('#alert-status')).to_have_text('提醒已停用。')

    def test_alert_cooldown_survives_reload_but_enable_does_not(self):
        self.mock_alert_apis();self.route(['ok'])
        self.page.check('#desktop-alerts'); self.page.click('#enable-alerts')
        expect(self.page.locator('#alert-status')).to_contain_text('已请求提醒')
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),1)
        self.page.reload(); self.mock_alert_apis()
        expect(self.page.locator('#enable-alerts')).to_have_text('启用提醒并自动刷新')
        self.page.check('#desktop-alerts'); self.page.click('#enable-alerts')
        expect(self.page.locator('#source')).to_have_text('币安现货 · 已获取')
        self.assertEqual(self.page.evaluate('window.sentNotifications.length'),0)
        self.assertEqual(self.page.evaluate('window.tones'),0)

    def test_no_available_alert_channel_does_not_claim_enabled(self):
        self.mock_alert_apis();self.route(['ok'])
        self.page.evaluate("() => { window.permissionDecision='denied';window.AudioContext.prototype.resume=()=>Promise.reject(new Error('blocked')); }")
        self.page.check('#desktop-alerts'); self.page.click('#enable-alerts')
        expect(self.page.locator('#alert-status')).to_contain_text('未能启用提醒')
        expect(self.page.locator('#test-alerts')).to_be_disabled()
        expect(self.page.locator('#auto')).not_to_be_checked()
        self.assertEqual(len(self.calls),0)


if __name__=='__main__': unittest.main(verbosity=2)
