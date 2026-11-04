import unittest
from order_review import review_csv
HEADER='order_id,direction,open_time_ms,expiry_time_ms,open_index,close_index,net_payout,stake\n'
class OrderReviewTests(unittest.TestCase):
    def test_single_point_decimal_ties_and_missing_separate(self):
        rows=['a,UP,1000,601000,70000.00000001,70000.00000002,.75,10',
              'b,DOWN,1000,601000,100,101,.8,5','c,UP,1000,601000,100,100,.8,10',
              'd,UP,1000,601000,100,,.8,10']
        result=review_csv(HEADER+'\n'.join(rows))
        self.assertEqual([result[k] for k in ['wins','losses','ties','unverified']],[1,1,1,1])
        self.assertEqual(result['decisive_win_rate_pct'],50)
        self.assertEqual(result['profit_usdt'],'2.50')
        self.assertIsNone(result['trades'][-1]['profit'])
        self.assertEqual(result['trades'][0]['entry'],'70000.00000001')
        self.assertFalse(result['verified_by_exchange'])
    def test_invalid_and_duplicate_orders_rejected(self):
        base='a,UP,1000,601000,100,101,.8,10'
        for csv in [HEADER+base+'\n'+base,HEADER+base.replace('601000','600000'),HEADER+base.replace(',100,',',NaN,'),HEADER+base.replace(',UP,',',BAD,'),HEADER+base.replace(',.8,',',-1,')]:
            with self.assertRaises(ValueError):review_csv(csv)
    def test_all_missing_has_no_win_rate(self):
        result=review_csv(HEADER+'a,UP,1000,601000,,,0.8,10')
        self.assertIsNone(result['decisive_win_rate_pct']);self.assertEqual(result['completed'],0)
        self.assertEqual(result['profit_usdt'],'0')

class OrderReviewHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app,threading
        from http.server import ThreadingHTTPServer
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def post(self,csv,origin=None):
        import json
        from urllib.request import Request,urlopen
        req=Request(self.url+'/api/order-review',data=json.dumps({'csv':csv}).encode(),headers={'Content-Type':'application/json','Origin':origin or self.url},method='POST')
        return urlopen(req,timeout=10)
    def test_review_works_without_phone_and_cross_origin_is_denied(self):
        import json
        from urllib.error import HTTPError
        with self.post(HEADER+'a,UP,1000,601000,100,101,.8,10') as response:
            data=json.load(response)
        self.assertEqual(data['wins'],1)
        with self.assertRaises(HTTPError) as error:self.post(HEADER,'http://evil.example')
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:self.post('not order data')
        self.assertEqual(error.exception.code,400)

if __name__=='__main__':unittest.main()
