import unittest
from frequency_research import rolling_frequency, MINUTE, WINDOW_MS


class FrequencyTests(unittest.TestCase):
    def test_twenty_per_window_is_not_daily_average(self):
        # Forty signals clustered in the first half of a whole day.
        result = rolling_frequency([i*MINUTE for i in range(1, 41)], 0, 2*WINDOW_MS)
        self.assertEqual(result['minimum'], 0)
        self.assertFalse(result['all_windows_met'])
        self.assertEqual(result['maximum'], 40)

    def test_cadence_and_half_open_boundary(self):
        times = [i*36*MINUTE for i in range(41)]
        result = rolling_frequency(times, 0, 2*WINDOW_MS)
        self.assertEqual(result['minimum'], 20)
        self.assertEqual(result['maximum'], 20)
        self.assertEqual(result['complete_window_count'], 721)
        self.assertTrue(result['all_windows_met'])

    def test_partial_window_and_duplicate_signals(self):
        result = rolling_frequency([], 0, WINDOW_MS-1)
        self.assertFalse(result['enough_data'])
        self.assertIsNone(result['minimum'])
        self.assertFalse(result['all_windows_met'])
        result = rolling_frequency([MINUTE, MINUTE], 0, WINDOW_MS)
        self.assertEqual(result['minimum'], 1)


if __name__ == '__main__':
    unittest.main()
