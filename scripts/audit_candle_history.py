#!/usr/bin/env python3
"""Read-only inventory of candle_history JSON files; never edits source data."""
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "candle_history"
NOW = datetime.now(timezone.utc).timestamp() * 1000
REPORT = ROOT / "candle_history_audit_report.json"


def finite(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def rows_from(payload):
    return payload.get("candles", []) if isinstance(payload, dict) else payload


def candle_ts(row):
    if isinstance(row, dict):
        return finite(row.get("ts", row.get("timestamp")))
    if isinstance(row, (list, tuple)) and row:
        return finite(row[0])
    return None


def validate(row):
    if isinstance(row, dict):
        vals = [row.get(k) for k in ("ts", "open", "high", "low", "close", "volume")]
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        vals = list(row[:6])
    else:
        return False
    nums = [finite(x) for x in vals]
    if any(x is None for x in nums):
        return False
    ts, op, hi, lo, cl, vol = nums
    return (ts >= 100_000_000_000 and ts % 1 == 0 and
            min(op, hi, lo, cl) > 0 and vol >= 0 and
            hi >= max(op, cl, lo) and lo <= min(op, cl))


def main():
    files = sorted(BASE.rglob("*.json")) if BASE.exists() else []
    counts, timeframe, details = Counter(), Counter(), []
    for path in files:
        counts["filesFound"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = rows_from(payload)
            if not isinstance(rows, list):
                counts["unreadableOrInvalid"] += 1
                status, last_ts, n, valid_n = "INVALID_ROWS_CONTAINER", None, 0, 0
            else:
                n = len(rows)
                valid_n = sum(validate(row) for row in rows)
                timestamps = [candle_ts(row) for row in rows]
                timestamps = [v for v in timestamps if v is not None and v > 0]
                meta_ts = finite(payload.get("lastTimestamp")) if isinstance(payload, dict) else None
                last_ts = meta_ts or (max(timestamps) if timestamps else None)
                if n == 0 or valid_n == 0:
                    counts["unreadableOrInvalid"] += 1
                    status = "NO_VALID_CANDLES"
                elif valid_n < n:
                    counts["partiallyInvalid"] += 1
                    status = "PARTIALLY_INVALID"
                elif last_ts is None:
                    counts["unreadableOrInvalid"] += 1
                    status = "NO_TIMESTAMP"
                else:
                    age = (NOW - last_ts) / 3_600_000
                    if age > 30:
                        counts["staleOver30h"] += 1
                        status = "STALE_OVER_30H"
                    else:
                        counts["freshOrRecent"] += 1
                        status = "FRESH_OR_RECENT"
            tf = (payload.get("bar") if isinstance(payload, dict) else None) or path.parent.name
            timeframe[str(tf)] += 1
            details.append({"file": str(path.relative_to(ROOT)), "timeframe": str(tf),
                            "rows": n, "validRows": valid_n, "lastTimestamp": last_ts,
                            "status": status})
        except Exception as exc:
            counts["unreadableOrInvalid"] += 1
            details.append({"file": str(path.relative_to(ROOT)), "status": "UNREADABLE",
                            "error": str(exc)})
    report = {"generatedAt": datetime.now(timezone.utc).isoformat(),
              "root": str(BASE.relative_to(ROOT)), "filesFound": len(files),
              "summary": dict(counts), "filesByTimeframe": dict(timeframe), "files": details,
              "note": "Read-only report; only files present in this checkout are included."}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "filesFound": len(files),
                      "summary": dict(counts), "filesByTimeframe": dict(timeframe)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
