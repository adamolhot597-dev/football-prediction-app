# -*- coding: utf-8 -*-
import io, os, re, json, math, time, html, hashlib, unicodedata, datetime as dt
from difflib import SequenceMatcher
import numpy as np
import pandas as pd
import pdfplumber
import requests
import streamlit as st

DATA_DIR = "data"
HIST_PATH = os.path.join(DATA_DIR, "history.csv")
PEND_PATH = os.path.join(DATA_DIR, "pending.csv")
CACHE_DIR = os.path.join(DATA_DIR, "cache")


def today_ma():
    return (dt.datetime.utcnow() + dt.timedelta(hours=1)).date()


def now_ma():
    return dt.datetime.utcnow() + dt.timedelta(hours=1)


LEAGUE_NAMES = {
    "ING1": "البريميرليغ", "SPA1": "الليغا", "SPA2": "الليغا 2", "SERIEA": "السيري آ", "GER1": "البوندسليغا",
    "FRA1": "ليغ 1", "OLA1": "هولندا", "BEL1": "بلجيكا", "TUR1": "تركيا", "MRC1": "المغرب (البطولة)",
    "SCO1": "إسكتلندا", "IRN1": "إيرلندا الشمالية", "BRA1": "البرازيل 1", "BRA2": "البرازيل 2",
    "COL1": "كولومبيا", "PER1": "بيرو", "ECU1": "الإكوادور", "USA1": "MLS", "AU1": "النمسا",
    "CRO1": "كرواتيا", "URU1": "الأوروغواي", "BOL1": "بوليفيا", "ING3": "إنجلترا 3", "ING4": "إنجلترا 4",
    "ING5": "إنجلترا 5", "NATION": "المنتخبات",
}


def league_name(code):
    return LEAGUE_NAMES.get(str(code), str(code))


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
def _row_tokens_x(chars, top, x_start):
    """أرقام السطر مع موقعها الأفقي (x) باش نعرفو لأي عمود كتنتمي، حتى إيلا كانت خلايا خاوية."""
    cs = sorted([c for c in chars if abs(c["top"] - top) <= 2.5 and c["x0"] >= x_start and c["text"].strip()],
                key=lambda c: c["x0"])
    segs, cur = [], []
    for c in cs:
        if cur and c["x0"] - cur[-1]["x1"] > 1.0:
            segs.append(cur); cur = []
        cur.append(c)
    if cur:
        segs.append(cur)
    out = []
    for seg in segs:
        txt = "".join(c["text"] for c in seg)
        for m in TOKEN_RE.finditer(txt):
            i, j = m.span()
            out.append((m.group(), (seg[i]["x0"] + seg[j - 1]["x1"]) / 2))
    return out


def _centers(rows):
    """مراكز الأعمدة الـ 32 من السطور الكاملة."""
    full = [r["tx"] for r in rows if r["tx"] is not None and len(r["tx"]) == N_TOKENS]
    if len(full) < 3:
        return None
    cols = [sorted(t[j][1] for t in full) for j in range(N_TOKENS)]
    return [c[len(c) // 2] for c in cols]


def _map_to_cols(tx, centers):
    """يوزع الأرقام على الأعمدة حسب الموقع. يرجع None إيلا وقع التباس."""
    gaps = [min(abs(centers[j] - centers[k]) for k in (j - 1, j + 1) if 0 <= k < N_TOKENS) for j in range(N_TOKENS)]
    res = {}
    for t, x in tx:
        j = min(range(N_TOKENS), key=lambda k: abs(centers[k] - x))
        if abs(centers[j] - x) > 0.55 * gaps[j] or j in res:
            return None
        res[j] = t
    return res


def parse_programme(pdf_file):
    raw, seen, cur_date, boundary_g = [], set(), None, None
    with pdfplumber.open(pdf_file) as pdf:
        for pno, page in enumerate(pdf.pages):
            words, chars = page.extract_words(), page.chars
            headers = []
            for i, w in enumerate(words[:-1]):
                if DAY_RE.match(w["text"]) and DATE_RE.match(words[i + 1]["text"]):
                    headers.append((w["top"], dt.datetime.strptime(words[i + 1]["text"], "%d/%m/%Y").date()))
            headers.sort()
            # الحد بين عمود الفريق 1 وعمود الفريق 2 (من عنوان "Equipe 1 / Equipe 2")
            cs = {}
            for i, w in enumerate(words[:-1]):
                if w["text"] == "Equipe" and words[i + 1]["text"] in ("1", "2") and words[i + 1]["text"] not in cs:
                    cs[words[i + 1]["text"]] = (w["x0"] + words[i + 1]["x1"]) / 2
            if "1" in cs and "2" in cs:
                boundary_g = (cs["1"] + cs["2"]) / 2
            boundary = boundary_g
            anchors = sorted([w for w in words if re.match(r"^\d{2}:\d{2}", w["text"]) and w["x0"] < 45],
                             key=lambda w: w["top"])
            for a in anchors:
                main = sorted([w for w in words if abs(w["top"] - a["top"]) <= 2.5], key=lambda w: w["x0"])
                line = re.sub(r"^(\d{2}:\d{2})(?=[A-Z])", r"\1 ", " ".join(w["text"] for w in main))
                m = re.search(r"^(\d{2}:\d{2})\s+([A-Z]+\d?)\s+(\S+)\s+(\d{7})(?:\s+(.*?))?\s1\s+(?=-|\d)(.*)$", line)
                if not m:
                    continue
                hhmm, league, coupon, terminal, _, tail = m.groups()
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
                t1 = [w["text"] for w in names if boundary is None or (w["x0"] + w["x1"]) / 2 < boundary]
                t2 = [w["text"] for w in names if boundary is not None and (w["x0"] + w["x1"]) / 2 >= boundary]
                tx = _row_tokens_x(chars, a["top"], min_w["x1"] + 0.3) if min_w else None
                seen.add((cur_date, terminal))
                raw.append({"page": pno, "date": cur_date, "time": hhmm, "league": league, "coupon": coupon,
                            "terminal": terminal, "team1": " ".join(t1), "team2": " ".join(t2),
                            "seq": TOKEN_RE.findall(tail), "tx": tx})
    centers_g = _centers(raw)
    centers_p = {p: _centers([r for r in raw if r["page"] == p]) for p in {r["page"] for r in raw}}
    rows = []
    for r in raw:
        vals, ok, n = [float("nan")] * N_TOKENS, False, len(r["seq"])
        if r["tx"] is not None:
            n = len(r["tx"])
            if n == N_TOKENS:
                vals, ok = [_f(t) for t, _ in r["tx"]], True
            else:
                cen = centers_p.get(r["page"]) or centers_g
                mp = _map_to_cols(r["tx"], cen) if cen else None
                if mp:
                    for j, t in mp.items():
                        vals[j] = _f(t)
                    ok = True
        if not ok:                                   # احتياط: ترتيب الأرقام فقط (1X2 غالبا صالح)
            for j, t in enumerate(r["seq"][:3]):
                vals[j] = _f(t)
        rec = {k: r[k] for k in ("date", "time", "league", "coupon", "terminal", "team1", "team2")}
        rec["match_text"] = (r["team1"] + " " + r["team2"]).strip()
        rec["n_tokens"] = n
        rec.update(dict(zip(ODDS_COLS, vals)))
        rec["ok"] = ok
        rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["mid"] = df["date"].astype(str) + "_" + df["terminal"]
    ssum = (1 / df[["o0", "o1", "o2"]]).sum(axis=1)
    df["partial"] = (~df["ok"]) & df[["o0", "o1", "o2"]].notna().all(axis=1) & ssum.between(1.0, 1.4)
    df["marge_1X2_%"] = ((ssum - 1) * 100).round(1)
    return df


PARSER_VERSION = "2026-11-11-v9"


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
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
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
    r = requests.get(f"{AF_BASE}/{path}", headers={"x-apisports-key": str(key).strip()}, params=params or {}, timeout=40)
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
    r = requests.get(f"{FD_BASE}/matches", headers={"X-Auth-Token": str(token).strip()}, timeout=40,
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
    return min(i for i, p in enumerate(prog_tokens) if SequenceMatcher(None, p, nt[0]).ratio() > 0.65) \
        if any(SequenceMatcher(None, p, nt[0]).ratio() > 0.65 for p in prog_tokens) else 99


def apply_pasted(res, text, threshold=0.55):
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
                         "score": f"{int(r['gh'])}-{int(r['ga'])}",
                         "ht": f"{h}-{a}" if h is not None and a is not None else "", **e})
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
        limit = N_TOKENS if bool(r.get("ok", True)) else 6      # السطور الجزئية: 1X2 + حظ مزدوج فقط
        for g in GROUPS:
            if max(g) >= limit:
                continue
            odds = [r[f"o{i}"] for i in g]
            if all(_ok(o) for o in odds):
                s = sum(1 / o for o in odds)
                for i, o in zip(g, odds):
                    fair[i] = (1 / o) / s
        if all(i in fair for i in (0, 1, 2)):
            fair[3], fair[4], fair[5] = fair[0] + fair[1], fair[1] + fair[2], fair[0] + fair[2]
        for idx, market, sel in SELECTIONS:
            if idx >= limit:
                continue
            odd = r[f"o{idx}"]
            if _ok(odd):
                rows.append({"mid": r["mid"], "date": r["date"], "time": r["time"], "league": r["league"],
                             "match": r["match_text"], "team1": str(r.get("team1", "")), "team2": str(r.get("team2", "")),
                             "terminal": str(r.get("terminal", "")), "coupon": str(r.get("coupon", "")),
                             "market": market, "selection": sel,
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
    if "team1" not in p.columns:
        p["team1"], p["team2"] = p["match_text"], ""
    p["team1"], p["team2"] = p["team1"].fillna("").astype(str), p["team2"].fillna("").astype(str)
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
# 8) أدوات العرض (جداول HTML واضحة: خط صغير + عرض كامل + أسماء الفرق كاملة)
# =====================================================================
TABLE_CSS = """
<style>
body, div, span, p, table, th, td { direction: rtl; text-align: right; }
.tbl-wrap { overflow: auto; border: 1px solid rgba(128,128,128,.35); border-radius: 8px; margin-bottom: 8px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; line-height: 1.35; }
.tbl th { position: sticky; top: 0; background: rgba(128,128,128,.22); font-weight: 600; z-index: 1; }
.tbl th, .tbl td { border: 1px solid rgba(128,128,128,.28); padding: 4px 7px; white-space: normal; word-break: break-word; }
.tbl tr:nth-child(even) td { background: rgba(128,128,128,.07); }
.tbl .good { color: #16a34a; font-weight: 700; }
.tbl .bad { color: #dc2626; font-weight: 700; }
</style>
"""


def html_table(df, fmt=None, max_height=520):
    """جدول للقراءة فقط: كل النص كيبان كامل بلا ما تكليكي على الخانة."""
    if df is None or df.empty:
        st.caption("— ما كاين والو —")
        return
    fmt = fmt or {}
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for _, r in df.iterrows():
        tds = []
        for c in df.columns:
            v = r[c]
            if v is None or (isinstance(v, float) and math.isnan(v)) or (v is pd.NaT):
                s = "—"
            elif c in fmt:
                try:
                    s = fmt[c].format(v)
                except Exception:
                    s = str(v)
            elif isinstance(v, (float, np.floating)):
                s = f"{v:.2f}"
            else:
                s = str(v)
            tds.append(f'<td dir="auto">{html.escape(s)}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    st.markdown(f'<div class="tbl-wrap" style="max-height:{max_height}px"><table class="tbl"><thead><tr>{head}</tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>', unsafe_allow_html=True)


def wilson_lb(hits, n, z=1.96):
    """الحد الأدنى المحافظ لنسبة النجاح (باش عينة صغيرة ما تخدعناش)."""
    if n <= 0:
        return 0.0
    p = hits / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


def pick_label(market, sel):
    if market == "1X2":
        return {"1": "فوز الفريق 1 (1)", "X": "تعادل (X)", "2": "فوز الفريق 2 (2)"}.get(sel, sel)
    if market == "حظ مزدوج":
        return {"1X": "الفريق 1 ما يخسرش (1X)", "X2": "الفريق 2 ما يخسرش (X2)", "12": "ما كاينش تعادل (12)"}.get(sel, sel)
    if market.startswith("M/P") and "شوط1" not in market:
        line = market.split()[1]
        return f"أكثر من {line} هدف (Plus {line})" if sel == "Plus" else f"أقل من {line} هدف (Moins {line})"
    if market == "BTTS":
        return "الفريقين يسجلو (BTTS نعم)" if sel == "Oui" else "ما يسجلوش الجوج (BTTS لا)"
    if market == "نتيجة + M/P 2.5":
        r, mp = sel.split("&")
        res = {"1": "فوز الفريق 1", "X": "تعادل", "2": "فوز الفريق 2"}[r]
        return f"{res} + " + ("أكثر من 2.5 هدف" if mp == "P" else "أقل من 2.5 هدف")
    if market == "1X2 شوط1":
        return "الشوط الأول: " + {"1": "فوز الفريق 1", "X": "تعادل", "2": "فوز الفريق 2"}.get(sel, sel)
    if market == "M/P 1.5 شوط1":
        return "الشوط الأول: " + ("أكثر من 1.5 هدف" if sel == "Plus" else "أقل من 1.5 هدف")
    return f"{market} {sel}"


def day_label(d):
    delta = (d - today_ma()).days
    base = {0: "اليوم", 1: "غدا", 2: "بعد غد"}.get(delta)
    return f"{base} ({d.strftime('%d/%m')})" if base else d.strftime("%d/%m/%Y")


def vs_text(r):
    t1, t2 = str(r.get("team1", "")).strip(), str(r.get("team2", "")).strip()
    return f"{t1}  ⚔  {t2}" if t1 and t2 else str(r.get("match", ""))


# =====================================================================
# 9) أنماط الأرشيف + Value Bet Filter + الأوراق
# =====================================================================

BAND_LO = dict(zip(BAND_LABELS, BANDS[:-1]))


def trap_flag(rate, imp, roi):
    """تحذير الفخ: النمط اللي تاريخيا كيخسر ولا نجاحو أقل من اللي كيوحي بيه الكوط."""
    if roi <= -10 or rate < imp - 3:
        return "⚠️ فخ محتمل"
    if roi >= 5:
        return "✅ ROI موجب"
    return "—"


def upcoming_only(df, margin_min=10):
    """يخلي غير الماتشات اللي مازال ما بداوش (بتوقيت المغرب)."""
    if df is None or df.empty:
        return df
    lim = now_ma() + dt.timedelta(minutes=margin_min)
    ko = [dt.datetime.combine(d, dt.datetime.strptime(t, "%H:%M").time()) for d, t in zip(df["date"], df["time"])]
    return df[[k > lim for k in ko]]


def pattern_table(hist, min_n, min_odd=0.0, sort_by="ROI %"):
    t = hit_table(hist)
    t["lo"] = t["band"].astype(str).map(BAND_LO)
    t = t[(t["n"] >= min_n) & (t["market"] != "هانديكاب") & (t["lo"] >= min_odd - 1e-9)].copy()
    t["تحذير"] = [trap_flag(r, i, roi) for r, i, roi in zip(t["نسبة الربح %"], t["توقع الكوط %"], t["ROI %"])]
    return t.sort_values([sort_by, "n"], ascending=False).reset_index(drop=True)


def matches_for_pattern(future, market, selection, band):
    f = add_band(future)
    m = f[(f["market"] == market) & (f["selection"] == selection) & (f["band"].astype(str) == band)]
    return m.sort_values(["date", "time"])



def strict_pool(future, table, min_n, min_rate, odd_lo, odd_hi, need_roi=False):
    f = add_band(future).merge(table, on=["market", "selection", "band"], how="inner")
    f = f[(f["n"] >= min_n) & (f["نسبة الربح %"] >= min_rate) & (f["market"] != "هانديكاب")
          & f["odd"].between(odd_lo, odd_hi)].copy()
    if need_roi:
        f = f[f["ROI %"] > 0]
    f["edge"] = [(wilson_lb(h, n) - 1 / o) * 100 for h, n, o in zip(f["hits"], f["n"], f["odd"])]
    f["score"] = f["edge"]
    return f.sort_values(["score", "n"], ascending=False)


def build_ticket(pool, n, cap_per_type, excluded):
    chosen, used, per = [], set(excluded), {}
    for _, r in pool.iterrows():
        if r["mid"] in used:
            continue
        key = (r["market"], r["selection"])
        if per.get(key, 0) >= cap_per_type:
            continue
        chosen.append(r); used.add(r["mid"]); per[key] = per.get(key, 0) + 1
        if len(chosen) == n:
            break
    return pd.DataFrame(chosen)



def ticket_display(t):
    return pd.DataFrame({
        "كود الكوبون": t["coupon"], "كود الجهاز": t["terminal"], "التاريخ": t["date"].map(day_label),
        "الساعة": t["time"], "الدوري": t["league"].map(league_name), "المقابلة": t.apply(vs_text, axis=1),
        "الرهان المطلوب": [pick_label(m, s) for m, s in zip(t["market"], t["selection"])],
        "الكوط": t["odd"], "ماتشات الأرشيف": t["n"].astype(int), "نجاح الأرشيف %": t["نسبة الربح %"],
        "ROI الأرشيف %": t["ROI %"],
        "تحذير": [trap_flag(r, i, roi) for r, i, roi in zip(t["نسبة الربح %"], t["توقع الكوط %"], t["ROI %"])]})


# =====================================================================
# 10) محرك العمليات الحسابية (Math Matrix): مئات المعادلات + اختبار على ماتشات ما تدرب عليهاش
# =====================================================================
MM_KEYS = [("1X2", "1", "1"), ("1X2", "X", "X"), ("1X2", "2", "2"), ("حظ مزدوج", "1X", "1X"),
           ("حظ مزدوج", "X2", "X2"), ("حظ مزدوج", "12", "12"), ("M/P 1.5", "Moins", "M1.5"),
           ("M/P 1.5", "Plus", "P1.5"), ("M/P 2.5", "Moins", "M2.5"), ("M/P 2.5", "Plus", "P2.5"),
           ("M/P 3.5", "Moins", "M3.5"), ("M/P 3.5", "Plus", "P3.5"), ("BTTS", "Oui", "BTTS+"),
           ("BTTS", "Non", "BTTS-")]
MM_MAP = {(m, s): k for m, s, k in MM_KEYS}
MM_ORDER = [k for _, _, k in MM_KEYS]
MM_TARGET = {k: (m, s) for m, s, k in MM_KEYS}
PCTS = [10, 20, 30, 40, 50, 60, 70, 80, 90]


def _yn(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return np.nan
    return 1.0 if bool(v) else 0.0


def wide_from_long(df, with_outcome=False):
    d = df.copy()
    d["k"] = [MM_MAP.get((m, s)) for m, s in zip(d["market"], d["selection"])]
    d = d[d["k"].notna()]
    odds = d.pivot_table(index="mid", columns="k", values="odd", aggfunc="first").reindex(columns=MM_ORDER)
    if not with_outcome:
        return odds, None, None
    d["y"] = d["won"].map(_yn)
    y = d.pivot_table(index="mid", columns="k", values="y", aggfunc="first").reindex(columns=MM_ORDER)
    dates = d.groupby("mid")["date"].first()
    return odds, y.reindex(odds.index), dates.reindex(odds.index)


def mm_formulas(O):
    """يولد مئات الصيغ (ضرب، قسمة، جمع، طرح، ومزيج) على أسعار الماتش."""
    n, out = O.shape[1], []
    with np.errstate(all="ignore"):
        for i in range(n):
            for j in range(i + 1, n):
                out.append(("mul", i, j, -1, O[:, i] * O[:, j]))
                out.append(("add", i, j, -1, O[:, i] + O[:, j]))
        for i in range(n):
            for j in range(n):
                if i != j:
                    out.append(("div", i, j, -1, O[:, i] / O[:, j]))
                    out.append(("sub", i, j, -1, O[:, i] - O[:, j]))
        a, b, c = O[:, 0], O[:, 1], O[:, 2]
        out += [("margin", 0, 1, 2, 1 / a + 1 / b + 1 / c), ("prod3", 0, 1, 2, a * b * c),
                ("ac_b", 0, 1, 2, a * c / b), ("b_ac", 0, 1, 2, b / (a * c)), ("spread", 0, 1, 2, (a - c) / b)]
        i_m, i_p = MM_ORDER.index("M2.5"), MM_ORDER.index("P2.5")
        out.append(("margin2", i_m, i_p, -1, 1 / O[:, i_m] + 1 / O[:, i_p]))
        i_b, i_n = MM_ORDER.index("BTTS+"), MM_ORDER.index("BTTS-")
        out.append(("margin2", i_b, i_n, -1, 1 / O[:, i_b] + 1 / O[:, i_n]))
    return [(op, i, j, k, np.where(np.isfinite(v), v, np.nan)) for op, i, j, k, v in out]


def eval_formula(op, i, j, k, O):
    with np.errstate(all="ignore"):
        a, b = O[:, i], O[:, j]
        v = {"mul": lambda: a * b, "add": lambda: a + b, "div": lambda: a / b, "sub": lambda: a - b,
             "margin": lambda: 1 / O[:, 0] + 1 / O[:, 1] + 1 / O[:, 2], "prod3": lambda: O[:, 0] * O[:, 1] * O[:, 2],
             "ac_b": lambda: O[:, 0] * O[:, 2] / O[:, 1], "b_ac": lambda: O[:, 1] / (O[:, 0] * O[:, 2]),
             "spread": lambda: (O[:, 0] - O[:, 2]) / O[:, 1], "margin2": lambda: 1 / a + 1 / b}[op]()
    return np.where(np.isfinite(v), v, np.nan)


def formula_text(op, i, j, k):
    a, b = MM_ORDER[i], MM_ORDER[j]
    return {"mul": f"({a}) × ({b})", "add": f"({a}) + ({b})", "div": f"({a}) ÷ ({b})", "sub": f"({a}) − ({b})",
            "margin": "1/(1) + 1/(X) + 1/(2)", "prod3": "(1) × (X) × (2)", "ac_b": "(1) × (2) ÷ (X)",
            "b_ac": "(X) ÷ ((1) × (2))", "spread": "((1) − (2)) ÷ (X)",
            "margin2": f"1/({a}) + 1/({b})"}[op]


def mm_search(hist, train_frac, min_train, min_test, odd_lo=1.0, odd_hi=1000.0, targets=None):
    odds, y, dates = wide_from_long(hist, with_outcome=True)
    order = np.argsort(dates.astype(str).values, kind="stable")
    O, Y = odds.values[order], y.values[order]
    n = len(O)
    n_tr = int(n * train_frac)
    tr, te = np.arange(0, n_tr), np.arange(n_tr, n)
    forms = mm_formulas(O)
    rows, meta, uses = [], [], []
    with np.errstate(all="ignore"):
        for fid, (op, i, j, k, S) in enumerate(forms):
            fin = S[tr][np.isfinite(S[tr])]
            if len(fin) < min_train:
                continue
            u = {i, j} | ({k} if k >= 0 else set())
            for t in np.unique(np.round(np.percentile(fin, PCTS), 4)):
                for d in (1, -1):
                    m = ((S >= t) if d == 1 else (S <= t)) & np.isfinite(S)
                    rows.append(m.astype(np.float32)); meta.append((fid, d, float(t))); uses.append(u)
    if not rows:
        return pd.DataFrame(), {"matches": n, "formulas": len(forms), "rules": 0}, forms
    M = np.vstack(rows)
    Mtr, Mte = M[:, tr], M[:, te]
    parts = []
    allowed_t = set(targets) if targets else set(MM_ORDER)
    for ti, tk in enumerate(MM_ORDER):
        if tk not in allowed_t:
            continue
        with np.errstate(all="ignore"):
            in_rng = (O[:, ti] >= odd_lo) & (O[:, ti] <= odd_hi)
        valid = (~np.isnan(Y[:, ti])) & (~np.isnan(O[:, ti])) & in_rng
        H = (Y[:, ti] == 1) & valid
        R = np.where(H, O[:, ti] - 1.0, np.where(valid, -1.0, 0.0))
        v_tr, v_te = valid[tr].astype(np.float32), valid[te].astype(np.float32)
        n1, h1, r1 = Mtr @ v_tr, Mtr @ H[tr].astype(np.float32), Mtr @ R[tr].astype(np.float32)
        n2, h2, r2 = Mte @ v_te, Mte @ H[te].astype(np.float32), Mte @ R[te].astype(np.float32)
        allowed = np.array([ti not in u for u in uses])
        idx = np.where(allowed & (n1 >= min_train))[0]
        if len(idx) == 0:
            continue
        base_te = H[te].sum() / max(valid[te].sum(), 1)
        o_te = np.where(valid[te], O[te, ti], 0.0).astype(np.float32)
        avg_te = (Mte @ o_te) / np.maximum(n2, 1)
        parts.append(pd.DataFrame({
            "rule": idx, "target": tk, "n_tr": n1[idx].astype(int), "h_tr": h1[idx] / n1[idx],
            "roi_tr": r1[idx] / n1[idx], "n_te": n2[idx].astype(int), "h_te": h2[idx] / np.maximum(n2[idx], 1),
            "roi_te": r2[idx] / np.maximum(n2[idx], 1), "base_te": base_te, "avg_te": avg_te[idx]}))
    if not parts:
        return pd.DataFrame(), {"matches": n, "formulas": len(forms), "rules": len(rows)}, forms
    res = pd.concat(parts, ignore_index=True)
    res = res[res["n_te"] >= min_test].copy()
    spec = []
    for r in res["rule"]:
        fid, d, t = meta[r]
        op, i, j, k, _ = forms[fid]
        spec.append((op, i, j, k, d, t))
    res["spec"] = spec
    res["formula"] = [f"{formula_text(op, i, j, k)} {'≥' if d == 1 else '≤'} {t:.2f}" for op, i, j, k, d, t in spec]
    res["lift"] = (res["h_te"] - res["base_te"]) * 100
    info = {"matches": n, "train": n_tr, "test": n - n_tr, "formulas": len(forms),
            "rules": len(rows) * len(MM_ORDER), "kept": len(res)}
    return res, info, forms



def mm_verdict(r, target):
    if r["roi_tr"] > 0 and r["roi_te"] > 0 and r["lift"] >= 3:
        return "✅ صامدة ورابحة (فالأرشيف)"
    if r["roi_te"] > 0:
        return "⚠️ ROI موجب فالاختبار فقط (احتمال صدفة)"
    if min(r["h_tr"], r["h_te"]) * 100 >= target:
        return "⚠️ نجاح بلا ربح"
    return "❌ فخ محتمل (ما صمدتش)"


# =====================================================================
# 11) فلتر H2H & Form (آخر 5 ماتشات + المواجهات المباشرة) عبر football-data.org
#     مع تخزين مؤقت 24 ساعة، وتحديد عدد الطلبات (10 فالدقيقة فالخطة المجانية)
# =====================================================================
FD_COMPS = ["PL", "ELC", "PD", "SA", "BL1", "FL1", "DED", "PPL", "BSA", "CL", "EC", "WC"]
FD_GAP = 6.5
_FD_LAST = {"t": 0.0}


def fd_get(path, token, params=None):
    wait = FD_GAP - (time.time() - _FD_LAST["t"])
    if wait > 0:
        time.sleep(wait)
    r = requests.get(FD_BASE + path, headers={"X-Auth-Token": str(token).strip()}, params=params or {}, timeout=40)
    _FD_LAST["t"] = time.time()
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {j.get('message', '')}")
    return j


def _read_cache(name, ttl):
    p = _cache_path(name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
        return json.load(open(p, encoding="utf-8"))
    return None


def _write_cache(name, obj):
    json.dump(obj, open(_cache_path(name), "w", encoding="utf-8"))


def load_team_dir():
    p = _cache_path("fd_teams.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


def refresh_team_dir(token, progress=None):
    teams, seen, errs = [], set(), []
    for i, code in enumerate(FD_COMPS):
        try:
            for t in fd_get(f"/competitions/{code}/teams", token).get("teams", []):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    teams.append({"id": t["id"], "name": t.get("name", ""), "short": t.get("shortName", ""), "comp": code})
        except Exception as e:
            errs.append(f"{code}: {e}")
        if progress:
            progress((i + 1) / len(FD_COMPS))
    if teams:
        _write_cache("fd_teams.json", teams)
    return teams, errs


def find_team(name, directory, thr=0.78):
    best, bs = None, 0.0
    for t in directory:
        s = max(similarity(name, t["name"]), similarity(name, t["short"]) if t.get("short") else 0)
        if s > bs:
            best, bs = t, s
    return best if bs >= thr else None


def team_recent(token, tid):
    c = _read_cache(f"tm_{tid}.json", 86400)
    if c is None:
        c = fd_get(f"/teams/{tid}/matches", token, {"status": "FINISHED", "limit": 50}).get("matches", [])
        _write_cache(f"tm_{tid}.json", c)
    out = []
    for m in c:
        sc = m.get("score") or {}
        ft = (sc.get("regularTime") if (sc.get("regularTime") or {}).get("home") is not None else sc.get("fullTime")) or {}
        if ft.get("home") is None:
            continue
        out.append({"when": m["utcDate"], "hid": m["homeTeam"]["id"], "aid": m["awayTeam"]["id"],
                    "gh": ft["home"], "ga": ft["away"]})
    return sorted(out, key=lambda x: x["when"], reverse=True)


def form_stats(tid, matches, n=5):
    w = d = l = gf = ga = bt = 0
    last = matches[:n]
    for m in last:
        f, a = (m["gh"], m["ga"]) if m["hid"] == tid else (m["ga"], m["gh"])
        gf += f; ga += a; bt += int(f > 0 and a > 0)
        w += f > a; d += f == a; l += f < a
    return {"n": len(last), "w": int(w), "d": int(d), "l": int(l), "pts": int(3 * w + d), "gf": gf, "ga": ga,
            "bt": bt, "tot": gf + ga}


def h2h_stats(t1, t2, matches1, n=5):
    hh = [m for m in matches1 if {m["hid"], m["aid"]} == {t1, t2}][:n]
    w1 = w2 = dr = tot = bt = 0
    for m in hh:
        f, a = (m["gh"], m["ga"]) if m["hid"] == t1 else (m["ga"], m["gh"])
        w1 += f > a; w2 += f < a; dr += f == a; tot += f + a; bt += int(f > 0 and a > 0)
    return {"n": len(hh), "w1": int(w1), "w2": int(w2), "d": int(dr), "avg": tot / len(hh) if hh else 0.0,
            "bt": bt}


def form_verdict(market, sel, f1, f2):
    if not f1["n"] or not f2["n"]:
        return "⚪ بلا بيانات", "ما كاينش آخر 5 ماتشات"
    d = f1["pts"] - f2["pts"]
    t = (f1["tot"] / f1["n"] + f2["tot"] / f2["n"]) / 2
    bt = (f1["bt"] / f1["n"] + f2["bt"] / f2["n"]) / 2

    def v_res(s):
        if s == "1": return 1 if d >= 3 else (-1 if d <= -3 else 0)
        if s == "2": return 1 if d <= -3 else (-1 if d >= 3 else 0)
        if s == "X": return 1 if (abs(d) <= 2 and t <= 2.4) else (-1 if abs(d) >= 6 else 0)
        if s == "1X": return 1 if d >= 0 else (-1 if d <= -5 else 0)
        if s == "X2": return 1 if d <= 0 else (-1 if d >= 5 else 0)
        if s == "12": return 1 if abs(d) >= 3 else (-1 if (abs(d) <= 1 and t <= 2.2) else 0)
        return 0

    def v_goals(side, line):
        up = 1 if t >= line + 0.4 else (-1 if t <= line - 0.4 else 0)
        return up if side == "Plus" else -up

    v = None
    if market in ("1X2", "حظ مزدوج"):
        v = v_res(sel)
    elif market.startswith("M/P") and "شوط1" not in market:
        v = v_goals(sel, float(market.split()[1]))
    elif market == "BTTS":
        up = 1 if bt >= 0.6 else (-1 if bt <= 0.35 else 0)
        v = up if sel == "Oui" else -up
    elif market == "نتيجة + M/P 2.5":
        r, mp = sel.split("&")
        v = min(v_res(r), v_goals("Plus" if mp == "P" else "Moins", 2.5))
    why = f"نقاط آخر 5: {f1['pts']} مقابل {f2['pts']} | معدل الأهداف {t:.1f} | BTTS {bt * 100:.0f}%"
    if v is None:
        return "⚪ غير مدعوم", why
    return {1: "✅ يدعم", 0: "⚠️ محايد", -1: "❌ يعارض"}[v], why


def standings_table(token, comp):
    c = _read_cache(f"st_{comp}.json", 86400)
    if c is None:
        c = fd_get(f"/competitions/{comp}/standings", token).get("standings", [])
        _write_cache(f"st_{comp}.json", c)
    tot = next((x for x in c if x.get("type") == "TOTAL"), c[0] if c else None)
    return tot["table"] if tot else []


def team_context(token, comp, tid):
    """سياق الدافع من الترتيب: خطر النزول / صراع القمة / وسط الترتيب (تقديري)."""
    if not comp or comp in ("CL", "EC", "WC"):
        return None
    tab = standings_table(token, comp)
    row = next((r for r in tab if r["team"]["id"] == tid), None)
    if not tab or not row:
        return None
    n, pos, pts = len(tab), row["position"], row["points"]
    safe = tab[max(n - 4, 0)]["points"]
    need_rel = pos >= n - 3 or (pts - safe) <= 2
    need_top = pos <= 4 or (tab[0]["points"] - pts) <= 3
    tag = "خطر النزول" if need_rel else ("صراع القمة" if need_top else "وسط الترتيب (مرتاح)")
    return {"need": bool(need_rel or need_top), "text": f"#{pos}/{n} ({pts} نقطة) — {tag}"}


def backed_side(sel):
    if sel in ("1", "1X") or sel.startswith("1&"):
        return 1
    if sel in ("2", "X2") or sel.startswith("2&"):
        return 2
    return 0


def hf_analyze(r, token, directory):
    team1, team2 = str(r.get("team1", "")), str(r.get("team2", ""))
    t1, t2 = find_team(team1, directory), find_team(team2, directory)
    base = {"المقابلة": vs_text(r), "الرهان": pick_label(r["market"], r["selection"]), "الكوط": r["odd"]}
    if not t1 or not t2:
        return {**base, "الفورما (1)": "—", "الفورما (2)": "—", "H2H": "—", "السياق (1)": "—", "السياق (2)": "—",
                "تحذير الدافع": "—", "الحكم": "⚪ خارج التغطية",
                "السبب": "أحد الفريقين ما كاينش فـ football-data.org (البطولات الكبرى فقط)"}
    m1, m2 = team_recent(token, t1["id"]), team_recent(token, t2["id"])
    f1, f2, hh = form_stats(t1["id"], m1), form_stats(t2["id"], m2), h2h_stats(t1["id"], t2["id"], m1)
    verdict, why = form_verdict(r["market"], r["selection"], f1, f2)
    fmt = lambda f: f"{f['w']}-{f['d']}-{f['l']} ({f['gf']}:{f['ga']})"
    h2h = (f"{hh['n']} مواجهة: {hh['w1']}-{hh['d']}-{hh['w2']} | معدل {hh['avg']:.1f} هدف" if hh["n"]
           else "ما كاينش مواجهات فالبيانات")
    try:
        c1 = team_context(token, t1.get("comp"), t1["id"])
        c2 = team_context(token, t2.get("comp"), t2["id"])
    except Exception:
        c1 = c2 = None
    side, warn = backed_side(r["selection"]), "—"
    if c1 and c2:
        mine, opp = (c1, c2) if side == 1 else ((c2, c1) if side == 2 else (None, None))
        if mine and opp and opp["need"] and not mine["need"]:
            warn = "⚠️ المنافس محتاج نقط والفريق المدعوم مرتاح (احتمال فخ)"
        elif c1["need"] and c2["need"]:
            warn = "⚠️ ماتش حاسم للجوج (مفتوح)"
    out = {**base, "الفورما (1)": fmt(f1), "الفورما (2)": fmt(f2), "H2H": h2h,
           "السياق (1)": c1["text"] if c1 else "—", "السياق (2)": c2["text"] if c2 else "—",
           "تحذير الدافع": warn, "الحكم": verdict, "السبب": why}
    if c1 and c2 and side:
        mine, opp = (c1, c2) if side == 1 else (c2, c1)
        out["_need"], out["_opp"], out["_relax"] = mine["need"], opp["need"], not mine["need"]
    return out


# =====================================================================
# 12) الواجهة
# =====================================================================

KEYS_PATH = os.path.join(DATA_DIR, "keys.json")


def load_keys_file():
    try:
        d = json.load(open(KEYS_PATH, encoding="utf-8"))
        return {"af": str(d.get("af", "")).strip(), "fd": str(d.get("fd", "")).strip()}
    except Exception:
        return {"af": "", "fd": ""}


def save_keys_file(keys):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(keys, open(KEYS_PATH, "w", encoding="utf-8"))


def get_keys():
    """المفاتيح كتبقى فالجلسة (حتى إيلا بدلتي القسم) وفملف على السيرفر، و Secrets إيلا كانو."""
    k = dict(st.session_state.get("_keys") or {"af": "", "fd": ""})
    try:
        k["af"] = k.get("af") or str(st.secrets.get("API_FOOTBALL_KEY", "")).strip()
        k["fd"] = k.get("fd") or str(st.secrets.get("FOOTBALL_DATA_KEY", "")).strip()
    except Exception:
        pass
    return k



def render_patterns(future, hist):
    if hist.empty:
        st.warning("الأرشيف خاوي. ابدا بالقسم الأول وزيد ماتشات بنتائجها.")
        return
    c1, c2, c3 = st.columns(3)
    min_n = c1.slider("أقل عدد ماتشات فالأرشيف:", 5, 60, 13, key="pat_min_n")
    min_odd = c2.slider("أقل كوط للنمط (باش نتفاداو الكوط القصيرة):", 1.0, 4.0, 1.40, 0.1, key="pat_min_odd")
    sort_by = c3.radio("رتب حسب:", ["ROI %", "نسبة الربح %"], horizontal=True, key="pat_sort")
    pt = pattern_table(hist, min_n, min_odd, sort_by)
    if pt.empty:
        st.warning("ما كاين حتى نمط بهاد الشروط. نقص 'أقل عدد ماتشات' ولا 'أقل كوط'.")
        return
    st.caption("كيفاش تقرا الجدول: **فئة الكوط** = مجال السعر، **عدد ماتشات الأرشيف** = شحال مرة تكرر هاد النمط، "
               "**عدد مرات النجاح** = شحال مرة ربح، **نسبة الربح** = النجاح ÷ التكرار، **توقع الشركة** = اللي كيوحي بيه السعر، "
               "**ROI** = الربح/الخسارة لكل 100 درهم (سالب = خسارة). **⚠️ فخ محتمل** = نمط كيخسر تاريخيا ولا نجاحو أقل من اللي كيوحي بيه الكوط.")
    disp = pt.rename(columns={"market": "السوق", "selection": "الرهان", "band": "فئة الكوط",
                              "n": "عدد ماتشات الأرشيف", "hits": "عدد مرات النجاح", "توقع الكوط %": "توقع الشركة %"})
    html_table(disp[["السوق", "الرهان", "فئة الكوط", "عدد ماتشات الأرشيف", "عدد مرات النجاح", "نسبة الربح %",
                     "توقع الشركة %", "ROI %", "تحذير"]].head(80),
               fmt={"نسبة الربح %": "{:.1f}%", "توقع الشركة %": "{:.1f}%", "ROI %": "{:+.1f}%",
                    "عدد ماتشات الأرشيف": "{:.0f}", "عدد مرات النجاح": "{:.0f}"}, max_height=420)

    st.markdown("#### 🎯 Value Bet Filter: كليكي على نمط باش تفرز الماتشات المطابقة فالبرنامج الجديد")
    top = pt.head(20)
    cols = st.columns(4)
    for i, (_, r) in enumerate(top.iterrows()):
        label = (f"{r['نسبة الربح %']:.0f}% · ROI {r['ROI %']:+.0f}% · {r['market']} {r['selection']} · "
                 f"{r['band']} ({int(r['hits'])}/{int(r['n'])})")
        if cols[i % 4].button(label, key=f"vb_{i}"):
            st.session_state.vb_pat = (r["market"], r["selection"], str(r["band"]))
    pat = st.session_state.get("vb_pat")
    if not pat:
        st.caption("ما تختارش حتى نمط بعد.")
        return
    row = pt[(pt["market"] == pat[0]) & (pt["selection"] == pat[1]) & (pt["band"].astype(str) == pat[2])]
    st.success(f"النمط المختار: **{pick_label(pat[0], pat[1])}** | فئة الكوط **{pat[2]}**"
               + (f" | نجح {int(row.iloc[0]['hits'])} من {int(row.iloc[0]['n'])} ({row.iloc[0]['نسبة الربح %']:.1f}%)"
                  f" | ROI {row.iloc[0]['ROI %']:+.1f}% {row.iloc[0]['تحذير']}" if not row.empty else ""))
    if st.button("✖ مسح الاختيار", key="vb_clear"):
        st.session_state.pop("vb_pat", None)
        st.rerun()
    m = matches_for_pattern(future, *pat)
    if m.empty:
        st.info("ما كاين حتى ماتش جاي فالبرنامج الجديد كيطابق هاد النمط.")
        return
    for d, g in m.groupby("date"):
        st.markdown(f"**📅 {day_label(d)}** — {len(g)} ماتش")
        html_table(pd.DataFrame({
            "الساعة": g["time"], "الدوري": g["league"].map(league_name), "المقابلة": g.apply(vs_text, axis=1),
            "الرهان المطلوب": [pick_label(a, b) for a, b in zip(g["market"], g["selection"])], "الكوط": g["odd"],
            "كود الكوبون": g["coupon"], "كود الجهاز": g["terminal"]}), max_height=360)


def show_ticket(t, title, key):
    st.markdown(f"#### {title}")
    if t.empty:
        st.info("ما لقيتش ماتشات كافية بهاد الشروط. نقص 'أقل عدد ماتشات' ولا 'أقل نسبة نجاح'.")
        return
    html_table(ticket_display(t), fmt={"نجاح الأرشيف %": "{:.1f}%", "ماتشات الأرشيف": "{:.0f}"}, max_height=420)
    c1, c2, c3 = st.columns(3)
    c1.metric("الكوط الإجمالية", f"{t['odd'].prod():.2f}")
    c2.metric("احتمال الورقة حسب الأرشيف", f"{np.prod(t['نسبة الربح %'] / 100) * 100:.1f}%")
    c3.metric("احتمال الورقة حسب الشركة", f"{np.nanprod(t['fair_p']) * 100:.1f}%")
    st.session_state.setdefault("hf", {})
    if st.button("💾 سجّل هاد الورقة فسجل التوقعات (باش نراجعو الأخطاء من بعد)", key=f"save_{key}"):
        k = journal_add(t, title, st.session_state.hf)
        st.success(f"تسجلو {k} اختيار. من بعد ما ديزو الماتشات سير لقسم '📓 سجل التوقعات'.")
    if st.button(f"🔬 فلتر H2H & Form ({len(t)} ماتش)", key=f"hf_btn_{key}"):
        keys, directory = get_keys(), load_team_dir()
        if not keys["fd"]:
            st.error("حط توكن football-data.org فقسم '⚙️ ربط API'.")
        elif not directory:
            st.error("دليل الفرق خاوي. دير 'تحديث دليل الفرق' فقسم '⚙️ ربط API' (مرة وحدة).")
        else:
            st.caption(f"كنجيب آخر 5 ماتشات (محدود بـ 10 طلبات فالدقيقة، بحد أقصى {2 * len(t)} طلب، والكاش 24 ساعة).")
            bar = st.progress(0.0)
            for i, (_, r) in enumerate(t.iterrows()):
                try:
                    st.session_state.hf[(r["mid"], r["market"], r["selection"])] = hf_analyze(r, keys["fd"], directory)
                except Exception as e:
                    st.session_state.hf[(r["mid"], r["market"], r["selection"])] = {
                        "المقابلة": vs_text(r), "الرهان": pick_label(r["market"], r["selection"]), "الكوط": r["odd"],
                        "الفورما (1)": "—", "الفورما (2)": "—", "H2H": "—", "السياق (1)": "—", "السياق (2)": "—",
                        "تحذير الدافع": "—", "الحكم": "⚪ خطأ", "السبب": explain_error(e)}
                bar.progress((i + 1) / len(t))
    got = [st.session_state.hf[k] for k in zip(t["mid"], t["market"], t["selection"]) if k in st.session_state.hf]
    if got:
        st.markdown("**نتيجة فلتر H2H & Form:**")
        gdf = pd.DataFrame(got)
        html_table(gdf[[c for c in gdf.columns if not str(c).startswith("_")]], fmt={"الكوط": "{:.2f}"}, max_height=360)



def render_tickets(future, hist):
    if hist.empty:
        st.warning("الأرشيف خاوي. ابدا بالقسم الأول.")
        return
    c1, c2, c3 = st.columns(3)
    min_n = c1.slider("أقل عدد ماتشات فالأرشيف (الصرامة):", 5, 40, 13, key="tk_min_n")
    min_rate = c2.slider("أقل نسبة نجاح فالأرشيف %:", 30, 95, 45, key="tk_min_rate")
    cap = c3.slider("أقصى اختيارات من نفس النوع فالورقة:", 1, 6, 2, key="tk_cap")
    odd_lo, odd_hi = st.slider("مجال الكوط (خليه عالي باش الورقة تطلع):", 1.05, 8.0, (1.40, 4.00), 0.05, key="tk_odds")
    need_roi = st.checkbox("اشترط ROI الأرشيف موجب (أصرم)", value=False, key="tk_roi")
    labels = {r["mid"]: f"{day_label(r['date'])} {r['time']} — {vs_text(r)}" for r in future.to_dict("records")}
    excl = st.multiselect("🚫 استبعد ماتشات (سمعتي خبر: نجم غايب، فريق محتاج نقط...):", sorted(set(labels.values())),
                          key="tk_excl")
    fut2 = future[~future["mid"].map(labels).isin(excl)] if excl else future
    pool = strict_pool(fut2, hit_table(hist), min_n, min_rate, odd_lo, odd_hi, need_roi)
    jr = st.session_state.get("journal")
    bl = derive_blacklist(jr) if jr is not None and not jr.empty else []
    use_bl = st.checkbox(f"طبق قائمة التجنب المتعلمة من أخطائي ({len(bl)} قاعدة)", value=True, key="tk_bl")
    if use_bl and bl and not pool.empty:
        pool, removed = apply_blacklist(pool, bl)
        if removed:
            st.caption(f"🛡️ تحيدو {removed} اختيار حسب قائمة التجنب (أنماط/دوريات خسرات فسجلك).")
    n_pos = int((pool["edge"] > 0).sum()) if not pool.empty else 0
    st.caption(f"اختيارات جاية طابقات أنماط الأرشيف (تكررت ≥ {min_n} مرة ونجاحها ≥ {min_rate}%): "
               f"**{len(pool)}** فـ **{pool['mid'].nunique() if not pool.empty else 0}** ماتش. "
               f"منها **{n_pos}** عندها أفضلية محافظة موجبة (نجاح الأرشيف المحافظ > نسبة تعادل الكوط).")
    if not pool.empty and n_pos == 0:
        st.warning("⚠️ حتى اختيار ما عندو أفضلية موجبة: تاريخيا هاد الأنماط كتخسر على المدى الطويل (الفخ ديال هامش الشركة).")
    st.caption("الترتيب: الأفضلية المحافظة = (نجاح الأرشيف بعد تصغير العينة) − (1 ÷ الكوط). نسبة النجاح العالية بوحدها ماشي ضمان.")
    used = set()
    plan = [("1) 🥇 الورقة الذهبية (3 ماتشات)", 3, "gold"), ("2) 🥈 الورقة الثانية (4 ماتشات)", 4, "second"),
            ("3) 🎲 الورقة عالية المخاطرة (6 ماتشات)", 6, "risk")]
    built = []
    for title, n, key in plan:
        t = build_ticket(pool, n, cap, used) if not pool.empty else pd.DataFrame()
        if not t.empty:
            used |= set(t["mid"])
            built.append((title, t))
            if len(t) < n:
                st.warning(f"{title}: لقيت غير {len(t)} من {n} ماتش بهاد الشروط.")
        show_ticket(t, title, key)
        st.markdown("---")
    if built and st.button("📤 صيفط الأوراق الثلاثة للسجل (دفعة وحدة)", type="primary", key="save_all"):
        k_all = sum(journal_add(t, title, st.session_state.get("hf", {})) for title, t in built)
        st.success(f"تسجلو {k_all} اختيار فسجل التوقعات. مللي تحط النتائج فالأرشيف غادي يتقارنو أوتوماتيكيا.")
    st.caption("الماتش اللي دخل ورقة ما كيتعاودش فالأوراق الأخرى، وكنعرض غير الماتشات اللي مازال ما بداوش.")



def render_math_matrix(prog):
    st.subheader("🧮 محرك العمليات الحسابية (Math Matrix Engine)")
    st.info("كيجرب مئات المعادلات (ضرب، قسمة، جمع، طرح، ومزيج) على أسعار كل ماتش فالأرشيف باش يلقى قواعد كتطلع فيها "
            "**الكوطات العالية** وكتربح، وكيقيسها على جزء ما تدرب عليهش. الكوط القصيرة (بحال 1.06) كيتحيدو بالمجال.")
    hist = st.session_state.hist
    if hist.empty:
        st.warning("الأرشيف خاوي. زيد ماتشات بنتائجها فالقسم الأول.")
        return
    c1, c2, c3, c4 = st.columns(4)
    train_frac = c1.slider("نسبة التدريب %", 50, 85, 70, key="mm_tr") / 100
    min_train = c2.slider("أقل ماتشات فالتدريب", 10, 120, 25, key="mm_mtr")
    min_test = c3.slider("أقل ماتشات فالاختبار", 5, 60, 8, key="mm_mte")
    target = c4.slider("أقل نسبة نجاح %", 30, 98, 55, key="mm_tgt")
    d1, d2 = st.columns([2, 3])
    odd_lo, odd_hi = d1.slider("مجال كوط الرهان:", 1.05, 8.0, (1.50, 4.00), 0.05, key="mm_odds")
    tg = d2.multiselect("الرهانات اللي كنقلبو عليها:", MM_ORDER, default=[k for k in MM_ORDER if k != "12"], key="mm_targets")
    need_roi = st.checkbox("اشترط ROI موجب فالتدريب والاختبار", value=True, key="mm_roi")
    st.caption("ملاحظة: فكوط حوالي 2.00 النجاح المطلوب باش ما تخسرش هو 50%، وفكوط 1.50 هو 67%. "
               "90% غير ممكن إلا مع الكوط القصيرة جدا (وهي كتخسر بسبب هامش الشركة).")
    nm = hist["mid"].nunique()
    if nm < 150:
        st.warning(f"الأرشيف فيه {nm} ماتش فقط. مع هاد العدد غالبا غادي تظهر معادلات 'ناجحة' بالصدفة.")
    if st.button("🚀 ابدأ البحث على المعادلات", type="primary", key="mm_run"):
        with st.spinner("كنجرب المعادلات..."):
            res, info, forms = mm_search(hist, train_frac, min_train, min_test, odd_lo, odd_hi, tg)
        st.session_state.mm = (res, info, target, need_roi, odd_lo, odd_hi)
    if "mm" not in st.session_state:
        return
    res, info, tgt, need, lo, hi = st.session_state.mm
    if res.empty:
        st.error("ما بقات حتى قاعدة بهاد الشروط. نقص 'أقل ماتشات' ولا وسّع مجال الكوط.")
        return
    st.caption(f"ماتشات الأرشيف: {info['matches']} (تدريب {info['train']} | اختبار {info['test']}) • معادلات: {info['formulas']} "
               f"• قواعد مجربة: {info['rules']:,} • قواعد فيها عدد كافي: {info['kept']:,} • مجال الكوط {lo:.2f}–{hi:.2f}")
    st.caption("⚠️ مللي كنجربو آلاف القواعد، بعضها كيربح غير بالصدفة. الحكم كيعتمد على الاختبار، وعلى الفرق بين النجاح "
               "والنسبة العامة (lift)، وعلى ROI. اللي ما صمدش فالاختبار هو الفخ.")
    cond = (res["h_tr"] * 100 >= tgt) & (res["h_te"] * 100 >= tgt)
    if need:
        cond &= (res["roi_tr"] > 0) & (res["roi_te"] > 0)
    ok = res[cond].copy().sort_values(["roi_te", "n_te"], ascending=False)
    if ok.empty:
        shown = res.assign(m=np.minimum(res["roi_tr"], res["roi_te"])).sort_values("m", ascending=False).head(25)
        st.warning("ما كاينة حتى قاعدة صامدة بهاد الشروط (نجاح + ROI موجب فالتدريب والاختبار). هادي أحسن 25 قاعدة "
                   "(مرتبة حسب أضعف ROI فيهم)، بحال إنذار ماشي توصية:")
    else:
        st.success(f"{len(ok)} قاعدة صامدة بهاد الشروط.")
        shown = ok.head(40)
    out = pd.DataFrame({
        "المعادلة والمعامل": shown["formula"], "الرهان": [pick_label(*MM_TARGET[t]) for t in shown["target"]],
        "متوسط الكوط": shown["avg_te"], "نقطة التعادل %": 100 / shown["avg_te"],
        "ماتشات التدريب": shown["n_tr"], "نجاح التدريب %": shown["h_tr"] * 100, "ROI التدريب %": shown["roi_tr"] * 100,
        "ماتشات الاختبار": shown["n_te"], "نجاح الاختبار %": shown["h_te"] * 100,
        "النسبة العامة %": shown["base_te"] * 100, "ROI الاختبار %": shown["roi_te"] * 100,
        "الحكم": [mm_verdict(r, tgt) for _, r in shown.iterrows()]})
    html_table(out, fmt={"نقطة التعادل %": "{:.1f}%", "نجاح التدريب %": "{:.1f}%", "نجاح الاختبار %": "{:.1f}%",
                         "النسبة العامة %": "{:.1f}%", "ROI التدريب %": "{:+.1f}%", "ROI الاختبار %": "{:+.1f}%",
                         "ماتشات التدريب": "{:.0f}", "ماتشات الاختبار": "{:.0f}"}, max_height=520)
    if prog is None or prog.empty:
        st.caption("ارفع برنامج جديد (فوق) باش نطبقو القواعد الصامدة عليه.")
        return
    inc = st.checkbox("عرض حتى الماتشات اللي داز وقتها (للتجربة فقط)", value=False, key="mm_inc_past")
    keep = ok.head(15)
    if keep.empty:
        st.caption("ما كاينة حتى قاعدة صامدة باش تتطبق على البرنامج الجديد.")
        return
    fut = selections_from_programme(prog[prog["ok"] | prog["partial"]])
    if not inc:
        fut = upcoming_only(fut)
    if fut.empty:
        st.info("ما بقى حتى ماتش جاي فهاد البرنامج.")
        return
    odds, _, _ = wide_from_long(fut)
    O = odds.values
    info_df = prog.drop_duplicates("mid").set_index("mid")
    picks = []
    for _, r in keep.iterrows():
        op, i, j, k, d, t = r["spec"]
        S = eval_formula(op, i, j, k, O)
        ti = MM_ORDER.index(r["target"])
        with np.errstate(all="ignore"):
            mask = (((S >= t) if d == 1 else (S <= t)) & np.isfinite(S) & np.isfinite(O[:, ti])
                    & (O[:, ti] >= lo) & (O[:, ti] <= hi))
        for pos in np.where(mask)[0]:
            mid = odds.index[pos]
            row = info_df.loc[mid]
            picks.append({"التاريخ": day_label(row["date"]), "الساعة": row["time"], "المقابلة": vs_text(row),
                          "كود الكوبون": row["coupon"], "كود الجهاز": row["terminal"],
                          "الرهان": pick_label(*MM_TARGET[r["target"]]), "الكوط": O[pos, ti],
                          "القاعدة": r["formula"], "ROI الاختبار %": r["roi_te"] * 100})
    st.markdown("#### 🎯 ماتشات البرنامج الجديد المطابقة للقواعد الصامدة (للتجريب فقط)")
    html_table(pd.DataFrame(picks) if picks else pd.DataFrame(), fmt={"ROI الاختبار %": "{:+.1f}%"}, max_height=420)


# =====================================================================
# 13) سجل التوقعات ومراجعة الأخطاء (التعلم من الأخطاء)
# =====================================================================
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.csv")
FLAGS = {"f_star": "نجم غايب", "f_need": "المدعوم محتاج نقط", "f_opp": "المنافس محتاج نقط",
         "f_relax": "المدعوم مرتاح", "f_cup": "كأس/روتاسيون"}
J_COLS = ["jid", "saved_at", "ticket", "mid", "date", "time", "league", "team1", "team2", "market", "selection",
          "odd", "n", "hits", "rate", "roi", "fair_p", "edge", "odds_json", "status", "gh", "ga", "hth", "hta",
          "won"] + list(FLAGS)
SEL_IDX = {(m, s): i for i, m, s in SELECTIONS}


def clean_journal(j):
    j = j.copy()
    for c in J_COLS:
        if c not in j.columns:
            j[c] = None
    j["date"] = pd.to_datetime(j["date"]).dt.date
    j["won"] = j["won"].map(_b).astype(object)
    for c in FLAGS:
        j[c] = j[c].map(lambda v: v in (True, "True", "true", 1, 1.0)).astype(bool)
    for c in ("jid", "ticket", "mid", "team1", "team2", "time", "odds_json", "status"):
        j[c] = j[c].fillna("").astype(str)
    for c in ("odd", "n", "hits", "rate", "roi", "fair_p", "edge", "gh", "ga", "hth", "hta"):
        j[c] = pd.to_numeric(j[c], errors="coerce")
    return j[J_COLS].reset_index(drop=True)


def load_journal():
    if os.path.exists(JOURNAL_PATH):
        return clean_journal(pd.read_csv(JOURNAL_PATH, dtype={"mid": str}))
    return clean_journal(pd.DataFrame(columns=J_COLS))


def save_journal(j):
    os.makedirs(DATA_DIR, exist_ok=True)
    j.to_csv(JOURNAL_PATH, index=False)


def journal_add(t, title, hf=None):
    hf = hf or {}
    stamp = now_ma().strftime("%Y-%m-%d %H:%M")
    rows = []
    for _, r in t.iterrows():
        h = hf.get((r["mid"], r["market"], r["selection"]), {})
        rows.append({"jid": f"{r['mid']}|{r['market']}|{r['selection']}|{title}", "saved_at": stamp, "ticket": title,
                     "mid": r["mid"], "date": r["date"], "time": r["time"], "league": r["league"],
                     "team1": r.get("team1", ""), "team2": r.get("team2", ""), "market": r["market"],
                     "selection": r["selection"], "odd": float(r["odd"]), "n": r.get("n"), "hits": r.get("hits"),
                     "rate": r.get("نسبة الربح %"), "roi": r.get("ROI %"), "fair_p": r.get("fair_p"),
                     "edge": r.get("edge"), "odds_json": r.get("odds_json", ""), "status": "", "gh": None,
                     "ga": None, "hth": None, "hta": None, "won": None,
                     "f_star": False, "f_need": bool(h.get("_need", False)), "f_opp": bool(h.get("_opp", False)),
                     "f_relax": bool(h.get("_relax", False)), "f_cup": False})
    new = clean_journal(pd.DataFrame(rows))
    both = pd.concat([st.session_state.journal, new], ignore_index=True).drop_duplicates("jid", keep="first")
    st.session_state.journal = both.reset_index(drop=True)
    save_journal(st.session_state.journal)
    return len(new)


def journal_sync():
    """يسوّي أوتوماتيكيا الاختيارات المسجلة اللي نتيجتها ديجا فالأرشيف (بلا ما تدخلها مرة أخرى)."""
    j, h = st.session_state.journal, st.session_state.hist
    if j.empty or h.empty:
        return 0
    pend = j["won"].isna() & ~j["mid"].astype(str).str.startswith("manual_")
    if not pend.any():
        return 0
    sub = h[h["mid"].isin(set(j.loc[pend, "mid"])) & h["won"].notna()]
    if sub.empty:
        return 0
    lookup = {(r["mid"], r["market"], r["selection"]): (bool(r["won"]), r.get("score", ""), r.get("ht", ""))
              for r in sub.to_dict("records")}
    j, n = j.copy(), 0
    for i in j.index[pend]:
        v = lookup.get((j.at[i, "mid"], j.at[i, "market"], j.at[i, "selection"]))
        if v is None:
            continue
        won, score, ht = v
        gh, ga = _parse_score(score)
        hh, ha = _parse_score(ht)
        j.at[i, "won"], j.at[i, "status"] = won, "FT"
        if gh is not None:
            j.at[i, "gh"], j.at[i, "ga"] = gh, ga
        if hh is not None:
            j.at[i, "hth"], j.at[i, "hta"] = hh, ha
        n += 1
    if n:
        st.session_state.journal = clean_journal(j)
        save_journal(st.session_state.journal)
        for k in ("jres", "jres_editor"):
            st.session_state.pop(k, None)
    return n


def pick_result(market, sel, gh, ga, hth=None, hta=None):
    idx = SEL_IDX.get((market, sel))
    if idx is None:
        return None
    r = {f"o{i}": float("nan") for i in range(N_TOKENS)}
    r[f"o{idx}"] = 2.0
    out = evaluate_match(r, int(gh), int(ga), hth, hta)
    return out[0]["won"] if out else None


def odds_row(js):
    r = {f"o{i}": float("nan") for i in range(N_TOKENS)}
    try:
        r.update({k: float(v) for k, v in json.loads(js).items()})
    except Exception:
        pass
    return r


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)


def loss_story(r):
    gh, ga, hth, hta = _num(r["gh"]), _num(r["ga"]), _num(r["hth"]), _num(r["hta"])
    res, tot, market, sel = _res(gh, ga), gh + ga, r["market"], r["selection"]
    parts = [f"النتيجة {gh}-{ga}" + (f" (الشوط الأول {hth}-{hta})" if hth is not None and hta is not None else "")]
    side = backed_side(sel) if market in ("1X2", "حظ مزدوج", "نتيجة + M/P 2.5") else 0
    if side and hth is not None and hta is not None:
        hr = _res(hth, hta)
        if hr == str(side) and res != str(side):
            parts.append("🔄 كان متقدم فالشوط الأول وقلبوها عليه")
        elif res == "X" and side:
            parts.append("تعادل")
        else:
            parts.append("خسر بفارق " + str(abs(gh - ga)))
    elif side:
        parts.append("خسر بفارق " + str(abs(gh - ga)) if res != "X" else "تعادل")
    if market.startswith("M/P") and "شوط1" not in market:
        parts.append(f"المجموع {tot} هدف مقابل خط {market.split()[1]}")
    if market == "BTTS":
        parts.append("واحد من الفريقين ما سجلش" if sel == "Oui" else "الجوج سجلو")
    return " | ".join(parts)


def trap_signals(r):
    sig = []
    fp = r["fair_p"]
    if pd.notna(fp) and fp < 0.45:
        sig.append(f"احتمال الشركة الصافي غير {fp * 100:.0f}%")
    o = odds_row(r["odds_json"])
    if r["market"] in ("1X2", "حظ مزدوج", "نتيجة + M/P 2.5") and all(np.isfinite([o["o0"], o["o1"], o["o2"]])):
        p = np.array([1 / o["o0"], 1 / o["o1"], 1 / o["o2"]])
        p = p / p.sum()
        if abs(p[0] - p[2]) < 0.12:
            sig.append("ماتش متكافئ (الفرق بين الفريقين أقل من 12%)")
        if p[1] > 0.30:
            sig.append("احتمال التعادل مرتفع (+30%)")
    if pd.notna(r["roi"]) and r["roi"] <= -10:
        sig.append(f"النمط كان كيخسر فالأرشيف (ROI {r['roi']:+.0f}%)")
    if pd.notna(r["n"]) and r["n"] < 20:
        sig.append(f"عينة الأرشيف صغيرة ({int(r['n'])} ماتش)")
    sig += [lab for k, lab in FLAGS.items() if r[k] and k != "f_relax"]
    return sig


def advice(sig):
    t = " ".join(sig)
    tips = []
    if "متكافئ" in t or "التعادل" in t:
        tips.append("فالماتش المتكافئ فضّل 1X/X2 ولا Plus/Moins بدل 1/2")
    if "احتمال الشركة" in t:
        tips.append("ما تدخلش اختيارات احتمالها الصافي أقل من 45%")
    if "ROI" in t:
        tips.append("فعّل 'اشترط ROI موجب'")
    if "عينة" in t:
        tips.append("طلّع 'أقل عدد ماتشات'")
    if any(lab in t for k, lab in FLAGS.items() if k != "f_relax"):
        tips.append("هاد الأخبار كانت خاصة تخرجو الماتش (استبعدو قبل التسجيل)")
    tips.append("شغّل فلتر H2H & Form + السياق قبل التسجيل")
    return " • ".join(tips)


def winners_text(r):
    o = odds_row(r["odds_json"])
    out = evaluate_match(o, int(r["gh"]), int(r["ga"]), _num(r["hth"]), _num(r["hta"]))
    keep = [e for e in out if e["won"] and e["market"] in ("1X2", "حظ مزدوج", "M/P 1.5", "M/P 2.5", "M/P 3.5", "BTTS")]
    keep = sorted(keep, key=lambda e: -e["odd"])[:4]
    return " | ".join(f"{pick_label(e['market'], e['selection'])} @ {e['odd']:.2f}" for e in keep) or "—"


def derive_blacklist(j):
    """قواعد التجنب من الأخطاء: نمط/نوع رهان/دوري فيه ≥ N رهانات ونجاحو ≤ العتبة."""
    min_n = int(st.session_state.get("bl_min_n", 3))
    max_rate = float(st.session_state.get("bl_max_rate", 34)) / 100
    s = j[j["won"].notna()].copy()
    if s.empty:
        return []
    s["w"] = s["won"].astype(bool).astype(int)
    s["band"] = pd.cut(s["odd"], BANDS, labels=BAND_LABELS, right=False).astype(str)
    rules = []
    for keys, kind in ((["market", "selection", "band"], "نمط"), (["market", "selection"], "نوع رهان"),
                       (["league"], "دوري")):
        g = s.groupby(keys).agg(n=("w", "size"), w=("w", "sum")).reset_index()
        g = g[(g["n"] >= min_n + (1 if kind != "نمط" else 0)) & (g["w"] / g["n"] <= max_rate)]
        for _, r in g.iterrows():
            rules.append({"kind": kind, "keys": {k: r[k] for k in keys}, "n": int(r["n"]), "w": int(r["w"])})
    return rules


def apply_blacklist(pool, rules):
    p = add_band(pool) if "band" not in pool.columns else pool.copy()
    drop = pd.Series(False, index=p.index)
    for ru in rules:
        m = pd.Series(True, index=p.index)
        for k, v in ru["keys"].items():
            m &= (p[k].astype(str) == str(v))
        drop |= m
    return pool[~drop], int(drop.sum())


def _stats(d, by):
    g = d.groupby(by, observed=True).agg(n=("w", "size"), wins=("w", "sum"), roi=("ret", "mean")).reset_index()
    g["نسبة النجاح %"] = g["wins"] / g["n"] * 100
    g["ROI %"] = g["roi"] * 100
    return g.drop(columns=["roi"])


MANUAL_OPTS = [(m, sel) for _, m, sel in SELECTIONS if m != "هانديكاب"]


def _parse_score(t):
    m = re.match(r"^\s*(\d{1,2})\s*[-:–]\s*(\d{1,2})\s*$", str(t or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def manual_entry_form():
    """زيد اختيار لعبتيه قبل (حتى بلا ما يكون خرج من النظام) باش النظام يتعلم منو."""
    with st.expander("➕ زيد اختيار لعبتيه (ماتش داز) باش النظام يتعلم منو", expanded=st.session_state.journal.empty):
        with st.form("j_manual_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            d = c1.date_input("التاريخ", value=today_ma(), key="jm_date")
            tm = c2.text_input("الساعة (HH:MM)", value="20:00", key="jm_time")
            lg = c3.selectbox("الدوري", list(LEAGUE_NAMES.keys()), format_func=league_name, key="jm_league")
            t1 = c1.text_input("الفريق 1", key="jm_t1")
            t2 = c2.text_input("الفريق 2", key="jm_t2")
            odd = c3.number_input("الكوط", min_value=1.01, max_value=100.0, value=1.80, step=0.01, key="jm_odd")
            pk = st.selectbox("الرهان اللي لعبتي", MANUAL_OPTS, format_func=lambda x: pick_label(*x), key="jm_pick")
            r1, r2 = st.columns(2)
            ft = r1.text_input("النتيجة النهائية (مثال 3-1) — خليها خاوية إيلا مازال", key="jm_ft")
            ht = r2.text_input("نتيجة الشوط الأول (اختياري، مثال 1-0)", key="jm_ht")
            fc = st.columns(len(FLAGS))
            fl = {k: fc[i].checkbox(lab, key=f"jm_{k}") for i, (k, lab) in enumerate(FLAGS.items())}
            go = st.form_submit_button("💾 زيد للسجل")
        if go:
            if not str(t1).strip() or not str(t2).strip() or not re.match(r"^\d{1,2}:\d{2}$", str(tm).strip()):
                st.error("كمّل الفريقين والساعة (HH:MM).")
                return
            gh, ga = _parse_score(ft)
            hh, ha = _parse_score(ht)
            market, sel = pk
            won = pick_result(market, sel, gh, ga, hh, ha) if gh is not None else None
            slug = re.sub(r"\W+", "", str(t1) + str(t2))
            mid = f"manual_{d}_{slug}"
            row = {"jid": f"{mid}|{market}|{sel}|يدوي", "saved_at": now_ma().strftime("%Y-%m-%d %H:%M"), "ticket": "يدوي",
                   "mid": mid, "date": d, "time": str(tm).strip(), "league": lg, "team1": str(t1).strip(),
                   "team2": str(t2).strip(), "market": market, "selection": sel, "odd": float(odd), "n": None,
                   "hits": None, "rate": None, "roi": None, "fair_p": None, "edge": None, "odds_json": "",
                   "status": "FT" if gh is not None else "", "gh": gh, "ga": ga, "hth": hh, "hta": ha, "won": won, **fl}
            new = clean_journal(pd.DataFrame([row]))
            both = pd.concat([st.session_state.journal, new], ignore_index=True).drop_duplicates("jid", keep="first")
            st.session_state.journal = both.reset_index(drop=True)
            save_journal(st.session_state.journal)
            st.session_state.pop("jres", None)
            st.session_state.pop("jres_editor", None)
            st.session_state.msg_j = "تزاد الاختيار للسجل" + (" (محسوب)." if gh is not None else " (فالانتظار: دخل نتيجتو تحت).")
            st.rerun()


def render_journal():
    st.subheader("📓 سجل التوقعات ومراجعة الأخطاء")
    st.info("كتسجل الأوراق اللي خرجات لك، ومن بعد ما ديزو الماتشات كتدخل النتائج، والنظام كيقارن، كيفسر فين غلط، "
            "وكيبني **قائمة تجنب** كتطبق فالأوراق الجاية.")
    if st.session_state.get("msg_j") and st.session_state.journal.empty is False and st.session_state.journal["won"].isna().sum() == 0:
        st.success(st.session_state.pop("msg_j"))
    k_sync = journal_sync()
    if k_sync:
        st.success(f"✅ تسوّاو {k_sync} اختيار أوتوماتيكيا من النتائج اللي ديجا فالأرشيف.")
    jj0 = st.session_state.journal
    waiting = int((jj0["won"].isna() & ~jj0["mid"].astype(str).str.startswith("manual_")).sum()) if not jj0.empty else 0
    if waiting:
        st.caption(f"⏳ {waiting} اختيار كيتسنى النتيجة. زيد نتائج ماتشاتهم فقسم '📥 رفع الأرشيف' (جلب ولا لصق النتائج) "
                   "وكليكي 'ابدأ استخراج وتحليل وحفظ الأرشيف'، وغادي يتسواو هنا وحدهم.")
    manual_entry_form()
    j = st.session_state.journal
    if j.empty:
        st.info("السجل خاوي. جوج طرق باش تعمرو: (1) فقسم '🧠 فحص البرنامج الجديد ← 🎟️ بناء الأوراق' كليكي "
                "'💾 سجّل هاد الورقة' قبل الماتشات، (2) ولا زيد اختيار لعبتيه يدويا فوق بالنتيجة ديالو. "
                "من بعد غادي يبان هنا جدول النتائج والتحليل.")
        return
    settled = j[j["won"].notna()]
    pend = j[j["won"].isna()]
    wins = int(settled["won"].astype(bool).sum()) if not settled.empty else 0
    roi = float((settled["won"].astype(bool) * settled["odd"] - 1).mean() * 100) if not settled.empty else 0.0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("اختيارات مسجلة", len(j)); c2.metric("محسوبة", len(settled))
    c3.metric("نجحات", f"{wins} ({wins / len(settled) * 100:.0f}%)" if len(settled) else "—")
    c4.metric("ROI فالسجل", f"{roi:+.1f}%" if len(settled) else "—")

    if pend.empty and st.session_state.get("msg_j"):
        st.success(st.session_state.pop("msg_j"))
    if not pend.empty:
        st.markdown("### 1) دخل نتائج الماتشات اللي داز وقتها")
        if "jres" not in st.session_state:
            st.session_state.jres = pd.DataFrame({
                "jid": pend["jid"].values, "mid": pend["mid"].values, "date": pend["date"].values,
                "time": pend["time"].values, "match_text": (pend["team1"] + " " + pend["team2"]).str.strip().values,
                "الرهان": [pick_label(m, s) for m, s in zip(pend["market"], pend["selection"])],
                "odd": pend["odd"].values, "status": pend["status"].values, "gh": pend["gh"].values,
                "ga": pend["ga"].values, "hth": pend["hth"].values, "hta": pend["hta"].values, "src": "",
                **{k: pend[k].values for k in FLAGS}})
        jres = st.session_state.jres
        if st.button("🔎 جلب النتائج من API", key="j_fetch"):
            keys = get_keys()
            if not keys["af"] and not keys["fd"]:
                st.error("حط مفتاح واحد على الأقل فقسم '⚙️ ربط API'.")
            else:
                sub = jres.drop_duplicates("mid").assign(terminal=lambda d: d["mid"])[["terminal", "date", "time", "match_text"]]
                res, errs = match_results(sub.reset_index(drop=True), keys)
                for e in errs:
                    st.warning(e)
                by_mid = res.set_index("terminal")
                for i, r in jres.iterrows():
                    if r["mid"] in by_mid.index and by_mid.at[r["mid"], "status"] in FINAL:
                        for c in ("status", "gh", "ga", "hth", "hta", "src"):
                            jres.at[i, c] = by_mid.at[r["mid"], c]
                st.session_state.jres = jres
                st.session_state.pop("jres_editor", None)
                st.rerun()
        edited = st.data_editor(
            jres, key="jres_editor", hide_index=True, use_container_width=True,
            column_order=["date", "time", "match_text", "الرهان", "odd", "status", "gh", "ga", "hth", "hta"] + list(FLAGS),
            disabled=["jid", "mid", "date", "time", "match_text", "الرهان", "odd", "src"],
            column_config={"date": st.column_config.DateColumn("التاريخ", width="small"),
                           "time": st.column_config.TextColumn("الساعة", width="small"),
                           "match_text": st.column_config.TextColumn("المقابلة", width="large"),
                           "الرهان": st.column_config.TextColumn("الرهان", width="medium"),
                           "odd": st.column_config.NumberColumn("الكوط", format="%.2f", width="small"),
                           "status": st.column_config.TextColumn("الحالة (FT)", width="small"),
                           "gh": st.column_config.NumberColumn("أهداف 1", min_value=0, step=1, width="small"),
                           "ga": st.column_config.NumberColumn("أهداف 2", min_value=0, step=1, width="small"),
                           "hth": st.column_config.NumberColumn("شوط1 (1)", min_value=0, step=1, width="small"),
                           "hta": st.column_config.NumberColumn("شوط1 (2)", min_value=0, step=1, width="small"),
                           **{k: st.column_config.CheckboxColumn(v, width="small") for k, v in FLAGS.items()}})
        with st.expander("📋 لصق النتائج من أي موقع"):
            txt = st.text_area("كل سطر: Arsenal 3-0 Hull City (1-0)", height=140, key="j_paste")
            if st.button("طبّق النتائج الملصوقة", key="j_apply"):
                new_res, n_app = apply_pasted(edited, txt)
                st.session_state.jres = new_res
                st.session_state.pop("jres_editor", None)
                st.session_state.msg_j = f"تطبقات {n_app} نتيجة."
                st.rerun()
        if st.session_state.get("msg_j"):
            st.success(st.session_state.pop("msg_j"))
        if st.button("✅ احسب النتائج وحلل الأخطاء", type="primary", key="j_calc"):
            jj = st.session_state.journal.set_index("jid")
            n_set = 0
            for _, r in edited.iterrows():
                for f in FLAGS:
                    jj.at[r["jid"], f] = bool(r[f])
                done = str(r["status"]).upper() in ("FT", "AET", "PEN")
                if done and pd.notna(r["gh"]) and pd.notna(r["ga"]):
                    row = jj.loc[r["jid"]]
                    h1, h2 = _num(r["hth"]), _num(r["hta"])
                    jj.at[r["jid"], "won"] = pick_result(row["market"], row["selection"], int(r["gh"]), int(r["ga"]), h1, h2)
                    for c, v in (("gh", r["gh"]), ("ga", r["ga"]), ("hth", r["hth"]), ("hta", r["hta"])):
                        jj.at[r["jid"], c] = v
                    jj.at[r["jid"], "status"] = "FT"
                    n_set += 1
            st.session_state.journal = clean_journal(jj.reset_index())
            save_journal(st.session_state.journal)
            for k in ("jres", "jres_editor"):
                st.session_state.pop(k, None)
            st.session_state.msg_j = f"تحسبو {n_set} اختيار."
            st.rerun()

    settled = st.session_state.journal[st.session_state.journal["won"].notna()]
    if settled.empty:
        return
    losers = settled[settled["won"] == False].sort_values("date", ascending=False)
    st.markdown(f"### 2) مراجعة الخسائر ({len(losers)})")
    if losers.empty:
        st.success("ما كاين حتى اختيار خاسر فالسجل.")
    else:
        rows = []
        for _, r in losers.head(40).iterrows():
            sig = trap_signals(r)
            rows.append({"التاريخ": day_label(r["date"]), "المقابلة": vs_text(r),
                         "الرهان": pick_label(r["market"], r["selection"]), "الكوط": r["odd"],
                         "قصة الماتش": loss_story(r),
                         "إشارات التحذير": " • ".join(sig) or "ما بانت حتى إشارة فالأرقام",
                         "اللي كان كيربح فهاد الماتش": winners_text(r), "كيفاش نتفاداه": advice(sig)})
        html_table(pd.DataFrame(rows), fmt={"الكوط": "{:.2f}"}, max_height=520)

    st.markdown("### 3) شنو تعلمنا من السجل")
    d = settled.copy()
    d["w"] = d["won"].astype(bool).astype(int)
    d["ret"] = d["w"] * d["odd"] - 1
    d["band"] = pd.cut(d["odd"], BANDS, labels=BAND_LABELS, right=False)
    d["الدوري"] = d["league"].map(league_name)
    d["الرهان"] = [pick_label(m, s) for m, s in zip(d["market"], d["selection"])]
    fm = {"نسبة النجاح %": "{:.0f}%", "ROI %": "{:+.0f}%", "n": "{:.0f}", "wins": "{:.0f}"}
    t1, t2, t3 = st.tabs(["حسب الرهان", "حسب الكوط والدوري", "حسب السياق (أخبار)"])
    with t1:
        html_table(_stats(d, ["الرهان"]).sort_values("n", ascending=False), fmt=fm, max_height=300)
    with t2:
        html_table(_stats(d, ["band"]), fmt=fm, max_height=250)
        html_table(_stats(d, ["الدوري"]).sort_values("n", ascending=False), fmt=fm, max_height=300)
    with t3:
        fr = []
        for k, lab in FLAGS.items():
            a, b = d[d[k]], d[~d[k]]
            fr.append({"السياق": lab, "عدد (إيه)": len(a), "نجاح (إيه) %": a["w"].mean() * 100 if len(a) else float("nan"),
                       "عدد (لا)": len(b), "نجاح (لا) %": b["w"].mean() * 100 if len(b) else float("nan")})
        html_table(pd.DataFrame(fr), fmt={"نجاح (إيه) %": "{:.0f}%", "نجاح (لا) %": "{:.0f}%"}, max_height=260)
        st.caption("علّم الأعلام (نجم غايب، محتاج نقط...) فجدول النتائج فوق. كلما تراكمت الأمثلة، كتبان هاد الفروق.")
    st.markdown("#### 🛡️ قائمة التجنب (كتطبق أوتوماتيكيا فالأوراق)")
    b1, b2 = st.columns(2)
    b1.slider("أقل عدد رهانات باش نحكمو", 2, 10, 3, key="bl_min_n")
    b2.slider("أقصى نسبة نجاح للتجنب %", 0, 60, 34, key="bl_max_rate")
    rules = derive_blacklist(st.session_state.journal)
    if rules:
        html_table(pd.DataFrame([{"النوع": r["kind"], "الشرط": " | ".join(f"{k}={league_name(v) if k == 'league' else v}"
                                                                    for k, v in r["keys"].items()),
                                  "الرهانات": r["n"], "نجحات": r["w"]} for r in rules]), max_height=260)
        st.caption("⚠️ مع عينة صغيرة القائمة ممكن تكون صدفة. كل ما كبر السجل كل ما تصح.")
    else:
        st.caption("ما كاينة حتى قاعدة تجنب بعد (خاص كثر ديال الخسائر المتشابهة).")


def main():
    st.set_page_config(page_title="System Diagnostics & Logs", page_icon="⚙️", layout="wide")
    st.markdown(TABLE_CSS, unsafe_allow_html=True)
    st.title("System Diagnostics & Database Logs")
    st.markdown("---")

    if "hist" not in st.session_state:
        st.session_state.hist = load_history()
    if "pending" not in st.session_state:
        st.session_state.pending = load_pending()
    if "_keys" not in st.session_state:
        st.session_state["_keys"] = load_keys_file()
    if "journal" not in st.session_state:
        st.session_state.journal = load_journal()
    hist, pend = st.session_state.hist, st.session_state.pending

    st.sidebar.header("🎯 قائمة التحكم")
    mode = st.sidebar.radio("اختر القسم:", ["📥 رفع الأرشيف والتعلم التلقائي",
                                            "🧠 فحص البرنامج الجديد واكتشاف النمط",
                                            "🧮 المحرك الحسابي (Math Matrix)",
                                            "📓 سجل التوقعات ومراجعة الأخطاء",
                                            "⚙️ ربط API النتائج (API-Football)"])
    st.sidebar.metric("ماتشات فالأرشيف", hist["mid"].nunique() if not hist.empty else 0)
    st.sidebar.metric("ماتشات فالانتظار", len(pend))
    _j = st.session_state.journal
    st.sidebar.metric("سجل التوقعات (محسوبة / مسجلة)", f"{int(_j['won'].notna().sum())} / {len(_j)}")
    if not hist.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية: الأرشيف", hist.to_csv(index=False).encode("utf-8"),
                                   "history.csv", "text/csv")
    if not pend.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية: قائمة الانتظار", pend.to_csv(index=False).encode("utf-8"),
                                   "pending.csv", "text/csv")
    if not st.session_state.journal.empty:
        st.sidebar.download_button("⬇️ نسخة احتياطية: سجل التوقعات", st.session_state.journal.to_csv(index=False).encode("utf-8"),
                                   "journal.csv", "text/csv")
    bk = st.sidebar.file_uploader("استرجاع نسخة محفوظة (CSV)", type="csv", key="bk")
    if bk is not None and st.sidebar.button("دمج النسخة المرفوعة"):
        raw = bk.getvalue()
        cols_bk = set(pd.read_csv(io.BytesIO(raw), nrows=0).columns)
        if "jid" in cols_bk:
            jb = clean_journal(pd.read_csv(io.BytesIO(raw), dtype={"mid": str}))
            st.session_state.journal = pd.concat([st.session_state.journal, jb]).drop_duplicates("jid", keep="first").reset_index(drop=True)
            save_journal(st.session_state.journal)
        elif "won" in cols_bk:
            st.session_state.hist = merge_history(hist, clean_history(pd.read_csv(io.BytesIO(raw))))
            save_history(st.session_state.hist)
        else:
            st.session_state.pending = merge_pending(
                pend, clean_pending(pd.read_csv(io.BytesIO(raw), dtype={"terminal": str, "coupon": str})))
            save_pending(st.session_state.pending)
        st.rerun()
    st.sidebar.caption("Streamlit Cloud كيمسح الملفات مللي تعاود النشر. للحفظ النهائي: حمّل النسخ الاحتياطية وحطها فـ GitHub "
                       "داخل مجلد data/ (history.csv و pending.csv و journal.csv) وغادي يقراهم التطبيق وحدو.")

    # ============================================================= القسم 1
    if mode.startswith("📥"):
        st.subheader("📁 قسم تدريب النظام على البرامج السابقة (Auto-Backtesting)")
        st.info("ارفع برنامج (ولا برامج) قديمة، زيدها للقائمة، من بعد جيب النتائج الحقيقية.")
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
            dates = st.multiselect("التواريخ:", all_dates, default=all_dates)
            refresh = st.checkbox("تجاهل الكاش")
            done = set(st.session_state.hist["mid"]) if not st.session_state.hist.empty else set()

            def pick_sub():
                return pend[pend["date"].isin(dates) & pend["ok"] & ~pend["mid"].isin(done)].reset_index(drop=True)

            if st.button("🔎 جلب النتائج الحقيقية عبر API", type="primary"):
                keys = get_keys()
                if not keys["af"] and not keys["fd"]:
                    st.error("حط مفتاح واحد على الأقل فقسم '⚙️ ربط API'.")
                else:
                    sub = pick_sub()
                    bar = st.progress(0.0)
                    res, errs = match_results(sub, keys, refresh=refresh, progress=bar.progress)
                    st.session_state.sub, st.session_state.res, st.session_state.errs = sub, res, errs
                    st.session_state.pop("res_editor", None)
            for e in st.session_state.get("errs", []):
                st.warning(e)
            if "res" not in st.session_state:
                sub = pick_sub()
                st.session_state.sub = sub
                st.session_state.res = pd.DataFrame({
                    "terminal": sub["terminal"], "date": sub["date"], "time": sub["time"],
                    "match_text": sub["match_text"], "api_match": "", "score_match": 0.0,
                    "status": "", "gh": None, "ga": None, "hth": None, "hta": None, "src": ""})
            res, sub = st.session_state.res, st.session_state.sub
            got = int(res["status"].isin(FINAL).sum())
            st.info(f"{len(res)} ماتش فالتواريخ المحددة (بلا اللي ديجا فالأرشيف). تلقات النتيجة ديال {got} منها.")
            edited = st.data_editor(
                res, key="res_editor", hide_index=True, use_container_width=True,
                column_order=["date", "time", "terminal", "match_text", "api_match", "status", "gh", "ga", "hth", "hta", "src"],
                disabled=["terminal", "date", "time", "match_text", "api_match", "score_match", "src"],
                column_config={
                    "date": st.column_config.DateColumn("التاريخ", width="small"),
                    "time": st.column_config.TextColumn("الساعة", width="small"),
                    "terminal": st.column_config.TextColumn("كود الجهاز", width="small"),
                    "match_text": st.column_config.TextColumn("المقابلة (الفريقين)", width="large"),
                    "api_match": st.column_config.TextColumn("المطابقة من API", width="large"),
                    "status": st.column_config.TextColumn("الحالة (FT)", width="small"),
                    "src": st.column_config.TextColumn("المصدر", width="small"),
                    "gh": st.column_config.NumberColumn("أهداف 1", min_value=0, step=1, width="small"),
                    "ga": st.column_config.NumberColumn("أهداف 2", min_value=0, step=1, width="small"),
                    "hth": st.column_config.NumberColumn("شوط1 (1)", min_value=0, step=1, width="small"),
                    "hta": st.column_config.NumberColumn("شوط1 (2)", min_value=0, step=1, width="small")})
            with st.expander("📋 لصق النتائج من أي موقع"):
                st.caption("كل سطر: `Arsenal 3-0 Hull City` ولا مع الشوط الأول `Arsenal 3-0 Hull City (1-0)`.")
                txt = st.text_area("النتائج الملصوقة:", height=160, key="paste_txt")
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
                    st.warning("ما كاين حتى ماتش منتهي بنتيجة من الماتشات المحددة.")
                else:
                    st.session_state.hist = merge_history(st.session_state.hist, new)
                    save_history(st.session_state.hist)
                    k_s = journal_sync()
                    st.session_state.msg = (f"تحفظو {new['mid'].nunique()} ماتش فالأرشيف."
                                            + (f" • تسوّاو {k_s} اختيار فسجل التوقعات وتقارنو." if k_s else ""))
                    for k in ("res", "sub", "errs", "res_editor"):
                        st.session_state.pop(k, None)
                    st.rerun()

    # ============================================================= القسم 2
    elif mode.startswith("🧠"):
        st.subheader("🔍 فحص البرنامج الجديد واكتشاف الأنماط")
        f = st.file_uploader("📂 ارفع برنامج الرهانات الجديد (PDF):", type=["pdf"], key="new_prog_pdf")
        if not f:
            st.info("ارفع البرنامج الجديد باش تبان الأنماط والأوراق.")
        else:
            prog = parse_bytes(f.getvalue())
            if prog.empty:
                st.error("ما قدرتش نقرا الماتشات من هاد الملف.")
            else:
                st.success(f"📄 قريت {len(prog)} ماتش ({int(prog['ok'].sum())} بقراءة كاملة، "
                           f"{int(prog['partial'].sum())} بقراءة جزئية).")
                future_all = selections_from_programme(prog[prog["ok"] | prog["partial"]])
                odds_map = {r["mid"]: json.dumps({f"o{i}": r[f"o{i}"] for i in range(N_TOKENS) if _ok(r[f"o{i}"])})
                            for r in prog.to_dict("records")}
                future_all["odds_json"] = future_all["mid"].map(odds_map)
                inc = st.checkbox("عرض حتى الماتشات اللي داز وقتها (للتجربة فقط)", value=False, key="inc_past")
                future = future_all if inc else upcoming_only(future_all)
                n_all, n_up = future_all["mid"].nunique(), future["mid"].nunique()
                st.caption(f"دابا: {now_ma():%d/%m/%Y %H:%M} (توقيت المغرب). كنعرض غير الماتشات اللي مازال ما بداوش: "
                           f"**{n_up}** ماتش" + (f" (خبيت {n_all - n_up} ماتش داز وقتهم)." if n_all > n_up else "."))
                if future.empty:
                    st.warning("ما بقى حتى ماتش جاي فهاد البرنامج. ارفع برنامج جديد.")
                    return
                t_pat, t_tk = st.tabs(["🔍 أنماط الأرشيف و Value Bet Filter", "🎟️ بناء الأوراق"])
                with t_pat:
                    render_patterns(future, hist)
                with t_tk:
                    render_tickets(future, hist)

    # ============================================================= القسم 3
    elif mode.startswith("🧮"):
        f = st.file_uploader("📂 ارفع برنامج جديد لتطبيق القواعد (اختياري):", type=["pdf"], key="math_prog_pdf")
        render_math_matrix(parse_bytes(f.getvalue()) if f else None)

    # ============================================================= القسم 4: السجل
    elif mode.startswith("📓"):
        render_journal()

    # ============================================================= القسم 5
    else:
        st.subheader("⚙️ إعدادات ربط النتائج الحية والتاريخية")
        st.info("دخل المفاتيح مرة وحدة: كتبقى محفوظة فالجلسة (حتى إيلا بدلتي القسم) وفملف على السيرفر. "
                "للحفظ الدائم 100% (حتى بعد Reboot) حطهم فـ Streamlit Cloud ← Settings ← Secrets: "
                "`API_FOOTBALL_KEY = \"...\"` و `FOOTBALL_DATA_KEY = \"...\"`.")
        cur = st.session_state["_keys"]
        af_in = st.text_input("مفتاح API-Football:", value=cur.get("af", ""), type="password", key="af_input")
        fd_in = st.text_input("توكن football-data.org:", value=cur.get("fd", ""), type="password", key="fd_input")
        new_keys = {"af": af_in.strip(), "fd": fd_in.strip()}
        if new_keys != cur:
            st.session_state["_keys"] = new_keys
            save_keys_file(new_keys)
            st.success("✅ تحفظو المفاتيح.")
        st.caption(f"المفاتيح الحالية: API-Football {'✅' if get_keys()['af'] else '❌'} | "
                   f"football-data.org {'✅' if get_keys()['fd'] else '❌'}. "
                   "⚠️ إيلا التطبيق عام (public)، أي واحد يفتحو يقدر يستعمل المفاتيح ديالك.")
        if st.button("🔗 فحص الاتصال (API-Football)"):
            try:
                info = api_get("status", get_keys()["af"]).get("response", {})
                req, sub = info.get("requests", {}), info.get("subscription", {})
                st.success(f"✅ المفتاح صحيح. الخطة: {sub.get('plan', '?')} | الطلبات: {req.get('current', '?')} / {req.get('limit_day', '?')}")
            except Exception as e:
                st.error(f"❌ {explain_error(e)}")
        if st.button("🔗 فحص الاتصال (football-data.org)"):
            try:
                r = requests.get(f"{FD_BASE}/competitions", headers={"X-Auth-Token": get_keys()["fd"]}, timeout=30)
                if r.status_code == 200:
                    st.success("✅ التوكن صحيح.")
                else:
                    st.error(f"❌ {r.status_code} {r.json().get('message', '')}")
            except Exception as e:
                st.error(f"❌ {explain_error(e)}")
        directory = load_team_dir()
        st.markdown("#### 🗂️ دليل الفرق (باش يخدم فلتر H2H & Form)")
        st.caption(f"فالدليل دابا: {len(directory)} فريق. التحديث كيدير 12 طلب (حوالي 80 ثانية) ومرة وحدة تكفي.")
        if st.button("🔄 تحديث دليل الفرق"):
            if not get_keys()["fd"]:
                st.error("حط توكن football-data.org أول.")
            else:
                bar = st.progress(0.0)
                teams, errs = refresh_team_dir(get_keys()["fd"], bar.progress)
                st.success(f"تحمّلو {len(teams)} فريق.")
                for e in errs:
                    st.warning(e)
        if st.session_state.get("af_window"):
            a, b = st.session_state["af_window"]
            st.caption(f"API-Football المجاني كيسمح غير من {a} حتى {b}.")


if __name__ == "__main__":
    main()
