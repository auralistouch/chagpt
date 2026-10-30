import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from phone import PhoneMonitor, BARK_ENDPOINT, device_key, send_bark

FAKE_KEY = 'test-device-key-not-a-real-recipient'
STAMP = 1_793_332_000_000


def observation(**overrides):
    return dict(action='UP', demo=False, signal_time=STAMP, data_age_seconds=10,
                source='币安现货', pair='BTC / USDT', reference_price=70000, **overrides)


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'local'/'phone.json'
        self.reporter = Mock(return_value=observation())
        self.sender = Mock()
        self.monitor = PhoneMonitor(self.path,self.reporter,self.sender)

    def tearDown(self):
        self.monitor.stop(); self.temp.cleanup()

    def configure(self, **values):
        return self.monitor.configure(dict(key=FAKE_KEY, enabled=True, mode='live', payout=.8, **values))

    def test_key_url_parsing_cannot_redirect_provider(self):
        self.assertEqual(device_key('https://api.day.app/'+FAKE_KEY+'/example'),FAKE_KEY)
        for value in ['http://api.day.app/'+FAKE_KEY, 'https://evil.example/'+FAKE_KEY, 'https://api.day.app.evil.example/'+FAKE_KEY, 'bad key', 'short']:
            with self.assertRaises(ValueError): device_key(value)

    def test_disabled_by_default_and_status_never_exposes_key(self):
        self.monitor.check_once();self.reporter.assert_not_called()
        self.configure()
        self.assertNotIn(FAKE_KEY,json.dumps(self.monitor.status()))
        self.assertEqual(os.stat(self.path).st_mode & 0o777,0o600)
        self.assertTrue(PhoneMonitor(self.path,self.reporter,self.sender).status()['enabled'])

    def test_real_signal_only_and_deduplication_survives_restart(self):
        self.configure();self.monitor.check_once();self.monitor.check_once()
        self.sender.assert_called_once()
        restored=PhoneMonitor(self.path,self.reporter,self.sender)
        restored.check_once();self.sender.assert_called_once()
        self.reporter.return_value={**observation(),'signal_time':STAMP+600000}
        restored.check_once();self.assertEqual(self.sender.call_count,2)

    def test_wait_demo_old_and_invalid_data_are_suppressed(self):
        self.configure()
        for values in [dict(action='WAIT'),dict(demo=True),dict(data_age_seconds=75),dict(data_age_seconds=-1),dict(data_age_seconds=float('nan'))]:
            self.reporter.return_value={**observation(),**values}
            self.monitor.check_once()
        self.sender.assert_not_called()

    def test_ambiguous_push_timeout_is_not_repeated(self):
        self.configure();self.sender.side_effect=RuntimeError('cannot confirm')
        self.monitor.check_once();self.monitor.check_once()
        self.sender.assert_called_once()
        self.assertIsNone(self.monitor.status()['last_push'])

    def test_provider_failure_and_stopping_prevent_sends(self):
        self.configure();self.reporter.side_effect=RuntimeError('unavailable')
        self.monitor.check_once();self.sender.assert_not_called()
        self.monitor.configure(dict(enabled=False,mode='live',payout=.8))
        self.reporter.reset_mock();self.monitor.check_once();self.reporter.assert_not_called()

    def test_test_notification_is_labelled_and_does_not_consume_signal(self):
        self.configure();result=self.monitor.test()
        self.assertIn('测试',self.sender.call_args.args[1])
        self.assertIn('实际收到',result['message'])
        self.monitor.check_once();self.assertEqual(self.sender.call_count,2)

    def test_no_demo_monitor_and_failed_save_keeps_old_configuration(self):
        self.configure()
        for values in [dict(enabled=True,mode='demo',payout=.8),dict(enabled=True,mode='live',payout=float('nan'))]:
            with self.assertRaises(ValueError):self.monitor.configure(values)
        with patch.object(self.monitor,'_save',side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):self.monitor.configure(dict(enabled=False))
        self.assertTrue(self.monitor.status()['enabled'])

    def test_background_thread_runs_without_browser(self):
        self.configure();event=threading.Event()
        self.monitor.interval=.02
        self.sender.side_effect=lambda *args:event.set()
        self.monitor.start()
        self.assertTrue(event.wait(2))
        self.assertTrue(self.monitor.status()['running'])

    def test_bark_post_keeps_key_out_of_url_and_masks_remote_errors(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('phone.urlopen',return_value=response) as call,patch('phone.json.load',return_value={'code':200}):
            send_bark(FAKE_KEY,'test','not a signal')
        request=call.call_args.args[0]
        self.assertEqual(request.full_url,BARK_ENDPOINT)
        self.assertNotIn(FAKE_KEY,request.full_url)
        self.assertEqual(json.loads(request.data)['device_key'],FAKE_KEY)
        with patch('phone.urlopen',side_effect=OSError(FAKE_KEY)):
            with self.assertRaises(RuntimeError) as error:send_bark(FAKE_KEY,'test','body')
            self.assertNotIn(FAKE_KEY,str(error.exception))


class PhoneHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.sender=Mock()
        cls.monitor=PhoneMonitor(Path(cls.temp.name)/'phone.json',Mock(return_value=observation()),cls.sender)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.server.phone_monitor=cls.monitor
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def post(self,path,body,origin=None,content_type='application/json'):
        request=Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':content_type,'Origin':origin or self.url},method='POST')
        return urlopen(request,timeout=5)

    def test_same_origin_save_and_explicit_test_never_echo_key(self):
        with self.post('/api/phone',dict(key=FAKE_KEY,enabled=False,mode='live',payout=.8)) as response:
            result=json.load(response)
        self.assertTrue(result['configured']);self.assertNotIn(FAKE_KEY,json.dumps(result))
        with self.post('/api/phone/test',{}) as response:self.assertIn('已接受',json.load(response)['message'])
        self.sender.assert_called()

    def test_cross_origin_form_and_missing_key_rejected(self):
        for origin,content in [('https://evil.example','application/json'),(self.url,'text/plain')]:
            with self.assertRaises(HTTPError) as error:self.post('/api/phone',{},origin,content)
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:self.post('/api/phone',dict(enabled=True,mode='demo'))
        self.assertEqual(error.exception.code,400)


if __name__=='__main__':unittest.main()
