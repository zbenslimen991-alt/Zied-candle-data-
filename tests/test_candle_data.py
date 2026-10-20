import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "refresh_candles.py"
spec = importlib.util.spec_from_file_location("refresh_candles", MODULE_PATH)
refresh_candles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh_candles)


class CandleProducerTests(unittest.TestCase):
    def test_read_existing_accepts_okx_array_rows_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTC-USDT_1H.json"
            path.write_text(json.dumps({"candles": [
                [1700003600000, "101", "104", "100", "103", "5", "5", "515", "1"],
                [1700000000000, "99", "102", "98", "101", "4", "4", "404", "1"],
                [1700003600000, "101", "105", "100", "104", "6", "6", "624", "1"]
            ]}), encoding="utf-8")
            rows = refresh_candles.read_existing(path)
            self.assertEqual(set(rows), {"1700000000000", "1700003600000"})
            self.assertEqual(rows["1700003600000"][4], "104")
            self.assertEqual(rows["1700003600000"][7], "624")

    def test_read_existing_handles_missing_or_invalid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertEqual(refresh_candles.read_existing(path), {})
            path.write_text("{broken json", encoding="utf-8")
            self.assertEqual(refresh_candles.read_existing(path), {})

    def test_candle_payload_has_expected_okx_schema_and_sorted_rows(self):
        rows = {
            "1700003600000": [1700003600000, "101", "104", "100", "103", "5", "5", "515", "1"],
            "1700000000000": [1700000000000, "99", "102", "98", "101", "4", "4", "404", "1"]
        }
        payload = refresh_candles.candle_payload("BTC-USDT", "1H", rows)
        self.assertEqual(payload["exchange"], "OKX")
        self.assertEqual(payload["market"], "SPOT")
        self.assertEqual(payload["instId"], "BTC-USDT")
        self.assertEqual(payload["bar"], "1H")
        self.assertEqual(payload["columns"][0:5], ["ts", "open", "high", "low", "close"])
        self.assertEqual([row[0] for row in payload["candles"]], [1700000000000, 1700003600000])

if __name__ == "__main__":
    unittest.main(verbosity=2)
