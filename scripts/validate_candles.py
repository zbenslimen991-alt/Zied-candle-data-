#!/usr/bin/env python3
"""Read-only structural validation for OKX candle-history JSON files.

Supports both the current per-timeframe layout (candle_history/1H/SYMBOL-USDT.json)
and legacy root-level files (SYMBOL-USDT_1H.json). Supports candle rows encoded as
OKX arrays or dictionaries. It never modifies source candle files.
"""
from __future__ import annotations
import json
import math
import re
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

def infer_timeframe(path: Path, payload: dict):
    if path.parent.name in BAR_MS:
        return path.parent.name
    match = re.search(r"_(15m|1H|4H|1D)(?:\.json)?$", path.name)
    if match:
        return match.group(1)
    bar = payload.get("bar")
    return bar if bar in BAR_MS else None

def normalize_row(row):
    # OKX raw array: [ts, open, high, low, close, volume, volCcy, volQuote, confirm]
    if isinstance(row, list):
        if len(row) < 5:
            return None
        return {
            "ts": row[0], "open": row[1], "high": row[2], "low": row[3],
            "close": row[4], "volume": row[5] if len(row) > 5 else 0,
            "quote": row[7] if len(row) > 7 else 0,
            "confirm": row[8] if len(row) > 8 else None,
        }
    if isinstance(row, dict):
        return {
            "ts": row.get("ts", row.get("timestamp")),
            "open": row.get("open", row.get("o")),
            "high": row.get("high", row.get("h")),
            "low": row.get("low", row.get("l")),
            "close": row.get("close", row.get("c")),
            "volume": row.get("volume", row.get("vol", 0)),
            "quote": row.get("quote", row.get("volCcyQuote", 0)),
            "confirm": row.get("confirm"),
        }
    return None

def validate_file(path: Path, timeframe: str):
    issues = Counter()
    timestamps = []
    gaps = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"file": str(path), "rows": 0, "timeframe": timeframe,
                "issues": {"unreadable_json": 1}, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"file": str(path), "rows": 0, "timeframe": timeframe,
                "issues": {"payload_not_object": 1}}
    inst_id = str(payload.get("instId") or path.stem)
    declared_bar = payload.get("bar")
    if declared_bar and declared_bar != timeframe:
        issues["timeframe_metadata_mismatch"] += 1
    declared_inst = payload.get("instId")
    if declared_inst and declared_inst != path.stem and not path.stem.startswith(str(declared_inst) + "_"):
        # Some files have a timeframe suffix after the instrument name.
        if path.stem.removesuffix("_" + timeframe) != declared_inst:
            issues["instrument_metadata_mismatch"] += 1
    if payload.get("exchange") and payload.get("exchange") != "OKX":
        issues["wrong_exchange"] += 1
    if payload.get("market") and payload.get("market") != "SPOT":
        issues["wrong_market"] += 1
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        issues["missing_or_empty_candles"] += 1
        rows = []
    seen = set()
    previous = None
    for row in rows:
        normalized = normalize_row(row)
        if normalized is None:
            issues["row_missing_columns"] += 1
            continue
        ts_num = finite(normalized["ts"])
        if ts_num is None or ts_num <= 0 or not ts_num.is_integer():
            issues["invalid_timestamp"] += 1
            continue
        ts = int(ts_num)
        timestamps.append(ts)
        if ts in seen:
            issues["duplicate_timestamp"] += 1
        seen.add(ts)
        if previous is not None:
            if ts < previous:
                issues["timestamps_out_of_order"] += 1
            elif ts - previous > BAR_MS[timeframe] * 1.5:
                issues["candle_time_gaps"] += 1
                delta_ms = ts - previous
                gaps.append({"previousTimestamp": previous, "previousUtc": datetime.fromtimestamp(previous / 1000, timezone.utc).isoformat(), "nextTimestamp": ts, "nextUtc": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), "deltaMs": delta_ms, "expectedIntervalMs": BAR_MS[timeframe], "estimatedMissingCandles": max(0, round(delta_ms / BAR_MS[timeframe]) - 1)})
        previous = ts
        o, h, low, close = (finite(normalized[k]) for k in ("open", "high", "low", "close"))
        if any(v is None or v <= 0 for v in (o, h, low, close)):
            issues["invalid_ohlc_value"] += 1
            continue
        if h < max(o, close, low) or low > min(o, close, h):
            issues["inconsistent_ohlc_relationship"] += 1
        for key in ("volume", "quote"):
            volume = finite(normalized[key])
            if volume is None or volume < 0:
                issues["invalid_volume"] += 1
        confirm = normalized["confirm"]
        if confirm is not None and str(confirm) not in ("0", "1", "0.0", "1.0", "true", "false", "True", "False"):
            issues["invalid_confirm_flag"] += 1
    return {
        "file": str(path), "instrument": inst_id, "timeframe": timeframe,
        "rows": len(rows), "firstTimestamp": min(timestamps) if timestamps else None,
        "lastTimestamp": max(timestamps) if timestamps else None, "issues": dict(issues),
        "gapDetails": gaps,
    }

def main():
    reports = []
    missing_timeframes = []
    if not DATA_ROOT.exists():
        raise SystemExit(f"Candle data root does not exist: {DATA_ROOT}")
    candidates = sorted(DATA_ROOT.rglob("*.json"))
    found_by_timeframe = Counter()
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Keep unreadable JSON in the report when its timeframe can be inferred.
            timeframe = path.parent.name if path.parent.name in BAR_MS else None
        else:
            timeframe = infer_timeframe(path, payload) if isinstance(payload, dict) else (
                path.parent.name if path.parent.name in BAR_MS else None
            )
        if timeframe not in BAR_MS:
            continue
        found_by_timeframe[timeframe] += 1
        reports.append(validate_file(path, timeframe))
    for timeframe in TIMEFRAMES:
        if not found_by_timeframe[timeframe]:
            missing_timeframes.append(timeframe)
    totals = Counter()
    gap_files = []
    for report in reports:
        totals.update(report.get("issues", {}))
        for gap in report.get("gapDetails", []):
            gap_files.append({"file": report.get("file"), "instrument": report.get("instrument"), "timeframe": report.get("timeframe"), **gap})
    if missing_timeframes:
        totals["missing_timeframe_dataset"] += len(missing_timeframes)
    result = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "readOnly": True, "dataRoot": str(DATA_ROOT),
        "filesChecked": len(reports), "rowsChecked": sum(x.get("rows", 0) for x in reports),
        "filesByTimeframe": dict(found_by_timeframe),
        "missingTimeframes": missing_timeframes, "issueTotals": dict(totals),
        "gapCount": len(gap_files), "gapDetails": gap_files,
        "status": "PASS" if not totals else "FAIL", "files": reports,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in (
        "dataRoot", "filesChecked", "rowsChecked", "filesByTimeframe",
        "missingTimeframes", "issueTotals", "status")}, ensure_ascii=False))
    return 1 if totals else 0

if __name__ == "__main__":
    raise SystemExit(main())
