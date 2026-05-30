import unittest

from nba_stats import classify_projection_confidence


class ProjectionConfidenceTests(unittest.TestCase):
    def test_stable_row(self):
        band, reasons = classify_projection_confidence(8, 20.0, 5.0, False, False)
        self.assertEqual(band, "stable")
        self.assertEqual(reasons, [])

    def test_medium_row_from_small_sample(self):
        band, reasons = classify_projection_confidence(5, 20.0, 5.0, False, False)
        self.assertEqual(band, "medium")
        self.assertIn("small_sample", reasons)

    def test_medium_row_from_minutes_fallback(self):
        band, reasons = classify_projection_confidence(8, 20.0, 5.0, True, False)
        self.assertEqual(band, "medium")
        self.assertIn("minutes_fallback", reasons)

    def test_fragile_row_from_rate_fallback(self):
        band, reasons = classify_projection_confidence(8, 20.0, 5.0, False, True)
        self.assertEqual(band, "fragile")
        self.assertIn("rate_fallback", reasons)

    def test_fragile_row_from_null_mean(self):
        band, reasons = classify_projection_confidence(8, None, None, False, False)
        self.assertEqual(band, "fragile")
        self.assertIn("high_dispersion", reasons)


if __name__ == "__main__":
    unittest.main()
