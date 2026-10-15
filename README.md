# Zied Candle Data

Daily OKX Spot USDT historical candle dataset for Zied Quant.

- Market: OKX Spot
- Quote currency: USDT
- Candle timeframe: 1H
- Target history: up to 10,000 candles per eligible live pair
- Scheduled refresh: daily at 02:17 UTC; can also be started manually from GitHub Actions.
- Output: compressed JSON files in `data/` plus `data/refresh_status.json`.

The refresh workflow must run the repository's `scripts/refresh_candles.py` and commit generated files under `data/`. It excludes stablecoin base assets and does not place trades or use private API credentials.
