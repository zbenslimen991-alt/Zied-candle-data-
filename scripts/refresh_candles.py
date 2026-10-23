#!/usr/bin/env python3
"""Refresh OKX Spot USDT candles for 15m, 1H, 4H and 1D into candle_history/<timeframe>/.

Fast path: if a stored file already has the target history, fetch only the newest
candles and merge them. Initial/backfill runs fetch history in pages of 300.
Pairs are processed concurrently while a shared limiter respects OKX's
history-candles IP rate limit.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.okx.com"
OUT = Path("candle_history")
BARS = ("15m", "1H", "4H", "1D")
TARGET = max(1, min(int(os.getenv("CANDLE_LIMIT", "10000")), 10000))
PAGE_SIZE = 300
WORKERS = max(1, min(int(os.getenv("CANDLE_WORKERS", "8")), 12))
# OKX history-candles is limited to 20 requests per 2 seconds per IP.
MIN_REQUEST_INTERVAL = max(0.105, float(os.getenv("REQUEST_INTERVAL", "0.11")))
_rate_lock = threading.Lock()
_last_request = 0.0

EXCLUDED_BASES = {
    "USDC", "USDG", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "USDP",
    "PYUSD", "EURC", "EURT", "UST", "USTC", "FRAX", "LUSD", "USDS",
    "USD0", "USD1", "RLUSD", "GUSD", "BUSD", "USDX", "USDK"
}

def rate_limit():
    global _last_request
    with _rate_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()

def get_json(path: str, params: dict | None = None):
    url = BASE + path + (("?" + urlencode(params)) if params else "")
    last_error = None
    for attempt in range(6):
        rate_limit()
        req = Request(url, headers={"User-Agent": "Zied-Candle-Data/2.0"})
        try:
            with urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") == "0":
                return payload.get("data", [])
            message = f"OKX API error {payload.get('code')}: {payload.get('msg', payload)}"
            # 50011 is OKX's rate-limit response; retry with a longer pause.
            if str(payload.get("code")) == "50011":
                last_error = RuntimeError(message)
                time.sleep(min(2 ** attempt, 20))
                continue
            raise RuntimeError(message)
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                raise
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(min(1.5 * (2 ** attempt), 20))
    raise RuntimeError(f"Request failed after retries: {url}: {last_error}")

def eligible_instruments():
    instruments = get_json("/api/v5/public/instruments", {"instType": "SPOT"})
    result = []
    for item in instruments:
        inst = item.get("instId", "")
        if not inst.endswith("-USDT") or item.get("state") != "live":
            continue
        base = (item.get("baseCcy") or inst[:-5]).upper()
        if base in EXCLUDED_BASES:
            continue
        result.append(inst)
    return sorted(set(result))

def read_existing(path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("candles", [])
        return {str(row[0]): row[:9] for row in rows if isinstance(row, list) and len(row) >= 9}
    except (OSError, ValueError, TypeError):
        return {}

def fetch_candles(inst: str, bar: str, limit: int):
    rows = {}
    cursor = None
    while len(rows) < limit:
        params = {"instId": inst, "bar": bar, "limit": str(min(PAGE_SIZE, limit - len(rows)))}
        if cursor is not None:
            params["after"] = cursor
        page = get_json("/api/v5/market/history-candles", params)
        if not page:
            break
        before_count = len(rows)
        timestamps = []
        for row in page:
            if len(row) < 9:
                continue
            ts = str(row[0])
            rows[ts] = row[:9]
            timestamps.append(int(ts))
        if not timestamps or len(rows) == before_count:
            break
        next_cursor = str(min(timestamps))
        if cursor is not None and int(next_cursor) >= int(cursor):
            break
        cursor = next_cursor
        if len(page) < PAGE_SIZE:
            break
    return rows

def fetch_latest_candles(inst: str, bar: str):
    rows = {}
    page = get_json("/api/v5/market/candles", {
        "instId": inst, "bar": bar, "limit": str(PAGE_SIZE)
    })
    for row in page:
        if len(row) >= 9:
            rows[str(row[0])] = row[:9]
    return rows

def candle_payload(inst: str, bar: str, rows: dict[str, list]):
    return {
        "exchange": "OKX", "market": "SPOT", "instId": inst,
        "bar": bar, "target_candles": TARGET,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "columns": ["ts", "open", "high", "low", "close", "volume",
                    "volume_currency", "volume_quote", "confirm"],
        "candles": [rows[k] for k in sorted(rows, key=int)[-TARGET:]],
    }

def update_pair(inst: str, bar: str):
    path = OUT / bar / f"{inst}_{bar}.json"
    existing = read_existing(path)
    if len(existing) >= TARGET:
        # Daily fast path: only fetch recent candles, then deduplicate and retain
        # the newest TARGET candles. No full 10,000-candle re-download.
        recent = fetch_latest_candles(inst, bar)
        merged = {**existing, **recent}
    else:
        # Initial load or incomplete history: fetch the full target range.
        history = fetch_candles(inst, bar, TARGET)
        merged = {**existing, **history}
        # Ensure the newest candle is included even if history pagination ends
        # just before the latest live candle.
        recent = fetch_latest_candles(inst, bar)
        merged.update(recent)
    if not merged:
        # Some live-listed instruments can temporarily return no candle rows
        # from OKX. Do not create an empty file or fail the entire refresh;
        # report this pair separately and preserve any existing file unchanged.
        print(f"[{bar}] SKIP {inst}: OKX returned no candles and no stored history exists")
        return 0
    payload = candle_payload(inst, bar, merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(path)
    return len(payload["candles"])

def main():
    instruments = eligible_instruments()
    OUT.mkdir(parents=True, exist_ok=True)
    overall = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exchange": "OKX", "market": "SPOT", "quote": "USDT",
        "target_candles": TARGET, "workers": WORKERS, "timeframes": {}
    }
    all_failures = []
    print(f"Using {WORKERS} workers, page size {PAGE_SIZE}, shared request interval {MIN_REQUEST_INTERVAL:.3f}s")
    for bar in BARS:
        failures = []
        updated = 0
        skipped_no_history = []
        started = time.monotonic()
        print(f"Starting {bar}: {len(instruments)} live Spot/USDT pairs, target={TARGET}")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(update_pair, inst, bar): inst for inst in instruments}
            for done, future in enumerate(as_completed(futures), 1):
                inst = futures[future]
                try:
                    count = future.result()
                    if count == 0:
                        skipped_no_history.append(inst)
                        print(f"[{bar} {done}/{len(instruments)}] SKIP {inst}: no candles available")
                    else:
                        updated += 1
                        print(f"[{bar} {done}/{len(instruments)}] {inst}: {count} candles")
                except Exception as exc:
                    failures.append({"instId": inst, "bar": bar, "error": str(exc)})
                    print(f"[{bar} {done}/{len(instruments)}] ERROR {inst}: {exc}")
        overall["timeframes"][bar] = {
            "eligible_pairs": len(instruments), "updated_pairs": updated,
            "skipped_no_history_pairs": len(skipped_no_history),
            "skipped_no_history": skipped_no_history,
            "failed_pairs": len(failures),
            "elapsed_seconds": round(time.monotonic() - started, 2)
        }
        all_failures.extend(failures)
        (OUT / "refresh_status.json").write_text(json.dumps(overall, indent=2), encoding="utf-8")
    overall["failed_pairs"] = all_failures
    (OUT / "refresh_status.json").write_text(json.dumps(overall, indent=2), encoding="utf-8")
    print(json.dumps(overall, indent=2))
    if any(not overall["timeframes"].get(bar, {}).get("updated_pairs") for bar in BARS):
        raise SystemExit("At least one timeframe did not update any pairs.")
    if all_failures:
        raise SystemExit(f"{len(all_failures)} pair/timeframe updates failed; inspect candle_history/refresh_status.json")

if __name__ == "__main__":
    main()
