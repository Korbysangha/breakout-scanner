#!/usr/bin/env python3
"""
Crypto breakout scanner.
Universe: CoinMarketCap top 100 by market cap.
Price data: Coinbase Exchange public candles (fallback: Kraken public OHLC).
Output: data/latest_scan.json  -- a ranked shortlist for MANUAL review and execution.

This produces candidates, not trade instructions. Every candidate must pass
the human chart check before any money moves.
"""

import json, os, sys, time, math, datetime as dt
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import urllib.error

CMC_KEY = os.environ.get("CMC_API_KEY", "")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ---------------------------------------------------------------- config
CFG = {
    "universe_size": 100,
    # Liquidity is two questions, not one:
    #  - is the COIN liquid?  -> global 24h turnover from CMC
    #  - is the VENUE usable? -> 30d average turnover on the exchange we'd trade
    # Using only the venue figure wrongly rejects majors: DOGE trades billions
    # globally but only ~$16m/day on the Coinbase USD book.
    "min_global_dollar_vol": 25_000_000,
    "min_venue_dollar_vol":   1_000_000,
    "stablecoin_skip": {"USDT","USDC","DAI","FDUSD","USDE","PYUSD","TUSD","USDS","BUSD","USDD","FRAX","LUSD","GUSD","EURC","RLUSD"},
    "wrapped_skip": {"WBTC","WETH","WBETH","STETH","WSTETH","RETH","CBBTC","WEETH","METH","SOLVBTC","BSC-USD"},
    # ---- profiles -------------------------------------------------------
    # A "coiled spring" profiles (swing/position) are UNCHANGED. Do not touch
    #   them: the paper-trading record is measured against these exact numbers.
    # B "momentum" is NEW and UNTESTED. It exists because A produced zero
    #   signals in 7 consecutive days and a system with no throughput cannot
    #   generate the 30 trades needed to judge it. Paper only.
    #
    # trend_rule:
    #   "golden" = close>50SMA AND 50SMA>200SMA   (lags a turn by 1-3 months)
    #   "slope"  = close>50SMA AND close>200SMA AND 50SMA rising over 20d
    # vol_ref:
    #   "mean"   = 20/50d average volume. A coin's own surge inflates this, so
    #              strong trends defeat the filter ~2 weeks in (ZEC at 0.21x).
    #   "median" = robust to 2-3 huge days. A stricter bar than it looks.
    "swing": {
        "donchian": 20, "vol_mult": 1.5, "vol_ref": "mean", "vol_window": 20,
        "base_min": 10, "base_max": 30, "max_base_range": 0.25,
        "max_extension": 0.10, "rsi_max": 80, "max_squeeze": 1.25,
        "max_risk_pct": 0.15, "max_atr_to_t1": 6, "trend_rule": "golden",
        "hold": "2-10 days", "profile": "A",
    },
    "position": {
        "donchian": 50, "vol_mult": 1.3, "vol_ref": "mean", "vol_window": 50,
        "base_min": 30, "base_max": 90, "max_base_range": 0.40,
        "max_extension": 0.12, "rsi_max": 80, "max_squeeze": 1.25,
        "max_risk_pct": 0.15, "max_atr_to_t1": 6, "trend_rule": "golden",
        "hold": "2-8 weeks", "profile": "A",
    },
    "momentum": {
        "donchian": 20, "vol_mult": 1.2, "vol_ref": "median", "vol_window": 20,
        "base_min": 10, "base_max": 30, "max_base_range": 0.50,
        "max_extension": 0.15, "rsi_max": 85, "max_squeeze": None,
        "max_risk_pct": 0.20, "max_atr_to_t1": 8, "trend_rule": "slope",
        "hold": "2-15 days", "profile": "B",
    },
    "modes": ["swing", "position", "momentum"],
    "top_n": 5,
}

# ---------------------------------------------------------------- http
def get_json(url, headers=None, tries=3):
    for i in range(tries):
        try:
            req = Request(url, headers=headers or {"User-Agent": "breakout-scanner/1.0"})
            with urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                return {"__error__": str(e)}
            time.sleep(1.5 * (i + 1))
    return {"__error__": "unreachable"}

# ---------------------------------------------------------------- indicators
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None

def median(xs):
    if not xs: return None
    s = sorted(xs); m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m-1] + s[m]) / 2.0

def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i-1]
        gains += max(d, 0.0); losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    if al == 0: return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))

def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1: return None
    trs = []
    for i in range(-n, 0):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs) / n

def pct(a, b):
    return (a / b - 1.0) if b else 0.0

# ---------------------------------------------------------------- universe
def cmc_top100():
    if not CMC_KEY:
        return [], "CMC_API_KEY not set"
    url = ("https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?"
           + urlencode({"limit": CFG["universe_size"], "convert": "USD", "sort": "market_cap"}))
    d = get_json(url, {"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"})
    if "__error__" in d or "data" not in d:
        return [], d.get("__error__", "bad CMC response")
    out = []
    for c in d["data"]:
        s = c["symbol"].upper()
        if s in CFG["stablecoin_skip"] or s in CFG["wrapped_skip"]:
            continue
        q = c["quote"]["USD"]
        out.append({
            "symbol": s, "name": c["name"], "rank": c["cmc_rank"],
            "price": q["price"], "mcap": q.get("market_cap") or 0,
            "vol24h": q.get("volume_24h") or 0,
            "chg7d": q.get("percent_change_7d") or 0,
            "chg30d": q.get("percent_change_30d") or 0,
        })
    return out, None

# ---------------------------------------------------------------- candles
def coinbase_daily(symbol, days=400):
    """Returns list of dicts oldest-first, or None."""
    prod = f"{symbol}-USD"
    end = dt.datetime.now(dt.timezone.utc)
    chunks = []
    remaining = days
    while remaining > 0:
        span = min(remaining, 295)
        start = end - dt.timedelta(days=span)
        url = (f"https://api.exchange.coinbase.com/products/{prod}/candles?"
               + urlencode({"granularity": 86400,
                            "start": start.replace(microsecond=0).isoformat(),
                            "end": end.replace(microsecond=0).isoformat()}))
        d = get_json(url)
        if isinstance(d, dict) or not d:
            break
        chunks.extend(d)
        remaining -= span
        end = start
        time.sleep(0.35)
    if not chunks:
        return None
    seen, rows = set(), []
    for c in chunks:                      # [time, low, high, open, close, volume]
        if c[0] in seen: continue
        seen.add(c[0])
        rows.append({"t": c[0], "l": c[1], "h": c[2], "o": c[3], "c": c[4], "v": c[5]})
    rows.sort(key=lambda r: r["t"])
    return rows if len(rows) >= 120 else None

KRAKEN_ALIAS = {"BTC": "XBT"}
def kraken_daily(symbol):
    k = KRAKEN_ALIAS.get(symbol, symbol)
    d = get_json(f"https://api.kraken.com/0/public/OHLC?pair={k}USD&interval=1440")
    if "__error__" in d or d.get("error"): return None
    res = d.get("result", {})
    key = next((x for x in res if x != "last"), None)
    if not key: return None
    rows = [{"t": int(c[0]), "l": float(c[3]), "h": float(c[2]),
             "o": float(c[1]), "c": float(c[4]), "v": float(c[6])} for c in res[key]]
    return rows if len(rows) >= 120 else None

def get_candles(symbol):
    r = coinbase_daily(symbol)
    if r: return r, "COINBASE"
    r = kraken_daily(symbol)
    if r: return r, "KRAKEN"
    return None, None

# ---------------------------------------------------------------- the ruleset
def evaluate(sym, rows, mode, btc_ret30, global_vol24h=None):
    """Evaluate EVERY gate and return the full matrix.

    This deliberately does NOT short-circuit on the first failure. The old
    version returned as soon as one gate failed, which made the output
    unusable for measurement: gates 5-8 never fired once in 579 coin-days,
    so their real strictness was completely unknown. You cannot tune what
    you cannot see.

    Always returns a dict containing:
      pass        - bool, every gate ok
      why         - first failing gate's reason (backwards compatible)
      gates       - [{id, name, ok, detail}, ...] every gate, always
      failed      - ["G2", "G4"] ids that failed
      m           - the raw measurements, so thresholds can be re-tested
                    offline from the JSON without refetching any candles
    """
    p = CFG[mode]
    c = [r["c"] for r in rows]; h = [r["h"] for r in rows]
    l = [r["l"] for r in rows]; v = [r["v"] for r in rows]
    if len(c) < 210:
        return {"pass": False, "why": "insufficient history", "mode": mode,
                "symbol": sym, "gates": [], "failed": ["G0"], "m": {}}

    # ---- measurements (all computed unconditionally) --------------------
    close = c[-1]
    sma50, sma200 = sma(c, 50), sma(c, 200)
    sma50_prev = sma(c[:-20], 50)                 # 50SMA as it stood 20d ago
    a14 = atr(h, l, c, 14); a50 = atr(h, l, c, 50)
    dollar_vol = sma([c[i]*v[i] for i in range(-30, 0)], 30)

    w = p["vol_window"]
    ref_vol = median(v[-w:]) if p["vol_ref"] == "median" else sma(v, w)
    vol_ratio = (v[-1] / ref_vol) if ref_vol else 0.0

    level = max(c[-(p["donchian"]+1):-1])          # prior N highest CLOSE
    bh = max(h[-(p["base_max"]+1):-1]); bl = min(l[-(p["base_max"]+1):-1])
    base_range = (bh - bl) / bl if bl else 99.0
    squeeze = (a14 / a50) if a50 else 99.0
    ext = pct(close, level)
    r14 = rsi(c, 14)
    ret30 = pct(c[-1], c[-31]); rs = ret30 - btc_ret30

    # trade plan arithmetic - computed even when the breakout gate fails, so
    # that G8 is measurable rather than invisible
    entry_lo, entry_hi = level, level * 1.03
    entry = level * 1.015
    swing_low = min(l[-10:])
    stop = max(level - 1.5 * a14, swing_low * 0.99)
    if stop >= entry * 0.98: stop = entry - 1.5 * a14
    R = entry - stop
    risk_pct = (R / entry) if entry else 99.0
    t1, t2 = entry + 2 * R, entry + 4 * R
    atr_to_t1 = ((t1 - entry) / a14) if a14 else 99.0

    # ---- gates: every one recorded, nothing short-circuits --------------
    G = []
    def gate(gid, name, ok, detail):
        G.append({"id": gid, "name": name, "ok": bool(ok), "detail": detail})

    gate("G1a", "coin liquid globally",
         global_vol24h is None or global_vol24h >= CFG["min_global_dollar_vol"],
         f"${(global_vol24h or 0)/1e6:.0f}m/24h vs ${CFG['min_global_dollar_vol']/1e6:.0f}m floor")
    gate("G1b", "venue usable",
         dollar_vol >= CFG["min_venue_dollar_vol"],
         f"${dollar_vol/1e6:.2f}m/day vs ${CFG['min_venue_dollar_vol']/1e6:.2f}m floor")

    if p["trend_rule"] == "slope":
        trend_ok = (close > sma50 and close > sma200
                    and sma50_prev is not None and sma50 > sma50_prev)
        trend_txt = (f"close/50SMA {close/sma50:.2f}, close/200SMA {close/sma200:.2f}, "
                     f"50SMA 20d slope {(sma50/sma50_prev-1)*100:+.1f}%"
                     if sma50_prev else "no slope history")
    else:
        trend_ok = (close > sma50 and sma50 > sma200)
        trend_txt = f"close/50SMA {close/sma50:.2f}, 50SMA/200SMA {sma50/sma200:.2f}"
    gate("G2", f"uptrend ({p['trend_rule']})", trend_ok, trend_txt)

    gate("G3", f"{p['donchian']}d close breakout", close > level,
         f"close {close:.6g} vs level {level:.6g} ({pct(close, level)*100:+.1f}%)")
    gate("G4", f"volume >= {p['vol_mult']}x {p['vol_window']}d {p['vol_ref']}",
         vol_ratio >= p["vol_mult"], f"{vol_ratio:.2f}x")
    gate("G5", f"base range < {p['max_base_range']*100:.0f}%",
         base_range <= p["max_base_range"], f"{base_range*100:.0f}%")
    gate("G6", "volatility contraction",
         p["max_squeeze"] is None or squeeze <= p["max_squeeze"],
         "disabled for this profile" if p["max_squeeze"] is None else f"ATR14/ATR50 {squeeze:.2f}")
    gate("G7a", f"not extended > {p['max_extension']*100:.0f}%",
         ext <= p["max_extension"], f"{ext*100:.1f}% past level")
    gate("G7b", f"RSI <= {p['rsi_max']}", (r14 is None or r14 <= p["rsi_max"]),
         f"RSI {r14:.0f}" if r14 else "no RSI")
    gate("G8a", f"T1 within {p['max_atr_to_t1']} ATR", atr_to_t1 <= p["max_atr_to_t1"],
         f"{atr_to_t1:.1f} ATR away")
    gate("G8b", f"stop within {p['max_risk_pct']*100:.0f}%",
         risk_pct <= p["max_risk_pct"], f"{risk_pct*100:.1f}%")

    failed = [g["id"] for g in G if not g["ok"]]
    first = next((g for g in G if not g["ok"]), None)
    ok = not failed

    # ---- scoring 0-100 ---------------------------------------------------
    s_vol   = min(vol_ratio / 3.0, 1.0) * 30
    s_base  = max(0.0, 1 - base_range / p["max_base_range"]) * 20
    s_rs    = min(max((rs + 0.10) / 0.40, 0.0), 1.0) * 25
    s_ext   = max(0.0, 1 - ext / p["max_extension"]) * 15
    s_trend = (sum(1 for i in range(-50, 0) if c[i] > (sma(c[:len(c)+i+1], 50) or 1e18)) / 50) * 10
    score = round(s_vol + s_base + s_rs + s_ext + s_trend, 1)

    m = {
        "close": round(close, 6), "level": round(level, 6),
        "vol_ratio": round(vol_ratio, 2), "base_range_pct": round(base_range * 100, 1),
        "squeeze": round(squeeze, 2), "extension_pct": round(ext * 100, 2),
        "rsi": round(r14, 1) if r14 else None, "risk_pct": round(risk_pct * 100, 2),
        "atr_to_t1": round(atr_to_t1, 1), "rs_vs_btc_30d": round(rs * 100, 1),
        "ret30_pct": round(ret30 * 100, 1),
        "close_over_50sma": round(close / sma50, 3) if sma50 else None,
        "sma50_over_200": round(sma50 / sma200, 3) if sma200 else None,
        "sma50_slope_20d_pct": round((sma50/sma50_prev - 1) * 100, 2) if sma50_prev else None,
        "venue_dollar_vol_m": round(dollar_vol / 1e6, 2),
        "global_vol_24h_m": round(global_vol24h / 1e6, 0) if global_vol24h else None,
    }

    out = {
        "pass": ok, "symbol": sym, "mode": mode, "profile": p["profile"],
        "score": score, "gates": G, "failed": failed, "n_failed": len(failed),
        "why": None if ok else f"{first['id']} {first['name']}: {first['detail']}",
        "m": m,
    }
    if ok:
        out.update({
            "close": m["close"], "breakout_level": m["level"],
            "entry_zone": [round(entry_lo, 6), round(entry_hi, 6)],
            "planned_entry": round(entry, 6), "stop": round(stop, 6),
            "risk_pct": m["risk_pct"], "target1": round(t1, 6), "target2": round(t2, 6),
            "rr_to_t1": 2.0, "rr_to_t2": 4.0, "atr_to_t1": m["atr_to_t1"],
            "vol_ratio": m["vol_ratio"], "base_range_pct": m["base_range_pct"],
            "extension_pct": m["extension_pct"], "rsi": m["rsi"],
            "atr_pct": round(a14 / close * 100, 2), "rs_vs_btc_30d": m["rs_vs_btc_30d"],
            "venue_dollar_vol_m": m["venue_dollar_vol_m"],
            "global_vol_24h_m": m["global_vol_24h_m"], "hold": p["hold"],
            "score_parts": {"volume": round(s_vol,1), "base": round(s_base,1),
                            "rel_strength": round(s_rs,1), "not_extended": round(s_ext,1),
                            "trend": round(s_trend,1)},
        })
    return out

def tv_link(sym, venue):
    return f"https://www.tradingview.com/chart/?symbol={venue}:{sym}USD"

# ---------------------------------------------------------------- main
def run():
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    universe, err = cmc_top100()
    if err:
        return {"error": err, "generated_at": started}

    # market regime from BTC
    btc_rows, _ = get_candles("BTC")
    regime, btc_ret30 = "unknown", 0.0
    if btc_rows:
        bc = [r["c"] for r in btc_rows]
        btc_ret30 = pct(bc[-1], bc[-31])
        b50, b200 = sma(bc, 50), sma(bc, 200)
        if bc[-1] > b50 and b50 > b200:   regime = "risk-on"
        elif bc[-1] > b50:                regime = "mixed"
        else:                             regime = "risk-off"

    cands, rejected, failed = [], {}, []
    matrix, census, near = {}, {}, []

    for coin in universe:
        sym = coin["symbol"]
        rows, venue = get_candles(sym)
        if not rows:
            failed.append(sym); continue
        for mode in CFG["modes"]:
            r = evaluate(sym, rows, mode, btc_ret30, coin.get("vol24h"))

            # full gate matrix - this is the whole point of the rewrite.
            # Every gate result for every coin, plus the raw measurements,
            # so thresholds can be re-tested from this file alone.
            matrix.setdefault(sym, {})[mode] = {
                "failed": r["failed"], "n_failed": r.get("n_failed", 1),
                "score": r.get("score"), "m": r.get("m", {}),
            }

            # census: count FIRST failing gate, per mode -> shows the bottleneck
            cen = census.setdefault(mode, {})
            key = r["failed"][0] if r["failed"] else "PASSED"
            cen[key] = cen.get(key, 0) + 1

            # near misses: failed exactly one gate. The most actionable list
            # in the file - it says what is closest and which gate to argue with.
            if r.get("n_failed") == 1:
                g = next(g for g in r["gates"] if not g["ok"])
                near.append({"symbol": sym, "mode": mode, "gate": g["id"],
                             "name": g["name"], "detail": g["detail"],
                             "score": r.get("score"), "chart": tv_link(sym, venue)})

            if r.get("pass"):
                r.update({"name": coin["name"], "cmc_rank": coin["rank"],
                          "venue": venue, "chart": tv_link(sym, venue)})
                r.pop("gates", None)          # keep the shortlist compact
                cands.append(r)
            else:
                rejected.setdefault(sym, {})[mode] = r["why"]

    cands.sort(key=lambda x: -x["score"])
    pick = lambda mo: [c for c in cands if c["mode"] == mo][:CFG["top_n"]]
    swing, posn, momo = pick("swing"), pick("position"), pick("momentum")
    near.sort(key=lambda x: -(x["score"] or 0))

    n_by = lambda mo: len([c for c in cands if c["mode"] == mo])
    return {
        "generated_at": started,
        "schema_version": 2,
        "market_regime": regime,
        "btc_30d_return_pct": round(btc_ret30 * 100, 1),
        "universe_scanned": len(universe),
        "no_price_data": failed,
        "counts": {"swing_setups": n_by("swing"),
                   "position_setups": n_by("position"),
                   "momentum_setups": n_by("momentum")},
        "swing_top": swing,
        "position_top": posn,
        # Profile B. UNTESTED and PAPER ONLY until it has its own 30-day record.
        "momentum_top": momo,
        "all_candidates": cands,
        "rejection_reasons": rejected,
        "near_misses": near[:25],
        "gate_census": census,
        "gate_matrix": matrix,
    }

# ---------------------------------------------------------------- self test
def selftest():
    """Synthetic data proves the gates fire correctly without any network.

    Three jobs:
      1. Profile A (swing) must behave EXACTLY as before the rewrite. If this
         regresses, the paper-trading record is invalidated.
      2. The gate matrix must be complete - every gate reported on every coin,
         pass or fail. That is what makes tuning measurable.
      3. Profile B must catch the recovery case that the golden cross misses
         (the ARB problem: +112% in a month, still 50SMA<200SMA).
    """
    import random
    ok = True

    def build(kind):
        random.seed(7)
        rows, price = [], 100.0
        for i in range(260):                       # long uptrend
            price *= 1.0 + random.uniform(-0.012, 0.019)
            rows.append([price*0.985, price*1.015, price, price, 1000 + random.uniform(-90, 90)])
        base = rows[-1][3]
        for i in range(25):                        # tight base, quiet volume
            p = base * (1 + random.uniform(-0.035, 0.035))
            rows.append([p*0.994, p*1.006, p, p, 900 + random.uniform(-60, 60)])
        top = max(r[3] for r in rows[-25:])
        if kind == "clean":     last = [top*1.02, top*1.05, top*1.02, top*1.04, 2600]
        elif kind == "novol":   last = [top*1.02, top*1.05, top*1.02, top*1.04, 950]
        elif kind == "nobreak": last = [top*0.96, top*0.99, top*0.97, top*0.98, 2600]
        elif kind == "chased":  last = [top*1.20, top*1.30, top*1.22, top*1.28, 3000]
        rows.append(last)
        return [{"t": i, "l": r[0], "h": r[1], "o": r[2], "c": r[3], "v": r[4] * 1e4}
                for i, r in enumerate(rows)]

    def build_recovery():
        """Long decline, then a rally, then a tight consolidation, then a
        breakout. Price reclaims BOTH averages while the 50SMA is still under
        the 200SMA - the shape the golden cross cannot see for months. This is
        the ARB case: +112% in a month and still 'not in uptrend'."""
        random.seed(11)
        rows, price = [], 400.0
        for i in range(200):                       # grinding decline
            price *= 1.0 - random.uniform(0.0, 0.014)
            rows.append([price*0.985, price*1.015, price, price, 1000+random.uniform(-90, 90)])
        for i in range(20):                        # sharp recovery
            price *= 1.0 + random.uniform(0.002, 0.045)
            rows.append([price*0.98, price*1.02, price, price, 1500+random.uniform(-200, 400)])
        base = price
        for i in range(32):                        # consolidation near the highs
            p = base * (1 + random.uniform(-0.06, 0.06))
            rows.append([p*0.99, p*1.01, p, p, 1100+random.uniform(-150, 150)])
        top = max(r[3] for r in rows[-32:])
        rows.append([top*1.005, top*1.05, top*1.01, top*1.04, 3000])
        return [{"t": i, "l": r[0], "h": r[1], "o": r[2], "c": r[3], "v": r[4] * 1e4}
                for i, r in enumerate(rows)]

    # ---- 1. Profile A regression -------------------------------------------
    print("1. Profile A (swing) - must be unchanged")
    print("   case      expect  got     detail")
    for kind, want in {"clean": True, "novol": False, "nobreak": False, "chased": False}.items():
        r = evaluate("TEST", build(kind), "swing", 0.0)
        got = r.get("pass", False)
        if got != want: ok = False
        detail = (f"score {r['score']}, vol {r['vol_ratio']}x, risk {r['risk_pct']}%"
                  if got else r["why"])
        print(f"   {kind:9} {str(want):6} {str(got):6} {'OK ' if got==want else 'FAIL'}  {detail}")
    # exact arithmetic lock: these are the pre-rewrite numbers. If any of them
    # move, Profile A has changed and the paper record is no longer comparable.
    ref = evaluate("TEST", build("clean"), "swing", 0.0)
    locks = {"score": 71.3, "vol_ratio": 2.64, "risk_pct": 6.29}
    bad = {k: (ref[k], want) for k, want in locks.items() if round(ref[k], 2) != want}
    if bad: ok = False
    print(f"   arithmetic lock {locks}  {'OK' if not bad else 'FAIL ' + str(bad)}")

    # ---- 2. gate matrix completeness ---------------------------------------
    print("\n2. Gate matrix completeness - every gate reported, no short-circuit")
    r = evaluate("TEST", build("nobreak"), "swing", 0.0)
    want_ids = ["G1a","G1b","G2","G3","G4","G5","G6","G7a","G7b","G8a","G8b"]
    got_ids = [g["id"] for g in r["gates"]]
    complete = got_ids == want_ids
    if not complete: ok = False
    print(f"   {len(got_ids)}/{len(want_ids)} gates reported on a FAILING coin  "
          f"{'OK' if complete else 'FAIL ' + str(got_ids)}")
    print(f"   failed: {r['failed']}   (old scanner would have shown only the first)")

    # ---- 3. Profile B catches the recovery ---------------------------------
    print("\n3. The ARB case - recovery off a deep base")
    rec = build_recovery()
    a = evaluate("RECOV", rec, "swing", 0.0)
    b = evaluate("RECOV", rec, "momentum", 0.0)
    m = b["m"]
    c200 = m["close_over_50sma"] * m["sma50_over_200"]
    print(f"   close/200SMA {c200:.3f} (above it)   50SMA/200SMA {m['sma50_over_200']:.3f} "
          f"(cross has NOT happened)   50SMA slope {m['sma50_slope_20d_pct']:+.1f}%")
    print(f"   swing    (golden cross): {'PASS' if a['pass'] else 'REJECT'}  {a['why'] or ''}")
    print(f"   momentum (50SMA slope) : {'PASS' if b['pass'] else 'REJECT'}  {b['why'] or ''}")
    cross_blocks_a = "G2" in a["failed"]
    slope_frees_b  = "G2" not in b["failed"]
    if not (cross_blocks_a and slope_frees_b): ok = False
    print(f"   golden cross blocks A: {cross_blocks_a}   slope rule clears B: {slope_frees_b}  "
          f"{'OK' if cross_blocks_a and slope_frees_b else 'FAIL'}")

    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    os.makedirs(OUT_DIR, exist_ok=True)
    result = run()
    with open(os.path.join(OUT_DIR, "latest_scan.json"), "w") as f:
        json.dump(result, f, indent=2)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(OUT_DIR, f"scan_{stamp}.json"), "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps({k: result[k] for k in ("generated_at","market_regime","counts") if k in result}, indent=2))
