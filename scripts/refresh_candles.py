#!/usr/bin/env python3
"""Download up to CANDLE_LIMIT historical OKX Spot 1H candles for live USDT pairs."""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.okx.com"
OUT = Path("data")
BAR = os.getenv("CANDLE_BAR", "1H")
TARGET = max(1, min(int(os.getenv("CANDLE_LIMIT", "10000")), 10000))
PAGE_SIZE = 100  # OKX history-candles endpoint maximum
DELAY = float(os.getenv("REQUEST_DELAY", "0.15"))
EXCLUDED_BASES = {
    "USDC", "USDG", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "USDP",
    "PYUSD", "EURC", "EURT", "UST", "USTC", "FRAX", "LUSD", "USDS",
    "USD0", "USD1", "RLUSD", "GUSD", "BUSD", "USDX", "USDK", "USDS"
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

def fetch_candles(inst: str):
    rows = {}
    cursor = None
    while len(rows) < TARGET:
        params = {
            "instId": inst,
            "bar": BAR,
            "limit": str(min(PAGE_SIZE, TARGET - len(rows))),
        }
        # OKX returns history in descending timestamp order; after requests older data.
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
        oldest = min(timestamps)
        next_cursor = str(oldest)
        if cursor == next_cursor or len(page) < 2:
            break
        cursor = next_cursor
        time.sleep(DELAY)
    return [rows[k] for k in sorted(rows, key=int)][-TARGET:]

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    instruments = eligible_instruments()
    print(f"Eligible live OKX Spot/USDT pairs: {len(instruments)}; bar={BAR}; target={TARGET}")
    failures = []
    updated = 0
    for i, inst in enumerate(instruments, 1):
        path = OUT / f"{inst.replace('/', '_')}_{BAR}.json.gz"
        try:
            candles = fetch_candles(inst)
            if not candles:
                raise RuntimeError("OKX returned no candles")
            payload = {
                "exchange": "OKX", "market": "SPOT", "instId": inst,
                "bar": BAR, "target_candles": TARGET,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "columns": ["ts", "open", "high", "low", "close", "volume",
                            "volume_currency", "volume_quote", "confirm"],
                "candles": candles,
            }
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
                json.dump(payload, f, separators=(",", ":"))
            updated += 1
            print(f"[{i}/{len(instruments)}] {inst}: {len(candles)} candles")
        except Exception as exc:
            failures.append({"instId": inst, "error": str(exc)})
            print(f"[{i}/{len(instruments)}] ERROR {inst}: {exc}")
        time.sleep(DELAY)

    status = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exchange": "OKX", "market": "SPOT", "quote": "USDT",
        "bar": BAR, "target_candles": TARGET,
        "eligible_pairs": len(instruments), "updated_pairs": updated,
        "failed_pairs": failures,
    }
    (OUT / "refresh_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    if failures:
        raise SystemExit(f"{len(failures)} pair(s) failed; inspect data/refresh_status.json")

if __name__ == "__main__":
    main()
