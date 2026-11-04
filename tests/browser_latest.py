"""Latest-only local UI tests. Mock quotes/Bark; historical rates never gate direction."""
import json,shutil,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from strategy import Candle,MINUTE
from latest_strategy import POLICY,VERSION
from latest_phone import LatestPhoneMonitor
from playwright.sync_api import sync_playwright,expect

def observation(action='WAIT'):
    stamp=1_793_800_000_000
    reference=dict(total=107,wins=60,losses=47,ties=0,unverified=0,profit_units=1,
                   from_time=stamp-86400000,to_time=stamp,trades=[],unverified_trades=[])
    return dict(policy=POLICY,strategy_version=VERSION,research_mode=True,action=action,
                demo=False,price_kind='index',source='币安 BTCUSDT 指数',pair='BTC / USDT',quote='USDT',
                signal_time=stamp,reference_price=78000,reference_entry_time=stamp+MINUTE,
                reference_expiry_time=stamp+11*MINUTE,estimated_expiry=stamp+600000,
                data_age_seconds=1,quote_age_seconds=1,freshness_limit_seconds=60,
                setup='双模型行情同向',metrics=dict(score=2,checks=[],reasons=[],qualified=action!='WAIT'),
                display_metrics=[],blocker_codes=[],blockers=[],warnings=[],
                quality_assessment=dict(status='research_only',note='方向来自当前行情，累计结果只作回顾。',
                                        test_samples=107,test_win_rate_pct=56.07),
                reference_study=dict(early=None,holdout=reference),
                chart=[dict(time=stamp-MINUTE,price=77990),dict(time=stamp,price=78000)])


class LatestBrowserTests(unittest.TestCase):
    def test_latest_only_waits_and_bark_test_still_works(self):
        data=observation()
        with tempfile.TemporaryDirectory() as folder,patch('app.report',return_value=data):
            app.CACHE.clear();sender=Mock();reporter=Mock(return_value=data)
            path=Path(folder)/'phone.json'
            path.write_text(json.dumps(dict(key='fake-local-browser-key',enabled=True,mode='live',payout=.8,policy='latest_v5',last_signal_time=0,extra='preserve')))
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
                    expect(page.locator('#quality-details')).to_contain_text('56.07%')
                    self.assertEqual(page.locator('#validation-status').count(),0)
                    self.assertEqual(page.locator('#frequency-status').count(),0)
                    self.assertEqual(page.evaluate('notices.length'),0)
                    body=page.locator('body').inner_text()
                    for old in ('v2','v3','v4','v5','旧版','pullback_v3','trend_v2','严格统计过滤','65%','最近100笔','目标约20次'):self.assertNotIn(old,body)
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
                    response=page.request.get(f'http://127.0.0.1:{server.server_port}/api/signals?policy=latest_v5')
                    self.assertEqual(response.status,400)
                    page.set_viewport_size(dict(width=390,height=844))
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    Path('work').mkdir(exist_ok=True);page.screenshot(path='work/latest-mobile.png',full_page=True)
                    self.assertEqual(errors,[]);browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();app.CACHE.clear()

    def test_current_market_alerts_ignore_historical_rate_and_keep_original_guards(self):
        data=observation('UP')
        phone=dict(configured=True,enabled=True,available=True,mode='live',payout=.8,
                   policy=POLICY,strategy_version=VERSION,research_mode=True,message='研究模式',
                   signal_activity_12h=dict(window_hours=12,candidate_count=24,
                       signal_attempts=18,accepted=17,rejected=0,unknown=1,
                       observation_start_ms=0,full_window=True,
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
                expect(page.locator('#phone-activity')).to_contain_text('Bark接受 17 次')
                self.assertNotIn('还差',page.locator('#phone-activity').inner_text())
                expect(page.locator('#phone-activity-coverage')).to_contain_text('记录不完整')
                page.click('#refresh')
                expect(page.locator('#action')).to_contain_text('看涨研究信号')
                expect(page.locator('#quality-details')).to_contain_text('已完成输出样本 107')
                expect(page.locator('#quality-details')).to_contain_text('56.07%')
                expect(page.locator('#stats')).to_contain_text('56.07%')
                self.assertEqual(page.locator('#validation-status').count(),0)
                self.assertEqual(page.locator('#frequency-status').count(),0)
                self.assertEqual(page.evaluate('notices.length'),1)
                self.assertIn('研究方向',page.evaluate('notices[0].title'))
                self.assertIn('准确率待持续验证',page.evaluate('notices[0].body'))
                self.assertNotIn('65%',page.evaluate('notices[0].body'))
                # Same-minute rechecks remain deduplicated. Historical outcome
                # fields never authorize a wrong source, version, or stale quote.
                page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',data)
                for changes in (dict(policy='latest_v5'),dict(strategy_version='activity_v5.0'),
                                dict(price_kind='spot'),dict(demo=True),
                                dict(data_age_seconds=60),dict(quote_age_seconds=15),
                                dict(research_mode=False),dict(research_mode='true'),dict(research_mode=1)):
                    sample={**data,'signal_time':data['signal_time']+601000,**changes}
                    page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',sample)
                self.assertEqual(page.evaluate('notices.length'),1)
                # Missing retrospective assessment is not an entry restriction.
                sample={**data,'signal_time':data['signal_time']+601000,'action':'DOWN'}
                sample.pop('quality_assessment')
                page.evaluate('(data)=>render(data)',sample)
                expect(page.locator('#action')).to_contain_text('看跌研究信号')
                page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',sample)
                self.assertEqual(page.evaluate('notices.length'),2)
                # Even an observed zero percent does not become an invented
                # historical-rate gate for a new current-market observation.
                sample={**sample,'signal_time':sample['signal_time']+601000,'action':'UP',
                        'quality_assessment':dict(status='research_only',note='模拟零命中回顾，仅测试无历史准入。',
                                                  test_samples=3,test_win_rate_pct=0)}
                page.evaluate('(data)=>render(data)',sample)
                expect(page.locator('#action')).to_contain_text('看涨研究信号')
                expect(page.locator('#quality-details')).to_contain_text('累计命中率 0%')
                page.evaluate('(data)=>window.signalAlerts.maybeNotify(data)',sample)
                self.assertEqual(page.evaluate('notices.length'),3)
                self.assertEqual(errors,[]);browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
