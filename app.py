# -*- coding: utf-8 -*-
import io, os, re, json, math, hashlib, unicodedata, datetime as dt
from difflib import SequenceMatcher
import pandas as pd
import pdfplumber
import requests
import streamlit as st

DATA_DIR = "data"
HIST_PATH = os.path.join(DATA_DIR, "history.csv")
PEND_PATH = os.path.join(DATA_DIR, "pending.csv")
CACHE_DIR = os.path.join(DATA_DIR, "cache")


def today_ma():
    """تاريخ اليوم بتوقيت المغرب (UTC+1)."""
    return (dt.datetime.utcnow() + dt.timedelta(hours=1)).date()


def now_ma():
    return dt.datetime.utcnow() + dt.timedelta(hours=1)


# =====================================================================
# 1) الأعمدة الـ 32 بعد خانة MIN (مستخرجة من الأرقام ماشي من العناوين)
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
# مجموعات الاختيارات المتنافية (باش نحيدو هامش الشركة ونحسبو الاحتمال الصافي)
GROUPS = [[0, 1, 2], [7, 8, 9], [10, 11, 12], [13, 14], [15, 16], [17, 18], [19, 20],
          [24, 25, 26, 27, 28, 29], [30, 31]]
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
                tokens = TOKEN_RE.findall(tail)
                d = None
                for top, hd in headers:
                    if top < a["top"]:
                        d = hd
                cur_date = d or cur_date
                if (cur_date, terminal) in seen:
                    continue
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
                seen.add((cur_date, terminal))
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
    df["mid"] = df["date"].astype(str) + "_" + df["terminal"]
    df["marge_1X2_%"] = ((1 / df[["o0", "o1", "o2"]]).sum(axis=1).sub(1) * 100).round(1)
    return df


PARSER_VERSION = "2026-11-10-v3"


def parse_bytes(b):
    """يقرا الـ PDF مرة وحدة لكل جلسة. ما كنستعملش st.cache_data حيت كيحتفظ بنتائج قديمة
    من نسخ الكود السابقة (هادا كان سبب KeyError: 'mid')."""
    cache = st.session_state.setdefault("_parse_cache", {})
    key = (hashlib.md5(b).hexdigest(), PARSER_VERSION)
    if key not in cache:
        df = parse_programme(io.BytesIO(b))
        if not df.empty and "mid" not in df.columns:
            df["mid"] = df["date"].astype(str) + "_" + df["terminal"].astype(str)
        cache[key] = df
    return cache[key].copy()


# =====================================================================
# 3) جلب النتائج: API-Football + football-data.org (بديل) + ربط الأسماء
# =====================================================================
AF_BASE = "https://v3.football.api-sports.io"
FD_BASE = "https://api.football-data.org/v4"
FINAL = {"FT", "AET", "PEN"}
NOT_DONE = {"NS", "TBD", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE",
            "SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}
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


def explain_error(err):
    e = str(err)
    if "suspended" in e:
        return ("حساب API-Football موقوف. دخل https://dashboard.api-football.com وشوف السبب "
                "(تأكيد الإيميل، الحد، الدفع...) ولا كلم الدعم ديالهم.")
    m = re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", e)
    if m:
        st.session_state["af_window"] = (m.group(1), m.group(2))
        return f"الخطة المجانية ديال API-Football كتسمح غير بالتواريخ من {m.group(1)} حتى {m.group(2)}."
    if "401" in e or "403" in e or "token" in e.lower() or "invalid" in e.lower():
        return f"المفتاح غير صحيح ولا ما عندوش صلاحية: {e}"
    if "429" in e or "rate" in e.lower() or "limit" in e.lower():
        return "وصلتي للحد المسموح، عاود من بعد شوية."
    return e


def _cache_path(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def api_get(path, key, params=None):
    r = requests.get(f"{AF_BASE}/{path}", headers={"x-apisports-key": key}, params=params or {}, timeout=40)
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(str(j["errors"]))
    return j


def fetch_af(date, key, refresh=False):
    path = _cache_path(f"fx_af_{date}.json")
    if os.path.exists(path) and not refresh:
        return json.load(open(path, encoding="utf-8"))
    resp = api_get("fixtures", key, {"date": str(date), "timezone": "Africa/Casablanca"}).get("response", [])
    if all(f["fixture"]["status"]["short"] not in NOT_DONE for f in resp):
        json.dump(resp, open(path, "w", encoding="utf-8"))
    return resp


def _fx_af(f):
    ft, ht = f["score"]["fulltime"], f["score"]["halftime"]
    gh = ft["home"] if ft["home"] is not None else f["goals"]["home"]
    ga = ft["away"] if ft["away"] is not None else f["goals"]["away"]
    return {"when": dt.datetime.fromisoformat(f["fixture"]["date"]).replace(tzinfo=None),
            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
            "gh": gh, "ga": ga, "hth": ht["home"], "hta": ht["away"],
            "status": f["fixture"]["status"]["short"], "src": "API-Football"}


def fetch_fd(date, token, refresh=False):
    path = _cache_path(f"fx_fd_{date}.json")
    if os.path.exists(path) and not refresh:
        return json.load(open(path, encoding="utf-8"))
    r = requests.get(f"{FD_BASE}/matches", headers={"X-Auth-Token": token}, timeout=40,
                     params={"dateFrom": str(date - dt.timedelta(days=1)), "dateTo": str(date + dt.timedelta(days=1))})
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {j.get('message', '')}")
    resp = j.get("matches", [])
    if all(m.get("status") not in NOT_DONE for m in resp):
        json.dump(resp, open(path, "w", encoding="utf-8"))
    return resp


def _fx_fd(m):
    sc = m.get("score", {})
    reg = sc.get("regularTime") if (sc.get("regularTime") or {}).get("home") is not None else sc.get("fullTime")
    reg, ht = reg or {}, sc.get("halfTime") or {}
    when = dt.datetime.strptime(m["utcDate"], "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(hours=1)
    status = "FT" if m.get("status") in ("FINISHED", "AWARDED") else m.get("status", "")
    return {"when": when, "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
            "gh": reg.get("home"), "ga": reg.get("away"), "hth": ht.get("home"), "hta": ht.get("away"),
            "status": status, "src": "football-data.org"}


def day_fixtures(d, keys, refresh=False):
    recs, errs = [], []
    if keys.get("af"):
        try:
            recs += [_fx_af(f) for f in fetch_af(d, keys["af"], refresh)]
        except Exception as e:
            errs.append(f"{d} | API-Football: {explain_error(e)}")
    if keys.get("fd"):
        try:
            recs += [x for x in (_fx_fd(m) for m in fetch_fd(d, keys["fd"], refresh)) if x["when"].date() == d]
        except Exception as e:
            errs.append(f"{d} | football-data.org: {explain_error(e)}")
    return recs, errs


def match_results(df, keys, refresh=False, threshold=0.72, progress=None):
    cache, errors, out = {}, [], []
    dates = sorted(df["date"].dropna().unique())
    for i, d in enumerate(dates):
        cache[d], errs = day_fixtures(d, keys, refresh)
        errors += errs
        if progress:
            progress((i + 1) / len(dates))
    for _, r in df.iterrows():
        pdt = dt.datetime.combine(r["date"], dt.datetime.strptime(r["time"], "%H:%M").time())
        best, bs = None, 0.0
        for f in cache.get(r["date"], []):
            s = similarity(r["match_text"], f"{f['home']} {f['away']}")
            if abs((f["when"] - pdt).total_seconds()) > 5400:
                s -= 0.15
            if s > bs or (s == bs and best is not None and best["hth"] is None and f["hth"] is not None):
                best, bs = f, s
        rec = {"terminal": r["terminal"], "date": r["date"], "time": r["time"], "match_text": r["match_text"],
               "api_match": "", "score_match": round(bs, 2), "status": "", "gh": None, "ga": None,
               "hth": None, "hta": None, "src": ""}
        if best and bs >= threshold and best["status"] in FINAL and best["gh"] is not None:
            rec.update(api_match=f"{best['home']} - {best['away']}", status=best["status"], gh=best["gh"],
                       ga=best["ga"], hth=best["hth"], hta=best["hta"], src=best["src"])
        out.append(rec)
    return pd.DataFrame(out), sorted(set(errors))


# ----- لصق النتائج يدويا (حل بديل إيلا ما خدمش API) -----
LINE_RE = re.compile(r"^\s*(.+?)\s+(\d{1,2})\s*[-:–]\s*(\d{1,2})\s+(.+?)\s*(?:\(\s*(\d{1,2})\s*[-:–]\s*(\d{1,2})\s*\))?\s*$")


def _first_pos(prog_tokens, name):
    nt = _tokens(name)
    if not nt or not prog_tokens:
        return 99
    return min(i for i, p in enumerate(prog_tokens) if SequenceMatcher(None, p, nt[0]).ratio() > 0.75) \
        if any(SequenceMatcher(None, p, nt[0]).ratio() > 0.75 for p in prog_tokens) else 99


def apply_pasted(res, text, threshold=0.68):
    res, applied = res.copy(), 0
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        t1, g1, g2, t2, h1, h2 = m.groups()
        best_i, best_s = None, 0.0
        for i, r in res.iterrows():
            if r["status"] in FINAL:
                continue
            s = similarity(r["match_text"], f"{t1} {t2}")
            if s > best_s:
                best_i, best_s = i, s
        if best_i is None or best_s < threshold:
            continue
        ptoks = _tokens(res.at[best_i, "match_text"])
        swap = _first_pos(ptoks, t1) > _first_pos(ptoks, t2)
        a, b = (int(g2), int(g1)) if swap else (int(g1), int(g2))
        res.at[best_i, "gh"], res.at[best_i, "ga"] = a, b
        if h1 is not None:
            x, y = (int(h2), int(h1)) if swap else (int(h1), int(h2))
            res.at[best_i, "hth"], res.at[best_i, "hta"] = x, y
        res.at[best_i, "status"], res.at[best_i, "src"] = "FT", "ملصوق"
        applied += 1
    return res, applied


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
    m = prog.merge(res[["terminal", "date", "status", "gh", "ga", "hth", "hta"]], on=["terminal", "date"])
    m = m[m["status"].isin(FINAL) & m["gh"].notna() & m["ga"].notna()]
    rows = []
    for _, r in m.iterrows():
        h = None if pd.isna(r["hth"]) else int(r["hth"])
        a = None if pd.isna(r["hta"]) else int(r["hta"])
        for e in evaluate_match(r, int(r["gh"]), int(r["ga"]), h, a):
            rows.append({"mid": r["mid"], "date": r["date"], "league": r["league"], "match": r["match_text"],
                         "score": f"{int(r['gh'])}-{int(r['ga'])}", **e})
    return pd.DataFrame(rows)


def winning_summary(sel):
    keep = ["1X2", "حظ مزدوج", "M/P 2.5", "M/P 3.5", "BTTS"]
    w = sel[(sel["won"] == True) & (sel["market"].isin(keep))]
    t = (w["market"] + ":" + w["selection"] + "@" + w["odd"].map("{:.2f}".format)).groupby(w["mid"]).apply(" | ".join)
    base = sel.drop_duplicates("mid").set_index("mid")[["date", "league", "match", "score"]]
    return base.join(t.rename("الكوطات الرابحة")).reset_index(drop=True)


# =====================================================================
# 5) التحليل (back-test) + الاحتمالات الصافية + بناء الأوراق
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
    """كل اختيار فالبرنامج + الاحتمال الصافي (بلا هامش الشركة) fair_p."""
    rows = []
    for _, r in prog.iterrows():
        fair = {}
        for g in GROUPS:
            odds = [r[f"o{i}"] for i in g]
            if all(_ok(o) for o in odds):
                s = sum(1 / o for o in odds)
                for i, o in zip(g, odds):
                    fair[i] = (1 / o) / s
        if all(i in fair for i in (0, 1, 2)):
            fair[3], fair[4], fair[5] = fair[0] + fair[1], fair[1] + fair[2], fair[0] + fair[2]
        for idx, market, sel in SELECTIONS:
            odd = r[f"o{idx}"]
            if _ok(odd):
                rows.append({"mid": r["mid"], "date": r["date"], "time": r["time"], "league": r["league"],
                             "match": r["match_text"], "market": market, "selection": sel,
                             "odd": float(odd), "fair_p": fair.get(idx, float("nan"))})
    return pd.DataFrame(rows)


def candidates(future, table, min_n):
    m = add_band(future).merge(table, on=["market", "selection", "band"], how="left")
    m = m[(m["n"] >= min_n) & (m["ROI %"] > 0)]
    return m.sort_values("ROI %", ascending=False)


def make_ticket(pool, n, cap_per_type=None):
    c = pool.sort_values("fair_p", ascending=False)
    chosen, used, per = [], set(), {}
    for _, r in c.iterrows():
        if r["mid"] in used:
            continue
        key = (r["market"], r["selection"])
        if cap_per_type and per.get(key, 0) >= cap_per_type:
            continue
        chosen.append(r); used.add(r["mid"]); per[key] = per.get(key, 0) + 1
        if len(chosen) == n:
            break
    return pd.DataFrame(chosen)


# =====================================================================
# 6) حفظ/تحميل الأرشيف وقائمة الانتظار
# =====================================================================
def _b(v):
    if v in (True, "True", "true", 1, 1.0): return True
    if v in (False, "False", "false", 0, 0.0): return False
    return None


def clean_history(h):
    h = h.copy()
    h["date"] = pd.to_datetime(h["date"]).dt.date
    h["won"] = h["won"].map(_b).astype(object)
    return h


def load_history():
    return clean_history(pd.read_csv(HIST_PATH)) if os.path.exists(HIST_PATH) else pd.DataFrame()


def merge_history(old, new):
    both = pd.concat([old, new], ignore_index=True) if not old.empty else new
    return both.drop_duplicates(["mid", "market", "selection"], keep="last").reset_index(drop=True)


def save_history(h):
    os.makedirs(DATA_DIR, exist_ok=True)
    h.to_csv(HIST_PATH, index=False)


def clean_pending(p):
    p = p.copy()
    p["date"] = pd.to_datetime(p["date"]).dt.date
    p["terminal"], p["coupon"] = p["terminal"].astype(str), p["coupon"].astype(str)
    p["ok"] = p["ok"].map(lambda v: v in (True, "True", "true", 1))
    p["mid"] = p["date"].astype(str) + "_" + p["terminal"]
    return p


def load_pending():
    if not os.path.exists(PEND_PATH):
        return pd.DataFrame()
    return clean_pending(pd.read_csv(PEND_PATH, dtype={"terminal": str, "coupon": str}))


def merge_pending(old, new):
    both = pd.concat([old, new], ignore_index=True) if not old.empty else new
    return both.drop_duplicates(["mid"], keep="last").reset_index(drop=True)


def save_pending(p):
    os.makedirs(DATA_DIR, exist_ok=True)
    p.to_csv(PEND_PATH, index=False)


# =====================================================================
# 7) الواجهة (نفس التنسيق: 3 أقسام + RTL)
# =====================================================================
def get_keys():
    keys = {"af": st.session_state.get("api_key", ""), "fd": st.session_state.get("fd_key", "")}
    try:
        keys["af"] = keys["af"] or st.secrets.get("API_FOOTBALL_KEY", "")
        keys["fd"] = keys["fd"] or st.secrets.get("FOOTBALL_DATA_KEY", "")
    except Exception:
        pass
    return keys


def show_ticket(df, title, hist, min_n):
    st.markdown(f"#### {title}")
    if df.empty:
        st.info("ما كاينش ماتشات كافية بهاد الشروط. وسّع مجال الكوط ولا الأسواق."); return
    tbl = hit_table(hist) if not hist.empty else None
    show = df.copy()
    if tbl is not None:
        sb = add_band(show).merge(tbl[["market", "selection", "band", "n", "نسبة الربح %"]],
                                  on=["market", "selection", "band"], how="left")
        sb.loc[sb["n"] < min_n, ["n", "نسبة الربح %"]] = float("nan")
        show = sb.rename(columns={"n": "ماتشات الأرشيف", "نسبة الربح %": "ربح الأرشيف %"}).drop(columns=["band"])
    show["احتمال الشركة الصافي %"] = (show["fair_p"] * 100).round(1)
    cols = ["date", "time", "league", "match", "market", "selection", "odd", "احتمال الشركة الصافي %"] + \
           [c for c in ["ماتشات الأرشيف", "ربح الأرشيف %"] if c in show.columns]
    st.dataframe(show[cols], use_container_width=True, hide_index=True)
    total_odd, prob = float(df["odd"].prod()), float(df["fair_p"].prod())
    c1, c2, c3 = st.columns(3)
    c1.metric("الكوط الإجمالية", f"{total_odd:.2f}")
    c2.metric("احتمال ربح الورقة كاملة (تقريبي)", f"{prob * 100:.1f}%")
    c3.metric("العائد المتوقع لكل 100 درهم", f"{(prob * total_odd - 1) * 100:+.0f}")


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
    if "pending" not in st.session_state:
        st.session_state.pending = load_pending()
    hist, pend = st.session_state.hist, st.session_state.pending

    st.sidebar.header("🎯 قائمة التحكم")
    mode = st.sidebar.radio("اختر القسم:", ["📥 رفع الأرشيف والتعلم التلقائي",
                                            "🧠 فحص البرنامج الجديد واكتشاف النمط",
                                            "⚙️ ربط API النتائج (API-Football)"])
    st.sidebar.metric("ماتشات فالأرشيف", hist["mid"].nunique() if not hist.empty else 0)
    st.sidebar.metric("ماتشات فالانتظار", len(pend))
    if not hist.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية: الأرشيف", hist.to_csv(index=False).encode("utf-8"),
                                   "history.csv", "text/csv")
    if not pend.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية: قائمة الانتظار", pend.to_csv(index=False).encode("utf-8"),
                                   "pending.csv", "text/csv")
    bk = st.sidebar.file_uploader("استرجاع نسخة محفوظة (CSV)", type="csv", key="bk")
    if bk is not None and st.sidebar.button("دمج النسخة المرفوعة"):
        raw = bk.getvalue()
        if "won" in pd.read_csv(io.BytesIO(raw), nrows=0).columns:
            st.session_state.hist = merge_history(hist, clean_history(pd.read_csv(io.BytesIO(raw))))
            save_history(st.session_state.hist)
        else:
            st.session_state.pending = merge_pending(
                pend, clean_pending(pd.read_csv(io.BytesIO(raw), dtype={"terminal": str, "coupon": str})))
            save_pending(st.session_state.pending)
        st.rerun()
    st.sidebar.caption("Streamlit Cloud كيمسح الملفات مللي تعاود النشر، فحمّل النسخ الاحتياطية بين وقت وآخر.")

    # ============================================================= القسم 1
    if mode.startswith("📥"):
        st.subheader("📁 قسم تدريب النظام على البرامج السابقة (Auto-Backtesting)")
        st.info("ارفع برنامج (ولا برامج) قديمة، زيدها للقائمة، من بعد جيب النتائج الحقيقية. "
                "API-Football المجاني كيعطي غير آخر 3 أيام تقريبا، فخلي البرنامج فالقائمة وجيب نتائجو فنفس الأسبوع.")
        files = st.file_uploader("📂 ارفع برنامج الرهانات القديم (PDF):", type=["pdf"],
                                 accept_multiple_files=True, key="arch_pdf")
        if files:
            frames = [f for f in (parse_bytes(x.getvalue()) for x in files) if not f.empty]
            if not frames:
                st.error("ما قدرتش نقرا الماتشات من هاد الملفات.")
            else:
                prog = pd.concat(frames, ignore_index=True).drop_duplicates(["mid"])
                c1, c2, c3 = st.columns(3)
                c1.metric("ماتشات مقروءة", len(prog)); c2.metric("أسطر كاملة", int(prog["ok"].sum()))
                c3.metric("هامش الشركة فـ 1X2", f"{prog['marge_1X2_%'].mean():.1f}%")
                if st.button("📌 أضف الماتشات لقائمة الانتظار", type="primary"):
                    st.session_state.pending = merge_pending(st.session_state.pending, prog[prog["ok"]])
                    save_pending(st.session_state.pending)
                    st.success("تزادو الماتشات للقائمة.")
                    pend = st.session_state.pending
        st.markdown("### 📋 الماتشات فالانتظار (باش نجيبو نتائجهم)")
        if pend.empty:
            st.caption("القائمة خاوية. ارفع برنامج وزيدو.")
        else:
            all_dates = sorted(pend["date"].dropna().unique())
            dates = st.multiselect("التواريخ:", all_dates, default=[d for d in all_dates if d <= today_ma()])
            refresh = st.checkbox("تجاهل الكاش")
            if st.button("🔎 جلب النتائج الحقيقية", type="primary"):
                keys = get_keys()
                if not keys["af"] and not keys["fd"]:
                    st.error("حط مفتاح واحد على الأقل فقسم '⚙️ ربط API'.")
                else:
                    sub = pend[pend["date"].isin(dates) & pend["ok"]].reset_index(drop=True)
                    bar = st.progress(0.0)
                    res, errs = match_results(sub, keys, refresh=refresh, progress=bar.progress)
                    st.session_state.sub, st.session_state.res, st.session_state.errs = sub, res, errs
                    st.session_state.pop("res_editor", None)
            for e in st.session_state.get("errs", []):
                st.warning(e)
            if "res" in st.session_state:
                res, sub = st.session_state.res, st.session_state.sub
                got = int(res["status"].isin(FINAL).sum())
                st.info(f"تلقات النتيجة ديال {got} من {len(res)} ماتش. اللي ناقص: لصق النتائج تحت ولا عمّرو بيدك "
                        "وكتب FT فخانة status (أهداف 1 = الفريق الأول فالبرنامج).")
                edited = st.data_editor(
                    res, key="res_editor", hide_index=True, use_container_width=True,
                    disabled=["terminal", "date", "time", "match_text", "api_match", "score_match", "src"],
                    column_config={"gh": st.column_config.NumberColumn("أهداف 1", min_value=0, step=1),
                                   "ga": st.column_config.NumberColumn("أهداف 2", min_value=0, step=1),
                                   "hth": st.column_config.NumberColumn("شوط1 (1)", min_value=0, step=1),
                                   "hta": st.column_config.NumberColumn("شوط1 (2)", min_value=0, step=1)})
                with st.expander("📋 لصق النتائج من أي موقع (حل بديل)"):
                    st.caption("كل سطر بهاد الشكل: `Arsenal 3-0 Hull City` ولا مع الشوط الأول `Arsenal 3-0 Hull City (1-0)`. "
                               "الترتيب كيتعرف من ترتيب الأسماء فالبرنامج، راجع الجدول من بعد.")
                    txt = st.text_area("النتائج:", height=160, key="paste_txt")
                    if st.button("طبّق النتائج الملصوقة"):
                        new_res, n_applied = apply_pasted(edited, txt)
                        st.session_state.res = new_res
                        st.session_state.pop("res_editor", None)
                        st.session_state.msg = f"تطبقات {n_applied} نتيجة."
                        st.rerun()
                if st.session_state.get("msg"):
                    st.success(st.session_state.pop("msg"))
                if st.button("🧠 ابدأ استخراج وتحليل وحفظ الأرشيف", type="primary"):
                    new = build_history(sub, edited)
                    if new.empty:
                        st.warning("ما كاين حتى ماتش منتهي بنتيجة.")
                    else:
                        st.session_state.hist = merge_history(st.session_state.hist, new)
                        save_history(st.session_state.hist)
                        st.session_state.pending = pend[~pend["mid"].isin(set(new["mid"]))].reset_index(drop=True)
                        save_pending(st.session_state.pending)
                        st.session_state.last = new
                        for k in ("res", "sub", "errs"):
                            st.session_state.pop(k, None)
                        st.session_state.pop("res_editor", None)
                        st.success(f"✨ تحفظو {new['mid'].nunique()} ماتش. الأرشيف دابا فيه "
                                   f"{st.session_state.hist['mid'].nunique()} ماتش.")
        if "last" in st.session_state:
            st.markdown("### 📊 الكوطات الرابحة فكل ماتش:")
            st.dataframe(winning_summary(st.session_state.last), use_container_width=True, hide_index=True)

    # ============================================================= القسم 2
    elif mode.startswith("🧠"):
        st.subheader("🧠 محرك الفحص والبحث عن الثغرات (Pattern Detection)")
        new_pdf = st.file_uploader("📂 ارفع برنامج الرهانات الجديد (PDF):", type=["pdf"], key="new_pdf")
        min_n = st.slider("أقل عدد ماتشات فالأرشيف باش نثق فالفئة", 10, 200, 30)
        if new_pdf is not None:
            newp = parse_bytes(new_pdf.getvalue())
            if newp.empty:
                st.error("ما قدرتش نقرا الماتشات."); return
            st.success(f"📄 قريت {len(newp)} ماتش من {new_pdf.name} ({int(newp['ok'].sum())} كاملين)")
            sel = selections_from_programme(newp[newp["ok"]])
            t1, t2, t3 = st.tabs(["🎟️ بناء الأوراق", "📋 كل الماتشات", "🔍 أنماط الأرشيف"])

            with t1:
                st.caption("الاحتمال الصافي = احتمال الشركة بعد حذف الهامش ديالها. هادشي هو رأي الشركة، ماشي ضمان.")
                dates = sorted(sel["date"].unique())
                c1, c2, c3 = st.columns(3)
                chosen_dates = c1.multiselect("التواريخ", dates, default=[d for d in dates if d >= today_ma()] or dates)
                legs = c2.slider("عدد الماتشات فالورقة", 3, 10, 6)
                skip = c3.checkbox("تجاهل الماتشات اللي بدات", value=True)
                omin, omax = st.slider("مجال الكوط لكل ماتش", 1.05, 4.0, (1.40, 2.20), 0.05)
                defaults = [m for m in ["1X2", "M/P 1.5", "M/P 2.5", "M/P 3.5", "BTTS", "حظ مزدوج",
                                        "نتيجة + M/P 2.5"] if m in set(sel["market"])]
                mk = st.multiselect("الأسواق", sorted(sel["market"].unique()), default=defaults)
                lg = st.multiselect("الدوريات", sorted(sel["league"].unique()), default=sorted(sel["league"].unique()))
                pool = sel[sel["date"].isin(chosen_dates) & sel["market"].isin(mk) & sel["league"].isin(lg)
                           & sel["fair_p"].notna()]
                if skip:
                    ko = pool.apply(lambda r: dt.datetime.combine(
                        r["date"], dt.datetime.strptime(r["time"], "%H:%M").time()), axis=1)
                    pool = pool[ko > now_ma()]
                show_ticket(make_ticket(pool[pool["odd"].between(omin, omax)], legs), "1) ورقة آمنة (الأكثر احتمالا)", hist, min_n)
                show_ticket(make_ticket(pool[pool["odd"].between(omin, omax)], legs, cap_per_type=2),
                            "2) ورقة متنوعة (بحد أقصى اختيارين من نفس النوع)", hist, min_n)
                show_ticket(make_ticket(pool[pool["odd"].between(max(omin, 1.8), max(omax, 3.0))], legs),
                            "3) ورقة بكوطات كبيرة (من 1.80)", hist, min_n)
                st.warning("كل كوط كتزيد فالورقة كتنقص احتمال الربح. غالبا الورقة الكاملة كتخسر، فلعب بمبلغ رمزي غير.")

            with t2:
                w = newp[["date", "time", "league", "match_text", "o0", "o1", "o2", "o15", "o16", "o30", "o31"]].copy()
                f1 = sel[sel["market"].isin(["1X2"])].pivot_table(index="mid", columns="selection", values="fair_p")
                w = w.assign(mid=newp["mid"]).merge(f1.add_prefix("p_"), left_on="mid", right_index=True, how="left")
                for c in ["p_1", "p_X", "p_2"]:
                    w[c] = (w[c] * 100).round(0)
                w = w.drop(columns=["mid"]).rename(columns={
                    "o0": "1", "o1": "X", "o2": "2", "o15": "Moins 2.5", "o16": "Plus 2.5", "o30": "BTTS oui",
                    "o31": "BTTS non", "p_1": "احتمال 1 %", "p_X": "احتمال X %", "p_2": "احتمال 2 %"})
                st.dataframe(w, use_container_width=True, hide_index=True)

            with t3:
                if hist.empty:
                    st.info("الأرشيف خاوي. ابدا بقسم '📥 رفع الأرشيف' بعدة برامج قديمة، وهنا غادي تبان الأنماط.")
                else:
                    n = hist["mid"].nunique()
                    if n < 300:
                        st.warning(f"الأرشيف فيه {n} ماتش فقط. أي نمط دابا غالبا صدفة (خاص 300 ماتش على الأقل).")
                    bets = hist[hist["won"].notna()].copy()
                    bets["ret"] = bets["won"].astype(bool) * bets["odd"] - 1
                    st.write("الربح/الخسارة لو لعبنا كل الاختيارات بمبلغ ثابت:")
                    st.dataframe(bets.groupby("market").agg(رهانات=("ret", "size"),
                                 ROI=("ret", lambda x: f"{x.mean() * 100:.1f}%")), use_container_width=True)
                    tbl = hit_table(hist)
                    cand = candidates(sel, tbl, min_n)
                    if cand.empty:
                        st.error("ما لقيت حتى اختيار فئتو ROI موجب بعينة كافية. هادا نتيجة صريحة: الكوطات كتعكس "
                                 "هامش الشركة، وما كاين حتى ثغرة مثبتة فالأرشيف الحالي.")
                    else:
                        st.success(f"{len(cand)} اختيار طابق فئات ROI موجب فالأرشيف:")
                        st.dataframe(cand[["date", "time", "match", "market", "selection", "odd", "band", "n",
                                           "نسبة الربح %", "توقع الكوط %", "ROI %"]],
                                     use_container_width=True, hide_index=True)
                    with st.expander("جدول الفئات الكامل"):
                        st.dataframe(tbl[tbl["n"] >= min_n].sort_values("ROI %", ascending=False),
                                     use_container_width=True, hide_index=True)

    # ============================================================= القسم 3
    else:
        st.subheader("⚙️ إعدادات ربط النتائج الحية والتاريخية")
        st.info("تقدر تستعمل مفتاح واحد ولا جوج. API-Football كيغطي بطولات بزاف لكن المجاني محدود فالتواريخ، "
                "وfootball-data.org (مجاني) كيغطي 12 بطولة كبيرة.")
        st.markdown("#### 1) API-Football")
        k = st.text_input("مفتاح API-Key الخاص بك:", type="password", value=st.session_state.get("api_key", ""))
        if st.button("🔗 فحص الاتصال (API-Football)", type="primary"):
            if not k and not get_keys()["af"]:
                st.warning("⚠️ الرجاء إدخال المفتاح.")
            else:
                st.session_state.api_key = k or get_keys()["af"]
                try:
                    info = api_get("status", st.session_state.api_key).get("response", {})
                    req, sub = info.get("requests", {}), info.get("subscription", {})
                    st.success("✅ المفتاح صحيح.")
                    st.write(f"الخطة: **{sub.get('plan', '?')}** | الطلبات اليوم: "
                             f"**{req.get('current', '?')} / {req.get('limit_day', '?')}**")
                except Exception as e:
                    st.error(f"❌ {explain_error(e)}")
        if st.session_state.get("af_window"):
            a, b = st.session_state["af_window"]
            st.caption(f"آخر مرة: الخطة المجانية كتسمح غير من {a} حتى {b}.")
        st.markdown("#### 2) football-data.org (بديل مجاني)")
        st.caption("سجل فـ football-data.org وتجيك التوكن فالإيميل.")
        k2 = st.text_input("التوكن (X-Auth-Token):", type="password", value=st.session_state.get("fd_key", ""))
        if st.button("🔗 فحص الاتصال (football-data.org)"):
            if not k2 and not get_keys()["fd"]:
                st.warning("⚠️ الرجاء إدخال التوكن.")
            else:
                st.session_state.fd_key = k2 or get_keys()["fd"]
                try:
                    r = requests.get(f"{FD_BASE}/competitions", headers={"X-Auth-Token": st.session_state.fd_key}, timeout=30)
                    if r.status_code == 200:
                        st.success(f"✅ التوكن صحيح ({len(r.json().get('competitions', []))} بطولة متاحة).")
                    else:
                        st.error(f"❌ {explain_error(str(r.status_code) + ' ' + r.json().get('message', ''))}")
                except Exception as e:
                    st.error(f"❌ {explain_error(e)}")


if __name__ == "__main__":
    main()
