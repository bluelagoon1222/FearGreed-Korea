#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FearGreed-Korea collector
------------------------
Builds  data/latest.json   (full snapshot + 250-day series for the dashboard)
Appends data/history.json  (one record per trading day, kept across runs)

Sources (all public, no login):
  * Naver Finance siseJson   : daily OHLCV for indices, ETFs and the 350-stock universe
  * Naver Finance market cap : all listed stocks (advance/decline counts, universe pick)
  * Naver Finance investor   : daily foreign net buying (KOSPI, 100M KRW)
  * KOFIA (optional)         : margin-loan balance; silently skipped if unavailable

Run:  python scripts/collect.py           (live)
      python scripts/collect.py --demo    (offline synthetic data, for UI testing)
"""
from __future__ import annotations

import ast
import json
import math
import random
import re
import sys
import time
import datetime as dt
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
DEMO = "--demo" in sys.argv

CFG = {
    "universe_kospi": 200,          # KOSPI large caps (KOSPI200 proxy; exact list used when available)
    "universe_kosdaq": 150,         # KOSDAQ large caps (KOSDAQ150 proxy)
    "history_years": 7,             # 5y chart + 250d percentile window + 252d 52w lookback
    "pct_window": 250,              # trading days used for percentile scoring
    "series_days": 1250,            # ~5 trading years published for charts
    "spark_days": 250,              # per-component sparkline length
    "timeout": 20,
    "retries": 3,
    "sleep": 0.12,
    "time_budget_sec": 1500,
    "bond_etf": "114260",                # KODEX 국고채3년
    "lev_etfs": ["122630", "233740"],    # KODEX 레버리지, KODEX 코스닥150레버리지
    "inv_etfs": ["252670", "251340"],    # KODEX 200선물인버스2X, KODEX 코스닥150선물인버스
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://finance.naver.com/",
}

START_TS = time.time()
LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{dt.datetime.now(KST).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)


def budget_left() -> float:
    return CFG["time_budget_sec"] - (time.time() - START_TS)


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------
if not DEMO:
    import requests
    from bs4 import BeautifulSoup

    SESSION = requests.Session()
    SESSION.headers.update(HEADERS)


def fetch_text(url: str, params: dict | None = None, encoding: str | None = None) -> str:
    last_err: Exception | None = None
    for attempt in range(CFG["retries"]):
        try:
            r = SESSION.get(url, params=params, timeout=CFG["timeout"])
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            time.sleep(CFG["sleep"])
            if encoding:
                return r.content.decode(encoding, errors="ignore")
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"fetch failed {url} {params}: {last_err}")


def to_num(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace(",", "").replace("%", "").replace("+", "").strip()
    t = re.sub(r"[^\d.\-]", "", t)
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Naver Finance: daily prices (indices, ETFs, stocks)
# ----------------------------------------------------------------------------
def parse_sise_json(txt: str) -> list[dict]:
    body = txt.strip()
    try:
        arr = ast.literal_eval(body)
    except Exception:  # noqa: BLE001
        arr = json.loads(body.replace("'", '"'))
    rows: list[dict] = []
    for row in arr[1:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        d = str(row[0]).strip()
        if not re.fullmatch(r"\d{8}", d):
            continue
        c = to_num(row[4])
        if c is None or c <= 0:
            continue
        rows.append({
            "date": d,
            "open": to_num(row[1]),
            "high": to_num(row[2]),
            "low": to_num(row[3]),
            "close": c,
            "volume": to_num(row[5]) if len(row) > 5 else None,
        })
    rows.sort(key=lambda x: x["date"])
    return rows


def naver_daily(symbol: str, start: str, end: str) -> list[dict]:
    url = "https://api.finance.naver.com/siseJson.naver"
    params = {"symbol": symbol, "requestType": 1, "startTime": start,
              "endTime": end, "timeframe": "day"}
    return parse_sise_json(fetch_text(url, params))


def naver_index_daily_html(code: str, start: str) -> list[dict]:
    """Fallback for indices: finance.naver.com/sise/sise_index_day.naver (6 rows per page)."""
    url = "https://finance.naver.com/sise/sise_index_day.naver"
    out: dict[str, dict] = {}
    for page in range(1, 700):
        html = fetch_text(url, {"code": code, "page": page}, encoding="euc-kr")
        soup = BeautifulSoup(html, "lxml")
        added, oldest = 0, None
        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 2:
                continue
            m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})$", tds[0])
            if not m:
                continue
            d = "".join(m.groups())
            c = to_num(tds[1])
            if c and d not in out:
                out[d] = {"date": d, "open": None, "high": None, "low": None, "close": c, "volume": None}
                added += 1
            oldest = d if oldest is None or d < oldest else oldest
        if added == 0 or (oldest and oldest < start):
            break
    return sorted(out.values(), key=lambda r: r["date"])


def index_history(symbol: str, start: str, end: str) -> list[dict]:
    try:
        rows = naver_daily(symbol, start, end)
        if len(rows) >= 300:
            return rows
        log(f"{symbol} siseJson short ({len(rows)}), using HTML fallback")
    except Exception as e:  # noqa: BLE001
        log(f"{symbol} siseJson failed ({e}), using HTML fallback")
    return naver_index_daily_html(symbol, start)


# ----------------------------------------------------------------------------
# Naver Finance: all listed stocks (market cap pages)
# ----------------------------------------------------------------------------
ETF_BRAND = re.compile(
    r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|PLUS|HANARO|KOSEF|ARIRANG|TIMEFOLIO|KoAct|"
    r"WON|1Q|UNICORN|VITA|TREX|FOCUS|KIWOOM|KCGI|DAISHIN343|히어로즈|마이티|파워|에셋플러스)\b",
    re.IGNORECASE,
)
EXCLUDE_NAME = re.compile(r"(스팩|ETN|리츠$|인프라투융자|레버리지|인버스|선물|채권|국고채|단기채|머니마켓|CD금리)")


def is_common_stock(code: str, name: str) -> bool:
    if not code.endswith("0"):          # preferred shares end with 5/7/K etc.
        return False
    if ETF_BRAND.search(name):
        return False
    if EXCLUDE_NAME.search(name):
        return False
    return True


STOCK_LINK = re.compile(r"code=(\d{6})")


def naver_market_sum(sosok: int) -> list[dict]:
    """sosok 0 = KOSPI, 1 = KOSDAQ. Parses Naver's market-cap list (HTML)."""
    url = "https://finance.naver.com/sise/sise_market_sum.naver"
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, 120):
        html = fetch_text(url, {"sosok": sosok, "page": page}, encoding="euc-kr")
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        table = max(tables, key=lambda t: len(t.find_all("a", href=STOCK_LINK)), default=None)
        if table is None or not table.find_all("a", href=STOCK_LINK):
            if page == 1:
                log(f"market_sum sosok={sosok}: no stock table (tables={len(tables)}, html={len(html)}b, "
                    f"title={soup.title.get_text(strip=True)[:60] if soup.title else '-'})")
            break
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        idx = {h: i for i, h in enumerate(headers)}
        i_price = idx.get("현재가", 2)
        i_pct = idx.get("등락률", 4)
        i_mcap = idx.get("시가총액", 6)
        i_vol = idx.get("거래량", 9)
        new = 0
        for tr in table.find_all("tr"):
            a = tr.find("a", href=STOCK_LINK)
            if a is None:
                continue
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            code = STOCK_LINK.search(a["href"]).group(1)
            if code in seen:
                continue
            seen.add(code)
            new += 1

            def cell(i):
                return tds[i].get_text(strip=True) if i < len(tds) else ""

            out.append({
                "code": code,
                "name": a.get_text(strip=True),
                "price": to_num(cell(i_price)),
                "chg_pct": to_num(cell(i_pct)),
                "mktcap": to_num(cell(i_mcap)),     # 억원
                "volume": to_num(cell(i_vol)),
                "market": "KOSPI" if sosok == 0 else "KOSDAQ",
            })
        if new == 0:
            break
    return out


def naver_market_sum_api(market: str) -> list[dict]:
    """Fallback: Naver mobile JSON list by market value. market = KOSPI | KOSDAQ."""
    url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, 80):
        r = SESSION.get(url, params={"page": page, "pageSize": 100}, timeout=CFG["timeout"])
        if r.status_code != 200:
            log(f"market api {market} page {page}: HTTP {r.status_code}")
            break
        js = r.json()
        stocks = js.get("stocks") if isinstance(js, dict) else (js if isinstance(js, list) else [])
        if not stocks:
            break
        added = 0
        for it in stocks:
            if not isinstance(it, dict):
                continue
            code = str(it.get("itemCode") or it.get("code") or "")
            if not re.fullmatch(r"\d{6}", code) or code in seen:
                continue
            seen.add(code)
            added += 1
            pct = to_num(it.get("fluctuationsRatio"))
            cmp_code = str((it.get("compareToPreviousPrice") or {}).get("code", ""))
            if pct is not None:
                if cmp_code in ("4", "5"):
                    pct = -abs(pct)
                elif cmp_code == "3":
                    pct = 0.0
            out.append({
                "code": code,
                "name": it.get("stockName") or it.get("name") or "",
                "price": to_num(it.get("closePrice")),
                "chg_pct": pct,
                "mktcap": to_num(it.get("marketValue")),
                "volume": to_num(it.get("accumulatedTradingVolume")),
                "market": market,
            })
        total = js.get("totalCount") if isinstance(js, dict) else None
        time.sleep(CFG["sleep"])
        if added == 0 or (total and len(out) >= int(total)):
            break
    return out


def naver_kospi200_codes() -> list[str]:
    """Exact KOSPI200 constituent codes from Naver's 편입종목 pages (best effort)."""
    url = "https://finance.naver.com/sise/entryJongmok.naver"
    codes: list[str] = []
    for page in range(1, 30):
        try:
            html = fetch_text(url, {"page": page}, encoding="euc-kr")
        except Exception as e:  # noqa: BLE001
            log(f"kospi200 page {page} failed: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        new = 0
        for table in soup.find_all("table"):
            head = table.get_text(" ", strip=True)[:200]
            if "현재가" not in head:
                continue
            for a in table.find_all("a", href=re.compile(r"code=\d{6}")):
                c = re.search(r"code=(\d{6})", a["href"]).group(1)
                if c not in codes:
                    codes.append(c)
                    new += 1
        if new == 0:
            break
    return codes


# ----------------------------------------------------------------------------
# Naver Finance: investor trend (foreign net buying, KOSPI, 억원)
# ----------------------------------------------------------------------------
def naver_investor_net(start: str) -> dict[str, dict]:
    """Daily net buying (억원) by investor type for KOSPI: {date: {"foreign": v, "individual": v}}."""
    url = "https://finance.naver.com/sise/investorDealTrendDay.naver"
    bizdate = NOW.strftime("%Y%m%d")
    out: dict[str, dict] = {}
    for page in range(1, 400):
        html = fetch_text(url, {"bizdate": bizdate, "sosok": "", "page": page}, encoding="euc-kr")
        soup = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.type_1") or soup.find("table")
        if table is None:
            break
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        fidx = next((i for i, h in enumerate(headers) if "외국인" in h), 2)
        iidx = next((i for i, h in enumerate(headers) if h.startswith("개인")), 1)
        added = 0
        for tr in table.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) <= max(fidx, iidx):
                continue
            m = re.match(r"(\d{2,4})\.(\d{2})\.(\d{2})", tds[0])
            if not m:
                continue
            y = int(m.group(1))
            y = y + 2000 if y < 100 else y
            d = f"{y:04d}{m.group(2)}{m.group(3)}"
            fv, iv = to_num(tds[fidx]), to_num(tds[iidx])
            if fv is None or d in out:
                continue
            out[d] = {"foreign": fv, "individual": iv}
            added += 1
        if added == 0 or (out and min(out) <= start):
            break
    return out


def naver_index_turnover(code: str, start: str) -> tuple[dict[str, float], str]:
    """Daily trading value of an index from Naver's mobile API (best effort). Returns ({date: value}, unit_label)."""
    url = f"https://m.stock.naver.com/api/index/{code}/price"
    out: dict[str, float] = {}
    for page in range(1, 60):
        r = SESSION.get(url, params={"pageSize": 100, "page": page}, timeout=CFG["timeout"])
        if r.status_code != 200:
            break
        js = r.json()
        items = js if isinstance(js, list) else (js.get("data") or js.get("items") or []) if isinstance(js, dict) else []
        added = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            d = re.sub(r"\D", "", str(it.get("localTradedAt") or ""))[:8]
            v = to_num(it.get("accumulatedTradingValue"))
            if re.fullmatch(r"\d{8}", d) and v and v > 0 and d not in out:
                out[d] = v
                added += 1
        time.sleep(CFG["sleep"])
        if added == 0 or min(out) <= start:
            break
    return out, "naver mobile api (accumulatedTradingValue)"


# ----------------------------------------------------------------------------
# Yahoo Finance fallback for the universe (batch download; used only if Naver siseJson fails)
# ----------------------------------------------------------------------------
def yahoo_universe(universe: list[dict], start: str, end: str) -> dict[str, list[dict]]:
    try:
        import yfinance as yf  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        log(f"yfinance not available: {e}")
        return {}
    tick = {f"{u['code']}.{'KS' if u['group'] == 'KOSPI' else 'KQ'}": u["code"] for u in universe}
    out: dict[str, list[dict]] = {}
    try:
        df = yf.download(list(tick), start=f"{start[:4]}-{start[4:6]}-{start[6:]}",
                         end=f"{end[:4]}-{end[4:6]}-{end[6:]}", group_by="ticker",
                         auto_adjust=False, threads=True, progress=False)
    except Exception as e:  # noqa: BLE001
        log(f"yfinance download failed: {e}")
        return {}
    for t, code in tick.items():
        try:
            sub = df[t] if t in df.columns.get_level_values(0) else None
            if sub is None:
                continue
            rows = []
            for ts, r in sub.iterrows():
                c = r.get("Close")
                if c is None or c != c or c <= 0:
                    continue
                rows.append({"date": ts.strftime("%Y%m%d"), "open": None, "high": None, "low": None,
                             "close": float(c), "volume": float(r.get("Volume") or 0)})
            if len(rows) >= 60:
                out[code] = rows
        except Exception:  # noqa: BLE001
            continue
    return out


# ----------------------------------------------------------------------------
# KOFIA: margin loan balance (optional, experimental)
# ----------------------------------------------------------------------------
# Known 신용거래융자 잔고 (KOSPI+KOSDAQ) values used to identify the right KOFIA column and unit
KOFIA_ANCHORS = {"20260211": 31.3180e12, "20260515": 36.5675e12}   # KRW


def kofia_margin(start: str, end: str) -> tuple[dict[str, float], str]:
    """Margin-loan (신용거래융자) balance by day in KRW.
    The KOFIA statistics table returns unnamed columns TMPV2..TMPVn; the column and unit (억원/백만원)
    are chosen by matching two published reference values, so a layout change cannot silently
    switch the series to a different credit aggregate. Returns ({date: KRW}, description)."""
    url = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
    raw_rows: list[dict] = []
    cur = dt.datetime.strptime(start, "%Y%m%d")
    last = dt.datetime.strptime(end, "%Y%m%d")
    while cur <= last:
        nxt = min(cur + dt.timedelta(days=364), last)
        payload = {"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                                "tmpV45": cur.strftime("%Y%m%d"), "tmpV46": nxt.strftime("%Y%m%d"),
                                "OBJ_NM": "STATSCU0100000060BO"}}
        r = SESSION.post(url, json=payload, timeout=CFG["timeout"],
                         headers={"Content-Type": "application/json",
                                  "Referer": "https://freesis.kofia.or.kr/"})
        r.raise_for_status()
        js = r.json()
        rows = js.get("ds1") if isinstance(js, dict) else None
        if not rows and isinstance(js, dict):
            rows = next((v for v in js.values() if isinstance(v, list)), [])
        raw_rows.extend(row for row in (rows or []) if isinstance(row, dict))
        cur = nxt + dt.timedelta(days=1)
        time.sleep(CFG["sleep"])
    by_date: dict[str, dict] = {}
    for row in raw_rows:
        d = str(row.get("TMPV1", ""))[:8]
        if re.fullmatch(r"\d{8}", d):
            by_date[d] = row
    if len(by_date) < 60:
        raise RuntimeError(f"kofia rows too few: {len(by_date)}")

    keys = sorted({k for row in by_date.values() for k in row if re.fullmatch(r"TMPV\d+", k) and k != "TMPV1"})
    best, best_err = None, 1e9
    for k in keys:
        for unit_name, mult in (("억원", 1e8), ("백만원", 1e6), ("원", 1.0)):
            errs = []
            for ad, target in KOFIA_ANCHORS.items():
                row = by_date.get(ad)
                v = to_num(row.get(k)) if row else None
                if v is None or v <= 0:
                    continue
                errs.append(abs(v * mult - target) / target)
            if len(errs) == len(KOFIA_ANCHORS):
                e = sum(errs) / len(errs)
                if e < best_err:
                    best, best_err = (k, unit_name, mult), e
    if best is None or best_err > 0.05:
        # anchors not in range (or layout unknown): fall back to the first column, flagged as unverified
        k, unit_name, mult = "TMPV2", "단위 미확인", 1.0
        desc = f"column {k} unverified (best err {best_err:.2%})"
    else:
        k, unit_name, mult = best
        desc = f"column {k} = 신용거래융자 ({unit_name}, err {best_err:.2%})"
    out = {d: to_num(row.get(k)) * mult for d, row in by_date.items()
           if to_num(row.get(k)) is not None and to_num(row.get(k)) > 0}
    return out, desc


# ----------------------------------------------------------------------------
# VKOSPI (best effort, several Naver routes; values validated 3 < v < 200)
# ----------------------------------------------------------------------------
def fetch_vkospi(start: str, end: str) -> tuple[dict[str, float], str]:
    def ok(vals: dict) -> bool:
        return len(vals) >= 250

    # 1) siseJson
    try:
        rows = naver_daily("VKOSPI", start, end)
        vals = {r["date"]: r["close"] for r in rows if 3 < r["close"] < 200}
        if ok(vals):
            return vals, "naver siseJson"
        log(f"vkospi siseJson rows: {len(vals)}")
    except Exception as e:  # noqa: BLE001
        log(f"vkospi siseJson failed: {e}")

    # 2) mobile API (paged JSON)
    try:
        out: dict[str, float] = {}
        for page in range(1, 120):
            r = SESSION.get("https://m.stock.naver.com/api/index/VKOSPI/price",
                            params={"pageSize": 100, "page": page}, timeout=CFG["timeout"])
            if r.status_code != 200:
                break
            js = r.json()
            items = js if isinstance(js, list) else (js.get("data") or js.get("items") or []) if isinstance(js, dict) else []
            added = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                d = re.sub(r"\D", "", str(it.get("localTradedAt") or it.get("date") or ""))[:8]
                v = to_num(it.get("closePrice") or it.get("close") or it.get("clpr"))
                if re.fullmatch(r"\d{8}", d) and v and 3 < v < 200 and d not in out:
                    out[d] = v
                    added += 1
            time.sleep(CFG["sleep"])
            if added == 0 or min(out) <= start:
                break
        if ok(out):
            return out, "naver mobile api"
        log(f"vkospi mobile api rows: {len(out)}")
    except Exception as e:  # noqa: BLE001
        log(f"vkospi mobile api failed: {e}")

    # 3) index daily HTML page
    try:
        rows = naver_index_daily_html("VKOSPI", start)
        vals = {r["date"]: r["close"] for r in rows if 3 < r["close"] < 200}
        if ok(vals):
            return vals, "naver index page"
        log(f"vkospi index page rows: {len(vals)}")
    except Exception as e:  # noqa: BLE001
        log(f"vkospi index page failed: {e}")

    # 4) realtime endpoint: current value only (accumulates in history.json over time)
    try:
        r = SESSION.get("https://polling.finance.naver.com/api/realtime/domestic/index/VKOSPI",
                        timeout=CFG["timeout"])
        js = r.json()
        datas = js.get("datas") if isinstance(js, dict) else None
        it = datas[0] if datas else (js if isinstance(js, dict) else {})
        v = to_num(it.get("closePrice") or it.get("close"))
        d = re.sub(r"\D", "", str(it.get("localTradedAt") or ""))[:8] or end
        if v and 3 < v < 200:
            return {d: v}, "naver realtime (current value only)"
    except Exception as e:  # noqa: BLE001
        log(f"vkospi realtime failed: {e}")
    return {}, "unavailable"


# ----------------------------------------------------------------------------
# Math helpers (lists with None)
# ----------------------------------------------------------------------------
def rolling_mean(vals: list, w: int) -> list:
    out = [None] * len(vals)
    q: deque = deque()
    s = 0.0
    for i, v in enumerate(vals):
        if v is None:
            q.clear()
            s = 0.0
            continue
        q.append(v)
        s += v
        if len(q) > w:
            s -= q.popleft()
        if len(q) == w:
            out[i] = s / w
    return out


def rolling_extreme(vals: list, w: int, want_max: bool) -> list:
    """Rolling max/min over the last w points (inclusive). None resets the window."""
    out = [None] * len(vals)
    dq: deque = deque()   # (index, value), monotonic
    count = 0
    for i, v in enumerate(vals):
        if v is None:
            dq.clear()
            count = 0
            continue
        count += 1
        while dq and ((dq[-1][1] <= v) if want_max else (dq[-1][1] >= v)):
            dq.pop()
        dq.append((i, v))
        while dq and dq[0][0] <= i - w:
            dq.popleft()
        if count >= w:
            out[i] = dq[0][1]
    return out


def pct_change(vals: list, n: int) -> list:
    out = [None] * len(vals)
    for i in range(n, len(vals)):
        a, b = vals[i - n], vals[i]
        if a and b and a > 0:
            out[i] = (b / a - 1.0) * 100.0
    return out


def realized_vol(closes: list, n: int = 20) -> list:
    rets = [None] * len(closes)
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b and a > 0 and b > 0:
            rets[i] = math.log(b / a)
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        win = rets[i - n + 1:i + 1]
        if any(x is None for x in win):
            continue
        m = sum(win) / n
        var = sum((x - m) ** 2 for x in win) / (n - 1)
        out[i] = math.sqrt(var) * math.sqrt(252) * 100.0
    return out


def percentile_scores(series: list, window: int, invert: bool = False) -> list:
    """0-100 empirical percentile of each value within its trailing window."""
    out = [None] * len(series)
    for i, v in enumerate(series):
        if v is None:
            continue
        win = [x for x in series[max(0, i - window + 1):i + 1] if x is not None]
        if len(win) < max(30, window // 3):
            continue
        less = sum(1 for x in win if x < v)
        equal = sum(1 for x in win if x == v)
        sc = 100.0 * (less + 0.5 * equal) / len(win)
        out[i] = 100.0 - sc if invert else sc
    return out


def label_for(score: float | None) -> str:
    if score is None:
        return "-"
    if score < 25:
        return "극단적 공포"
    if score < 45:
        return "공포"
    if score <= 55:
        return "중립"
    if score <= 75:
        return "탐욕"
    return "극단적 탐욕"


def heat_label(score: float | None) -> str:
    if score is None:
        return "-"
    if score < 25:
        return "냉각"
    if score < 45:
        return "낮음"
    if score <= 55:
        return "보통"
    if score <= 75:
        return "과열 주의"
    return "과열 위험"


def median(vals: list) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def sma_last(vals: list, w: int):
    m = rolling_mean(vals, w)
    return m[-1] if m else None


def rnd(x, nd=2):
    return None if x is None else round(x, nd)


# ----------------------------------------------------------------------------
# Demo data (offline)
# ----------------------------------------------------------------------------
def demo_dates(n: int) -> list[str]:
    d = NOW.date()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)
    return out[::-1]


def demo_walk(dates: list[str], start: float, vol: float, drift: float = 0.0002, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    p = start
    rows = []
    for d in dates:
        p *= math.exp(rng.gauss(drift, vol))
        rows.append({"date": d, "open": p, "high": p * 1.01, "low": p * 0.99,
                     "close": p, "volume": abs(rng.gauss(1e6, 3e5))})
    return rows


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    end = NOW.strftime("%Y%m%d")
    start = (NOW - dt.timedelta(days=365 * CFG["history_years"] + 20)).strftime("%Y%m%d")
    status: dict[str, str] = {}

    # ---------------- 1. indices ----------------
    if DEMO:
        dts = demo_dates(1760)
        kospi = demo_walk(dts, 2600, 0.012, 0.0006, 1)
        kosdaq = demo_walk(dts, 850, 0.015, 0.0002, 2)
        kpi200 = demo_walk(dts, 350, 0.012, 0.0006, 3)
        status["index"] = "demo"
    else:
        kospi = index_history("KOSPI", start, end)
        kosdaq = index_history("KOSDAQ", start, end)
        try:
            kpi200 = naver_daily("KPI200", start, end)
        except Exception as e:  # noqa: BLE001
            log(f"KPI200 failed: {e}")
            kpi200 = []
        status["index"] = "ok"
    if len(kospi) < 300:
        raise SystemExit(f"KOSPI history too short: {len(kospi)}")
    dates = [r["date"] for r in kospi]
    di = {d: i for i, d in enumerate(dates)}
    n_days = len(dates)
    asof = dates[-1]
    log(f"index rows: KOSPI {len(kospi)}  KOSDAQ {len(kosdaq)}  asof {asof}")

    def align(rows: list[dict], key: str = "close") -> list:
        m = {r["date"]: r.get(key) for r in rows}
        return [m.get(d) for d in dates]

    kospi_c = align(kospi)
    kosdaq_c = align(kosdaq)

    # ---------------- 2. all listed stocks (A/D counts + universe) ----------------
    if DEMO:
        rng = random.Random(7)
        all_stocks = []
        for m, n_st in (("KOSPI", 900), ("KOSDAQ", 1700)):
            for k in range(n_st):
                all_stocks.append({"code": f"{(k + (0 if m == 'KOSPI' else 5000)) * 10:06d}",
                                   "name": f"{m}종목{k}", "price": 10000.0,
                                   "chg_pct": rng.gauss(0.2, 2.5), "mktcap": 1e6 / (k + 1),
                                   "volume": 1e5, "market": m})
        status["market_sum"] = "demo"
    else:
        try:
            all_stocks = naver_market_sum(0) + naver_market_sum(1)
        except Exception as e:  # noqa: BLE001
            log(f"market_sum html failed: {e}")
            all_stocks = []
        status["market_sum"] = f"html {len(all_stocks)}"
        if len(all_stocks) < 500:
            log("market list from HTML too short, trying Naver mobile API")
            try:
                api_rows = naver_market_sum_api("KOSPI") + naver_market_sum_api("KOSDAQ")
            except Exception as e:  # noqa: BLE001
                log(f"market api failed: {e}")
                api_rows = []
            if len(api_rows) > len(all_stocks):
                all_stocks = api_rows
            status["market_sum"] += f" / mobile api {len(api_rows)}"
    log(f"listed rows: {len(all_stocks)}")

    def ad_counts(market: str) -> dict:
        rows = [s for s in all_stocks if s["market"] == market and s["chg_pct"] is not None]
        adv = sum(1 for s in rows if s["chg_pct"] > 0)
        dec = sum(1 for s in rows if s["chg_pct"] < 0)
        unch = len(rows) - adv - dec
        return {"adv": adv, "dec": dec, "unch": unch, "total": len(rows)}

    breadth_all = {"KOSPI": ad_counts("KOSPI"), "KOSDAQ": ad_counts("KOSDAQ")}

    common = [s for s in all_stocks if is_common_stock(s["code"], s["name"])]
    n_mcap = sum(1 for s in common if s["mktcap"])
    if n_mcap < len(common) * 0.5:      # market-cap column not parsed -> Naver's page order is already by market cap
        log(f"mktcap parsed for {n_mcap}/{len(common)} rows; using page order")
        kospi_common = [s for s in common if s["market"] == "KOSPI"]
        kosdaq_common = [s for s in common if s["market"] == "KOSDAQ"]
    else:
        kospi_common = sorted([s for s in common if s["market"] == "KOSPI" and s["mktcap"]], key=lambda s: -s["mktcap"])
        kosdaq_common = sorted([s for s in common if s["market"] == "KOSDAQ" and s["mktcap"]], key=lambda s: -s["mktcap"])
    status["listed"] = f"{len(all_stocks)} rows, {len(common)} common stocks, mktcap parsed {n_mcap}"

    universe: list[dict] = []
    k200_exact = []
    if not DEMO:
        try:
            k200_exact = naver_kospi200_codes()
        except Exception as e:  # noqa: BLE001
            log(f"kospi200 list failed: {e}")
    if len(k200_exact) >= 150:
        by_code = {s["code"]: s for s in all_stocks}
        universe += [dict(by_code.get(c, {"code": c, "name": c, "price": None, "chg_pct": None,
                                          "mktcap": None, "volume": None, "market": "KOSPI"}), group="KOSPI")
                     for c in k200_exact]
        status["universe_kospi"] = f"kospi200 exact ({len(k200_exact)})"
    else:
        universe += [dict(s, group="KOSPI") for s in kospi_common[:CFG["universe_kospi"]]]
        status["universe_kospi"] = "top mktcap proxy"
    universe += [dict(s, group="KOSDAQ") for s in kosdaq_common[:CFG["universe_kosdaq"]]]
    status["universe_kosdaq"] = "top mktcap proxy"
    log(f"universe: {len(universe)}  ({status['universe_kospi']})")

    # ---------------- 3. universe price history ----------------
    closes: dict[str, list] = {}
    vols: dict[str, list] = {}
    fails = 0
    errors: list[str] = []
    naver_aborted = False
    for k, s in enumerate(universe):
        if budget_left() < 240:
            log("time budget low, stopping universe fetch")
            break
        if k >= 8 and not closes:
            log("first 8 universe fetches all failed, aborting Naver universe fetch")
            naver_aborted = True
            break
        try:
            if DEMO:
                rows = demo_walk(dates, 10000 + k * 37, 0.02 + (k % 7) * 0.003, 0.0004, 100 + k)
            else:
                rows = naver_daily(s["code"], start, end)
            if len(rows) < 60:
                fails += 1
                if len(errors) < 3:
                    errors.append(f"{s['code']} rows={len(rows)}")
                continue
            closes[s["code"]] = align(rows)
            vols[s["code"]] = align(rows, "volume")
        except Exception as e:  # noqa: BLE001
            fails += 1
            if len(errors) < 3:
                errors.append(f"{s['code']} {str(e)[:120]}")
            if fails <= 5:
                log(f"history failed {s['code']} {s['name']}: {e}")
    status["universe_history"] = f"naver: {len(closes)} ok / {fails} failed"
    if errors:
        status["universe_errors"] = " | ".join(errors)
    if not DEMO and (naver_aborted or len(closes) < len(universe) * 0.5):
        log("falling back to Yahoo Finance for the universe")
        y_rows = yahoo_universe(universe, start, end)
        added = 0
        for code, rows in y_rows.items():
            if code not in closes:
                closes[code] = align(rows)
                vols[code] = align(rows, "volume")
                added += 1
        status["universe_history"] += f" + yahoo: {added} added"
    log(f"universe history: {status['universe_history']}")

    # ---------------- 4. breadth series over the universe ----------------
    group_of = {s["code"]: s["group"] for s in universe}
    groups = ("ALL", "KOSPI", "KOSDAQ")
    agg = {g: {k: [0] * n_days for k in ("n", "adv", "dec", "n20", "a20", "n50", "a50",
                                          "n200", "a200", "nhl", "nh", "nl")} for g in groups}
    W52 = 252
    for code, c in closes.items():
        ma20, ma50, ma200 = rolling_mean(c, 20), rolling_mean(c, 50), rolling_mean(c, 200)
        rmax, rmin = rolling_extreme(c, W52, True), rolling_extreme(c, W52, False)
        gs = ("ALL", group_of.get(code, "KOSPI"))
        for i in range(1, n_days):
            v, p = c[i], c[i - 1]
            if v is None or p is None:
                continue
            for g in gs:
                a = agg[g]
                a["n"][i] += 1
                if v > p:
                    a["adv"][i] += 1
                elif v < p:
                    a["dec"][i] += 1
                for w, mm, nk, ak in ((20, ma20, "n20", "a20"), (50, ma50, "n50", "a50"),
                                      (200, ma200, "n200", "a200")):
                    if mm[i] is not None:
                        a[nk][i] += 1
                        if v > mm[i]:
                            a[ak][i] += 1
                if rmax[i] is not None:
                    a["nhl"][i] += 1
                    if v >= rmax[i]:
                        a["nh"][i] += 1
                    if v <= rmin[i]:
                        a["nl"][i] += 1

    def ratio(num: list, den: list) -> list:
        return [(100.0 * num[i] / den[i]) if den[i] else None for i in range(n_days)]

    # median 20-day return across the universe (used by the overheat gauge)
    ret20_by_day: list[list] = [[] for _ in range(n_days)]
    for code, c in closes.items():
        r20 = pct_change(c, 20)
        for i in range(n_days):
            if r20[i] is not None:
                ret20_by_day[i].append(r20[i])
    median_ret20 = [median(v) if len(v) >= 50 else None for v in ret20_by_day]

    # universe trading value (KRW) = sum(close x volume) over stocks with both fields
    uni_turnover = [0.0] * n_days
    uni_turnover_n = [0] * n_days
    for code, c in closes.items():
        v = vols.get(code)
        if not v:
            continue
        for i in range(n_days):
            if c[i] and v[i]:
                uni_turnover[i] += c[i] * v[i]
                uni_turnover_n[i] += 1

    breadth_series = {}
    for g in groups:
        a = agg[g]
        adr = [((a["adv"][i] - a["dec"][i]) / a["n"][i] * 100.0) if a["n"][i] else None for i in range(n_days)]
        hl = [((a["nh"][i] - a["nl"][i]) / a["nhl"][i] * 100.0) if a["nhl"][i] else None for i in range(n_days)]
        adl, run, started = [None] * n_days, 0, False
        for i in range(n_days):
            if a["n"][i]:
                started = True
                run += a["adv"][i] - a["dec"][i]
            if started:
                adl[i] = run
        breadth_series[g] = {
            "adr": adr, "hl": hl, "adl": adl,
            "p20": ratio(a["a20"], a["n20"]), "p50": ratio(a["a50"], a["n50"]),
            "p200": ratio(a["a200"], a["n200"]),
        }

    # ---------------- 5. other inputs ----------------
    hist_path = DATA_DIR / "history.json"
    history: list[dict] = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            history = []
    stored_vk = {h["date"]: h["vkospi"] for h in history
                 if isinstance(h, dict) and h.get("vkospi") and re.fullmatch(r"\d{8}", str(h.get("date", "")))}

    if DEMO:
        bond = demo_walk(dates, 100000, 0.002, 0.0001, 11)
        lev = [demo_walk(dates, 20000, 0.024, 0.001, 21), demo_walk(dates, 8000, 0.03, 0.0005, 22)]
        inv = [demo_walk(dates, 3000, 0.024, -0.001, 23), demo_walk(dates, 4000, 0.03, -0.0005, 24)]
        rng = random.Random(3)
        foreign = {d: rng.gauss(0, 4000) for d in dates}
        individual = {d: -foreign[d] + rng.gauss(0, 1500) for d in dates}
        turnover = {dates[i]: uni_turnover[i] for i in range(n_days) if uni_turnover_n[i] >= 100}
        turnover_src = "demo (universe trading value)"
        status["turnover"] = turnover_src
        margin = {}
        p = 2.0e7
        for d in dates:
            p *= math.exp(rng.gauss(0.0003, 0.006))
            margin[d] = p
        margin = {d: v * 1e6 for d, v in margin.items()}      # demo values -> KRW scale (백만원 기준)
        margin_desc = "demo"
        vk, lvl = {}, 22.0
        for d in dates:
            lvl = max(8.0, lvl + rng.gauss(0, 1.2) + (20 - lvl) * 0.03)
            vk[d] = lvl
        vk_src = "demo"
        status.update(bond="demo", etf="demo", foreign="demo", margin="demo")
    else:
        try:
            bond = naver_daily(CFG["bond_etf"], start, end)
            status["bond"] = "ok"
        except Exception as e:  # noqa: BLE001
            bond, status["bond"] = [], f"fail: {e}"
        lev, inv = [], []
        try:
            lev = [naver_daily(c, start, end) for c in CFG["lev_etfs"]]
            inv = [naver_daily(c, start, end) for c in CFG["inv_etfs"]]
            status["etf"] = "ok"
        except Exception as e:  # noqa: BLE001
            status["etf"] = f"fail: {e}"
        try:
            inv_flows = naver_investor_net(start)
            foreign = {d: v["foreign"] for d, v in inv_flows.items() if v.get("foreign") is not None}
            individual = {d: v["individual"] for d, v in inv_flows.items() if v.get("individual") is not None}
            status["foreign"] = f"ok ({len(foreign)} days, individual {len(individual)})"
        except Exception as e:  # noqa: BLE001
            foreign, individual, status["foreign"] = {}, {}, f"fail: {e}"
        try:
            turnover, turnover_src = naver_index_turnover("KOSPI", start)
            if len(turnover) < 250:
                raise RuntimeError(f"rows={len(turnover)}")
            med = median(list(turnover.values()))
            if med and med < 1e10:                       # values reported in 백만원 -> KRW
                turnover = {d: v * 1e6 for d, v in turnover.items()}
            turnover_src = "KOSPI 거래대금 (" + turnover_src + ")"
        except Exception as e:  # noqa: BLE001
            log(f"turnover api failed ({e}); using universe trading value")
            turnover = {dates[i]: uni_turnover[i] for i in range(n_days) if uni_turnover_n[i] >= 100}
            turnover_src = "유니버스 350종목 거래대금 합계 (계산값)"
        status["turnover"] = f"{turnover_src} ({len(turnover)} days)"
        try:
            margin, margin_desc = kofia_margin(start, end)
            status["margin"] = f"ok ({len(margin)} days, {margin_desc})"
        except Exception as e:  # noqa: BLE001
            margin, margin_desc, status["margin"] = {}, "", f"skip: {e}"
        vk, vk_src = fetch_vkospi(start, end)
    vk_all = dict(stored_vk)
    vk_all.update(vk)
    status["vkospi"] = f"{vk_src} ({len(vk)} fetched, {len(vk_all)} total incl. stored)"
    log(f"inputs: bond={status['bond']} etf={status['etf']} foreign={status['foreign']} "
        f"margin={status['margin']} vkospi={status['vkospi']}")

    # ---------------- 6. component raw series ----------------
    W = CFG["pct_window"]
    comps: list[dict] = []

    # 6-1 momentum: KOSPI vs 125-day average
    ma125 = rolling_mean(kospi_c, 125)
    mom = [((kospi_c[i] / ma125[i] - 1) * 100.0) if (kospi_c[i] and ma125[i]) else None for i in range(n_days)]
    comps.append(dict(key="momentum", name="시장 모멘텀", raw=mom, invert=False, unit="%",
                      desc="KOSPI 종가가 125일 이동평균에서 벗어난 정도. 평균을 크게 웃돌면 탐욕, 밑돌면 공포.",
                      source="네이버 금융 KOSPI 일별 지수"))

    # 6-2 price strength: 52w new highs minus new lows (5-day average)
    comps.append(dict(key="strength", name="주가 강도", raw=rolling_mean(breadth_series["ALL"]["hl"], 5),
                      invert=False, unit="%p",
                      desc="유니버스(KOSPI200·KOSDAQ150 대용 350종목) 중 52주 신고가 종목 비율에서 신저가 비율을 뺀 값의 5일 평균.",
                      source="네이버 금융 종목별 일별 시세"))

    # 6-3 breadth: advance-decline ratio (10-day average)
    comps.append(dict(key="breadth", name="시장 폭", raw=rolling_mean(breadth_series["ALL"]["adr"], 10),
                      invert=False, unit="%p",
                      desc="유니버스 상승 종목 비율에서 하락 종목 비율을 뺀 값의 10일 평균. 소수 대형주만 오르는 장은 낮게 나옵니다.",
                      source="네이버 금융 종목별 일별 시세"))

    # 6-4 volatility: VKOSPI when >= 200 daily values are available, else realized 20-day vol (inverted)
    vk_al = [vk_all.get(d) for d in dates]
    last_v = None
    for i in range(n_days):                     # forward-fill gaps of a few days
        if vk_al[i] is None and last_v is not None and i >= 1 and vk_al[i - 1] is not None:
            vk_al[i] = last_v
        elif vk_al[i] is not None:
            last_v = vk_al[i]
    vk_count = sum(1 for v in vk_al if v is not None)
    if vk_count >= 200:
        comps.append(dict(key="volatility", name="변동성 (VKOSPI)", raw=vk_al, invert=True, unit="pt",
                          desc="코스피200 변동성지수(VKOSPI). 옵션 가격에 반영된 향후 30일 기대 변동성으로, 1년 범위에서 높을수록 공포.",
                          source=f"네이버 금융 VKOSPI ({vk_src})"))
        volatility_mode = "vkospi"
    else:
        comps.append(dict(key="volatility", name="변동성 (실현변동성 대용)", raw=realized_vol(kospi_c, 20),
                          invert=True, unit="%",
                          desc="KOSPI 20일 실현변동성(연율). VKOSPI 일별 자료가 200일 이상 확보되면 자동으로 VKOSPI로 전환됩니다.",
                          source="네이버 금융 KOSPI 일별 지수 (계산값)"))
        volatility_mode = "realized"
    status["volatility_mode"] = f"{volatility_mode} (vkospi days aligned: {vk_count})"

    # 6-5 safe haven: KOSPI 20d return minus bond ETF 20d return
    if bond:
        bond_c = align(bond)
        k20, b20 = pct_change(kospi_c, 20), pct_change(bond_c, 20)
        sh = [(k20[i] - b20[i]) if (k20[i] is not None and b20[i] is not None) else None for i in range(n_days)]
        comps.append(dict(key="safehaven", name="안전자산 수요", raw=sh, invert=False, unit="%p",
                          desc="KOSPI 20일 수익률에서 국고채 3년 ETF(KODEX 국고채3년) 20일 수익률을 뺀 값. 채권이 주식을 이기면 공포.",
                          source="네이버 금융 지수·ETF 일별 시세"))

    # 6-6 foreign flow: 20-day cumulative net buying (KOSPI, 억원)
    if foreign:
        f_al = [foreign.get(d) for d in dates]
        f_al = [0.0 if (v is None and i >= len(dates) - 3 and any(x is not None for x in f_al[max(0, i - 5):i])) else v
                for i, v in enumerate(f_al)]
        f20 = [None] * n_days
        for i in range(19, n_days):
            win = f_al[i - 19:i + 1]
            vals = [x for x in win if x is not None]
            if len(vals) >= 15:
                f20[i] = sum(vals)
        comps.append(dict(key="foreign", name="외국인 수급", raw=f20, invert=False, unit="억원",
                          desc="KOSPI 외국인 순매수 20일 누적 금액. 1년 범위에서 순매수가 두터울수록 탐욕.",
                          source="네이버 금융 투자자별 매매동향"))

    # 6-7 leverage appetite: leverage ETF turnover share vs inverse (5-day)
    if lev and inv:
        def etf_turnover(rows_list):
            tot = [0.0] * n_days
            for rows in rows_list:
                c, v = align(rows), align(rows, "volume")
                for i in range(n_days):
                    if c[i] and v[i]:
                        tot[i] += c[i] * v[i]
            return tot
        lt, it = etf_turnover(lev), etf_turnover(inv)
        share = [None] * n_days
        for i in range(4, n_days):
            l5, i5 = sum(lt[i - 4:i + 1]), sum(it[i - 4:i + 1])
            if l5 + i5 > 0:
                share[i] = 100.0 * l5 / (l5 + i5)
        comps.append(dict(key="leverage", name="레버리지 선호", raw=share, invert=False, unit="%",
                          desc="레버리지 ETF 거래대금 ÷ (레버리지+인버스 ETF 거래대금), 5일 합산. 개인의 방향성 베팅 강도.",
                          source="네이버 금융 ETF 일별 시세 (KODEX 레버리지·코스닥150레버리지 vs 200선물인버스2X·코스닥150선물인버스)"))

    # 6-8 margin (experimental): 20-day change of margin loan balance
    if margin:
        m_al = [margin.get(d) for d in dates]
        # forward-fill short gaps (holiday/reporting lag)
        last = None
        for i in range(n_days):
            if m_al[i] is None and last is not None:
                m_al[i] = last
            elif m_al[i] is not None:
                last = m_al[i]
        comps.append(dict(key="margin", name="신용융자 (실험)", raw=pct_change(m_al, 20), invert=False, unit="%",
                          desc="신용거래융자 잔고의 20일 변화율. 빚투가 빠르게 늘면 탐욕. 금융투자협회 데이터가 열리지 않는 날은 자동 제외됩니다.",
                          source="금융투자협회 종합통계"))

    # ---------------- 7. scoring ----------------
    for c in comps:
        c["score"] = percentile_scores(c["raw"], W, invert=c["invert"])

    composite = [None] * n_days
    for i in range(n_days):
        sc = [c["score"][i] for c in comps if c["score"][i] is not None]
        if len(sc) >= 3:
            composite[i] = sum(sc) / len(sc)

    last_i = n_days - 1
    if composite[last_i] is None:
        raise SystemExit("composite could not be computed for the latest day")

    # ---------------- 7b. overheat gauge (top-finding) ----------------
    W3 = 750
    ret20 = pct_change(kospi_c, 20)
    ret60 = pct_change(kospi_c, 60)
    heat: list[dict] = []

    ma200 = rolling_mean(kospi_c, 200)
    dist200 = [((kospi_c[i] / ma200[i] - 1) * 100.0) if (kospi_c[i] and ma200[i]) else None for i in range(n_days)]
    heat.append(dict(key="h_trend", name="장기 추세 이격", raw=dist200, unit="%", window=W3,
                     desc="KOSPI가 200일 이동평균에서 위로 벗어난 정도(3년 백분위). 장기 평균에서 멀어질수록 과열.",
                     source="네이버 금융 KOSPI 일별 지수"))
    heat.append(dict(key="h_speed", name="상승 속도", raw=ret60, unit="%", window=W3,
                     desc="KOSPI 60일 수익률(3년 백분위). 석 달 안에 얼마나 빨리 올랐는지.",
                     source="네이버 금융 KOSPI 일별 지수"))

    vol_raw = vk_al if volatility_mode == "vkospi" else realized_vol(kospi_c, 20)
    vol_pct = percentile_scores(vol_raw, W, invert=False)
    up_vol = [None] * n_days
    for i in range(n_days):
        if vol_pct[i] is not None and ret60[i] is not None:
            factor = max(0.0, min(1.0, ret60[i] / 10.0))    # full weight once the 60-day gain reaches +10%
            up_vol[i] = vol_pct[i] * factor
    heat.append(dict(key="h_upvol", name="상승장 변동성", raw=vol_raw, unit="%", window=None, score_direct=up_vol,
                     desc="상승 추세(60일 수익률 +10% 이상)에서의 변동성 백분위. 오르면서 흔들림이 커지는 것은 과열, 하락장의 변동성은 0으로 처리.",
                     source="네이버 금융 KOSPI 일별 지수 (계산값)" + (" / VKOSPI" if volatility_mode == "vkospi" else "")))

    if individual:
        i_al = [individual.get(d) for d in dates]
        i20 = [None] * n_days
        for i in range(19, n_days):
            vals = [x for x in i_al[i - 19:i + 1] if x is not None]
            if len(vals) >= 15:
                i20[i] = sum(vals)
        heat.append(dict(key="h_retail", name="개인 순매수", raw=i20, unit="억원", window=W,
                         desc="KOSPI 개인 순매수 20일 누적(1년 백분위). 개인 매수 강도가 높을수록 과열.",
                         source="네이버 금융 투자자별 매매동향"))

    if margin:
        heat.append(dict(key="h_margin", name="신용융자 잔고 수준", raw=m_al, unit="원", window=W3,
                         desc="신용거래융자 잔고 수준(3년 백분위). 잔고가 역사적 고점에 머물면 계속 과열로 읽습니다.",
                         source="금융투자협회 종합통계"))

    lev_comp = next((c for c in comps if c["key"] == "leverage"), None)
    if lev_comp:
        heat.append(dict(key="h_leverage", name="레버리지 선호", raw=lev_comp["raw"], unit="%", window=W,
                         desc="레버리지 ETF 거래대금 비중(1년 백분위). 공포·탐욕 지수와 같은 자료.",
                         source=lev_comp["source"]))

    if any(v is not None for v in median_ret20):
        div = [None] * n_days
        for i in range(n_days):
            if ret20[i] is not None and median_ret20[i] is not None:
                gap = ret20[i] - median_ret20[i]
                div[i] = gap if ret20[i] > 0 else min(0.0, gap)
        heat.append(dict(key="h_gap", name="지수-종목 괴리", raw=div, unit="%p", window=W3,
                         desc="KOSPI 20일 수익률 − 유니버스 종목 중앙값 20일 수익률(3년 백분위). 소수 대형주만 지수를 끌어올리는 좁은 상승일수록 과열.",
                         source="네이버 금융 지수·종목별 일별 시세 (계산값)"))

    if turnover:
        t_al = [turnover.get(d) for d in dates]
        t20 = rolling_mean([v if v else None for v in t_al], 20)
        heat.append(dict(key="h_turnover", name="거래 강도", raw=t20, unit="원", window=W3,
                         desc="거래대금 20일 평균의 3년 백분위. 거래가 역사적으로 뜨거울수록 과열.",
                         source=turnover_src))

    for h in heat:
        h["score"] = h["score_direct"] if h.get("score_direct") is not None else percentile_scores(h["raw"], h["window"], invert=False)

    overheat = [None] * n_days
    for i in range(n_days):
        sc = [h["score"][i] for h in heat if h["score"][i] is not None]
        if len(sc) >= 4:
            overheat[i] = sum(sc) / len(sc)

    # ---------------- 7c. backtest on known tops and bottoms ----------------
    episodes = [
        ("고점", "2021.1 동학개미 정점", "20210101", "20210228"),
        ("고점", "2024.7 밸류업 랠리 고점", "20240601", "20240731"),
        ("고점", "2026.2 KOSPI 5000", "20260115", "20260305"),
        ("고점", "2026.6 KOSPI 9000", "20260515", "20260705"),
        ("저점", "2022.9~10 긴축 충격", "20220901", "20221031"),
        ("저점", "2024.8 블랙먼데이", "20240725", "20240815"),
        ("저점", "2025.4 관세 충격", "20250325", "20250430"),
        ("저점", "2026.3 이란 전쟁", "20260301", "20260415"),
        ("저점", "2026.7~8 급락", "20260710", "20260820"),
    ]
    backtest = []
    for kind, name, a, b in episodes:
        idxs = [i for i, d in enumerate(dates) if a <= d <= b and kospi_c[i]]
        if not idxs:
            continue
        j = max(idxs, key=lambda i: kospi_c[i]) if kind == "고점" else min(idxs, key=lambda i: kospi_c[i])
        fg, oh = composite[j], overheat[j]
        if kind == "고점":
            verdict = "포착" if (oh is not None and oh >= 70) else ("부분" if (oh is not None and oh >= 55) else "미포착")
        else:
            verdict = "포착" if (fg is not None and fg <= 25) else ("부분" if (fg is not None and fg <= 40) else "미포착")
        backtest.append({"kind": kind, "name": name, "date": dates[j], "kospi": rnd(kospi_c[j]),
                         "feargreed": rnd(fg, 1), "overheat": rnd(oh, 1), "verdict": verdict})

    def at(series: list, back: int):
        j = last_i - back
        return rnd(series[j]) if j >= 0 else None

    S = CFG["series_days"]
    sl = slice(max(0, n_days - S), n_days)
    sp = slice(max(0, n_days - CFG["spark_days"]), n_days)

    def cut(series: list, nd=2, s_=None):
        return [rnd(x, nd) for x in series[s_ or sl]]

    def fmt_value(c: dict):
        v = c["raw"][last_i]
        if v is None:
            return None
        if c["unit"] == "억원":
            return f"{v:+,.0f}억원"
        if c["unit"] == "원":                       # KRW amounts
            return f"{v / 1e12:,.1f}조원" if v >= 1e11 else f"{v / 1e8:,.0f}억원"
        if c["key"] in ("leverage", "volatility", "h_leverage", "h_upvol"):
            return f"{v:.1f}{'pt' if c['unit'] == 'pt' else '%'}"
        return f"{v:+.2f}{c['unit']}"

    comp_out = []
    for c in comps:
        comp_out.append({
            "key": c["key"], "name": c["name"], "unit": c["unit"], "invert": c["invert"],
            "value": rnd(c["raw"][last_i], 3), "value_fmt": fmt_value(c),
            "score": rnd(c["score"][last_i], 1), "label": label_for(c["score"][last_i]),
            "score_prev": at(c["score"], 1), "score_week": at(c["score"], 5), "score_month": at(c["score"], 21),
            "desc": c["desc"], "source": c["source"],
            "series_score": cut(c["score"], 1, sp), "series_raw": cut(c["raw"], 3, sp),
        })

    heat_out = []
    for h in heat:
        heat_out.append({
            "key": h["key"], "name": h["name"], "unit": h["unit"],
            "value": rnd(h["raw"][last_i], 3), "value_fmt": fmt_value(h),
            "score": rnd(h["score"][last_i], 1), "label": heat_label(h["score"][last_i]),
            "score_prev": at(h["score"], 1), "score_week": at(h["score"], 5), "score_month": at(h["score"], 21),
            "desc": h["desc"], "source": h["source"],
            "series_score": cut(h["score"], 1, sp),
        })

    def gsum(g: str) -> dict:
        a = agg[g]
        i = last_i
        return {
            "n": a["n"][i], "adv": a["adv"][i], "dec": a["dec"][i], "unch": a["n"][i] - a["adv"][i] - a["dec"][i],
            "p20": rnd(breadth_series[g]["p20"][i], 1), "p50": rnd(breadth_series[g]["p50"][i], 1),
            "p200": rnd(breadth_series[g]["p200"][i], 1),
            "nh": a["nh"][i], "nl": a["nl"][i], "adr10": rnd(rolling_mean(breadth_series[g]["adr"], 10)[i], 2),
        }

    latest = {
        "status": status,
        "log_tail": LOG_LINES[-12:],
        "asof": asof,
        "asof_fmt": f"{asof[:4]}.{asof[4:6]}.{asof[6:]}",
        "generated_at": NOW.strftime("%Y-%m-%d %H:%M KST"),
        "composite": {
            "score": rnd(composite[last_i], 1), "label": label_for(composite[last_i]),
            "prev": at(composite, 1), "week": at(composite, 5), "month": at(composite, 21), "year": at(composite, 250),
            "year3": at(composite, 750),
            "n_components": len([c for c in comps if c["score"][last_i] is not None]),
        },
        "index": {
            "kospi": rnd(kospi_c[last_i]), "kospi_chg": rnd(pct_change(kospi_c, 1)[last_i]),
            "kosdaq": rnd(kosdaq_c[last_i]), "kosdaq_chg": rnd(pct_change(kosdaq_c, 1)[last_i]),
        },
        "overheat": {
            "score": rnd(overheat[last_i], 1), "label": heat_label(overheat[last_i]),
            "prev": at(overheat, 1), "week": at(overheat, 5), "month": at(overheat, 21), "year": at(overheat, 250),
            "year3": at(overheat, 750),
            "n_components": len([h for h in heat if h["score"][last_i] is not None]),
        },
        "components": comp_out,
        "heat_components": heat_out,
        "backtest": backtest,
        "flows": {
            "foreign20": rnd(next((c for c in comps if c["key"] == "foreign"), {"raw": [None] * n_days})["raw"][last_i], 0),
            "individual20": rnd(next((h for h in heat if h["key"] == "h_retail"), {"raw": [None] * n_days})["raw"][last_i], 0),
        },
        "breadth_all": breadth_all,
        "universe": {"ALL": gsum("ALL"), "KOSPI": gsum("KOSPI"), "KOSDAQ": gsum("KOSDAQ"),
                     "size": len(closes), "kospi_basis": status.get("universe_kospi"),
                     "kosdaq_basis": status.get("universe_kosdaq")},
        "series": {
            "dates": dates[sl], "composite": cut(composite, 1), "kospi": cut(kospi_c), "kosdaq": cut(kosdaq_c),
            "p20": cut(breadth_series["ALL"]["p20"], 1), "p50": cut(breadth_series["ALL"]["p50"], 1),
            "p200": cut(breadth_series["ALL"]["p200"], 1),
            "kospi_p200": cut(breadth_series["KOSPI"]["p200"], 1), "kosdaq_p200": cut(breadth_series["KOSDAQ"]["p200"], 1),
            "hl": cut(breadth_series["ALL"]["hl"], 2), "adl": cut(breadth_series["ALL"]["adl"], 0),
            "nh": agg["ALL"]["nh"][sl], "nl": agg["ALL"]["nl"][sl],
            "vkospi": cut(vk_al, 2),
            "overheat": cut(overheat, 1),
            "composite_days": sum(1 for x in composite if x is not None),
        },
    }

    # ---------------- 8. history.json (persistent daily log) ----------------
    rec = {
        "date": asof, "composite": latest["composite"]["score"], "overheat": latest["overheat"]["score"],
        "scores": {c["key"]: c["score"] for c in comp_out},
        "kospi": latest["index"]["kospi"], "kosdaq": latest["index"]["kosdaq"],
        "breadth_all": breadth_all,
        "universe_p200": latest["universe"]["ALL"]["p200"],
        "vkospi": rnd(vk_all.get(asof), 2),
    }
    # keep newly fetched VKOSPI values for past dates too (so a current-only source accumulates)
    known = {h.get("date"): h for h in history if isinstance(h, dict)}
    for d, v in vk.items():
        if d != asof and d in known and not known[d].get("vkospi"):
            known[d]["vkospi"] = rnd(v, 2)
    history = [h for h in history if h.get("date") != asof] + [rec]
    history.sort(key=lambda h: h["date"])
    hist_path.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    latest["history_days"] = len(history)

    latest["log_tail"] = LOG_LINES[-15:]
    (DATA_DIR / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, separators=(",", ":")),
                                          encoding="utf-8")
    (DATA_DIR / "status.json").write_text(json.dumps({
        "asof": asof, "generated_at": latest["generated_at"], "composite": latest["composite"],
        "overheat": latest["overheat"],
        "components": {c["key"]: c["score"] for c in comp_out},
        "heat_components": {h["key"]: h["score"] for h in heat_out},
        "backtest": backtest, "breadth_all": breadth_all,
        "universe": {k: v for k, v in latest["universe"].items() if k in ("size", "kospi_basis", "kosdaq_basis")},
        "universe_all": latest["universe"]["ALL"], "status": status, "log": LOG_LINES[-40:],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"done: feargreed {latest['composite']['score']} ({latest['composite']['label']}) / "
        f"overheat {latest['overheat']['score']} ({latest['overheat']['label']}), "
        f"{latest['composite']['n_components']}+{latest['overheat']['n_components']} components, "
        f"{time.time() - START_TS:.0f}s")


if __name__ == "__main__":
    main()
