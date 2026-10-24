import base64
import json
import os
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

APP_REPO = os.environ["APP_REPO"].strip()
if "/" not in APP_REPO:
    raise RuntimeError("APP_REPO must be in owner/repository format, e.g. zbenslimen991-alt/Zied")
OWNER, REPO = APP_REPO.split("/", 1)
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
TOKEN = os.environ["APP_REPO_TOKEN"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
MAX_CELL_CHARS = 40000
MAX_BATCH_BYTES = 900000


def github_get(url, timeout=90):
    r = requests.get(url, headers=GH_HEADERS, timeout=timeout)
    if r.status_code != 200:
        try:
            detail = r.json().get("message", "No API error message returned")
        except ValueError:
            detail = (r.text or "No response body")[:300]
        raise RuntimeError(f"GitHub read failed: HTTP {r.status_code}; reason: {detail}. Check APP_REPO_TOKEN access to {OWNER}/{REPO} with Contents: Read-only permission.")
    return r


def get_tree():
    data = github_get(f"https://api.github.com/repos/{OWNER}/{REPO}/git/trees/main?recursive=1").json()
    if data.get("truncated"):
        raise RuntimeError("GitHub returned a truncated repository file tree; refusing to export an incomplete inventory.")
    return [x for x in data.get("tree", []) if x.get("type") == "blob"]


def fetch_json(path, file_index):
    item = file_index.get(path)
    if not item:
        raise RuntimeError(f"Expected JSON file not found in repository tree: {path}")
    # Git Blob API supports JSON data files larger than the Contents API's 1 MB inline-content threshold.
    obj = github_get(item["url"]).json()
    if obj.get("encoding") != "base64" or not obj.get("content"):
        raise RuntimeError(f"GitHub did not return readable base64 content for {path} (size={item.get('size')}).")
    raw = base64.b64decode(obj["content"]).decode("utf-8-sig")
    return json.loads(raw)


def column_letter(n):
    result = ""
    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def safe(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, str) and v.startswith(("=", "+", "-", "@")):
        return "'" + v
    return v


def tab(headers, rows):
    return [headers] + [[safe(v) for v in row] for row in rows]


def flatten_json(value, path="$"):
    # Produce a lossless row representation of each JSON leaf. Long strings are split
    # into chunks so Google Sheets' per-cell character limit is not exceeded.
    if isinstance(value, dict):
        if not value:
            yield (path, "object", "{}", 1)
        else:
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                yield from flatten_json(child, f"{path}/{escaped}")
    elif isinstance(value, list):
        if not value:
            yield (path, "array", "[]", 1)
        else:
            for i, child in enumerate(value):
                yield from flatten_json(child, f"{path}/{i}")
    else:
        if value is None:
            yield (path, "null", "", 1)
        elif isinstance(value, bool):
            yield (path, "boolean", str(value).lower(), 1)
        elif isinstance(value, (int, float)):
            yield (path, "number", value, 1)
        else:
            value = str(value)
            chunks = [value[i:i + MAX_CELL_CHARS] for i in range(0, len(value), MAX_CELL_CHARS)] or [""]
            for part, chunk in enumerate(chunks, 1):
                yield (path, "string" if len(chunks) == 1 else "string_chunk", chunk, part)


def records_table(items, preferred_headers=None):
    if not isinstance(items, list):
        items = []
    dict_items = [x for x in items if isinstance(x, dict)]
    keys = list(preferred_headers or [])
    for item in dict_items:
        for key in item:
            if key not in keys:
                keys.append(key)
    if not keys:
        keys = ["Record"]
        return [keys] + [[safe(x)] for x in items]
    return [keys] + [[safe(x.get(k, "")) for k in keys] for x in dict_items]


def build_data(tree):
    file_index = {x["path"]: x for x in tree}
    json_files = [x for x in tree if x["path"].lower().endswith(".json") and not x["path"].startswith("candle_history/")]
    candle_files = [x for x in tree if x["path"].startswith("candle_history/") and x["type"] == "blob"]
    required = [
        "portfolio_0001.json", "strategy_stats_0001.json", "signal_history_0001.json",
        "backtest_results_0001.json", "auto_tuned_config.json"
    ]
    missing = [p for p in required if p not in file_index]
    if missing:
        raise RuntimeError("Required dashboard source file(s) missing: " + ", ".join(missing))

    # Load every non-candle JSON data file, including the large analysis-memory file.
    json_data = {}
    for item in json_files:
        try:
            json_data[item["path"]] = fetch_json(item["path"], file_index)
        except Exception as exc:
            raise RuntimeError(f"Could not export JSON file {item['path']}: {exc}") from exc

    p = json_data["portfolio_0001.json"]
    s = json_data["strategy_stats_0001.json"]
    signals = json_data["signal_history_0001.json"]
    bt = json_data["backtest_results_0001.json"]
    tuning = json_data["auto_tuned_config.json"]
    opened = p.get("openPositions", []) if isinstance(p, dict) else []
    pending = p.get("pendingLimitOrders", []) if isinstance(p, dict) else []
    closed = p.get("closedPositions", []) if isinstance(p, dict) else []

    strategies = []
    if isinstance(s, dict):
        for name, v in s.items():
            if not isinstance(v, dict):
                continue
            trades = v.get("trades", 0) or 0
            wins = v.get("wins", 0) or 0
            losses = v.get("losses", 0) or 0
            strategies.append((name, trades, wins, losses, round(wins * 100 / trades, 2) if trades else 0,
                v.get("pnlSum", 0), v.get("weight", ""), v.get("tpHits", 0), v.get("slHits", 0),
                v.get("timeHits", 0), v.get("effectiveSamples", 0), v.get("lastWeeklyUpdate", "")))
    strategies.sort(key=lambda x: (x[1], x[4], x[5]), reverse=True)

    backtests = []
    if isinstance(bt, dict):
        for key, v in bt.items():
            if key.startswith("__") or not isinstance(v, dict):
                continue
            backtests.append((v.get("instId", key), v.get("trades", ""), v.get("wins", ""), v.get("losses", ""),
                v.get("winRate", ""), v.get("profitFactor", ""), v.get("averageReturn", ""), v.get("maxDrawdown", ""),
                v.get("sharpeRatio", ""), v.get("oosTrades", ""), v.get("positiveOosWindowRate", ""),
                v.get("dataQuality", ""), v.get("updated", "")))
    backtests.sort(key=lambda x: str(x[0]))

    metrics = tuning.get("metrics") or {} if isinstance(tuning, dict) else {}
    total_trades = sum(row[1] for row in strategies)
    total_wins = sum(row[2] for row in strategies)
    total_losses = sum(row[3] for row in strategies)
    overall_win_rate = round(total_wins * 100 / total_trades, 2) if total_trades else 0

    # A full inventory covers every tracked file, not just JSON files.
    inventory_rows = []
    for f in sorted(tree, key=lambda x: x["path"].lower()):
        path = f["path"]
        parts = path.split("/")
        if path.startswith("candle_history/"):
            category = "Historical candle data"
        elif path.startswith(".github/workflows/"):
            category = "Automation workflow"
        elif path.endswith(".json"):
            category = "Program data / learning / metrics"
        elif path.endswith((".py", ".js")):
            category = "Program source code"
        elif path.lower().endswith((".md", ".txt", ".yml", ".yaml")):
            category = "Documentation / configuration"
        else:
            category = "Other repository file"
        inventory_rows.append([path, parts[0] if len(parts) > 1 else "(root)",
            path.rsplit(".", 1)[-1].lower() if "." in parts[-1] else "(no extension)",
            f.get("size", 0), round(f.get("size", 0) / 1048576, 3), category, f.get("sha", "")])

    candle_rows = []
    candle_counts = Counter()
    candle_bytes = Counter()
    for f in sorted(candle_files, key=lambda x: x["path"].lower()):
        path = f["path"]
        parts = path.split("/")
        interval = parts[1] if len(parts) > 2 else ""
        filename = parts[-1]
        pair = filename
        for suffix in ("_15m.json", "_1H.json", "_4H.json", "_1D.json"):
            if pair.endswith(suffix):
                pair = pair[:-len(suffix)] + "-USDT"
                break
        if pair.endswith(".json"):
            pair = pair[:-5]
        if pair == "refresh_status":
            kind = "Refresh status"
        else:
            kind = "Candle/history file"
        size = f.get("size", 0)
        candle_counts[interval] += 1
        candle_bytes[interval] += size
        candle_rows.append([path, interval, filename, pair, size, round(size / 1048576, 3), kind, f.get("sha", "")])

    raw_rows = []
    for path in sorted(json_data):
        for json_path, value_type, value, chunk_no in flatten_json(json_data[path]):
            raw_rows.append([path, json_path, value_type, safe(value), chunk_no])

    candle_summary = [["Interval Folder", "Files", "Total Size MB"]]
    for interval in sorted(candle_counts):
        candle_summary.append([interval, candle_counts[interval], round(candle_bytes[interval] / 1048576, 2)])
    candle_summary.append(["TOTAL", sum(candle_counts.values()), round(sum(candle_bytes.values()) / 1048576, 2)])

    tuning_last = tuning.get("lastRun", "") if isinstance(tuning, dict) else ""
    return {
      "Overview": [["Metric", "Value"],
        ["Updated UTC", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["Source repository", APP_REPO],
        ["Initial capital USD", p.get("initialCapital", "") if isinstance(p, dict) else ""],
        ["Cash USD", p.get("cash", "") if isinstance(p, dict) else ""],
        ["Open positions", len(opened) if isinstance(opened, list) else 0],
        ["Pending orders", len(pending) if isinstance(pending, list) else 0],
        ["Closed positions", len(closed) if isinstance(closed, list) else 0],
        ["Strategies tracked", len(strategies)],
        ["Total strategy trades", total_trades], ["Total strategy wins", total_wins],
        ["Total strategy losses", total_losses], ["Overall strategy win rate %", overall_win_rate],
        ["Backtest instruments", len(backtests)], ["Non-candle JSON files exported", len(json_files)],
        ["All repository files inventoried", len(tree)], ["Historical candle files inventoried", len(candle_files)],
        ["Raw JSON data rows exported", len(raw_rows)],
        ["AI tuning last run", tuning_last],
        ["Scope note", "All non-candle JSON data is expanded into Raw Data. All repository files and 3,765 historical candle files are indexed. Raw candle arrays remain in the source repo to respect Google Sheets workbook limits."]],
      "Open Positions": records_table(opened),
      "Pending Orders": records_table(pending),
      "Closed Positions": records_table(closed),
      "All Strategies": tab(["Strategy","Trades","Wins","Losses","Win Rate %","PnL Sum","Weight","TP Hits","SL Hits","Time Exits","Effective Samples","Last Weekly Update"], strategies),
      "Top 20 Strategies": tab(["Strategy","Trades","Wins","Losses","Win Rate %","PnL Sum","Weight","TP Hits","SL Hits","Time Exits","Effective Samples","Last Weekly Update"], strategies[:20]),
      "Signal History": records_table(signals if isinstance(signals, list) else []),
      "Backtest Summary": tab(["Instrument","Trades","Wins","Losses","Win Rate","Profit Factor","Average Return","Max Drawdown","Sharpe Ratio","OOS Trades","Positive OOS Window Rate","Data Quality","Updated"], backtests),
      "AI Learning": [["Metric","Value"]] + [[safe(k), safe(v)] for k,v in metrics.items()] + [["Last run",safe(tuning_last)],["Window trades",safe(tuning.get("windowTrades","") if isinstance(tuning,dict) else "")]],
      "Candle Summary": candle_summary,
      "Candle Catalog": tab(["Repository Path","Interval Folder","File Name","Pair / Symbol","Size Bytes","Size MB","File Type","Git Blob SHA"], candle_rows),
      "Repository Files": tab(["Path","Top-level Folder","Extension","Size Bytes","Size MB","Category","Git Blob SHA"], inventory_rows),
      "Raw Data": tab(["Source JSON File","JSON Pointer","Value Type","Value / Chunk","Chunk Number"], raw_rows),
      "Data Status": [["Item","Value"],
        ["Source",f"Private repository {APP_REPO} via read-only GitHub API"],
        ["Refresh UTC",datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["Repository files inventoried",len(tree)],["Non-candle JSON files fully exported",len(json_files)],
        ["Candle/history files cataloged",len(candle_files)],["Raw JSON leaf/chunk rows",len(raw_rows)],
        ["Mode","Read-only dashboard export; no trading logic changes"],
        ["Historical candle data","Indexed by file; raw candle arrays remain in the private repository due Google Sheets size limits"]],
      "Charts": [["Strategy", "Trades"]] + [[row[0], row[1]] for row in strategies[:10]] + [["", ""], ["Outcome", "Count"], ["Wins", total_wins], ["Losses", total_losses]],
    }


def write_sheet_in_chunks(api, sheet_id, title, rows):
    # Keep each Sheets API request comfortably below its recommended payload size.
    start = 1
    batch = []
    batch_bytes = 0
    for row in rows:
        row = [safe(v) for v in row]
        row_size = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if batch and (len(batch) >= 250 or batch_bytes + row_size > MAX_BATCH_BYTES):
            api.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"'{title}'!A{start}",
                valueInputOption="RAW", body={"majorDimension":"ROWS","values":batch}
            ).execute()
            start += len(batch)
            batch, batch_bytes = [], 0
        if row_size > MAX_BATCH_BYTES:
            raise RuntimeError(f"Single row is too large to write to Google Sheets in tab {title}; split the source field further.")
        batch.append(row)
        batch_bytes += row_size
    if batch:
        api.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{title}'!A{start}",
            valueInputOption="RAW", body={"majorDimension":"ROWS","values":batch}
        ).execute()


def main():
    tree = get_tree()
    data = build_data(tree)
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    api = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = api.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    ).execute()
    existing = {x["properties"]["title"]: x for x in meta.get("sheets", [])}
    additions = []
    for name, rows in data.items():
        if name not in existing:
            needed_rows = max(1000, len(rows) + 10)
            needed_cols = max(7 if name == "Charts" else 1, max((len(row) for row in rows), default=1))
            additions.append({"addSheet":{"properties":{
                "title":name,
                "gridProperties":{"rowCount":needed_rows,"columnCount":needed_cols}
            }}})
    if additions:
        api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":additions}).execute()
    # Expand existing tabs before writing large inventories and raw JSON exports.
    meta = api.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    ).execute()
    existing = {x["properties"]["title"]: x for x in meta.get("sheets", [])}
    shrink_columns = []
    expand_rows = []
    for name, rows in data.items():
        sheet = existing[name]["properties"]
        grid = sheet.get("gridProperties", {})
        needed_rows = max(1000, len(rows) + 10)
        needed_cols = max(7 if name == "Charts" else 1, max((len(row) for row in rows), default=1))
        current_cols = grid.get("columnCount", 26)
        current_rows = grid.get("rowCount", 1000)
        # Shrink default 26-column grids first; otherwise expanding a long tab can
        # transiently exceed the workbook-wide 20 million cell limit.
        if current_cols > needed_cols:
            shrink_columns.append({"updateSheetProperties":{
                "properties":{"sheetId":sheet["sheetId"],"gridProperties":{"columnCount":needed_cols}},
                "fields":"gridProperties.columnCount"
            }})
        elif current_cols < needed_cols:
            shrink_columns.append({"updateSheetProperties":{
                "properties":{"sheetId":sheet["sheetId"],"gridProperties":{"columnCount":needed_cols}},
                "fields":"gridProperties.columnCount"
            }})
        if current_rows < needed_rows:
            expand_rows.append({"updateSheetProperties":{
                "properties":{"sheetId":sheet["sheetId"],"gridProperties":{"rowCount":needed_rows}},
                "fields":"gridProperties.rowCount"
            }})
    if shrink_columns:
        api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":shrink_columns}).execute()
    if expand_rows:
        api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":expand_rows}).execute()
    # Clear the tabs first so stale rows disappear when a data source shrinks.
    api.spreadsheets().values().batchClear(
        spreadsheetId=SHEET_ID,
        body={"ranges":[f"'{name}'!A:{column_letter(max(1, (7 if name == 'Charts' else max((len(row) for row in rows), default=1))))}" for name, rows in data.items()]}
    ).execute()
    for name, rows in data.items():
        write_sheet_in_chunks(api, SHEET_ID, name, rows)

    meta = api.spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets(properties(sheetId,title),charts(chartId))"
    ).execute()
    chart_sheet = next(x for x in meta["sheets"] if x["properties"]["title"] == "Charts")
    chart_sheet_id = chart_sheet["properties"]["sheetId"]
    delete_requests = [{"deleteEmbeddedObject":{"objectId":x["chartId"]}} for x in chart_sheet.get("charts", [])]
    if delete_requests:
        api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":delete_requests}).execute()
    charts = [
        {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": "Top 10 Strategies by Number of Trades",
                        "basicChart": {
                            "chartType": "BAR",
                            "legendPosition": "NO_LEGEND",
                            "headerCount": 1,
                            "domains": [{
                                "domain": {"sourceRange": {"sources": [{
                                    "sheetId": chart_sheet_id, "startRowIndex": 0, "endRowIndex": 11,
                                    "startColumnIndex": 0, "endColumnIndex": 1
                                }]}}
                            }],
                            "series": [{
                                "series": {"sourceRange": {"sources": [{
                                    "sheetId": chart_sheet_id, "startRowIndex": 0, "endRowIndex": 11,
                                    "startColumnIndex": 1, "endColumnIndex": 2
                                }]}},
                                "targetAxis": "BOTTOM_AXIS"
                            }],
                            "axis": [
                                {"position": "BOTTOM_AXIS", "title": "Trades"},
                                {"position": "LEFT_AXIS", "title": "Strategy"}
                            ]
                        }
                    },
                    "position": {"overlayPosition": {
                        "anchorCell": {"sheetId": chart_sheet_id, "rowIndex": 0, "columnIndex": 6},
                        "widthPixels": 720, "heightPixels": 360
                    }}
                }
            }
        },
        {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": "Wins vs Losses",
                        "pieChart": {
                            "legendPosition": "RIGHT_LEGEND",
                            "threeDimensional": False,
                            "domain": {"sourceRange": {"sources": [{
                                "sheetId": chart_sheet_id, "startRowIndex": 12, "endRowIndex": 15,
                                "startColumnIndex": 0, "endColumnIndex": 1
                            }]}},
                            "series": {"sourceRange": {"sources": [{
                                "sheetId": chart_sheet_id, "startRowIndex": 12, "endRowIndex": 15,
                                "startColumnIndex": 1, "endColumnIndex": 2
                            }]}}
                        }
                    },
                    "position": {"overlayPosition": {
                        "anchorCell": {"sheetId": chart_sheet_id, "rowIndex": 20, "columnIndex": 6},
                        "widthPixels": 600, "heightPixels": 360
                    }}
                }
            }
        }
    ]
    api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":charts}).execute()
    print("Google Sheets full repository-data sync succeeded:", ", ".join(data))
    print(f"Repository files={len(tree)}; non-candle JSON files fully exported; raw data rows={len(data['Raw Data'])-1}; candle files cataloged={len(data['Candle Catalog'])-1}")


if __name__ == "__main__":
    main()
