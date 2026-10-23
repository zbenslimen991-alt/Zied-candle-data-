#!/usr/bin/env python3
"""Read-only reconciliation of closed trades, fees, and persisted strategy statistics."""
import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICE_KEYS = {
    "entry": ("entryPrice", "entry_price", "entry", "avgEntryPrice", "openPrice", "price"),
    "exit": ("exitPrice", "exit_price", "exit", "avgExitPrice", "closePrice", "closedPrice"),
}
QUANTITY_KEYS = ("units", "quantity", "qty", "baseQuantity", "filledSize", "size")
PNL_KEYS = ("profitUsd", "realizedPnl", "realized_pnl", "pnlUsd", "profit_usd", "pnl", "profit")
FEE_KEYS = ("feesUsd", "feeUsd", "fees", "fee", "tradingFees", "commission")
REASON_MAP = {
    "TP_HIT": "TP", "TAKE_PROFIT": "TP", "TP": "TP",
    "SL_HIT": "SL", "STOP_LOSS": "SL", "SL": "SL",
    "TIME_EXIT": "TIME_EXIT", "TIMEOUT": "TIME_EXIT", "TIME_EXIT_24H": "TIME_EXIT",
}
STRATEGY_STATS = ROOT / "strategy_stats_0001.json"
PORTFOLIO = ROOT / "portfolio_0001.json"

def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None

def first_value(row, keys):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None

def trade_pnl(row):
    return number(first_value(row, PNL_KEYS))

def audit(portfolio, stats):
    closed = portfolio.get("closedPositions", []) if isinstance(portfolio, dict) else []
    if not isinstance(closed, list):
        closed = []
    ledger_by_strategy = defaultdict(lambda: Counter())
    issues = Counter()
    details = []
    reasons = Counter()
    pnl_sum = 0.0
    pnl_count = 0
    recomputed_pnl_sum = 0.0
    recomputed_pnl_count = 0
    recompute_differences = []
    seen_ids = set()
    duplicate_ids = 0

    for index, row in enumerate(closed):
        if not isinstance(row, dict):
            issues["non_object_trade"] += 1
            details.append({"index": index, "status": "INVALID_TRADE_RECORD"})
            continue

        label = str(row.get("strategy") or "").strip()
        if not label:
            issues["missing_strategy"] += 1
            label = "UNKNOWN_STRATEGY"
        reason_raw = str(row.get("exitReason") or row.get("closeReason") or row.get("close_reason") or "").upper().strip()
        reason = REASON_MAP.get(reason_raw, "UNKNOWN")
        reasons[reason] += 1
        ledger_by_strategy[label]["trades"] += 1
        ledger_by_strategy[label][reason] += 1

        trade_id = first_value(row, ("tradeId", "trade_id", "id", "positionId", "position_id"))
        if trade_id is not None:
            token = str(trade_id)
            if token in seen_ids:
                duplicate_ids += 1
            seen_ids.add(token)

        entry = number(first_value(row, PRICE_KEYS["entry"]))
        exit_price = number(first_value(row, PRICE_KEYS["exit"]))
        quantity = number(first_value(row, QUANTITY_KEYS))
        if entry is None or entry <= 0: issues["missing_or_invalid_entry_price"] += 1
        if exit_price is None or exit_price <= 0: issues["missing_or_invalid_exit_price"] += 1
        if quantity is None or quantity <= 0: issues["missing_or_invalid_quantity"] += 1
        pnl = trade_pnl(row)
        if pnl is None:
            issues["missing_or_invalid_realized_pnl"] += 1
        else:
            pnl_sum += pnl
            pnl_count += 1
        fee_raw = first_value(row, FEE_KEYS)
        fee = number(fee_raw) if fee_raw is not None else None
        if fee is None or fee < 0:
            issues["fees_not_separately_recorded_or_invalid"] += 1

        # The Zied close producer defines profitUsd = (exitPrice-entryPrice)*units - feesUsd.
        # Recompute only when all source inputs exist; never subtract fees from stored PnL again.
        recomputed = None
        delta = None
        if entry is not None and entry > 0 and exit_price is not None and exit_price > 0 and quantity is not None and quantity > 0 and fee is not None and fee >= 0:
            recomputed = (exit_price - entry) * quantity - fee
            recomputed_pnl_sum += recomputed
            recomputed_pnl_count += 1
            if pnl is not None:
                delta = pnl - recomputed
                tolerance = max(0.01, abs(recomputed) * 1e-6)
                if abs(delta) > tolerance:
                    issues["stored_pnl_differs_from_recomputed_net_pnl"] += 1
                    recompute_differences.append({
                        "index": index, "tradeId": trade_id, "strategy": label,
                        "storedPnl": round(pnl, 8), "recomputedNetPnl": round(recomputed, 8),
                        "difference": round(delta, 8),
                    })
        if reason == "UNKNOWN": issues["unknown_close_reason"] += 1

        ledger_by_strategy[label]["wins"] += int(pnl is not None and pnl > 0)
        ledger_by_strategy[label]["losses"] += int(pnl is not None and pnl < 0)
        ledger_by_strategy[label]["flats"] += int(pnl is not None and pnl == 0)
        ledger_by_strategy[label]["pnlSum"] += pnl if pnl is not None else 0.0
        details.append({
            "index": index, "tradeId": trade_id, "strategy": label, "reason": reason,
            "entryPricePresent": entry is not None and entry > 0,
            "exitPricePresent": exit_price is not None and exit_price > 0,
            "quantityPresent": quantity is not None and quantity > 0,
            "realizedPnlPresent": pnl is not None, "feeFieldPresent": fee is not None and fee >= 0,
            "recomputedNetPnl": round(recomputed, 8) if recomputed is not None else None,
            "storedMinusRecomputedPnl": round(delta, 8) if delta is not None else None,
            "tradeIdPresent": trade_id is not None,
        })

    stats = stats if isinstance(stats, dict) else {}
    stats_totals = Counter()
    stats_by_strategy = {}
    for label, stat in stats.items():
        if not isinstance(stat, dict):
            issues["invalid_strategy_stat_record"] += 1
            continue
        row = {
            "trades": int(number(stat.get("trades")) or 0),
            "wins": int(number(stat.get("wins")) or 0),
            "losses": int(number(stat.get("losses")) or 0),
            "TP": int(number(stat.get("tpHits")) or 0),
            "SL": int(number(stat.get("slHits")) or 0),
            "TIME_EXIT": int(number(stat.get("timeHits")) or 0),
            "pnlSum": number(stat.get("pnlSum")),
        }
        stats_by_strategy[label] = row
        for key in ("trades", "wins", "losses", "TP", "SL", "TIME_EXIT"):
            stats_totals[key] += row[key]

    ledger_totals = {
        "closedTradeRecords": len(closed), "TP": reasons["TP"], "SL": reasons["SL"],
        "TIME_EXIT": reasons["TIME_EXIT"], "unknownCloseReason": reasons["UNKNOWN"],
        "realizedPnlRecords": pnl_count, "storedRealizedPnlSum": round(pnl_sum, 8),
        "recomputedNetPnlRecords": recomputed_pnl_count,
        "recomputedNetPnlSum": round(recomputed_pnl_sum, 8),
    }
    mismatch = []
    for key in ("TP", "SL", "TIME_EXIT"):
        if stats_totals[key] != reasons[key]:
            mismatch.append({"metric": key, "statsCount": stats_totals[key],
                             "ledgerCount": reasons[key], "difference": stats_totals[key] - reasons[key]})
    per_strategy_mismatch = []
    reconstructed_stats = {}
    for label, counts in sorted(ledger_by_strategy.items()):
        row = dict(counts)
        row["pnlSum"] = round(float(row.get("pnlSum", 0.0)), 8)
        reconstructed_stats[label] = row
        stored = stats_by_strategy.get(label)
        if stored is None:
            per_strategy_mismatch.append({"strategy": label, "problem": "missing_from_saved_stats", "ledgerTrades": row["trades"]})
            continue
        for metric, ledger_key in (("trades", "trades"), ("wins", "wins"), ("losses", "losses"),
                                   ("tpHits", "TP"), ("slHits", "SL"), ("timeHits", "TIME_EXIT")):
            saved_value = stored["TP"] if metric == "tpHits" else stored["SL"] if metric == "slHits" else stored["TIME_EXIT"] if metric == "timeHits" else stored[metric]
            if saved_value != row.get(ledger_key, 0):
                per_strategy_mismatch.append({
                    "strategy": label, "metric": metric, "saved": saved_value,
                    "ledger": row.get(ledger_key, 0),
                })
        if stored["pnlSum"] is not None and abs(stored["pnlSum"] - row["pnlSum"]) > max(0.01, abs(row["pnlSum"]) * 1e-6):
            per_strategy_mismatch.append({"strategy": label, "metric": "pnlSum",
                                          "saved": stored["pnlSum"], "ledger": row["pnlSum"]})
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(), "readOnly": True,
        "source": {"portfolio": str(PORTFOLIO), "strategyStats": str(STRATEGY_STATS)},
        "ledgerTotals": ledger_totals, "strategyStatsTotals": dict(stats_totals),
        "reasonReconciliation": {
            "status": "MATCH" if not mismatch and reasons["UNKNOWN"] == 0 else "MISMATCH",
            "mismatches": mismatch,
        },
        "qualityIssues": dict(issues), "duplicateTradeIds": duplicate_ids,
        "strategiesInLedger": len(ledger_by_strategy), "strategiesInStats": len(stats_by_strategy),
        "ledgerByStrategy": {label: dict(counts) for label, counts in sorted(ledger_by_strategy.items())},
        "reconstructedStrategyStatsFromLedger": reconstructed_stats,
        "perStrategyStatsMismatches": per_strategy_mismatch,
        "pnlReconciliation": {
            "status": "MATCH" if recompute_differences == [] and recomputed_pnl_count == pnl_count else "MISMATCH_OR_INCOMPLETE",
            "recomputedTrades": recomputed_pnl_count, "storedPnlTrades": pnl_count,
            "mismatches": recompute_differences,
        },
        "statsByStrategy": stats_by_strategy, "tradeDetails": details,
        "importantNote": "Recomputed net PnL uses (exitPrice-entryPrice)*units-feesUsd, matching Zied's close producer. Fees are not subtracted a second time from stored profitUsd. Reconstructed statistics are report-only and do not overwrite saved weights or statistics.",
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio", default=str(PORTFOLIO))
    parser.add_argument("--stats", default=str(STRATEGY_STATS))
    parser.add_argument("--output", default="trade_ledger_audit_report.json")
    args = parser.parse_args()
    report = audit(load_json(Path(args.portfolio)), load_json(Path(args.stats)))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(output), "ledgerTotals": report["ledgerTotals"],
        "reasonReconciliation": report["reasonReconciliation"],
        "pnlReconciliation": report["pnlReconciliation"],
        "perStrategyStatsMismatches": len(report["perStrategyStatsMismatches"]),
        "qualityIssues": report["qualityIssues"], "readOnly": True,
    }, ensure_ascii=False))
    return 1 if report["reasonReconciliation"]["status"] != "MATCH" or report["qualityIssues"] or report["pnlReconciliation"]["status"] != "MATCH_OR_INCOMPLETE" else 0

if __name__ == "__main__":
    raise SystemExit(main())
