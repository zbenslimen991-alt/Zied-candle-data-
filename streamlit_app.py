import json
import re
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Zied Quant Dashboard", page_icon="📊", layout="wide")
st.title("📊 Zied Quant — لوحة البيانات")
st.caption("قراءة فقط من مستودع GitHub الخاص. لا تُنفَّذ أي أوامر تداول من هذه اللوحة.")

def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

GITHUB_TOKEN = secret("GITHUB_TOKEN", "")
REPO = secret("GITHUB_REPO", "zbenslimen991-alt/Zied")
DASHBOARD_PASSWORD = secret("DASHBOARD_PASSWORD", "")

if not GITHUB_TOKEN or not DASHBOARD_PASSWORD:
    st.error("يلزم إعداد GITHUB_TOKEN وDASHBOARD_PASSWORD في إعدادات Secrets الخاصة بتطبيق Streamlit قبل استخدام اللوحة.")
    st.info("لا تضع هذه القيم داخل ملفات GitHub أو داخل كود التطبيق.")
    st.stop()

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.subheader("دخول آمن")
    password = st.text_input("كلمة مرور لوحة التحكم", type="password")
    if st.button("دخول", type="primary"):
        if password and password == DASHBOARD_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("كلمة المرور غير صحيحة.")
    st.stop()

API = "https://api.github.com"

@st.cache_data(ttl=300, show_spinner=False)
def repo_root(repo, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    response = requests.get(f"{API}/repos/{repo}/contents", headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

@st.cache_data(ttl=300, show_spinner=False)
def read_json(repo, path, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    response = requests.get(f"{API}/repos/{repo}/contents/{path}", headers=headers, timeout=90)
    response.raise_for_status()
    return response.json() if "application/json" in response.headers.get("Content-Type", "") else json.loads(response.text)

try:
    root_items = repo_root(REPO, GITHUB_TOKEN)
except Exception as exc:
    st.error("تعذّر الاتصال بمستودع البيانات. تحقق من صلاحية الرمز واسم المستودع.")
    st.caption(f"تفاصيل تقنية: {type(exc).__name__}")
    st.stop()

root_files = {item["name"]: item for item in root_items if item.get("type") == "file"}
json_files = sorted(name for name in root_files if name.lower().endswith(".json"))
excluded = re.compile(r"(secret|token|credential|subscription|user|payment|\.env)", re.I)
safe_json_files = [name for name in json_files if not excluded.search(name)]

categories = {
    "المحفظة": ["portfolio", "signal_history", "trade"],
    "الاستراتيجيات": ["strategy_stats", "strategy", "performance"],
    "Backtest": ["backtest", "test_result"],
    "التعلم والتحليل": ["analysis_memory", "ml_model", "learning", "auto_tuned", "top_gainer", "news_intelligence", "candle_pattern"],
}
with st.sidebar:
    st.header("الإعدادات")
    st.write(f"المستودع: {REPO}")
    st.caption("تحديث بيانات العرض: كل 5 دقائق")
    if st.button("تحديث الآن"):
        st.cache_data.clear()
        st.rerun()
    if st.button("تسجيل الخروج"):
        st.session_state.authenticated = False
        st.rerun()

st.success(f"تم الاتصال بالمستودع. عُثر على {len(safe_json_files)} ملف JSON في المستوى الرئيسي.")
tabs = st.tabs(["نظرة عامة", "المحفظة", "الاستراتيجيات", "Backtest", "التعلم والأخبار", "ملفات البيانات"])

def load_matching(keywords):
    candidates = [f for f in safe_json_files if any(k in f.lower() for k in keywords)]
    loaded = []
    for name in candidates[:12]:
        try:
            loaded.append((name, read_json(REPO, name, GITHUB_TOKEN), None))
        except Exception as exc:
            loaded.append((name, None, type(exc).__name__))
    return loaded

def flatten_for_table(data):
    if isinstance(data, list):
        return pd.json_normalize(data, max_level=2) if data else pd.DataFrame()
    if isinstance(data, dict):
        for key in ("trades", "signals", "strategies", "results", "history", "positions", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return pd.json_normalize(value, max_level=2) if value else pd.DataFrame()
        return pd.json_normalize(data, max_level=1)
    return pd.DataFrame({"value": [str(data)]})

with tabs[0]:
    c1, c2, c3 = st.columns(3)
    c1.metric("ملفات JSON المكتشفة", len(safe_json_files))
    c2.metric("المستودع", "خاص" if REPO.lower().endswith("/zied") else "متصل")
    c3.metric("آخر تحديث للعرض", datetime.now(timezone.utc).strftime("%H:%M UTC"))
    st.subheader("ملفات البيانات المتاحة")
    st.dataframe(pd.DataFrame({"الملف": safe_json_files}), use_container_width=True, hide_index=True)
    st.warning("قائمة الاكتشاف الأولية تشمل ملفات المستوى الرئيسي فقط؛ لم يتم تحميل كل ملفات المجلدات أو جميع الشموع.")

for tab, category, keywords in zip(tabs[1:5], list(categories.keys()), list(categories.values())):
    with tab:
        st.subheader(category)
        matches = load_matching(keywords)
        if not matches:
            st.info("لم يتم العثور على ملفات مطابقة في المستوى الرئيسي للمستودع.")
        for name, data, error in matches:
            with st.expander(name, expanded=(len(matches) == 1)):
                if error:
                    st.warning(f"تعذرت قراءة هذا الملف ({error}). قد يكون كبيرًا أو غير متاح عبر واجهة GitHub.")
                    continue
                try:
                    df = flatten_for_table(data)
                    if df.empty:
                        st.info("الملف لا يحتوي على سجلات جدولية.")
                    else:
                        st.caption(f"عدد الصفوف المعروضة: {len(df)}")
                        st.dataframe(df, use_container_width=True, hide_index=True)
                        csv = df.to_csv(index=False).encode("utf-8-sig")
                        st.download_button("تنزيل CSV", csv, file_name=name.replace(".json", ".csv"), mime="text/csv", key=f"csv_{name}")
                except Exception:
                    st.json(data)

with tabs[5]:
    st.subheader("ملفات JSON المكتشفة")
    selected = st.selectbox("اختر ملفًا من المستوى الرئيسي", safe_json_files)
    if selected:
        item = root_files[selected]
        st.write({"name": item.get("name"), "size_bytes": item.get("size"), "path": item.get("path")})
        if st.button("قراءة الملف المختار"):
            try:
                st.json(read_json(REPO, selected, GITHUB_TOKEN))
            except Exception as exc:
                st.error(f"تعذرت قراءة الملف: {type(exc).__name__}")
    st.info("مجلد candle_history لا يُحمّل تلقائيًا لأن حجمه قد يكون كبيرًا؛ يحتاج إلى قارئ مخصص محدود الحجم.")

st.caption("Zied Quant Dashboard • قراءة فقط • لا تغييرات على الاستراتيجيات أو ملفات التداول.")
