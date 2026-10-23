#!/usr/bin/env python3
"""Read-only reconciliation of closed trade ledger vs strategy statistics.

This audit never changes portfolio, strategy weights, trades, or trading rules.
"""
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
    closed = portfolio.get("closedPositions", [])
    if not isinstance(closed, list):
        closed = []
    ledger_by_strategy = defaultdict(lambda: Counter())
    issues = Counter()
    details = []
    reasons = Counter()
    pnl_sum = 0.0
    pnl_count = 0
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
        if entry is None or entry <= 0:
            issues["missing_or_invalid_entry_price"] += 1
        if exit_price is None or exit_price <= 0:
            issues["missing_or_invalid_exit_price"] += 1
        pnl = trade_pnl(row)
        if pnl is None:
            issues["missing_or_invalid_realized_pnl"] += 1
        else:
            pnl_sum += pnl
            pnl_count += 1
        fee_raw = first_value(row, FEE_KEYS)
        fee = number(fee_raw) if fee_raw is not None else None
        if fee is None:
            issues["fees_not_separately_recorded"] += 1
        elif pnl is not None:
            # Report only; do not subtract fees again because stored PnL may already be net.
            pass
        if reason == "UNKNOWN":
            issues["unknown_close_reason"] += 1

        details.append({
            "index": index, "strategy": label, "reason": reason,
            "entryPricePresent": entry is not None and entry > 0,
            "exitPricePresent": exit_price is not None and exit_price > 0,
            "realizedPnlPresent": pnl is not None,
            "feeFieldPresent": fee is not None,
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
        "closedTradeRecords": len(closed),
        "TP": reasons["TP"],
        "SL": reasons["SL"],
        "TIME_EXIT": reasons["TIME_EXIT"],
        "unknownCloseReason": reasons["UNKNOWN"],
        "realizedPnlRecords": pnl_count,
        "realizedPnlSum": round(pnl_sum, 8),
    }
    mismatch = []
    for key, stat_key in (("TP", "TP"), ("SL", "SL"), ("TIME_EXIT", "TIME_EXIT")):
        if stats_totals[stat_key] != reasons[stat_key]:
            mismatch.append({
                "metric": key, "statsCount": stats_totals[stat_key],
                "ledgerCount": reasons[stat_key],
                "difference": stats_totals[stat_key] - reasons[stat_key],
            })
    ledger_strategy_summary = {
        label: dict(counts) for label, counts in sorted(ledger_by_strategy.items())
    }
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "readOnly": True,
        "source": {"portfolio": str(PORTFOLIO), "strategyStats": str(STRATEGY_STATS)},
        "ledgerTotals": ledger_totals,
        "strategyStatsTotals": dict(stats_totals),
        "reasonReconciliation": {
            "status": "MATCH" if not mismatch and reasons["UNKNOWN"] == 0 else "MISMATCH",
            "mismatches": mismatch,
        },
        "qualityIssues": dict(issues),
        "duplicateTradeIds": duplicate_ids,
        "strategiesInLedger": len(ledger_by_strategy),
        "strategiesInStats": len(stats_by_strategy),
        "ledgerByStrategy": ledger_strategy_summary,
        "statsByStrategy": stats_by_strategy,
        "tradeDetails": details,
        "importantNote": "PnL source fields are reported as stored. Fees are not subtracted again because the stored realized PnL may already be net; verify the producer's accounting before deriving net PnL.",
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
        "report": str(output),
        "ledgerTotals": report["ledgerTotals"],
        "strategyStatsTotals": report["strategyStatsTotals"],
        "reasonReconciliation": report["reasonReconciliation"],
        "qualityIssues": report["qualityIssues"],
        "readOnly": True,
    }, ensure_ascii=False))
    return 1 if report["reasonReconciliation"]["status"] != "MATCH" or report["qualityIssues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
