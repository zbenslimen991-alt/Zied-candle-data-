import base64, json, os
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

def fetch_json(path):
    r = requests.get(
        f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{quote(path, safe='/')}",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        timeout=45,
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("message", "No API error message returned")
        except ValueError:
            detail = (r.text or "No response body")[:300]
        raise RuntimeError(f"GitHub read failed for {path}: HTTP {r.status_code}; reason: {detail}. Check APP_REPO_TOKEN access to {OWNER}/{REPO} and Contents: Read-only permission.")
    obj = r.json()
    return json.loads(base64.b64decode(obj["content"]).decode("utf-8-sig"))

def safe(v):
    if v is None: return ""
    if isinstance(v, (dict, list)): v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str) and v.startswith(("=", "+", "-", "@")): return "'" + v
    return v

def tab(headers, rows):
    return [headers] + [[safe(v) for v in row] for row in rows]

def build_data():
    p = fetch_json("portfolio_0001.json")
    s = fetch_json("strategy_stats_0001.json")
    signals = fetch_json("signal_history_0001.json")
    bt = fetch_json("backtest_results_0001.json")
    tuning = fetch_json("auto_tuned_config.json")
    opened, pending, closed = p.get("openPositions", []), p.get("pendingLimitOrders", []), p.get("closedPositions", [])
    strategies = []
    for name, v in s.items():
        if not isinstance(v, dict): continue
        trades, wins, losses = v.get("trades", 0) or 0, v.get("wins", 0) or 0, v.get("losses", 0) or 0
        strategies.append((name, trades, wins, losses, round(wins * 100 / trades, 2) if trades else 0,
            v.get("pnlSum", 0), v.get("weight", ""), v.get("tpHits", 0), v.get("slHits", 0),
            v.get("timeHits", 0), v.get("effectiveSamples", 0), v.get("lastWeeklyUpdate", "")))
    strategies.sort(key=lambda x: (x[1], x[4], x[5]), reverse=True)
    backtests = []
    for key, v in bt.items():
        if key.startswith("__") or not isinstance(v, dict): continue
        backtests.append((v.get("instId", key), v.get("trades", ""), v.get("wins", ""), v.get("losses", ""),
            v.get("winRate", ""), v.get("profitFactor", ""), v.get("averageReturn", ""), v.get("maxDrawdown", ""),
            v.get("sharpeRatio", ""), v.get("oosTrades", ""), v.get("positiveOosWindowRate", ""),
            v.get("dataQuality", ""), v.get("updated", "")))
    backtests.sort(key=lambda x: str(x[0]))
    metrics = tuning.get("metrics") or {}
    total_trades = sum(row[1] for row in strategies)
    total_wins = sum(row[2] for row in strategies)
    total_losses = sum(row[3] for row in strategies)
    overall_win_rate = round(total_wins * 100 / total_trades, 2) if total_trades else 0
    return {
      "Overview": [["Metric", "Value"],
        ["Updated UTC", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["Initial capital USD", p.get("initialCapital", "")], ["Cash USD", p.get("cash", "")],
        ["Open positions", len(opened)], ["Pending orders", len(pending)], ["Closed positions", len(closed)],
        ["Strategies tracked", len(s)], ["Top strategies shown", min(20, len(strategies))],
        ["Signal history rows", min(300, len(signals) if isinstance(signals, list) else 0)],
        ["Backtest instruments", len(backtests)], ["AI tuning last run", tuning.get("lastRun", "")],
        ["Total strategy trades", total_trades], ["Total strategy wins", total_wins],
        ["Total strategy losses", total_losses], ["Overall strategy win rate %", overall_win_rate]],
      "Open Positions": tab(["Pair","Direction","Strategy","Timeframe","Entry","Current","TP","SL","Units","Allocated USD","Signal Quality","Opened At"],
        [(x.get("instId",""),x.get("direction",""),x.get("strategy",""),x.get("timeframe",""),x.get("entryPrice",""),x.get("currentPrice",""),x.get("target",""),x.get("stop",""),x.get("units",""),x.get("allocatedUsd",""),x.get("signalQualityScoreAtEntry",""),x.get("openTime","")) for x in opened]),
      "Pending Orders": tab(["Pair","Type","Limit Price","Market at Order","TP","SL","Allocated USD","Units","Strategy","Created At"],
        [(x.get("instId",""),x.get("orderType",""),x.get("limitPrice",""),x.get("marketPriceAtOrder",""),x.get("target",""),x.get("stop",""),x.get("allocatedUsd",""),x.get("units",""),x.get("strategy",""),x.get("createdAt","")) for x in pending]),
      "Top 20 Strategies": tab(["Strategy","Trades","Wins","Losses","Win Rate %","PnL Sum","Weight","TP Hits","SL Hits","Time Exits","Effective Samples","Last Weekly Update"], strategies[:20]),
      "Signal History": tab(["Timestamp","Type","Public Notification","Dedupe Key"],
        [(x.get("timestamp",""),x.get("notificationType",""),x.get("publicNotification",""),x.get("dedupeKey","")) for x in (signals if isinstance(signals,list) else [])[-300:] if isinstance(x,dict)]),
      "Backtest Summary": tab(["Instrument","Trades","Wins","Losses","Win Rate","Profit Factor","Average Return","Max Drawdown","Sharpe Ratio","OOS Trades","Positive OOS Window Rate","Data Quality","Updated"], backtests),
      "AI Learning": [["Metric","Value"]] + [[safe(k),safe(v)] for k,v in metrics.items()] + [["Last run",safe(tuning.get("lastRun",""))],["Window trades",safe(tuning.get("windowTrades",""))]],
      "Data Status": [["Item","Value"],["Source",f"Repository {APP_REPO} via read-only API"],["Refresh UTC",datetime.now(timezone.utc).isoformat(timespec="seconds")],["Mode","Read-only dashboard export; no trading logic changes"]],
      "Charts": [["Strategy", "Trades"]] + [[row[0], row[1]] for row in strategies[:10]] + [["", ""], ["Outcome", "Count"], ["Wins", sum(row[2] for row in strategies)], ["Losses", sum(row[3] for row in strategies)]],
    }

def main():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    api = build("sheets", "v4", credentials=creds, cache_discovery=False)
    data = build_data()
    meta = api.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = {x["properties"]["title"] for x in meta.get("sheets", [])}
    additions = [{"addSheet":{"properties":{"title":name}}} for name in data if name not in existing]
    if additions: api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":additions}).execute()
    api.spreadsheets().values().batchClear(spreadsheetId=SHEET_ID, body={"ranges":[f"'{name}'!A:Z" for name in data]}).execute()
    updates = [{"range":f"'{name}'!A1","majorDimension":"ROWS","values":rows} for name,rows in data.items()]
    api.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID, body={"valueInputOption":"RAW","data":updates}).execute()
    meta = api.spreadsheets().get(spreadsheetId=SHEET_ID, fields="sheets(properties(sheetId,title),charts(chartId))").execute()
    chart_sheet = next(x for x in meta["sheets"] if x["properties"]["title"] == "Charts")
    chart_sheet_id = chart_sheet["properties"]["sheetId"]
    delete_requests = [{"deleteEmbeddedObject":{"objectId":x["chartId"]}} for x in chart_sheet.get("charts", [])]
    if delete_requests: api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":delete_requests}).execute()
    charts = [
      {"addChart":{"chart":{"spec":{"title":"Top 10 Strategies by Number of Trades","basicChart":{"chartType":"BAR","legendPosition":"NO_LEGEND","headerCount":1,"domains":[{"domain":{"sourceRange":{"sources":[{"sheetId":chart_sheet_id,"startRowIndex":0,"endRowIndex":11,"startColumnIndex":0,"endColumnIndex":1}]}}}],"series":[{"series":{"sourceRange":{"sources":[{"sheetId":chart_sheet_id,"startRowIndex":0,"endRowIndex":11,"startColumnIndex":1,"endColumnIndex":2}]}},"targetAxis":"BOTTOM_AXIS"}],"axis":[{"position":"BOTTOM_AXIS","title":"Trades"},{"position":"LEFT_AXIS","title":"Strategy"}]}},"position":{"overlayPosition":{"anchorCell":{"sheetId":chart_sheet_id,"rowIndex":0,"columnIndex":6},"widthPixels":720,"heightPixels":360}}}}},
      {"addChart":{"chart":{"spec":{"title":"Wins vs Losses","pieChart":{"legendPosition":"RIGHT_LEGEND","threeDimensional":False,"domain":{"sourceRange":{"sources":[{"sheetId":chart_sheet_id,"startRowIndex":12,"endRowIndex":15,"startColumnIndex":0,"endColumnIndex":1}]}},"series":{"sourceRange":{"sources":[{"sheetId":chart_sheet_id,"startRowIndex":12,"endRowIndex":15,"startColumnIndex":1,"endColumnIndex":2}]}}}},"position":{"overlayPosition":{"anchorCell":{"sheetId":chart_sheet_id,"rowIndex":20,"columnIndex":6},"widthPixels":600,"heightPixels":360}}}}}
    ]
    api.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests":charts}).execute()
    print("Google Sheets dashboard sync succeeded with totals and charts:", ", ".join(data))

if __name__ == "__main__":
    main()
