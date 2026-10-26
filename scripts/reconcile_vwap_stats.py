#!/usr/bin/env python3
"""Reconcile VWAP_RECLAIM descriptive statistics from the saved closed-trade ledger.

This is a data-consistency repair only. It deliberately preserves the strategy's
existing weight, stopMultiplier, targetRR and lastWeeklyUpdate fields.
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys

PORTFOLIO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("zied-data/portfolio_0001.json")
STATS = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("zied-data/strategy_stats_0001.json")
LABEL = "VWAP_RECLAIM"
HALF_LIFE_DAYS = 120.0
REASONS = {
    "TP_HIT": "TP", "TAKE_PROFIT": "TP", "TP": "TP",
    "SL_HIT": "SL", "STOP_LOSS": "SL", "SL": "SL",
    "TIME_EXIT": "TIME_EXIT", "TIMEOUT": "TIME_EXIT", "TIME_EXIT_24H": "TIME_EXIT",
}


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def pnl_of(row):
    for key in ("profitUsd", "realizedPnl", "realized_pnl", "pnlUsd", "profit_usd", "pnl", "profit"):
        value = row.get(key)
        if value is None or value == "":
            continue
        parsed = number(value)
        if parsed is not None:
            return parsed
    return None


def close_time(row):
    for key in ("closeTime", "closeTimestamp", "closedAt", "closed_time", "closed_at",
                "exitTime", "exit_time", "exitTimestamp", "closedTimestamp", "timestamp"):
        if row.get(key):
            return row[key]
    return None


def recency_weight(row, now):
    value = close_time(row)
    if not value:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 86400.0)
        return 0.5 ** (age_days / HALF_LIFE_DAYS) if HALF_LIFE_DAYS > 0 else 1.0
    except Exception:
        return 1.0


def bucket_name(row, kind):
    if kind == "inst":
        return str(row.get("instId") or row.get("instrument") or row.get("symbol") or row.get("pair") or "").strip()
    if kind == "regime":
        return str(row.get("dynamicMarketRegime") or row.get("marketRegime") or "UNKNOWN").upper().strip() or "UNKNOWN"
    if kind == "direction":
        return str(row.get("direction") or "LONG").upper().strip() or "LONG"
    return str(row.get("timeframe") or "1H").upper().strip() or "1H"


def build_bucket(rows, old_bucket, now):
    total = len(rows)
    wins = sum(pnl_of(r) is not None and pnl_of(r) > 0 for r in rows)
    losses = sum(pnl_of(r) is not None and pnl_of(r) < 0 for r in rows)
    pnl_sum = sum(pnl_of(r) or 0.0 for r in rows)
    weights = [recency_weight(r, now) for r in rows]
    weighted_total = sum(weights)
    weighted_wins = sum(w for r, w in zip(rows, weights) if (pnl_of(r) or 0.0) > 0)
    result = dict(old_bucket) if isinstance(old_bucket, dict) else {}
    result.update({
        "trades": total, "wins": wins, "losses": losses,
        "pnlSum": round(pnl_sum, 12),
        "weightedWinRate": weighted_wins / weighted_total * 100.0 if weighted_total else 50.0,
        "effectiveSamples": weighted_total,
    })
    # Preserve all existing bucket weights; a newly seen instrument gets neutral weight.
    result.setdefault("weight", 1.0)
    return result


def main():
    portfolio = json.loads(PORTFOLIO.read_text(encoding="utf-8"))
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    closed = portfolio.get("closedPositions", []) if isinstance(portfolio, dict) else []
    rows = [dict(r) for r in closed if isinstance(r, dict) and str(r.get("strategy") or "").strip() == LABEL and pnl_of(r) is not None]
    if not rows:
        raise SystemExit(f"No valid closed ledger trades found for {LABEL}; refusing to change statistics.")

    stat = stats.get(LABEL)
    if not isinstance(stat, dict):
        raise SystemExit(f"{LABEL} is missing from saved strategy stats; refusing to create a new record.")
    old_weight = stat.get("weight")
    old_stop = stat.get("stopMultiplier")
    old_rr = stat.get("targetRR")
    old_update = stat.get("lastWeeklyUpdate")

    now = datetime.now(timezone.utc)
    wins = sum((pnl_of(r) or 0.0) > 0 for r in rows)
    losses = sum((pnl_of(r) or 0.0) < 0 for r in rows)
    pnl_sum = sum(pnl_of(r) or 0.0 for r in rows)
    reason_counts = {"SL": 0, "TP": 0, "TIME_EXIT": 0}
    for row in rows:
        raw = str(row.get("exitReason") or row.get("closeReason") or row.get("close_reason") or "").upper().strip()
        reason = REASONS.get(raw, "UNKNOWN")
        if reason in reason_counts:
            reason_counts[reason] += 1

    weights = [recency_weight(r, now) for r in rows]
    weighted_total = sum(weights)
    weighted_wins = sum(w for r, w in zip(rows, weights) if (pnl_of(r) or 0.0) > 0)
    weighted_pnl = sum((pnl_of(r) or 0.0) * w for r, w in zip(rows, weights))
    stat.update({
        "trades": len(rows), "wins": wins, "losses": losses, "pnlSum": round(pnl_sum, 12),
        "slHits": reason_counts["SL"], "tpHits": reason_counts["TP"], "timeHits": reason_counts["TIME_EXIT"],
        "weightedWinRate": weighted_wins / weighted_total * 100.0 if weighted_total else 50.0,
        "weightedPnl": weighted_pnl, "effectiveSamples": weighted_total,
    })

    for key, kind in (("byInstId", "inst"), ("byRegime", "regime"), ("byDirection", "direction"), ("byTimeframe", "timeframe")):
        old = stat.get(key) if isinstance(stat.get(key), dict) else {}
        grouped = {}
        for row in rows:
            name = bucket_name(row, kind)
            if name:
                grouped.setdefault(name, []).append(row)
        stat[key] = {name: build_bucket(bucket_rows, old.get(name), now) for name, bucket_rows in grouped.items()}

    # Hard guard: this repair must not change the strategy's configured weight/risk parameters.
    if stat.get("weight") != old_weight or stat.get("stopMultiplier") != old_stop or stat.get("targetRR") != old_rr:
        raise SystemExit("Safety guard failed: strategy weight/risk parameters changed.")
    stat["lastWeeklyUpdate"] = old_update
    STATS.write_text(json.dumps(stats, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({
        "strategy": LABEL, "closedTrades": len(rows), "wins": wins, "losses": losses,
        "slHits": reason_counts["SL"], "tpHits": reason_counts["TP"], "timeHits": reason_counts["TIME_EXIT"],
        "pnlSum": round(pnl_sum, 8), "weightPreserved": stat.get("weight") == old_weight,
        "stopMultiplierPreserved": stat.get("stopMultiplier") == old_stop,
        "targetRrPreserved": stat.get("targetRR") == old_rr,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
