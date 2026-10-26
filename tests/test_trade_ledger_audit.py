import importlib.util
import unittest

MODULE_PATH = "okx_quant/audit_trade_ledger.py"
spec = importlib.util.spec_from_file_location("audit_trade_ledger", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TradeLedgerAuditTests(unittest.TestCase):
    def test_reconciles_reasons_and_recomputes_net_pnl(self):
        portfolio = {"closedPositions": [
            {"tradeId": "1", "strategy": "A", "exitReason": "TP_HIT",
             "entryPrice": 10, "exitPrice": 12, "units": 1, "profitUsd": 1.9, "feesUsd": 0.1},
            {"tradeId": "2", "strategy": "A", "exitReason": "SL_HIT",
             "entryPrice": 10, "exitPrice": 9, "units": 1, "profitUsd": -1.1, "feesUsd": 0.1},
            {"tradeId": "3", "strategy": "B", "exitReason": "TIME_EXIT",
             "entryPrice": 10, "exitPrice": 10.5, "units": 1, "profitUsd": 0.4, "feesUsd": 0.1},
        ]}
        stats = {
            "A": {"trades": 2, "wins": 1, "losses": 1, "pnlSum": 0.8,
                  "tpHits": 1, "slHits": 1, "timeHits": 0},
            "B": {"trades": 1, "wins": 1, "losses": 0, "pnlSum": 0.4,
                  "tpHits": 0, "slHits": 0, "timeHits": 1},
        }
        result = module.audit(portfolio, stats)
        self.assertEqual(result["reasonReconciliation"]["status"], "MATCH")
        self.assertEqual(result["pnlReconciliation"]["status"], "MATCH")
        self.assertEqual(result["ledgerTotals"]["closedTradeRecords"], 3)
        self.assertEqual(result["ledgerTotals"]["TP"], 1)
        self.assertEqual(result["ledgerTotals"]["SL"], 1)
        self.assertEqual(result["ledgerTotals"]["TIME_EXIT"], 1)
        self.assertAlmostEqual(result["ledgerTotals"]["recomputedNetPnlSum"], 1.2)
        self.assertEqual(result["qualityIssues"], {})
        self.assertEqual(result["perStrategyStatsMismatches"], [])

    def test_flags_missing_strategy_prices_quantity_pnl_fees_and_reason(self):
        # This fixture intentionally omits all trade metadata so each quality check is exercised.
        result = module.audit(
            {"closedPositions": [{}]},
            {"A": {"trades": 0, "tpHits": 0, "slHits": 0, "timeHits": 0}},
        )
        self.assertEqual(result["reasonReconciliation"]["status"], "MISMATCH")
        self.assertGreater(result["qualityIssues"]["missing_strategy"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_entry_price"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_exit_price"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_quantity"], 0)
        self.assertGreater(result["qualityIssues"]["missing_or_invalid_realized_pnl"], 0)
        self.assertGreater(result["qualityIssues"]["fees_not_separately_recorded_or_invalid"], 0)
        self.assertGreater(result["qualityIssues"]["unknown_close_reason"], 0)

    def test_detects_stored_pnl_mismatch(self):
        result = module.audit(
            {"closedPositions": [{
                "tradeId": "1", "strategy": "A", "exitReason": "TP_HIT",
                "entryPrice": 10, "exitPrice": 12, "units": 2,
                "feesUsd": 0.2, "profitUsd": 5.0,
            }]},
            {"A": {"trades": 1, "wins": 1, "losses": 0, "pnlSum": 5.0,
                   "tpHits": 1, "slHits": 0, "timeHits": 0}},
        )
        self.assertEqual(result["pnlReconciliation"]["status"], "MISMATCH_OR_INCOMPLETE")
        self.assertEqual(result["qualityIssues"]["stored_pnl_differs_from_recomputed_net_pnl"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
