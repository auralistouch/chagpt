"""Latest-only local UI tests. Mock quotes/Bark; no real 65% success claim."""
import json,shutil,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from strategy import Candle,MINUTE
from latest_strategy import analyze_latest,POLICY,VERSION
from latest_phone import LatestPhoneMonitor
from playwright.sync_api import sync_playwright,expect

class LatestBrowserTests(unittest.TestCase):
    def test_latest_only_waits_and_bark_test_still_works(self):
        candles=[Candle(i*MINUTE,100,101,99,100,0) for i in range(300)]
        now=300*MINUTE;quote=dict(price=100,time=now)
        data=analyze_latest(candles,now,price_kind='index',quote=quote)
        data.update(source='币安 BTCUSDT 指数',pair='BTC / USDT',quote='USDT')
        with tempfile.TemporaryDirectory() as folder,patch('app.fetch_index_candles',return_value=(candles,now)),patch('app.fetch_index_quote',return_value=quote):
            app.CACHE.clear();sender=Mock();reporter=Mock(return_value=data)
            path=Path(folder)/'phone.json'
            path.write_text(json.dumps(dict(key='fake-local-browser-key',enabled=True,mode='live',payout=.8,policy='pullback_v3',last_signal_time=0,extra='preserve')))
            monitor=LatestPhoneMonitor(path,reporter,sender)
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);server.phone_monitor=monitor
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    context=browser.new_context(viewport=dict(width=1280,height=950))
                    context.add_init_script('''localStorage.setItem('btc-observer-alert-preferences-v1',JSON.stringify({enabled:true,sound:false,desktop:true,auto:false}));window.notices=[];window.Notification=class {static permission='granted';constructor(title,options){notices.push({title,...options});}close(){}};''')
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    expect(page.locator('#phone-status')).to_contain_text('推送地址已保存在本机')
                    self.assertEqual(page.locator('#policy').input_value(),POLICY)
                    self.assertEqual(page.locator('select#policy').count(),0)
                    page.click('#refresh')
                    expect(page.locator('#action')).to_contain_text('等待')
                    expect(page.locator('#validation-status')).to_contain_text('等待')
                    self.assertEqual(page.evaluate('notices.length'),0)
                    body=page.locator('body').inner_text()
                    for old in ('v2','v3','旧版','pullback_v3','trend_v2','严格统计过滤'):self.assertNotIn(old,body)
                    self.assertIn(VERSION,body)
                    monitor.check_once();sender.assert_not_called()
                    page.click('#phone-test')
                    expect(page.locator('#phone-status')).to_contain_text('已接受测试请求')
                    sender.assert_called_once();self.assertIn('测试',sender.call_args.args[1])
                    page.click('#phone-save')
                    self.assertEqual(json.loads(path.read_text())['extra'],'preserve')
                    response=page.request.get(f'http://127.0.0.1:{server.server_port}/api/signals?policy=trend_v2')
                    self.assertEqual(response.status,400)
                    page.set_viewport_size(dict(width=390,height=844))
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    Path('work').mkdir(exist_ok=True);page.screenshot(path='work/latest-mobile.png',full_page=True)
                    self.assertEqual(errors,[]);browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();app.CACHE.clear()

if __name__=='__main__':unittest.main(verbosity=2)
