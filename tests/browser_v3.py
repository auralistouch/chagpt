"""Local Chromium: real v3 signal handler, mock exchange and Bark, CSV review."""
import json,math,shutil,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from strategy import Candle,MINUTE
from pullback_strategy import pullback_features,analyze_pullback
from phone import PhoneMonitor
from playwright.sync_api import sync_playwright,expect

class V3BrowserTests(unittest.TestCase):
    def test_points_actual_import_web_and_bark(self):
        candles=[];prior=100
        for i in range(600):
            close=100+.02*i+.1*math.sin(i/2)
            candles.append(Candle(i*MINUTE,prior,max(prior,close)+.015,min(prior,close)-.015,close,0));prior=close
        latest=max(i for i,f in enumerate(pullback_features(candles)) if f['qualified'])
        candles=candles[:latest+1];now=candles[-1].time+MINUTE+1000;quote=dict(price=candles[-1].close,time=now)
        data=analyze_pullback(candles,now,price_kind='index',quote=quote)
        data.update(source='币安 BTCUSDT 指数',pair='BTC / USDT',quote='USDT')
        with tempfile.TemporaryDirectory() as folder,patch('app.fetch_index_candles',return_value=(candles,now)),patch('app.fetch_index_quote',return_value=quote):
            app.CACHE.clear();sender=Mock();reporter=Mock(return_value=data)
            monitor=PhoneMonitor(Path(folder)/'phone.json',reporter,sender)
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);server.phone_monitor=monitor
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    context=browser.new_context(viewport=dict(width=1280,height=950))
                    context.add_init_script('''
                    localStorage.setItem('btc-observer-alert-preferences-v1',JSON.stringify({enabled:true,sound:false,desktop:true,auto:false}));window.notices=[];
                    window.Notification=class {static permission='granted';constructor(title,options){notices.push({title,...options});}close(){}};
                    ''')
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    expect(page.locator('#phone-status')).to_contain_text('尚未保存')
                    self.assertEqual(page.locator('#policy').input_value(),'pullback_v3')
                    page.click('#refresh')
                    expect(page.locator('#action')).to_contain_text('看涨信号')
                    expect(page.locator('#signal-note')).to_contain_text('回踩收回确认')
                    self.assertGreater(page.evaluate('notices.length'),0)
                    page.locator('#trades').evaluate('node => node.closest("details").open=true')
                    text=page.locator('#trades').inner_text()
                    self.assertNotIn(' – ',text);self.assertNotIn('不确定',text)
                    expect(page.locator('#range')).to_contain_text('不是你的实际订单胜率')
                    header='order_id,direction,open_time_ms,expiry_time_ms,open_index,close_index,net_payout,stake\n'
                    csv=header+'one,UP,1000,601000,100,101,.8,10\ntwo,DOWN,1000,601000,100,101,.8,10\nthree,UP,1000,601000,100,,.8,10'
                    page.set_input_files('#order-file',dict(name='orders.csv',mimeType='text/csv',buffer=csv.encode()))
                    expect(page.locator('#order-stats')).to_contain_text('50%')
                    expect(page.locator('#order-stats')).to_contain_text('1 / 1 / 0 / 1')
                    page.fill('#phone-key','fake-browser-v3-key');page.click('#phone-save')
                    expect(page.locator('#phone-status')).to_contain_text('新版：顺向趋势')
                    monitor.check_once();monitor.check_once();sender.assert_called_once()
                    self.assertIn('看涨',sender.call_args.args[1]);self.assertIn('回踩收回',sender.call_args.args[2])
                    Path('work').mkdir(exist_ok=True);page.screenshot(path='work/v3-desktop.png',full_page=True)
                    page.set_viewport_size(dict(width=390,height=844))
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.screenshot(path='work/v3-mobile.png',full_page=True)
                    self.assertEqual(errors,[])
                    browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();app.CACHE.clear()

if __name__=='__main__':unittest.main(verbosity=2)
