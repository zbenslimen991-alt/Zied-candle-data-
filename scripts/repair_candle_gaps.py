#!/usr/bin/env python3
"""Repair only confirmed missing 15m OKX spot candles in private Zied history.

Existing rows are preserved. Missing rows are fetched from OKX's historical-candles
endpoint; no synthetic candle data is created. Exits non-zero if any gap cannot be
fully recovered, so the workflow will not commit a partial repair.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BAR_MS = 900_000
BASE_URL = "https://www.okx.com/api/v5/market/history-candles"
USER_AGENT = "ZiedQuant-CandleGapRepair/1.0"


def ts_of(row):
    try:
        value = row.get("ts", row.get("timestamp")) if isinstance(row, dict) else row[0]
        return int(value)
    except (TypeError, ValueError, IndexError, AttributeError):
        raise ValueError(f"Invalid candle timestamp: {row!r}")


def fetch_page(inst_id: str, after_ts: int) -> list[dict]:
    params = urllib.parse.urlencode({"instId": inst_id, "bar": "15m", "after": str(after_ts), "limit": "300"})
    request = urllib.request.Request(
        f"{BASE_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error for {inst_id}: {payload.get('msg') or payload.get('code')}")
            result = []
            for row in payload.get("data", []):
                if len(row) < 8:
                    continue
                result.append({
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "quote": float(row[7]),
                })
            return result
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Unable to fetch OKX candles for {inst_id} after retries: {last_error}")


def fetch_missing(inst_id: str, previous_ts: int, next_ts: int) -> list[dict]:
    expected = list(range(previous_ts + BAR_MS, next_ts, BAR_MS))
    if not expected:
        return []
    wanted = set(expected)
    found: dict[int, dict] = {}
    cursor = next_ts
    pages = 0
    while cursor > previous_ts and len(found) < len(wanted):
        rows = fetch_page(inst_id, cursor)
        pages += 1
        if not rows:
            break
        for row in rows:
            ts = row["ts"]
            if ts in wanted:
                found[ts] = row
        oldest = min(row["ts"] for row in rows)
        if oldest >= cursor:
            # Protect against a non-advancing pagination cursor.
            cursor = oldest - 1
        else:
            cursor = oldest
        if pages > 20:
            raise RuntimeError(f"Pagination safety limit reached for {inst_id} gap {previous_ts}..{next_ts}")
        time.sleep(0.12)
    missing = sorted(wanted - set(found))
    if missing:
        start = datetime.fromtimestamp(missing[0] / 1000, timezone.utc).isoformat()
        end = datetime.fromtimestamp(missing[-1] / 1000, timezone.utc).isoformat()
        raise RuntimeError(f"{inst_id}: OKX did not return {len(missing)} expected 15m candles ({start} through {end})")
    return [found[ts] for ts in expected]


def repair_file(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        return 0
    inst_id = str(payload.get("instId") or path.stem)
    if payload.get("bar") != "15m":
        return 0

    by_ts: dict[int, object] = {}
    for row in rows:
        ts = ts_of(row)
        if ts in by_ts:
            # Keep the first existing row; do not silently replace existing source data.
            continue
        by_ts[ts] = row
    timestamps = sorted(by_ts)
    gaps = []
    for previous_ts, next_ts in zip(timestamps, timestamps[1:]):
        if next_ts - previous_ts > BAR_MS * 1.5:
            gaps.append((previous_ts, next_ts))

    added = 0
    for previous_ts, next_ts in gaps:
        recovered = fetch_missing(inst_id, previous_ts, next_ts)
        for row in recovered:
            if row["ts"] not in by_ts:
                by_ts[row["ts"]] = row
                added += 1

    if added:
        merged = [by_ts[ts] for ts in sorted(by_ts)]
        payload["candles"] = merged
        payload["count"] = len(merged)
        payload["lastTimestamp"] = ts_of(merged[-1])
        payload["updated"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return added


def main() -> int:
    root = Path("zied-data/candle_history/15m")
    if not root.is_dir():
        raise SystemExit(f"15m candle directory not found: {root}")
    total_added = 0
    changed_files = 0
    for path in sorted(root.glob("*.json")):
        added = repair_file(path)
        if added:
            changed_files += 1
            total_added += added
            print(f"REPAIRED {path.name}: added {added} verified OKX candles")
    print(f"REPAIR SUMMARY: changed_files={changed_files} added_candles={total_added}")
    if changed_files == 0:
        print("No gaps were repaired; inspect the validator report and file selection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
