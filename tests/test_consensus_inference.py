import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

import consensus_inference as ci
from strategy import Candle, MINUTE

START=ci.load_bundle()['metadata']['calibration_end_exclusive']


class ConsensusInferenceTests(unittest.TestCase):
    def candles(self):
        return [Candle(START+i*MINUTE,100+i*.01,100.1+i*.01,99.9+i*.01,100.02+i*.01,0) for i in range(180)]

    def test_disagreeing_models_cannot_send_even_when_both_are_strong(self):
        with patch.object(ci,'scores',return_value={'hgb':.9,'extra_trees':.1}):
            self.assertFalse(any(r['qualified'] for r in ci.build_features(self.candles())))

    def test_agreement_alone_is_not_enough_and_no_win_history_is_required(self):
        with patch.object(ci,'scores',return_value={'hgb':.51,'extra_trees':.51}):
            self.assertFalse(any(r['qualified'] for r in ci.build_features(self.candles())))
        with patch.object(ci,'scores',return_value={'hgb':.9,'extra_trees':.9}):
            result=ci.build_features(self.candles())
        self.assertTrue(result[-1]['qualified'])
        self.assertEqual(result[-1]['direction'],'UP')
        self.assertFalse(result[-1]['score_is_win_probability'])

    def test_missing_or_tampered_model_fails_without_a_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            missing=Path(folder)/'missing.b85'
            with self.assertRaisesRegex(ValueError,'模型文件缺失'):
                ci.load_bundle(str(missing))
            original=Path(ci.__file__).with_name(ci.BUNDLE_NAME).read_bytes()
            envelope=json.loads(zlib.decompress(base64.b85decode(original)))
            envelope['model']['hgb']['baseline']+=.1
            missing.write_bytes(base64.b85encode(zlib.compress(json.dumps(envelope).encode())))
            with self.assertRaisesRegex(ValueError,'模型校验失败'):
                ci.load_bundle(str(missing))

    def test_standard_library_inference_matches_independent_sklearn_golden_values(self):
        samples=json.loads(Path(__file__).with_name('fixtures').joinpath('consensus_scores.json').read_text())
        self.assertGreaterEqual(len(samples),8)
        for row in samples:
            actual=ci.scores(row['values'])
            for name,expected in row['expected'].items():
                self.assertAlmostEqual(actual[name],expected,places=12)


if __name__=='__main__':unittest.main()
