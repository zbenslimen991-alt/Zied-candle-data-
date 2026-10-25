import base64
import json
import os
import time
from datetime import datetime, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

APP_REPO = os.environ["APP_REPO"].strip()
if "/" not in APP_REPO:
    raise RuntimeError("APP_REPO must be in owner/repository format, e.g. zbenslimen991-alt/Zied")
OWNER, REPO = APP_REPO.split("/", 1)
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
TOKEN = os.environ["APP_REPO_TOKEN"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
MAX_CELL_CHARS = 40000
MAX_BATCH_BYTES = 1500000
MAX_BATCH_ROWS = 2000
MIN_SECONDS_BETWEEN_SHEET_WRITES = 1.1
_last_sheet_write_at = 0.0


def execute_with_retry(request, attempts=8):
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            delay = min(60, 5 * (2 ** attempt)) if exc.resp.status == 429 else min(30, 2 ** attempt)
            print(f"Google API returned HTTP {exc.resp.status}; retrying in {delay}s.")
            time.sleep(delay)


def execute_sheet_write(request):
    """Throttle Sheets writes to reduce per-user quota errors."""
    global _last_sheet_write_at
    elapsed = time.monotonic() - _last_sheet_write_at
    if elapsed < MIN_SECONDS_BETWEEN_SHEET_WRITES:
        time.sleep(MIN_SECONDS_BETWEEN_SHEET_WRITES - elapsed)
    result = execute_with_retry(request)
    _last_sheet_write_at = time.monotonic()
    return result


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


def first_value(record, *keys, default=""):
    if not isinstance(record, dict):
        return default
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default


def build_data(tree):
    file_index = {x["path"]: x for x in tree}
    # Read only sources needed by the retained dashboard tabs. Avoid downloading
    # every JSON file and exporting large analysis-memory blobs to Google Sheets.
    required = [
        "portfolio_0001.json", "strategy_stats_0001.json",
        "backtest_results_0001.json", "news_intelligence.json"
    ]
    missing = [path for path in required if path not in file_index]
    if missing:
        raise RuntimeError("Required dashboard source file(s) missing: " + ", ".join(missing))
    source = {path: fetch_json(path, file_index) for path in required}
    p = source["portfolio_0001.json"]
    s = source["strategy_stats_0001.json"]
    bt = source["backtest_results_0001.json"]
    news_doc = source["news_intelligence.json"]
    opened = p.get("openPositions", []) if isinstance(p, dict) else []
    pending = p.get("pendingLimitOrders", []) if isinstance(p, dict) else []
    closed = p.get("closedPositions", []) if isinstance(p, dict) else []

    def compact_positions(items, fields):
        rows = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            values = []
            for aliases in fields:
                value = first_value(item, *aliases)
                if aliases[0] == "changePct" and value == "":
                    entry = first_value(item, "entryPrice", "entry_price", "avgEntryPrice", "openPrice")
                    current = first_value(item, "currentPrice", "markPrice", "lastPrice")
                    try:
                        entry_num, current_num = float(entry), float(current)
                        value = round((current_num / entry_num - 1) * 100, 4) if entry_num else ""
                    except (TypeError, ValueError):
                        value = ""
                values.append(value)
            rows.append(values)
        return rows

    open_fields = [
        ("اسم العملة", ("instId", "symbol", "coin", "asset", "instrument")),
        ("سعر الدخول", ("entryPrice", "entry_price", "price", "avgEntryPrice")),
        ("TP", ("target", "takeProfit", "tp", "tpPrice", "takeProfitPrice")),
        ("SL", ("stop", "stopLoss", "sl", "slPrice", "stopLossPrice")),
        ("التغير %", ("changePct", "changePercent", "pnlPercent", "unrealizedPnlPct", "pnlPct")),
        ("الاستراتيجية", ("strategy", "strategyName", "strategy_name")),
    ]
    order_fields = [
        ("اسم العملة", ("instId", "symbol", "coin", "asset", "instrument")),
        ("سعر الدخول", ("entryPrice", "entry_price", "price", "limitPrice", "avgEntryPrice")),
        ("TP", ("target", "takeProfit", "tp", "tpPrice", "takeProfitPrice")),
        ("SL", ("stop", "stopLoss", "sl", "slPrice", "stopLossPrice")),
        ("الاستراتيجية", ("strategy", "strategyName", "strategy_name")),
    ]
    closed_fields = [
        ("اسم العملة", ("instId", "symbol", "coin", "asset", "instrument")),
        ("سعر الدخول", ("entryPrice", "entry_price", "avgEntryPrice", "openPrice")),
        ("طريقة الخروج", ("exitReason", "closeReason", "exitType", "closeType", "reason", "exit_reason")),
        ("صافي الربح", ("netPnl", "netProfit", "realizedPnl", "pnlNet", "profitUsd", "pnl", "profit")),
        ("الاستراتيجية", ("strategy", "strategyName", "strategy_name")),
    ]

    strategies = []
    if isinstance(s, dict):
        for name, v in s.items():
            if not isinstance(v, dict):
                continue
            trades = v.get("trades", 0) or 0
            wins = v.get("wins", 0) or 0
            losses = v.get("losses", 0) or 0
            strategies.append((name, trades, wins, losses,
                round(wins * 100 / trades, 2) if trades else 0, v.get("pnlSum", 0),
                v.get("weight", ""), v.get("tpHits", 0), v.get("slHits", 0),
                v.get("timeHits", 0), v.get("effectiveSamples", 0), v.get("lastWeeklyUpdate", "")))
    strategies.sort(key=lambda x: (x[1], x[4], x[5]), reverse=True)

    backtests = []
    if isinstance(bt, dict):
        for key, v in bt.items():
            if key.startswith("__") or not isinstance(v, dict):
                continue
            backtests.append((v.get("instId", key), v.get("trades", ""), v.get("wins", ""),
                v.get("losses", ""), v.get("winRate", ""), v.get("profitFactor", ""),
                v.get("averageReturn", ""), v.get("maxDrawdown", ""), v.get("sharpeRatio", ""),
                v.get("oosTrades", ""), v.get("positiveOosWindowRate", ""),
                v.get("dataQuality", ""), v.get("updated", "")))
    backtests.sort(key=lambda x: str(x[0]))

    # Preserve the monthly trade view, deriving it from closed positions only.
    monthly = {}
    for item in closed if isinstance(closed, list) else []:
        if not isinstance(item, dict):
            continue
        date_value = first_value(item, "closedAt", "closeTime", "exitTime", "updatedAt", "timestamp", "date")
        month = str(date_value)[:7] if date_value else "غير محدد"
        pnl = first_value(item, "netPnl", "netProfit", "realizedPnl", "pnlNet", "profitUsd", "pnl", "profit", default=0)
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            pnl = 0.0
        slot = monthly.setdefault(month, {"count": 0, "profit": 0.0, "wins": 0, "losses": 0})
        slot["count"] += 1
        slot["profit"] += pnl
        slot["wins"] += int(pnl > 0)
        slot["losses"] += int(pnl < 0)
    monthly_rows = [["الشهر", "عدد الصفقات", "الصفقات الرابحة", "الصفقات الخاسرة", "صافي الربح"]]
    monthly_rows += [[m, v["count"], v["wins"], v["losses"], round(v["profit"], 8)]
                     for m, v in sorted(monthly.items(), reverse=True)]

    total_trades = sum((row[1] or 0) for row in strategies)
    total_wins = sum((row[2] or 0) for row in strategies)
    total_losses = sum((row[3] or 0) for row in strategies)
    overall_win_rate = round(total_wins * 100 / total_trades, 2) if total_trades else 0
    portfolio_summary = [
        ["المؤشر", "القيمة"],
        ["آخر تحديث UTC", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["رأس المال الأولي USD", first_value(p, "initialCapital")],
        ["النقد USD", first_value(p, "cash")],
        ["عدد الصفقات المفتوحة", len(opened) if isinstance(opened, list) else 0],
        ["عدد الأوامر المعلقة", len(pending) if isinstance(pending, list) else 0],
        ["عدد الصفقات المغلقة", len(closed) if isinstance(closed, list) else 0],
        ["إجمالي صفقات الاستراتيجيات", total_trades],
        ["الصفقات الرابحة للاستراتيجيات", total_wins],
        ["الصفقات الخاسرة للاستراتيجيات", total_losses],
        ["نسبة الفوز للاستراتيجيات %", overall_win_rate],
    ]

    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 60 * 60
    recent_news = []
    articles = news_doc.get("articles", []) if isinstance(news_doc, dict) else []
    for article in articles if isinstance(articles, list) else []:
        if not isinstance(article, dict):
            continue
        published = article.get("publishedAt", "")
        try:
            published_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            if published_dt.timestamp() < cutoff:
                continue
        except (TypeError, ValueError, OverflowError):
            pass
        recent_news.append(article)
    recent_news.sort(key=lambda a: str(a.get("publishedAt", "")), reverse=True)
    news_rows = [[a.get("publishedAt", ""), a.get("title", ""), a.get("source", ""),
        ", ".join(str(x) for x in (a.get("assets", []) if isinstance(a.get("assets", []), list) else [])),
        a.get("impact", ""), a.get("sentiment", ""), a.get("sentimentScore", ""),
        a.get("newsLevel", ""), a.get("url", ""), a.get("id", "")] for a in recent_news]
    news_status = [
        ["المؤشر", "القيمة"],
        ["آخر تحديث للأخبار UTC", news_doc.get("updatedAt", "") if isinstance(news_doc, dict) else ""],
        ["حالة الأخبار", news_doc.get("status", "UNKNOWN") if isinstance(news_doc, dict) else "UNKNOWN"],
        ["المصادر الناجحة", (news_doc.get("health", {}) or {}).get("feedsOk", "") if isinstance(news_doc, dict) else ""],
        ["إجمالي المصادر", (news_doc.get("health", {}) or {}).get("feedsTotal", "") if isinstance(news_doc, dict) else ""],
        ["الأخبار خلال آخر 24 ساعة", len(recent_news)],
        ["المعنويات العامة", news_doc.get("sentiment", "") if isinstance(news_doc, dict) else ""],
        ["درجة المعنويات", news_doc.get("sentimentScore", "") if isinstance(news_doc, dict) else ""],
        ["مستوى المخاطر الإخباري", news_doc.get("riskLevel", "") if isinstance(news_doc, dict) else ""],
        ["أخبار عالية التأثير", news_doc.get("highImpactCount", "") if isinstance(news_doc, dict) else ""],
    ]
    return {
        "Open Positions": tab([x[0] for x in open_fields], compact_positions(opened, [x[1] for x in open_fields])),
        "Pending Orders": tab([x[0] for x in order_fields], compact_positions(pending, [x[1] for x in order_fields])),
        "Closed Positions": tab([x[0] for x in closed_fields], compact_positions(closed, [x[1] for x in closed_fields])),
        "All Strategies": tab(["Strategy","Trades","Wins","Losses","Win Rate %","PnL Sum","Weight","TP Hits","SL Hits","Time Exits","Effective Samples","Last Weekly Update"], strategies),
        "Backtest Summary": tab(["Instrument","Trades","Wins","Losses","Win Rate","Profit Factor","Average Return","Max Drawdown","Sharpe Ratio","OOS Trades","Positive OOS Window Rate","Data Quality","Updated"], backtests),
        "News": tab(["تاريخ النشر UTC","عنوان الخبر","المصدر","العملات المعنية","الأهمية","المعنويات","درجة المعنويات","التصنيف","رابط الخبر","معرّف الخبر"], news_rows),
        "News Summary": news_status,
        "Monthly Trades": monthly_rows,
        "Portfolio Summary": portfolio_summary,
    }


def write_sheet_in_chunks(api, sheet_id, title, rows):
    # Keep each Sheets API request comfortably below its recommended payload size.
    start = 1
    batch = []
    batch_bytes = 0
    for row in rows:
        row = [safe(v) for v in row]
        row_size = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if batch and (len(batch) >= MAX_BATCH_ROWS or batch_bytes + row_size > MAX_BATCH_BYTES):
            execute_sheet_write(api.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"'{title}'!A{start}",
                valueInputOption="RAW", body={"majorDimension":"ROWS","values":batch}
            ))
            start += len(batch)
            batch, batch_bytes = [], 0
        if row_size > MAX_BATCH_BYTES:
            raise RuntimeError(f"Single row is too large to write to Google Sheets in tab {title}; split the source field further.")
        batch.append(row)
        batch_bytes += row_size
    if batch:
        execute_sheet_write(api.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{title}'!A{start}",
            valueInputOption="RAW", body={"majorDimension":"ROWS","values":batch}
        ))


def main():
    tree = get_tree()
    data = build_data(tree)
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    api = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheet_id = SHEET_ID
    meta = api.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    ).execute()
    existing = {x["properties"]["title"]: x for x in meta.get("sheets", [])}
    # Monthly Trades and Portfolio Summary are user-requested preserve-as-is tabs.
    # Do not overwrite their existing contents during this compact sync.
    preserve_tabs = {"Portfolio Summary"}
    write_data = {name: rows for name, rows in data.items()
                  if name not in preserve_tabs or name not in existing}

    # Create missing retained tabs.
    additions = []
    for name, rows in write_data.items():
        if name not in existing:
            additions.append({"addSheet": {"properties": {
                "title": name,
                "gridProperties": {
                    "rowCount": max(1000, len(rows) + 10),
                    "columnCount": max(1, max((len(row) for row in rows), default=1))
                }
            }}})
    if additions:
        api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": additions}).execute()
        meta = api.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
        ).execute()
        existing = {x["properties"]["title"]: x for x in meta.get("sheets", [])}

    # The user requested only these tabs. Delete all other worksheet tabs,
    # including Top 20 Strategies, Raw Data, Charts, Signal History and inventories.
    retained = set(data)
    obsolete = [{"deleteSheet": {"sheetId": item["properties"]["sheetId"]}}
                for item in meta.get("sheets", []) if item["properties"]["title"] not in retained]
    if obsolete:
        api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": obsolete}).execute()

    # Refresh metadata after deletion; fit grid sizes to the compact exports.
    meta = api.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    ).execute()
    existing = {x["properties"]["title"]: x for x in meta.get("sheets", [])}
    grid_requests = []
    for name, rows in write_data.items():
        props = existing[name]["properties"]
        needed_rows = max(1000, len(rows) + 10)
        needed_cols = max(1, max((len(row) for row in rows), default=1))
        grid = props.get("gridProperties", {})
        if grid.get("columnCount", 26) != needed_cols:
            grid_requests.append({"updateSheetProperties": {
                "properties": {"sheetId": props["sheetId"], "gridProperties": {"columnCount": needed_cols}},
                "fields": "gridProperties.columnCount"
            }})
        if grid.get("rowCount", 1000) < needed_rows:
            grid_requests.append({"updateSheetProperties": {
                "properties": {"sheetId": props["sheetId"], "gridProperties": {"rowCount": needed_rows}},
                "fields": "gridProperties.rowCount"
            }})
    for name, widths in {
        "Open Positions": [145, 115, 85, 85, 95, 180],
        "Pending Orders": [145, 115, 85, 85, 180],
        "Closed Positions": [145, 115, 130, 110, 180],
    }.items():
        if name in existing:
            sid = existing[name]["properties"]["sheetId"]
            for col, px in enumerate(widths):
                grid_requests.append({"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": px}, "fields": "pixelSize"
                }})
    if grid_requests:
        api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": grid_requests}).execute()

    # Clear and rewrite only retained, compact tabs so old trailing values disappear.
    execute_sheet_write(api.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id,
        body={"ranges": [f"'{name}'!A:{column_letter(max(1, max((len(row) for row in rows), default=1)))}"
                         for name, rows in write_data.items()]}
    ))
    for name, rows in write_data.items():
        write_sheet_in_chunks(api, sheet_id, name, rows)

    print("Google Sheets compact sync succeeded:", ", ".join(write_data))
    print("Only the nine requested tabs remain; raw JSON and Top 20 export are disabled.")


if __name__ == "__main__":
    main()
