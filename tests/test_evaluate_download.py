import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import evaluate
from history_download import atomic_text
from market import MarketError

class EvaluateDownloadTests(unittest.TestCase):
    def test_offline_input_writes_complete_report_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'index.csv';output=Path(folder)/'report.json'
            with source.open('w',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(['time_ms','open','high','low','close'])
                writer.writerows((i*60000,100,101,99,100) for i in range(300))
            with patch.object(sys,'argv',['evaluate.py','--input',str(source),'--output',str(output)]),patch('evaluate.download_index_history',side_effect=AssertionError('must stay offline')),contextlib.redirect_stdout(io.StringIO()):
                evaluate.main()
            result=json.loads(output.read_text())
            self.assertFalse(result['actual_order_results'])
            self.assertIsNone(result['download_window'])
            self.assertEqual(result['inputs_sha256']['candles'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertIn('generated_at_utc',result)

    def test_failed_download_does_not_create_or_overwrite_report(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            for existing in (False,True):
                if existing:output.write_text('previous-complete-report')
                with patch.object(sys,'argv',['evaluate.py','--output',str(output)]),patch('evaluate.download_index_history',side_effect=MarketError('断流','incomplete_response','币安指数')),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(MarketError):
                    evaluate.main()
                if existing:self.assertEqual(output.read_text(),'previous-complete-report')
                else:self.assertFalse(output.exists())

    def test_atomic_replace_failure_keeps_previous_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json';output.write_text('previous-valid-report')
            with patch('history_download.os.replace',side_effect=OSError('disk error')),self.assertRaises(OSError):
                atomic_text(output,'new-report')
            self.assertEqual(output.read_text(),'previous-valid-report')
            self.assertEqual(list(Path(folder).iterdir()),[output])

if __name__=='__main__':unittest.main()
