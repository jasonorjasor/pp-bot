import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_JS_PATH = REPO_ROOT / "src" / "js" / "index.js"


class ProjectionSerializationTests(unittest.TestCase):
    def test_build_posted_alert_record_persists_projection_fields(self):
        script = r"""
const { buildPostedAlertRecord } = require(%s);
const record = buildPostedAlertRecord({
  propId: '123',
  playerName: 'Test Player',
  attr: {
    line_score: 10.5,
    stat_type: 'Points',
    description: 'LAL',
    start_time: '2026-04-01T00:00:00.000Z',
  },
  lineChangeText: 'New',
  decision: { recommendation: 'over', tier: 'best_bet', score: 8.1 },
  analytics: {
    sampleSize: 10,
    hitSampleSize: 10,
    mean: 12,
    median: 12,
    stdDev: 3,
    edge: 1,
    edgePct: 10,
    overHitRate: 60,
    underHitRate: 40,
    overWeightedPct: 65,
    underWeightedPct: 35,
    overScore: 7.5,
    underScore: 4.5,
    baseScore: 7.5,
    finalScore: 8.1,
    minutesBaseline: 30,
    minutesRegimeCounts: { full: 8, limited: 2, dnp: 0 },
    dnpRateWeighted: 0,
    limitedRateWeighted: 10,
    roleRiskPenalty: 0.2,
    projectionMethod: 'scaled_rate_empirical',
    projectionConfidenceBand: 'medium',
    projectionFamilyStatus: 'watch_only',
    projectionMinutes: 32,
    projectionRate: 0.35,
    projectionMean: 11.2,
    projectionStd: 3.4,
    projectionSampleSizeFull: 8,
    projectionSampleSizeLimited: 2,
    pOverFull: 0.6,
    pUnderFull: 0.4,
    pOverAdjusted: 0.58,
    pUnderAdjusted: 0.42,
    pVoid: 0,
    projectionLowConfidence: true,
    projectionConfidenceReasons: ['small_sample'],
    projectionInputs: {
      minutesLast5: 31,
      minutesLast10: 30,
      minutesSampleFull: 30,
      recentRate: 0.34,
      sampleFullRate: 0.36,
    },
  },
});
process.stdout.write(JSON.stringify(record));
""" % json.dumps(str(INDEX_JS_PATH))
        result = subprocess.run(
            ["node", "-e", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload_line = [line for line in result.stdout.splitlines() if line.strip()][-1]
        payload = json.loads(payload_line)
        analytics = payload["analytics"]

        self.assertEqual(analytics["projectionConfidenceBand"], "medium")
        self.assertEqual(analytics["projectionFamilyStatus"], "watch_only")
        self.assertTrue(analytics["projectionLowConfidence"])
        self.assertEqual(analytics["projectionConfidenceReasons"], ["small_sample"])
        self.assertEqual(payload["recommendedSide"], "over")


if __name__ == "__main__":
    unittest.main()
