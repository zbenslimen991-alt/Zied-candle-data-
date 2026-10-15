#!/usr/bin/env python3
"""Refresh OKX Spot USDT candles for 15m, 1H, 4H and 1D into candle_history/<timeframe>/."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.okx.com"
OUT = Path("candle_history")
BARS = ("15m", "1H", "4H", "1D")
TARGET = max(1, min(int(os.getenv("CANDLE_LIMIT", "10000")), 10000))
PAGE_SIZE = 100
DELAY = float(os.getenv("REQUEST_DELAY", "0.15"))
EXCLUDED_BASES = {
    "USDC", "USDG", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "USDP",
    "PYUSD", "EURC", "EURT", "UST", "USTC", "FRAX", "LUSD", "USDS",
    "USD0", "USD1", "RLUSD", "GUSD", "BUSD", "USDX", "USDK"
}

def get_json(path: str, params: dict | None = None):
    url = BASE + path + (("?" + urlencode(params)) if params else "")
    req = Request(url, headers={"User-Agent": "Zied-Candle-Data/1.0"})
    with urlopen(req, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX API error: {payload}")
    return payload.get("data", [])

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

def fetch_candles(inst: str, bar: str):
    rows = {}
    cursor = None
    while len(rows) < TARGET:
        params = {"instId": inst, "bar": bar, "limit": str(min(PAGE_SIZE, TARGET - len(rows)))}
        if cursor is not None:
            params["after"] = cursor
        page = get_json("/api/v5/market/history-candles", params)
        if not page:
            break
        timestamps = []
        for row in page:
            if len(row) < 9:
                continue
            ts = str(row[0])
            rows[ts] = row[:9]
            timestamps.append(int(ts))
        if not timestamps:
            break
        next_cursor = str(min(timestamps))
        if cursor == next_cursor or len(page) < 2:
            break
        cursor = next_cursor
        time.sleep(DELAY)
    return [rows[k] for k in sorted(rows, key=int)][-TARGET:]

def main():
    instruments = eligible_instruments()
    OUT.mkdir(parents=True, exist_ok=True)
    overall = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exchange": "OKX", "market": "SPOT", "quote": "USDT",
        "target_candles": TARGET, "timeframes": {}
    }
    all_failures = []
    for bar in BARS:
        folder = OUT / bar
        folder.mkdir(parents=True, exist_ok=True)
        failures = []
        updated = 0
        print(f"Starting {bar}: {len(instruments)} live Spot/USDT pairs, target={TARGET}")
        for i, inst in enumerate(instruments, 1):
            path = folder / f"{inst}_{bar}.json"
            try:
                candles = fetch_candles(inst, bar)
                if not candles:
                    raise RuntimeError("OKX returned no candles")
                payload = {
                    "exchange": "OKX", "market": "SPOT", "instId": inst,
                    "bar": bar, "target_candles": TARGET,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "columns": ["ts", "open", "high", "low", "close", "volume",
                                "volume_currency", "volume_quote", "confirm"],
                    "candles": candles,
                }
                path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                updated += 1
                print(f"[{bar} {i}/{len(instruments)}] {inst}: {len(candles)} candles")
            except Exception as exc:
                failures.append({"instId": inst, "bar": bar, "error": str(exc)})
                print(f"[{bar} {i}/{len(instruments)}] ERROR {inst}: {exc}")
            time.sleep(DELAY)
        overall["timeframes"][bar] = {
            "eligible_pairs": len(instruments), "updated_pairs": updated,
            "failed_pairs": len(failures)
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
