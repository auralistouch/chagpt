"""Optional local browser test; Bark and market reports are mocked."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from phone import PhoneMonitor
from playwright.sync_api import sync_playwright, expect

FAKE_KEY='test-browser-device-not-a-real-recipient'


class PhoneBrowserTests(unittest.TestCase):
    def test_save_and_test_on_same_origin_without_exposing_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            reporter=Mock(return_value=dict(action='WAIT',demo=False))
            sender=Mock()
            monitor=PhoneMonitor(Path(folder)/'phone.json',reporter,sender,interval=.05)
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            server.phone_monitor=monitor
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();monitor.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':390,'height':844})
                    page.route('**/api/signals?**',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'Test market disabled'})))
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    expect(page.locator('#phone-status')).to_contain_text('尚未保存')
                    self.assertEqual(page.locator('#mode').input_value(),'live')
                    page.fill('#phone-key','https://api.day.app/'+FAKE_KEY+'/example')
                    page.check('#phone-enabled');page.click('#phone-save')
                    expect(page.locator('#phone-status')).to_contain_text('推送地址已保存在本机')
                    self.assertEqual(page.locator('#phone-key').input_value(),'')
                    self.assertNotIn(FAKE_KEY,page.locator('body').inner_text())
                    expect(page.locator('#phone-test')).to_be_enabled()
                    page.reload()
                    expect(page.locator('#phone-status')).to_contain_text('推送地址已保存在本机')
                    expect(page.locator('#phone-enabled')).to_be_checked()
                    page.click('#phone-test')
                    expect(page.locator('#phone-status')).to_contain_text('已接受测试请求')
                    sender.assert_called_once()
                    self.assertIn('测试',sender.call_args.args[1])
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    page.select_option('#mode','demo');page.click('#phone-stop')
                    expect(page.locator('#phone-status')).to_contain_text('手机监测未启用')
                    self.assertFalse(monitor.status()['enabled'])
                    # Enable again, then close the browser: the worker keeps
                    # checking without browser polling or page notifications.
                    page.select_option('#mode','live');page.check('#phone-enabled')
                    expect(page.locator('#phone-status')).to_contain_text('手机监测已启用')
                    calls_before=reporter.call_count
                    self.assertEqual(errors,[])
                    browser.close()
                    event=threading.Event()
                    reporter.side_effect=lambda *args:(event.set() or dict(action='WAIT',demo=False))
                    self.assertTrue(event.wait(2))
                    self.assertGreater(reporter.call_count,calls_before)
            finally:
                monitor.stop();server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main(verbosity=2)
