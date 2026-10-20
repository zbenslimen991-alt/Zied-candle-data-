#!/usr/bin/env python3
"""Regression test for the read-only historical candle inventory."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit_candle_history.py"


class CandleAuditTests(unittest.TestCase):
    def test_discovers_payload_and_counts_valid_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "history"
            tf = base / "1H"
            tf.mkdir(parents=True)
            now_ms = 1_800_000_000_000
            payload = {
                "bar": "1H",
                "candles": [[str(now_ms), "10", "12", "9", "11", "3", "3", "33", "1"]],
            }
            (tf / "BTC-USDT_1H.json").write_text(json.dumps(payload), encoding="utf-8")
            report_path = Path(tmp) / "report.json"
            env = os.environ.copy()
            env["CANDLE_HISTORY_DIR"] = str(base)
            env["CANDLE_AUDIT_REPORT"] = str(report_path)
            run = subprocess.run([sys.executable, str(AUDIT)], env=env,
                                 capture_output=True, text=True, check=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["filesFound"], 1, run.stdout)
            self.assertEqual(report["filesByTimeframe"], {"1H": 1})
            self.assertEqual(report["files"][0]["validRows"], 1)
            self.assertTrue(report["readOnly"])

    def test_reports_missing_history_directory_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "missing"
            report_path = Path(tmp) / "report.json"
            env = os.environ.copy()
            env["CANDLE_HISTORY_DIR"] = str(base)
            env["CANDLE_AUDIT_REPORT"] = str(report_path)
            subprocess.run([sys.executable, str(AUDIT)], env=env,
                           capture_output=True, text=True, check=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["filesFound"], 0)
            self.assertFalse(report["directoryExists"])
            self.assertIn("does not exist", report["emptyReason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
