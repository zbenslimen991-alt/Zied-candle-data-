#!/usr/bin/env python3
"""Read-only structural validation for stored OKX candle-history JSON files."""
from __future__ import annotations
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "candle_history"
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "candle_validation_report.json"
BAR_MS = {"15m": 900_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}
TIMEFRAMES = tuple(BAR_MS)

def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None

def validate_file(path: Path, timeframe: str):
    issues = Counter()
    row_count = 0
    timestamps = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"file": str(path.relative_to(ROOT)), "rows": 0,
                "issues": {"unreadable_json": 1}, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"file": str(path.relative_to(ROOT)), "rows": 0,
                "issues": {"payload_not_object": 1}}
    inst_id = path.stem.removesuffix("_" + timeframe)
    if payload.get("exchange") != "OKX": issues["wrong_or_missing_exchange"] += 1
    if payload.get("market") != "SPOT": issues["wrong_or_missing_market"] += 1
    if payload.get("instId") != inst_id: issues["instrument_metadata_mismatch"] += 1
    if payload.get("bar") != timeframe: issues["timeframe_metadata_mismatch"] += 1
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        issues["missing_or_empty_candles"] += 1
        rows = []
    seen = set()
    previous = None
    for index, row in enumerate(rows):
        row_count += 1
        if not isinstance(row, list) or len(row) < 9:
            issues["row_missing_columns"] += 1
            continue
        ts_num = finite(row[0])
        if ts_num is None or ts_num <= 0 or not ts_num.is_integer():
            issues["invalid_timestamp"] += 1
            continue
        ts = int(ts_num)
        timestamps.append(ts)
        if ts in seen: issues["duplicate_timestamp"] += 1
        seen.add(ts)
        if previous is not None:
            if ts < previous: issues["timestamps_out_of_order"] += 1
            elif ts == previous: pass
            elif ts - previous > BAR_MS[timeframe] * 1.5:
                issues["candle_time_gaps"] += 1
        previous = ts
        o, h, low, close = (finite(row[i]) for i in (1, 2, 3, 4))
        if any(v is None or v <= 0 for v in (o, h, low, close)):
            issues["invalid_ohlc_value"] += 1
            continue
        if h < max(o, close, low) or low > min(o, close, h):
            issues["inconsistent_ohlc_relationship"] += 1
        for volume_index in (5, 6, 7):
            volume = finite(row[volume_index])
            if volume is None or volume < 0:
                issues["invalid_volume"] += 1
        confirm = str(row[8])
        if confirm not in ("0", "1", "0.0", "1.0"):
            issues["invalid_confirm_flag"] += 1
    return {
        "file": str(path.relative_to(ROOT)), "instrument": inst_id,
        "timeframe": timeframe, "rows": row_count,
        "firstTimestamp": min(timestamps) if timestamps else None,
        "lastTimestamp": max(timestamps) if timestamps else None,
        "issues": dict(issues),
    }

def main():
    reports = []
    missing_timeframes = []
    for timeframe in TIMEFRAMES:
        directory = DATA_ROOT / timeframe
        files = sorted(directory.glob(f"*-{timeframe}.json")) if directory.exists() else []
        if not files:
            missing_timeframes.append(timeframe)
        for path in files:
            reports.append(validate_file(path, timeframe))
    totals = Counter()
    for report in reports:
        totals.update(report.get("issues", {}))
    if missing_timeframes:
        totals["missing_timeframe_dataset"] += len(missing_timeframes)
    result = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "readOnly": True,
        "dataRoot": str(DATA_ROOT),
        "filesChecked": len(reports),
        "rowsChecked": sum(x.get("rows", 0) for x in reports),
        "missingTimeframes": missing_timeframes,
        "issueTotals": dict(totals),
        "status": "PASS" if not totals else "FAIL",
        "files": reports,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("filesChecked", "rowsChecked", "missingTimeframes", "issueTotals", "status")}, ensure_ascii=False))
    return 1 if totals else 0

if __name__ == "__main__":
    raise SystemExit(main())
