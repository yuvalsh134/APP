"""
Headless stock scanner — same logic as the original Colab script,
adapted to run unattended in CI (GitHub Actions):

  - No input() prompts, no google.colab imports
  - Tickers read from scanner/tickers.txt (commit your list there)
  - Cache lives in scanner/stock_cache (ephemeral per run unless you
    add an actions/cache step for it — see workflow file)
  - Results written to docs/alerts.json and docs/*.pdf so GitHub Pages
    can serve them as a static URL for the mobile app
  - Telegram token/chat id come from environment variables (GitHub secrets),
    never hardcoded

Run: python scanner/scan_headless.py
"""

import os
import sys
import time
import json
import pickle
import bisect
import random
import threading
import io
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore

import numpy as np
import pandas as pd
import requests
import pytz
import yfinance as yf
from defeatbeta_api.data.ticker import Ticker

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.pdfgen import canvas

import warnings, logging
warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.CRITICAL)

# ══════════════════════════════════════════════════════════
#  PATHS — relative to repo root, made for CI
# ══════════════════════════════════════════════════════════
ROOT_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS_FILE  = os.path.join(ROOT_DIR, "scanner", "tickers.txt")
CACHE_DIR     = os.path.join(ROOT_DIR, "scanner", "stock_cache")
DOCS_DIR      = os.path.join(ROOT_DIR, "docs")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════
#  SECRETS — from GitHub Actions env, not hardcoded
# ══════════════════════════════════════════════════════════
TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ══════════════════════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════════════════════
API_SEMAPHORE     = Semaphore(300)
MAX_RETRIES       = 2
MAX_WORKERS_SCAN  = 40   # CI runners have fewer cores than Colab — keep modest
MAX_WORKERS_RETRY = 20
BATCH_SIZE_PRICE  = 100
CACHE_DURATION    = timedelta(hours=4)

_price_mem: dict = {}
_fin_mem:   dict = {}
_PENDING         = object()
_fin_lock        = threading.Lock()

_SPX_DATES: np.ndarray = None
_SPX_VALS:  np.ndarray = None


# ── everything below (indicators, scan logic, PDF, ticker loading) is the
#    same algorithm as the Colab version — only I/O and entry points changed.

def _load_spx_once():
    global _SPX_DATES, _SPX_VALS
    try:
        t = yf.Ticker("SPY")
        raw = t.history(period="2y", interval="1d", auto_adjust=False, actions=False)
        if raw is None or raw.empty: return
        raw.columns = [c.lower().strip() for c in raw.columns]
        col = next((c for c in ["close", "adj close", "adjclose"] if c in raw.columns), None)
        if col is None: return
        s = pd.to_numeric(raw[col], errors="coerce").dropna()
        idx = pd.to_datetime(raw.index)
        if idx.tz is not None: idx = idx.tz_localize(None)
        order = idx.argsort()
        _SPX_DATES = idx.values[order].astype("datetime64[ns]")
        _SPX_VALS = s.values[order].astype(float)
    except Exception:
        pass


def get_tickers(limit=70):
    if not os.path.exists(TICKERS_FILE):
        raise FileNotFoundError(
            f"Missing {TICKERS_FILE}. Commit a tickers.txt (one ticker per line, or CSV with ticker first) to scanner/."
        )
    tickers = []
    with open(TICKERS_FILE) as f:
        for line in f:
            t = line.split(",")[0].strip().upper()
            if 1 <= len(t) <= 6 and t.replace(".", "").isalnum():
                tickers.append(t)
    return list(dict.fromkeys(tickers))[:limit]


def retry_with_backoff(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception:
            if attempt == MAX_RETRIES - 1: return None
            time.sleep(0.2 + random.uniform(0, 0.1))
    return None


def safe_float(v):
    if v is None: return None
    try: return float(v)
    except Exception: return None


def format_market_cap(v):
    if v is None: return "—"
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    return f"${v/1e3:.0f}K"


def _extract_numeric_series(row: pd.Series) -> pd.Series:
    result = {}
    for col, val in row.items():
        col_str = str(col).strip()
        if col_str.lower() in ("breakdown", "ttm"): continue
        if len(col_str) < 8 or col_str.count("-") < 2: continue
        try: ts = pd.Timestamp(col_str)
        except Exception: continue
        val_str = str(val).strip()
        if val_str in ("*", "", "nan", "None", "-", "N/A", "n/a"): continue
        try:
            num = float(val_str)
            if pd.notna(num): result[ts] = num
        except Exception: continue
    if not result: return pd.Series(dtype=float)
    s = pd.Series(result); s.index = pd.DatetimeIndex(s.index)
    return s.sort_index()


# ── disk cache ────────────────────────────────────────────
def _cache_path(t): return os.path.join(CACHE_DIR, f"{t}.pkl")

def save_to_cache(t, data):
    try:
        with open(_cache_path(t), "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception: pass

def load_from_cache(t):
    p = _cache_path(t)
    if not os.path.exists(p): return None
    try:
        with open(p, "rb") as f: return pickle.load(f)
    except Exception: return None

def is_cache_valid(t):
    p = _cache_path(t)
    if not os.path.exists(p): return False
    return (datetime.now() - datetime.fromtimestamp(os.path.getmtime(p))) < CACHE_DURATION

def _is_cache_entry_valid(data):
    try:
        df = data.get("df")
        if df is None or df.empty: return False
        if "close" not in df.columns or "high" not in df.columns: return False
        if data.get("last_price", 0) <= 0 or data.get("year_high", 0) <= 0: return False
        if float(df["close"].std()) == 0: return False
        return True
    except Exception: return False

def preload_cache_to_memory(tickers):
    def _load(t):
        if not is_cache_valid(t): return t, None
        d = load_from_cache(t)
        return t, d if (d and _is_cache_entry_valid(d)) else None
    with ThreadPoolExecutor(max_workers=64) as ex:
        for ticker, data in ex.map(_load, tickers):
            if data: _price_mem[ticker] = data


# ── batch price download ─────────────────────────────────
def _process_raw_batch(raw, batch):
    results = {}
    for ticker in batch:
        try:
            df_t = raw[ticker].copy() if len(batch) > 1 else raw.copy()
            if df_t is None or df_t.empty: continue
            df_t.columns = [str(c).lower().strip() for c in df_t.columns]
            col_map = {"open": ["open"], "close": ["close", "adj close", "adjclose"],
                       "high": ["high"], "low": ["low"], "volume": ["volume"]}
            needed = {}
            for want, alts in col_map.items():
                for alt in alts:
                    if alt in df_t.columns:
                        s = df_t[alt].copy(); s.name = want; needed[want] = s; break
            if "close" not in needed or "high" not in needed: continue
            df2 = pd.DataFrame(needed).apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
            df2.index = pd.to_datetime(df2.index)
            if df2.index.tz is not None: df2.index = df2.index.tz_localize(None)
            df2 = df2.sort_index()
            if len(df2) < 100: continue
            results[ticker] = {"df": df2, "last_price": float(df2["close"].iloc[-1]),
                                "year_high": float(df2["high"].max())}
        except Exception: continue
    return results

def prefetch_price_history_batch(tickers, force_refresh=False):
    to_fetch = tickers if force_refresh else [t for t in tickers if t not in _price_mem]
    if not to_fetch: return
    total, done = len(to_fetch), 0
    for i in range(0, total, BATCH_SIZE_PRICE):
        batch = to_fetch[i:i + BATCH_SIZE_PRICE]
        try:
            raw = yf.download(batch, period="2y", interval="1d", auto_adjust=False,
                               actions=False, group_by="ticker", threads=True, progress=False)
            if raw is not None and not raw.empty:
                for ticker, data in _process_raw_batch(raw, batch).items():
                    _price_mem[ticker] = data
                    save_to_cache(ticker, data)
        except Exception as e:
            print(f"batch error: {e}")
        done += len(batch)
        if done % 500 == 0 or done == total:
            print(f"  price download {done}/{total}")

def get_stock_data(ticker):
    if ticker in _price_mem: return _price_mem[ticker]
    try:
        t = yf.Ticker(ticker)
        raw = t.history(period="2y", interval="1d", auto_adjust=False, actions=False)
        if raw is None or raw.empty: return None
        raw.columns = [c.lower().strip() for c in raw.columns]
        col_map = {"open": ["open"], "close": ["close", "adj close", "adjclose"],
                   "high": ["high"], "low": ["low"], "volume": ["volume"]}
        needed = {}
        for want, alts in col_map.items():
            for alt in alts:
                if alt in raw.columns:
                    s = raw[alt].copy(); s.name = want; needed[want] = s; break
        if "close" not in needed or "high" not in needed: return None
        df = pd.DataFrame(needed).apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        df = df.sort_index()
        if len(df) < 100: return None
        result = {"df": df, "last_price": float(df["close"].iloc[-1]), "year_high": float(df["high"].max())}
        _price_mem[ticker] = result
        return result
    except Exception: return None


# ── RS rating ────────────────────────────────────────────
def _perf_np_arr(arr, n):
    n = min(n, len(arr) - 1)
    if n <= 0: return 0.0
    past = arr[-(n + 1)]; curr = arr[-1]
    return 0.0 if past <= 0 else (curr / past) - 1.0

def calculate_rs_raw(close_dates, close_vals):
    if _SPX_DATES is None or len(close_vals) < 63: return None
    mask_stk = np.isin(close_dates, _SPX_DATES)
    mask_spx = np.isin(_SPX_DATES, close_dates[mask_stk])
    stk_arr = close_vals[mask_stk]; ref_arr = _SPX_VALS[mask_spx]
    n = min(len(stk_arr), len(ref_arr))
    if n < 63: return None
    stk_arr = stk_arr[-n:]; ref_arr = ref_arr[-n:]
    rs_s = (.40*_perf_np_arr(stk_arr,63) + .20*_perf_np_arr(stk_arr,126) +
            .20*_perf_np_arr(stk_arr,189) + .20*_perf_np_arr(stk_arr,252))
    rs_r = (.40*_perf_np_arr(ref_arr,63) + .20*_perf_np_arr(ref_arr,126) +
            .20*_perf_np_arr(ref_arr,189) + .20*_perf_np_arr(ref_arr,252))
    return rs_s - rs_r

def normalize_rs_ratings(alerts_by_type, all_raw_scores):
    if not all_raw_scores: return
    scores_sorted = sorted(all_raw_scores.values()); n = len(scores_sorted)
    def to_pct(raw):
        if raw is None: return None
        return round(min(max((bisect.bisect_right(scores_sorted, raw)/n)*98.0+1.0, 1.0), 99.0), 1)
    for alerts in alerts_by_type.values():
        for alert in alerts:
            raw = alert.pop("_rs_raw", None)
            alert["rs_rating"] = to_pct(raw)


# ── indicators (numpy) ──────────────────────────────────
def _ema_np(arr, span):
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr); out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha*arr[i] + (1-alpha)*out[i-1]
    return out

def calculate_emas_batch(close, spans=(8,13,21,55,89,200)):
    return {s: float(_ema_np(close, s)[-1]) for s in spans}

def calculate_vortex_fast_np(high, low, close, period=14):
    pc = np.roll(close,1); pc[0] = close[0]
    tr = np.maximum.reduce([np.abs(high-low), np.abs(high-pc), np.abs(low-pc)])
    vmp = np.abs(high-np.roll(low,1)); vmp[0]=0
    vmm = np.abs(low-np.roll(high,1)); vmm[0]=0
    def roll_sum(a,n): return np.convolve(a, np.ones(n), "full")[n-1:len(a)]
    tr_s = roll_sum(tr,period)
    vip_s = roll_sum(vmp,period)/np.where(tr_s==0,1,tr_s)
    vim_s = roll_sum(vmm,period)/np.where(tr_s==0,1,tr_s)
    return float(vip_s[-1]), float(vim_s[-1])

def calculate_rsi_fast_np(close, period=14):
    delta = np.diff(close)
    gain = np.where(delta>0, delta, 0); loss = np.where(delta<0, -delta, 0)
    alpha = 1.0/period; ag = gain[0]; al = loss[0]
    for g,l in zip(gain[1:], loss[1:]):
        ag = alpha*g+(1-alpha)*ag; al = alpha*l+(1-alpha)*al
    return 100.0 - (100.0/(1.0+ag/al)) if al>0 else 100.0

def calculate_anchored_vwap_np(high, low, close, volume, lookback=252):
    n = min(lookback, len(close))
    if n < 20: return None
    hi,lo,cl,vol = high[-n:],low[-n:],close[-n:],volume[-n:]
    mask = ~(np.isnan(hi)|np.isnan(lo)|np.isnan(cl)|np.isnan(vol))
    hi,lo,cl,vol = hi[mask],lo[mask],cl[mask],vol[mask]
    sv = vol.sum()
    if sv == 0: return None
    return round(float(((hi+lo+cl)/3.0*vol).sum()/sv), 4)

def has_two_lower_shadow_candles(hi,lo,op,cl,pct=0.50):
    rng = hi[-2:]-lo[-2:]
    if np.any(rng<=0): return False
    body_low = np.minimum(op[-2:],cl[-2:])
    return bool(np.all(np.maximum(body_low-lo[-2:],0)/rng >= pct))

def has_high_volume_strong_close(hi,lo,cl,vol,lookback=20,top_pct=0.90):
    if len(vol) < lookback+1: return False
    rng = hi[-1]-lo[-1]
    if rng <= 0: return False
    vol_prev = vol[-(lookback+1):-1]; vol_prev = vol_prev[~np.isnan(vol_prev)]
    if len(vol_prev) == 0: return False
    if vol[-1] <= vol_prev.mean(): return False
    return bool((cl[-1]-lo[-1])/rng >= top_pct)


# ── financials ────────────────────────────────────────────
def get_financial_data(ticker_str):
    v = _fin_mem.get(ticker_str, _PENDING)
    if v is not _PENDING: return v
    with _fin_lock:
        v = _fin_mem.get(ticker_str, _PENDING)
        if v is not _PENDING: return v
        _fin_mem[ticker_str] = _PENDING

    API_SEMAPHORE.acquire()
    result = None
    try:
        t = Ticker(ticker_str)
        df_income = retry_with_backoff(lambda: t.quarterly_income_statement().df()
                                        if t.quarterly_income_statement() else None)
        market_cap = pe = roe = None
        try:
            mc = t.market_capitalization()
            if mc is not None and not mc.empty:
                market_cap = safe_float(mc.iloc[-1]["market_capitalization"])
        except Exception: pass
        try:
            def _last(df, col):
                if df is None or df.empty: return None
                try: return safe_float(df.iloc[-1][col])
                except Exception: return None
            pe = _last(t.ttm_pe(), "ttm_pe")
            roe = _last(t.roe(), "roe")
        except Exception: pass

        base = {"market_cap": market_cap, "pe": pe, "roe": roe,
                "rev_growth": None, "rev_qtr_count": None, "eps_qtr_count": None}

        if df_income is not None:
            try:
                rr = df_income[df_income["Breakdown"].str.contains("Revenue", na=False, case=False)]
                if not rr.empty:
                    rs2 = _extract_numeric_series(rr.iloc[0])
                    if len(rs2) >= 8:
                        qr = rs2.tail(8)
                        if all(v2 > 0 for v2 in qr.values):
                            nt = qr.tail(4).sum(); ot = qr.head(4).sum()
                            if ot > 0: base["rev_growth"] = round(((nt/ot)-1)*100, 2)
                        g = 0
                        for cd, cv in qr.tail(4).items():
                            tgt = cd - pd.DateOffset(days=365); tol = pd.Timedelta(days=45)
                            m = qr[(qr.index >= tgt-tol) & (qr.index <= tgt+tol)]
                            if not m.empty and cv > m.iloc[0]: g += 1
                        base["rev_qtr_count"] = f"{g}/4"

                er = pd.DataFrame()
                for pat in ["Earnings Per Share", "Basic Earnings Per Share",
                            "Diluted Earnings Per Share", "EPS", "Basic EPS", "Diluted EPS"]:
                    er = df_income[df_income["Breakdown"].str.contains(pat, na=False, case=False)]
                    if not er.empty: break
                if not er.empty:
                    es2 = _extract_numeric_series(er.iloc[0])
                    if len(es2) >= 8:
                        qe = es2.tail(8); g = 0
                        for cd, cv in qe.tail(4).items():
                            tgt = cd - pd.DateOffset(days=365); tol = pd.Timedelta(days=45)
                            m = qe[(qe.index >= tgt-tol) & (qe.index <= tgt+tol)]
                            if not m.empty and cv > m.iloc[0]: g += 1
                        base["eps_qtr_count"] = f"{g}/4"
            except Exception: pass
            result = base
        else:
            result = None
    finally:
        API_SEMAPHORE.release()
        _fin_mem[ticker_str] = result
    return result


# ── per-ticker processing (same rules as the original) ────
def process_ticker(ticker):
    try:
        cached = get_stock_data(ticker)
        if cached is None: return ("no_history", [], None, None, "No price history")
        df = cached["df"]; last_price = cached["last_price"]; year_high = cached["year_high"]
        if len(df) < 100: return ("no_history", [], None, None, "Less than 100 days")

        cl_arr = df["close"].values.astype(float); hi_arr = df["high"].values.astype(float)
        lo_arr = df["low"].values.astype(float); op_arr = df["open"].values.astype(float)
        vl_arr = df["volume"].values.astype(float); dt_arr = df.index.values.astype("datetime64[ns]")

        ema = calculate_emas_batch(cl_arr)
        vi_plus, vi_minus = calculate_vortex_fast_np(hi_arr, lo_arr, cl_arr)
        rsi = calculate_rsi_fast_np(cl_arr)
        ath = max(year_high, last_price)
        ath_dist = (ath-last_price)/ath*100 if ath > 0 else 0.0
        vwap_val = calculate_anchored_vwap_np(hi_arr, lo_arr, cl_arr, vl_arr)
        rs_raw = calculate_rs_raw(dt_arr, cl_arr)
        two_ls = len(df) >= 2 and has_two_lower_shadow_candles(hi_arr, lo_arr, op_arr, cl_arr)
        hvsc = has_high_volume_strong_close(hi_arr, lo_arr, cl_arr, vl_arr)
        last_volume = int(vl_arr[-1]) if not np.isnan(vl_arr[-1]) else None
        avg_volume_20 = int(np.nanmean(vl_arr[-20:])) if len(vl_arr) >= 20 else None

        try:
            fin = get_financial_data(ticker)
        except Exception as e:
            return ("no_data", [], rs_raw, None, f"Financial error: {str(e)[:80]}")
        if fin is None:
            return ("no_financials", [], rs_raw, None, "No Income Statement")

        rev_growth = fin.get("rev_growth"); rev_qtr_count = fin.get("rev_qtr_count")
        eps_qtr_count = fin.get("eps_qtr_count"); market_cap = fin.get("market_cap")
        pe = fin.get("pe"); roe = fin.get("roe")
        if rev_growth is None and rev_qtr_count is None and eps_qtr_count is None:
            return ("no_data", [], rs_raw, market_cap, "Revenue/EPS < 8 quarters")

        base_alert = {"ticker": ticker, "price": round(last_price, 2), "market_cap": market_cap,
                       "rev_growth": rev_growth, "rev_qtr_count": rev_qtr_count,
                       "eps_qtr_count": eps_qtr_count, "ath_dist": round(ath_dist, 2),
                       "vwap": vwap_val, "pe": pe, "roe": roe, "volume": last_volume,
                       "avg_volume_20": avg_volume_20, "rs_rating": None, "_rs_raw": rs_raw,
                       "two_lower_shadow": two_ls, "high_vol_strong_close": hvsc}
        alerts = []
        def _a(cat): alerts.append((cat, dict(base_alert)))

        if ema[200]<ema[89]<ema[55]<last_price and vi_plus>1.2 and rev_growth is not None and rev_growth>15:
            _a("REVERSAL BUY")
        if (ema[200]<ema[89]<ema[55]<ema[21]<last_price and rev_growth is not None and rev_growth>30
                and (vwap_val is None or last_price>vwap_val)):
            _a("TREND BUY")
        if (rev_growth is not None and rev_growth>30 and rev_qtr_count in ("4/4","3/4")
                and eps_qtr_count in ("4/4","3/4") and last_price>ema[200]):
            _a("GROWTH STOCKS")
        if (ema[200]<ema[89]<ema[55] and ema[200]*0.99<last_price<ema[200]*1.03
                and rev_qtr_count in ("4/4","3/4") and eps_qtr_count in ("4/4","3/4")
                and rev_growth is not None and rev_growth>6):
            _a("EMA200 SUPPORT")
        if rsi>60 and vi_plus>1.1 and last_price>ema[200]:
            _a("BUY RSI")
        if (vi_minus-vi_plus)>10.5 and last_price>ema[200]:
            _a("Counter-Trend BUY")
        if (vi_plus-vi_minus)>10.9 and last_price<ema[200]:
            _a("Counter-Trend SELL")

        return ("ok", alerts, rs_raw, market_cap, None)
    except Exception as e:
        return ("error", [], None, None, f"Error: {str(e)[:100]}")


# ── telegram ────────────────────────────────────────────
session = requests.Session()

def send_message_to_telegram(text):
    if not TOKEN or not CHAT_ID: return
    try:
        session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
                            "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        print("telegram failed:", e)

def send_pdf_to_telegram(pdf_bytes, filename):
    if not TOKEN or not CHAT_ID: return
    try:
        session.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                      data={"chat_id": CHAT_ID, "caption": f"📊 {datetime.now(pytz.timezone('Asia/Jerusalem')).strftime('%Y-%m-%d %H:%M')}",
                            "parse_mode": "Markdown"},
                      files={"document": (filename, pdf_bytes, "application/pdf")}, timeout=20)
    except Exception as e:
        print("telegram pdf failed:", e)


# ── JSON export (this is what the mobile app reads) ───────
def export_json(alerts_by_type, stats, elapsed):
    export_data = {
        "scan_time": datetime.now(pytz.timezone("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M:%S"),
        "scan_duration_sec": round(elapsed),
        "stats": {"total_scanned": sum(stats.values()), **stats},
        "alerts": [],
    }
    for category, alerts in alerts_by_type.items():
        for alert in alerts:
            row = {k: v for k, v in alert.items() if k != "_rs_raw"}
            row["category"] = category
            for k, v in row.items():
                if isinstance(v, float) and v != v: row[k] = None
            export_data["alerts"].append(row)
    out_path = os.path.join(DOCS_DIR, "alerts.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    print(f"wrote {out_path}")


# ── scan orchestration ──────────────────────────────────
def _run_scan_batch(tickers, alerts_by_type, stats, failed_by_reason, ticker_details,
                     all_raw_scores, max_workers, label=""):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_ticker, t): t for t in tickers}
        completed = 0; start = last_print = time.time()
        for future in as_completed(futures):
            completed += 1; now = time.time()
            if completed % 200 == 0 or (now - last_print) >= 5:
                elapsed = now - start; rate = completed/elapsed if elapsed > 0 else 0
                print(f"{label}{completed}/{len(tickers)} ({rate:.1f} t/s)")
                last_print = now
            ticker = futures[future]
            status, alerts, rs_raw, mc, error_detail = future.result()
            stats[status] = stats.get(status, 0) + 1
            if rs_raw is not None: all_raw_scores[ticker] = rs_raw
            if status in failed_by_reason:
                failed_by_reason[status].append(ticker)
                ticker_details[ticker] = {"market_cap": mc, "reason": status, "details": error_detail or ""}
            for typ, ad in alerts:
                if typ in alerts_by_type: alerts_by_type[typ].append(ad)


def run_scan():
    tickers = get_tickers()
    alerts_by_type = {"REVERSAL BUY": [], "TREND BUY": [], "GROWTH STOCKS": [],
                       "EMA200 SUPPORT": [], "BUY RSI": [], "Counter-Trend BUY": [],
                       "Counter-Trend SELL": []}
    stats = {"ok": 0, "no_history": 0, "no_financials": 0, "no_data": 0, "error": 0}
    failed_by_reason = {"no_history": [], "no_financials": [], "no_data": [], "error": []}
    ticker_details = {}
    all_raw_scores: dict = {}

    print(f"scanning {len(tickers)} tickers...")
    preload_cache_to_memory(tickers)
    prefetch_price_history_batch(tickers)
    _load_spx_once()

    start_time = time.time()
    _run_scan_batch(tickers, alerts_by_type, stats, failed_by_reason, ticker_details,
                     all_raw_scores, max_workers=MAX_WORKERS_SCAN)

    retry_tickers = list(failed_by_reason.get("error", []))
    if retry_tickers:
        for t in retry_tickers:
            failed_by_reason["error"].remove(t)
            stats["error"] = max(0, stats["error"]-1)
            ticker_details.pop(t, None)
        _run_scan_batch(retry_tickers, alerts_by_type, stats, failed_by_reason, ticker_details,
                         all_raw_scores, max_workers=MAX_WORKERS_RETRY, label="RETRY ")

    elapsed = time.time() - start_time
    normalize_rs_ratings(alerts_by_type, all_raw_scores)

    total = sum(len(v) for v in alerts_by_type.values())
    print(f"done in {elapsed:.0f}s | {total} alerts | ok:{stats['ok']} err:{stats['error']}")

    export_json(alerts_by_type, stats, elapsed)

    if total > 0:
        send_message_to_telegram(f"Scan complete — {total} alerts across {sum(1 for v in alerts_by_type.values() if v)} strategies")
    else:
        send_message_to_telegram("Scan completed – No new alerts")


if __name__ == "__main__":
    run_scan()
