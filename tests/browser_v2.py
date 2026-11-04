"""Local end-to-end UI tests; exchange, desktop notifications and Bark mocked."""
import json
import math
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
from strategy import Candle, MINUTE
from trend_strategy import analyze_trend, trend_features
from playwright.sync_api import sync_playwright, expect


def fixture():
    candles=[];previous=100
    for i in range(300):
        close=100+.02*i+.1*math.sin(i/2)
        candles.append(Candle(i*MINUTE,previous,max(previous,close)+.015,min(previous,close)-.015,close,0));previous=close
    end=next(i for i,f in enumerate(trend_features(candles)) if f['qualified'])+1
    candles=candles[:end];now=candles[-1].time+MINUTE+1000
    result=analyze_trend(candles,now,price_kind='index',quote=dict(price=candles[-1].close,time=now))
    return {**result,'source':'币安 BTCUSDT 指数','volume_source':'不使用成交量','pair':'BTC / USDT','quote':'USDT'}


class NewStrategyBrowserTests(unittest.TestCase):
    def test_web_direction_desktop_dedup_bark_and_mobile(self):
        data=fixture()
        with tempfile.TemporaryDirectory() as folder:
            sender=Mock();reporter=Mock(return_value=data)
            monitor=PhoneMonitor(Path(folder)/'phone.json',reporter,sender)
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);server.phone_monitor=monitor
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    context=browser.new_context(viewport=dict(width=1280,height=950))
                    context.add_init_script('''
                        localStorage.setItem('btc-observer-alert-preferences-v1',JSON.stringify({enabled:true,sound:false,desktop:true,auto:false}));
                        window.notices=[];
                        window.Notification=class {static permission='granted';constructor(title,options){notices.push({title,...options});}close(){}};
                    ''')
                    page=context.new_page();errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.route('**/api/signals?**',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(data)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    expect(page.locator('#phone-status')).to_contain_text('尚未保存')
                    self.assertEqual(page.locator('#policy').input_value(),'trend_v2')
                    page.click('#refresh')
                    expect(page.locator('#action')).to_contain_text('看涨信号')
                    expect(page.locator('#signal-note')).to_contain_text('回踩重启')
                    self.assertEqual(page.locator('#trades').is_visible(),False)
                    self.assertEqual(page.evaluate('notices.length'),1)
                    self.assertIn('新策略',page.evaluate('notices[0].title'))
                    page.click('#refresh')
                    expect(page.locator('#refresh')).to_be_enabled()
                    self.assertEqual(page.evaluate('notices.length'),1)
                    page.fill('#phone-key','fake-browser-only-key');page.click('#phone-save')
                    expect(page.locator('#phone-status')).to_contain_text('新策略：5分钟趋势')
                    self.assertEqual(monitor.status()['policy'],'trend_v2')
                    monitor.check_once();monitor.check_once();sender.assert_called_once()
                    self.assertIn('看涨',sender.call_args.args[1])
                    self.assertNotIn('fake-browser-only-key',page.locator('body').inner_text())
                    Path('work').mkdir(exist_ok=True)
                    page.screenshot(path='work/v2-desktop.png',full_page=True)
                    page.set_viewport_size(dict(width=390,height=844))
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.screenshot(path='work/v2-mobile.png',full_page=True)
                    self.assertEqual(errors,[])
                    browser.close()
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main(verbosity=2)
