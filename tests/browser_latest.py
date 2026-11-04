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
        data['research_mode']=False
        with tempfile.TemporaryDirectory() as folder,patch('app.report',return_value=data):
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
                    header='order_id,direction,open_time_ms,expiry_time_ms,open_index,close_index,net_payout,stake\n'
                    csv=header+'a,UP,1000,601000,100,101,.8,1\nb,UP,1000,601000,100,101,.8,1\nc,UP,1000,601000,100,99,.8,1\nd,UP,1000,601000,100,100,.8,1'
                    page.set_input_files('#order-file',dict(name='test-orders.csv',mimeType='text/csv',buffer=csv.encode()))
                    expect(page.locator('#order-stats')).to_contain_text('累计命中率（平价计入总数）')
                    expect(page.locator('#order-stats')).to_contain_text('50%')
                    self.assertNotIn('66.67%',page.locator('#order-stats').inner_text())
                    page.click('#phone-test')
                    expect(page.locator('#phone-status')).to_contain_text('已接受测试请求')
                    sender.assert_called_once();self.assertIn('测试',sender.call_args.args[1])
                    with page.expect_response(lambda response: response.request.method=='POST' and response.url.endswith('/api/phone')) as saved:
                        page.click('#phone-save')
                    self.assertEqual(saved.value.status,200)
                    expect(page.locator('#phone-status')).to_contain_text('手机监测已启用')
                    self.assertEqual(json.loads(path.read_text())['extra'],'preserve')
                    response=page.request.get(f'http://127.0.0.1:{server.server_port}/api/signals?policy=trend_v2')
                    self.assertEqual(response.status,400)
                    page.set_viewport_size(dict(width=390,height=844))
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    Path('work').mkdir(exist_ok=True);page.screenshot(path='work/latest-mobile.png',full_page=True)
                    self.assertEqual(errors,[]);browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();app.CACHE.clear()

    def test_research_alerts_show_frequency_and_keep_source_freshness_version_guards(self):
        candles=[Candle(i*MINUTE,100,101,99,100,0) for i in range(300)]
        now=300*MINUTE
        data=analyze_latest(candles,now,price_kind='index',quote=dict(price=100,time=now))
        data.update(policy=POLICY,strategy_version=VERSION,research_mode=True,action='UP',
                    source='币安 BTCUSDT 指数',pair='BTC / USDT',quote='USDT',
                    data_age_seconds=1,quote_age_seconds=1,freshness_limit_seconds=60,
                    frequency_research=dict(window_hours=12,target=20,complete_window_count=100,
                        minimum=3,median=22,average=20.15,maximum=40,windows_at_target_pct=70.22,
                        all_windows_met=False,enough_data=True))
        data['research_validation'].update(passed=False,total=40,wins=21,losses=19,ties=0,
                    observed_pct=52.5,reason='模拟研究未达到65%，仅测试通知逻辑')
        phone=dict(configured=True,enabled=True,available=True,mode='live',payout=.8,
                   policy=POLICY,strategy_version=VERSION,research_mode=True,message='研究模式',
                   signal_activity_12h=dict(window_hours=12,target=20,candidate_count=24,
                       signal_attempts=18,accepted=17,rejected=0,unknown=1,gap=2,
                       accepted_gap=3,observation_start_ms=0,full_window=True,
                       tracking_complete=False,note='服务接受不证明手机展示。'))
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                context=browser.new_context()
                context.add_init_script('''localStorage.setItem('btc-observer-alert-preferences-v1',JSON.stringify({enabled:true,sound:false,desktop:true,auto:false}));window.notices=[];window.Notification=class {static permission='granted';constructor(title,options){notices.push({title,...options});}close(){}};''')
                page=context.new_page();page.clock.install();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/signals?**',lambda route:route.fulfill(content_type='application/json',body=json.dumps(data)))
                page.route('**/api/phone',lambda route:route.fulfill(content_type='application/json',body=json.dumps(phone)))
                page.goto(f'http://127.0.0.1:{server.server_port}')
                expect(page.locator('#phone-activity')).to_contain_text('研究方向发送尝试 18 次')
                expect(page.locator('#phone-activity')).to_contain_text('还差 2 次')
                expect(page.locator('#phone-activity-coverage')).to_contain_text('记录不完整')
                page.click('#refresh')
                expect(page.locator('#action')).to_contain_text('看涨研究信号')
                expect(page.locator('#validation-status')).to_contain_text('未验证65%')
                expect(page.locator('#frequency-status')).to_contain_text('平均 20.15 次，最少 3 次')
                expect(page.locator('#frequency-details')).to_contain_text('70.22%')
                self.assertEqual(page.evaluate('notices.length'),1)
                self.assertIn('未验证65%',page.evaluate('notices[0].title'))
                self.assertNotIn('通过验收',page.evaluate('notices[0].body'))
                # Repeated refreshes and every original real-market guard still
                # suppress alerts even when research mode is explicitly true.
                page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',data)
                for changes in (dict(policy='other'),dict(strategy_version='other'),
                                dict(price_kind='spot'),dict(demo=True),
                                dict(data_age_seconds=60),dict(quote_age_seconds=15),
                                dict(research_mode=False),dict(research_mode='true')):
                    sample={**data,'signal_time':data['signal_time']+601000,**changes}
                    page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',sample)
                self.assertEqual(page.evaluate('notices.length'),1)
                # A distinct, fresh research observation can notify without a
                # fabricated passed flag or statistical success claim.
                sample={**data,'signal_time':data['signal_time']+601000,'action':'DOWN'}
                page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',sample)
                self.assertEqual(page.evaluate('notices.length'),2)
                self.assertIn('看跌',page.evaluate('notices[1].title'))
                # An incomplete research window must never appear as achieved.
                sample['frequency_research']=dict(enough_data=False,complete_window_count=0)
                page.evaluate('(data)=>render(data)',sample)
                expect(page.locator('#frequency-status')).to_contain_text('尚无完整12小时')
                self.assertEqual(errors,[]);browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
