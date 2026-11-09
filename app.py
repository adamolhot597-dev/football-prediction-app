# -*- coding: utf-8 -*-
import io, os, re, json, math, unicodedata, datetime as dt
from difflib import SequenceMatcher
import pandas as pd
import pdfplumber
import requests
import streamlit as st

DATA_DIR = "data"
HIST_PATH = os.path.join(DATA_DIR, "history.csv")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# =====================================================================
# 1) تعريف الأعمدة الـ 32 بعد خانة MIN (مستخرج من الأرقام ماشي من العناوين)
#    الحظ المزدوج = (1X, X2, 12) | المركبة = (1&M, X&M, 2&M, 1&P, X&P, 2&P)
# =====================================================================
def build_selections():
    s = []
    for i, l in enumerate(["1", "X", "2"]):        s.append((0 + i, "1X2", l))
    for i, l in enumerate(["1X", "X2", "12"]):     s.append((3 + i, "حظ مزدوج", l))
    for i, l in enumerate(["1", "X", "2"]):        s.append((7 + i, "هانديكاب", l))      # index 6 = قيمة H
    for i, l in enumerate(["1", "X", "2"]):        s.append((10 + i, "1X2 شوط1", l))
    for i, (line, side) in enumerate([(1.5, "Moins"), (1.5, "Plus"), (2.5, "Moins"),
                                       (2.5, "Plus"), (3.5, "Moins"), (3.5, "Plus")]):
        s.append((13 + i, f"M/P {line}", side))
    s.append((19, "M/P 1.5 شوط1", "Moins")); s.append((20, "M/P 1.5 شوط1", "Plus"))
    for i, l in enumerate(["1&M", "X&M", "2&M", "1&P", "X&P", "2&P"]):
        s.append((24 + i, "نتيجة + M/P 2.5", l))
    s.append((30, "BTTS", "Oui")); s.append((31, "BTTS", "Non"))
    return s

SELECTIONS = build_selections()
N_TOKENS = 32
ODDS_COLS = [f"o{i}" for i in range(N_TOKENS)]

DAY_RE = re.compile(r"^(LUNDI|MARDI|MERCREDI|JEUDI|VENDREDI|SAMEDI|DIMANCHE)$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TOKEN_RE = re.compile(r"-?\d+,\d{2}|-?\d+|-")


def _f(t):
    return float("nan") if t == "-" else float(t.replace(",", "."))


def _ok(*v):
    return all(x is not None and not (isinstance(x, float) and math.isnan(x)) for x in v)


# =====================================================================
# 2) قراءة PDF البرنامج
# =====================================================================
def parse_programme(pdf_file):
    rows, cur_date, seen = [], None, set()
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            headers = []
            for i, w in enumerate(words[:-1]):
                if DAY_RE.match(w["text"]) and DATE_RE.match(words[i + 1]["text"]):
                    headers.append((w["top"], dt.datetime.strptime(words[i + 1]["text"], "%d/%m/%Y").date()))
            headers.sort()
            anchors = sorted([w for w in words if re.match(r"^\d{2}:\d{2}", w["text"]) and w["x0"] < 45],
                             key=lambda w: w["top"])
            for a in anchors:
                main = sorted([w for w in words if abs(w["top"] - a["top"]) <= 2.5], key=lambda w: w["x0"])
                line = re.sub(r"^(\d{2}:\d{2})(?=[A-Z])", r"\1 ", " ".join(w["text"] for w in main))
                m = re.search(r"^(\d{2}:\d{2})\s+([A-Z]+\d?)\s+(\S+)\s+(\d{7})(?:\s+(.*?))?\s1\s+(?=-|\d)(.*)$", line)
                if not m:
                    continue
                hhmm, league, coupon, terminal, _, tail = m.groups()
                if terminal in seen:
                    continue
                tokens = TOKEN_RE.findall(tail)
                d = None
                for top, hd in headers:
                    if top < a["top"]:
                        d = hd
                cur_date = d or cur_date
                term_w = next((w for w in main if w["text"] == terminal), None)
                min_w = next((w for w in main if w["text"] == "1" and term_w and w["x0"] > term_w["x1"]), None)
                x_lo = term_w["x1"] if term_w else 0
                x_hi = min_w["x0"] if min_w else 9999
                names = []
                for w in words:
                    if x_lo <= w["x0"] and w["x1"] <= x_hi + 1 and abs(w["top"] - a["top"]) <= 9:
                        if min(anchors, key=lambda b: abs(b["top"] - w["top"])) is a:
                            names.append(w)
                names.sort(key=lambda w: (round(w["top"]), w["x0"]))
                seen.add(terminal)
                rec = {"date": cur_date, "time": hhmm, "league": league, "coupon": coupon,
                       "terminal": terminal, "match_text": " ".join(w["text"] for w in names),
                       "n_tokens": len(tokens)}
                vals = [_f(t) for t in tokens[:N_TOKENS]] + [float("nan")] * (N_TOKENS - len(tokens))
                rec.update(dict(zip(ODDS_COLS, vals)))
                rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ok"] = df["n_tokens"] == N_TOKENS
    df["marge_1X2_%"] = ((1 / df[["o0", "o1", "o2"]]).sum(axis=1).sub(1) * 100).round(1)
    return df


@st.cache_data(show_spinner=False)
def parse_bytes(b):
    return parse_programme(io.BytesIO(b))


# =====================================================================
# 3) API-Football: جلب النتائج + ربط الأسماء
# =====================================================================
API_BASE = "https://v3.football.api-sports.io"
FINAL = {"FT", "AET", "PEN"}
ALIAS = {"mayence": "mainz", "cologne": "koln", "francfort": "frankfurt", "fribourg": "freiburg",
         "hambourg": "hamburg", "naples": "napoli", "rome": "roma", "seville": "sevilla",
         "barcelone": "barcelona", "corogne": "coruna", "malines": "mechelen", "gantoise": "gent",
         "gand": "gent", "courtrai": "kortrijk", "anvers": "antwerp", "bruges": "brugge",
         "vienne": "wien", "lyonnais": "lyon", "nimegue": "nec", "bale": "basel", "bergame": "bergamo",
         "saint": "st", "trond": "truiden", "turin": "torino", "munich": "munchen", "athletico": "atletico"}
STOP = {"fc", "cf", "sc", "ac", "as", "fk", "sk", "cd", "ud", "club", "de", "la", "le", "ca", "rc",
        "afc", "sv", "fsv", "1907", "calcio", "sport"}


def _tokens(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    t = [ALIAS.get(x, x) for x in re.findall(r"[a-z0-9]+", s)]
    return [x for x in t if x not in STOP and len(x) > 1]


def similarity(a_text, b_text):
    a, b = _tokens(a_text), _tokens(b_text)
    if not a or not b:
        return 0.0
    best = lambda t, pool: max(SequenceMatcher(None, t, u).ratio() for u in pool)
    return (sum(best(t, b) for t in a) / len(a) + sum(best(t, a) for t in b) / len(b)) / 2


def api_get(path, key, params=None):
    r = requests.get(f"{API_BASE}/{path}", headers={"x-apisports-key": key}, params=params or {}, timeout=40)
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(str(j["errors"]))
    return j


def fetch_fixtures(date, key, refresh=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"fx_{date}.json")
    if os.path.exists(path) and not refresh:
        return json.load(open(path, encoding="utf-8"))
    resp = api_get("fixtures", key, {"date": str(date), "timezone": "Africa/Casablanca"}).get("response", [])
    if date < dt.date.today():
        json.dump(resp, open(path, "w", encoding="utf-8"))
    return resp


def _fx(f):
    ft, ht = f["score"]["fulltime"], f["score"]["halftime"]
    gh = ft["home"] if ft["home"] is not None else f["goals"]["home"]
    ga = ft["away"] if ft["away"] is not None else f["goals"]["away"]
    return {"when": dt.datetime.fromisoformat(f["fixture"]["date"]).replace(tzinfo=None),
            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
            "gh": gh, "ga": ga, "hth": ht["home"], "hta": ht["away"], "status": f["fixture"]["status"]["short"]}


def match_results(df, key, refresh=False, threshold=0.72, progress=None):
    cache, dates, out = {}, sorted(df["date"].dropna().unique()), []
    for i, d in enumerate(dates):
        cache[d] = [_fx(f) for f in fetch_fixtures(d, key, refresh)]
        if progress:
            progress((i + 1) / len(dates))
    for _, r in df.iterrows():
        pdt = dt.datetime.combine(r["date"], dt.datetime.strptime(r["time"], "%H:%M").time())
        best, bs = None, 0.0
        for f in cache.get(r["date"], []):
            s = similarity(r["match_text"], f"{f['home']} {f['away']}")
            if abs((f["when"] - pdt).total_seconds()) > 5400:
                s -= 0.15
            if s > bs:
                best, bs = f, s
        rec = {"terminal": r["terminal"], "date": r["date"], "time": r["time"], "match_text": r["match_text"],
               "api_match": "", "score_match": round(bs, 2), "status": "", "gh": None, "ga": None,
               "hth": None, "hta": None}
        if best and bs >= threshold:
            rec.update(api_match=f"{best['home']} - {best['away']}", status=best["status"], gh=best["gh"],
                       ga=best["ga"], hth=best["hth"], hta=best["hta"])
        out.append(rec)
    return pd.DataFrame(out)


# =====================================================================
# 4) حساب الكوطات الرابحة
# =====================================================================
def _res(a, b):
    return "1" if a > b else ("2" if a < b else "X")


def evaluate_match(r, gh, ga, hth, hta):
    out, H, total, res = [], r["o6"], gh + ga, _res(gh, ga)
    for idx, market, sel in SELECTIONS:
        odd = r[f"o{idx}"]
        if not _ok(odd):
            continue
        won = None
        if market == "1X2":
            won = res == sel
        elif market == "حظ مزدوج":
            won = (res != "X") if sel == "12" else (res in sel)
        elif market == "هانديكاب":
            if _ok(H): won = _res(gh - H, ga) == sel
        elif market == "1X2 شوط1":
            if _ok(hth, hta): won = _res(hth, hta) == sel
        elif "شوط1" in market:
            if _ok(hth, hta): won = (hth + hta < 1.5) if sel == "Moins" else (hth + hta > 1.5)
        elif market.startswith("M/P"):
            line = float(market.split()[1]); won = (total < line) if sel == "Moins" else (total > line)
        elif market == "نتيجة + M/P 2.5":
            rr, mp = sel.split("&"); won = (res == rr) and ((total < 2.5) if mp == "M" else (total > 2.5))
        elif market == "BTTS":
            both = gh > 0 and ga > 0; won = both if sel == "Oui" else (not both)
        out.append({"market": market, "selection": sel, "odd": float(odd), "won": won})
    return out


def build_history(prog, res):
    m = prog.merge(res[["terminal", "status", "gh", "ga", "hth", "hta"]], on="terminal")
    m = m[m["status"].isin(FINAL) & m["gh"].notna() & m["ga"].notna()]
    rows = []
    for _, r in m.iterrows():
        h = None if pd.isna(r["hth"]) else int(r["hth"])
        a = None if pd.isna(r["hta"]) else int(r["hta"])
        for e in evaluate_match(r, int(r["gh"]), int(r["ga"]), h, a):
            rows.append({"mid": f"{r['date']}_{r['terminal']}", "date": r["date"], "league": r["league"],
                         "match": r["match_text"], "score": f"{int(r['gh'])}-{int(r['ga'])}", **e})
    return pd.DataFrame(rows)


def winning_summary(sel):
    keep = ["1X2", "حظ مزدوج", "M/P 2.5", "M/P 3.5", "BTTS"]
    w = sel[(sel["won"] == True) & (sel["market"].isin(keep))]
    t = (w["market"] + ":" + w["selection"] + "@" + w["odd"].map("{:.2f}".format)).groupby(w["mid"]).apply(" | ".join)
    base = sel.drop_duplicates("mid").set_index("mid")[["date", "league", "match", "score"]]
    return base.join(t.rename("الكوطات الرابحة")).reset_index(drop=True)


# =====================================================================
# 5) التحليل (back-test) واستخراج الأنماط
# =====================================================================
BANDS = [1.0, 1.2, 1.4, 1.7, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0, 1000]
BAND_LABELS = ["<1.20", "1.20-1.39", "1.40-1.69", "1.70-1.99", "2.00-2.49", "2.50-2.99",
               "3.00-3.99", "4.00-5.99", "6.00-9.99", "10+"]


def add_band(df):
    df = df.copy()
    df["band"] = pd.cut(df["odd"], BANDS, labels=BAND_LABELS, right=False)
    return df


def hit_table(sel, by=("market", "selection", "band")):
    d = add_band(sel[sel["won"].notna()])
    d["won"] = d["won"].astype(bool)
    d["ret"] = d["won"] * d["odd"] - 1
    d["imp"] = 1 / d["odd"]
    g = d.groupby(list(by), observed=True).agg(n=("won", "size"), hits=("won", "sum"),
                                                imp=("imp", "mean"), roi=("ret", "mean")).reset_index()
    g["نسبة الربح %"] = (g["hits"] / g["n"] * 100).round(1)
    g["توقع الكوط %"] = (g["imp"] * 100).round(1)
    g["ROI %"] = (g["roi"] * 100).round(1)
    return g[list(by) + ["n", "hits", "نسبة الربح %", "توقع الكوط %", "ROI %"]]


def selections_from_programme(prog):
    rows = []
    for _, r in prog.iterrows():
        for idx, market, sel in SELECTIONS:
            odd = r[f"o{idx}"]
            if _ok(odd):
                rows.append({"date": r["date"], "time": r["time"], "match": r["match_text"],
                             "market": market, "selection": sel, "odd": float(odd)})
    return pd.DataFrame(rows)


def candidates(future, table, min_n):
    m = add_band(future).merge(table, on=["market", "selection", "band"], how="left")
    m = m[(m["n"] >= min_n) & (m["ROI %"] > 0)]
    return m.sort_values("ROI %", ascending=False)


# =====================================================================
# 6) حفظ/تحميل الأرشيف
# =====================================================================
def _b(v):
    if v in (True, "True", "true", 1, 1.0): return True
    if v in (False, "False", "false", 0, 0.0): return False
    return None


def load_history():
    if not os.path.exists(HIST_PATH):
        return pd.DataFrame()
    return clean_history(pd.read_csv(HIST_PATH))


def clean_history(h):
    h = h.copy()
    h["date"] = pd.to_datetime(h["date"]).dt.date
    h["won"] = h["won"].map(_b).astype(object)
    return h


def merge_history(old, new):
    both = pd.concat([old, new], ignore_index=True) if not old.empty else new
    return both.drop_duplicates(["mid", "market", "selection"], keep="last").reset_index(drop=True)


def save_history(h):
    os.makedirs(DATA_DIR, exist_ok=True)
    h.to_csv(HIST_PATH, index=False)


# =====================================================================
# 7) الواجهة (نفس تنسيق الموقع ديالك: 3 أقسام + RTL)
# =====================================================================
def get_key():
    k = st.session_state.get("api_key", "")
    if k:
        return k
    try:
        return st.secrets.get("API_FOOTBALL_KEY", "")
    except Exception:
        return ""


def main():
    st.set_page_config(page_title="منصة تحليل وتعلم كوطات MDJS", page_icon="⚽", layout="wide")
    st.markdown("""
    <style>
        body, div, span, p, table, th, td { direction: rtl; text-align: right; }
        .stDataFrame { direction: rtl; }
    </style>
    """, unsafe_allow_html=True)
    st.title("⚽ محرك الهندسة العكسية والتعلم الذكي (MDJS)")
    st.markdown("---")

    if "hist" not in st.session_state:
        st.session_state.hist = load_history()
    hist = st.session_state.hist

    st.sidebar.header("🎯 قائمة التحكم")
    mode = st.sidebar.radio("اختر القسم:", ["📥 رفع الأرشيف والتعلم التلقائي",
                                            "🧠 فحص البرنامج الجديد واكتشاف النمط",
                                            "⚙️ ربط API النتائج (API-Football)"])
    st.sidebar.metric("ماتشات فالأرشيف", hist["mid"].nunique() if not hist.empty else 0)
    if not hist.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية من الأرشيف (CSV)", hist.to_csv(index=False).encode("utf-8"),
                                   "history.csv", "text/csv")
    bk = st.sidebar.file_uploader("استرجاع أرشيف محفوظ (CSV)", type="csv", key="bk")
    if bk is not None and st.sidebar.button("دمج الأرشيف المرفوع"):
        st.session_state.hist = merge_history(hist, clean_history(pd.read_csv(bk)))
        save_history(st.session_state.hist)
        st.rerun()

    # ------------------------------------------------------------- القسم 1
    if mode.startswith("📥"):
        st.subheader("📁 قسم تدريب النظام على البرامج السابقة (Auto-Backtesting)")
        st.info("ارفع برنامج قديم (PDF). النظام كيقرا الماتشات والكوطات، كيجيب النتائج الحقيقية من API-Football، "
                "كيحسب أي كوط ربحات، وكيحفظهم فالأرشيف.")
        pdf = st.file_uploader("📂 ارفع برنامج الرهانات القديم (PDF):", type=["pdf"], key="arch_pdf")
        if pdf is not None:
            prog = parse_bytes(pdf.getvalue())
            if prog.empty:
                st.error("ما قدرتش نقرا الماتشات من هاد الملف."); return
            c1, c2, c3 = st.columns(3)
            c1.metric("ماتشات مقروءة", len(prog)); c2.metric("أسطر كاملة", int(prog["ok"].sum()))
            c3.metric("هامش الشركة فـ 1X2", f"{prog['marge_1X2_%'].mean():.1f}%")
            all_dates = sorted(prog["date"].dropna().unique())
            past = [d for d in all_dates if d < dt.date.today()]
            dates = st.multiselect("التواريخ اللي بغيتي تجيب نتائجهم (كل تاريخ = طلب API واحد):", all_dates, default=past)
            refresh = st.checkbox("تجاهل الكاش")
            if st.button("🔎 جلب النتائج الحقيقية من API-Football", type="primary"):
                key = get_key()
                if not key:
                    st.error("حط مفتاح API فقسم '⚙️ ربط API' أول."); return
                sub = prog[prog["date"].isin(dates) & prog["ok"]].reset_index(drop=True)
                bar = st.progress(0.0)
                try:
                    res = match_results(sub, key, refresh=refresh, progress=bar.progress)
                except Exception as e:
                    st.error(f"❌ خطأ فـ API: {e}"); return
                st.session_state.sub, st.session_state.res = sub, res
                st.session_state.pop("res_editor", None)
            if "res" in st.session_state:
                res, sub = st.session_state.res, st.session_state.sub
                st.info(f"تلقات النتيجة ديال {int(res['gh'].notna().sum())} من {len(res)} ماتش. "
                        "اللي ما تلقاش، عمّر أهدافو بيدك وكتب FT فخانة الحالة (أهداف 1 = الفريق الأول فالبرنامج).")
                edited = st.data_editor(
                    res, key="res_editor", hide_index=True, use_container_width=True,
                    disabled=["terminal", "date", "time", "match_text", "api_match", "score_match"],
                    column_config={"gh": st.column_config.NumberColumn("أهداف 1", min_value=0, step=1),
                                   "ga": st.column_config.NumberColumn("أهداف 2", min_value=0, step=1),
                                   "hth": st.column_config.NumberColumn("شوط1 (1)", min_value=0, step=1),
                                   "hta": st.column_config.NumberColumn("شوط1 (2)", min_value=0, step=1)})
                if st.button("🧠 ابدأ استخراج وتحليل وحفظ الأرشيف", type="primary"):
                    new = build_history(sub, edited)
                    if new.empty:
                        st.warning("ما كاين حتى ماتش منتهي بنتيجة.")
                    else:
                        st.session_state.hist = merge_history(st.session_state.hist, new)
                        save_history(st.session_state.hist)
                        st.session_state.last = new
                        st.success(f"✨ تحفظو {new['mid'].nunique()} ماتش. الأرشيف دابا فيه "
                                   f"{st.session_state.hist['mid'].nunique()} ماتش.")
            if "last" in st.session_state:
                st.markdown("### 📊 الكوطات الرابحة فكل ماتش:")
                st.dataframe(winning_summary(st.session_state.last), use_container_width=True, hide_index=True)

    # ------------------------------------------------------------- القسم 2
    elif mode.startswith("🧠"):
        st.subheader("🧠 محرك الفحص والبحث عن الثغرات (Pattern Detection)")
        if hist.empty:
            st.warning("الأرشيف خاوي. ابدا بقسم '📥 رفع الأرشيف' بعدة برامج قديمة.")
        else:
            n = hist["mid"].nunique()
            c1, c2 = st.columns(2)
            min_n = c1.slider("أقل عدد ماتشات فالفئة باش نثق فيها", 10, 200, 30)
            by_league = c2.checkbox("فصّل حسب الدوري")
            if n < 300:
                st.warning(f"الأرشيف فيه {n} ماتش فقط. أي نمط دابا غالبا صدفة، زيد برامج (300 ماتش على الأقل).")
            by = ("market", "selection", "band") + (("league",) if by_league else ())
            tbl = hit_table(hist, by=by)
            with st.expander("📈 شنو تعلم النظام من الأرشيف (نسبة الربح الحقيقية حسب فئة الكوط)"):
                bets = hist[hist["won"].notna()].copy()
                bets["ret"] = bets["won"].astype(bool) * bets["odd"] - 1
                st.write("الربح/الخسارة لو لعبنا كل الاختيارات بمبلغ ثابت:")
                st.dataframe(bets.groupby("market").agg(رهانات=("ret", "size"),
                             ROI=("ret", lambda x: f"{x.mean() * 100:.1f}%")), use_container_width=True)
                st.dataframe(tbl[tbl["n"] >= min_n].sort_values("ROI %", ascending=False),
                             use_container_width=True, hide_index=True)
        new_pdf = st.file_uploader("📂 ارفع برنامج الرهانات الجديد (PDF):", type=["pdf"], key="new_pdf")
        if new_pdf is not None and not hist.empty:
            newp = parse_bytes(new_pdf.getvalue())
            if newp.empty:
                st.error("ما قدرتش نقرا الماتشات."); return
            st.success(f"📄 قريت {len(newp)} ماتش من {new_pdf.name}")
            if st.button("🚀 تحليل المباريات والبحث عن الثغرات الرياضية", type="primary"):
                fut = selections_from_programme(newp[newp["ok"]])
                cand = candidates(fut, hit_table(hist), min_n)
                if cand.empty:
                    st.error("ما لقيت حتى اختيار فئتو ROI موجب بعينة كافية. هادا نتيجة صريحة: الكوطات هنا "
                             "كتعكس هامش الشركة، وما كاين حتى ثغرة مثبتة فالأرشيف الحالي.")
                else:
                    st.markdown(f"### 📊 {len(cand)} اختيار طابق فئات ROI موجب فالأرشيف:")
                    st.dataframe(cand[["date", "time", "match", "market", "selection", "odd", "band", "n",
                                       "نسبة الربح %", "توقع الكوط %", "ROI %"]],
                                 use_container_width=True, hide_index=True)
                    st.caption("هادشي إحصاء تاريخي ماشي ضمان، وما فيهش الإصابات والتشكيلات.")
            with st.expander("كل ماتشات البرنامج الجديد"):
                st.dataframe(newp[["date", "time", "league", "match_text", "o0", "o1", "o2", "o15", "o16", "o30", "o31"]]
                             .rename(columns={"o0": "1", "o1": "X", "o2": "2", "o15": "Moins 2.5",
                                              "o16": "Plus 2.5", "o30": "BTTS oui", "o31": "BTTS non"}),
                             use_container_width=True, hide_index=True)

    # ------------------------------------------------------------- القسم 3
    else:
        st.subheader("⚙️ إعدادات ربط النتائج الحية والتاريخية")
        st.info("حط مفتاح API-Football ديالك، ودير فحص الاتصال. الخطة المجانية فيها 100 طلب فاليوم.")
        k = st.text_input("مفتاح API-Key الخاص بك:", type="password", value=st.session_state.get("api_key", ""))
        if st.button("🔗 فحص الاتصال", type="primary"):
            if not k and not get_key():
                st.warning("⚠️ الرجاء إدخال المفتاح.")
            else:
                st.session_state.api_key = k or get_key()
                try:
                    j = api_get("status", st.session_state.api_key)
                    info = j.get("response", {})
                    req, sub = info.get("requests", {}), info.get("subscription", {})
                    st.success("✅ المفتاح صحيح.")
                    st.write(f"الخطة: **{sub.get('plan', '?')}** | الطلبات اليوم: "
                             f"**{req.get('current', '?')} / {req.get('limit_day', '?')}**")
                except Exception as e:
                    st.error(f"❌ فشل الاتصال: {e}")


if __name__ == "__main__":
    main()
