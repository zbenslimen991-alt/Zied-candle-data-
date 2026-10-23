import importlib.util
import unittest

MODULE_PATH = "okx_quant/audit_trade_ledger.py"
spec = importlib.util.spec_from_file_location("audit_trade_ledger", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TradeLedgerAuditTests(unittest.TestCase):
    def test_reconciles_reasons_from_closed_ledger(self):
        portfolio = {"closedPositions": [
            {"tradeId": "1", "strategy": "A", "exitReason": "TP_HIT",
             "entryPrice": 10, "exitPrice": 12, "profitUsd": 2, "feesUsd": 0.1},
            {"tradeId": "2", "strategy": "A", "exitReason": "SL_HIT",
             "entryPrice": 10, "exitPrice": 9, "profitUsd": -1, "feesUsd": 0.1},
            {"tradeId": "3", "strategy": "B", "exitReason": "TIME_EXIT",
             "entryPrice": 10, "exitPrice": 10.5, "profitUsd": 0.5, "feesUsd": 0.1},
        ]}
        stats = {
            "A": {"trades": 2, "wins": 1, "losses": 1, "tpHits": 1, "slHits": 1, "timeHits": 0},
            "B": {"trades": 1, "wins": 1, "losses": 0, "tpHits": 0, "slHits": 0, "timeHits": 1},
        }
        result = module.audit(portfolio, stats)
        self.assertEqual(result["reasonReconciliation"]["status"], "MATCH")
        self.assertEqual(result["ledgerTotals"]["closedTradeRecords"], 3)
        self.assertEqual(result["ledgerTotals"]["TP"], 1)
        self.assertEqual(result["ledgerTotals"]["SL"], 1)
        self.assertEqual(result["ledgerTotals"]["TIME_EXIT"], 1)
        self.assertEqual(result["qualityIssues"], {})

    def test_flags_missing_strategy_prices_pnl_and_reason(self):
        result = module.audit(
            {"closedPositions": [{"entryPrice": 10}]},
            {"A": {"trades": 0, "tpHits": 0, "slHits": 0, "timeHits": 0}},
        )
        self.assertEqual(result["reasonReconciliation"]["status"], "MISMATCH")
        self.assertGreater(result["qualityIssues"]["missing_strategy"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_entry_price"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_exit_price"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_realized_pnl"], 0)
        self.assertGreater(result["qualityIssues"]["unknown_close_reason"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
