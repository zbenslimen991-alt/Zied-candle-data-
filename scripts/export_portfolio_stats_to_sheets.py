#!/usr/bin/env python3
import json, os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

TZ = ZoneInfo("Africa/Tunis")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "19sSL3mIVUNHGZk9_C0glieR30LG7Q3TJPbEg7O-nErw")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SUMMARY = "ملخص المحفظة"
MONTHLY = "الصفقات الشهرية"

def parse_dt(v):
    if not v: return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError: pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
    except ValueError: return None

def num(v):
    try:
        x = float(v or 0)
        return x if x == x and abs(x) != float("inf") else 0.0
    except (ValueError, TypeError): return 0.0

def month_start(d): return date(d.year, d.month, 1)
def next_month(d): return date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)

def main():
    with open("portfolio_0001.json", encoding="utf-8") as f: p = json.load(f)
    now = datetime.now(TZ); today = now.date(); week = today - timedelta(days=today.weekday())
    trades = []
    for t in p.get("closedPositions", []):
        if not isinstance(t, dict): continue
        dt = parse_dt(t.get("closeTime") or t.get("closedAt") or t.get("exitTime"))
        if dt and ("profitUsd" in t or "pnlUsd" in t):
            trades.append((dt.date(), num(t.get("profitUsd", t.get("pnlUsd")))))
    trades.sort()
    periods = [
        ("اليوم", today, today + timedelta(days=1)),
        ("هذا الأسبوع", week, today + timedelta(days=1)),
        ("هذا الشهر", month_start(today), today + timedelta(days=1)),
        ("هذه السنة", date(today.year, 1, 1), today + timedelta(days=1)),
        ("الإجمالي المتاح", date.min, today + timedelta(days=1)),
    ]
    summary = [["الفترة", "من", "إلى", "عدد الصفقات المغلقة", "الرابحة", "الخاسرة", "الربح الصافي (USDT)"]]
    for label, start, end in periods:
        rows = [(d, pnl) for d, pnl in trades if start <= d < end]
        summary.append([label, "" if start == date.min else start.isoformat(), (end-timedelta(days=1)).isoformat(),
                        len(rows), sum(x > 0 for _, x in rows), sum(x < 0 for _, x in rows), round(sum(x for _, x in rows), 4)])
    monthly = [["الشهر", "عدد الصفقات المغلقة", "الرابحة", "الخاسرة", "الربح الصافي (USDT)"]]
    cursor = month_start(trades[0][0]) if trades else month_start(today)
    while cursor <= month_start(today):
        end = next_month(cursor); rows = [(d, pnl) for d, pnl in trades if cursor <= d < end]
        monthly.append([cursor.strftime("%Y-%m"), len(rows), sum(x > 0 for _, x in rows), sum(x < 0 for _, x in rows), round(sum(x for _, x in rows), 4)])
        cursor = end
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    cred = Credentials.from_service_account_info(info, scopes=SCOPES)
    s = AuthorizedSession(cred); base = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
    r = s.get(base, params={"fields":"sheets.properties.title"}, timeout=30); r.raise_for_status()
    tabs = {x["properties"]["title"] for x in r.json().get("sheets", [])}
    add = [{"addSheet":{"properties":{"title":t}}} for t in (SUMMARY, MONTHLY) if t not in tabs]
    if add:
        r = s.post(base + ":batchUpdate", json={"requests":add}, timeout=30); r.raise_for_status()
    for tab, values in ((SUMMARY, summary), (MONTHLY, monthly)):
        q = "'" + tab.replace("'", "''") + "'"
        r = s.post(f"{base}/values/{q}:clear", json={}, timeout=30); r.raise_for_status()
        r = s.put(f"{base}/values/{q}!A1", params={"valueInputOption":"USER_ENTERED"}, json={"values":values}, timeout=30); r.raise_for_status()
    print(f"Google Sheets updated. closed_trades={len(trades)} at {now.isoformat()}")

if __name__ == "__main__": main()
