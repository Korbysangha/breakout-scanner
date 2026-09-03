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
    "min_global_dollar_vol": 50_000_000,
    "min_venue_dollar_vol":   1_000_000,
    "stablecoin_skip": {"USDT","USDC","DAI","FDUSD","USDE","PYUSD","TUSD","USDS","BUSD","USDD","FRAX","LUSD","GUSD","EURC","RLUSD"},
    "wrapped_skip": {"WBTC","WETH","WBETH","STETH","WSTETH","RETH","CBBTC","WEETH","METH","SOLVBTC","BSC-USD"},
    "swing":    {"donchian": 20, "vol_mult": 1.5, "base_min": 10, "base_max": 30, "max_base_range": 0.25, "max_extension": 0.10},
    "position": {"donchian": 50, "vol_mult": 1.3, "base_min": 30, "base_max": 90, "max_base_range": 0.40, "max_extension": 0.12},
    "rsi_max": 80,
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
    """Return a candidate dict if every gate passes, else a dict explaining the first failure."""
    p = CFG[mode]
    c = [r["c"] for r in rows]; h = [r["h"] for r in rows]
    l = [r["l"] for r in rows]; v = [r["v"] for r in rows]
    if len(c) < 210:
        return {"pass": False, "why": "insufficient history"}

    close = c[-1]
    sma50, sma200 = sma(c, 50), sma(c, 200)
    a14 = atr(h, l, c, 14); a50 = atr(h, l, c, 50)
    avg_vol20 = sma(v, 20); avg_vol50 = sma(v, 50)
    dollar_vol = sma([c[i]*v[i] for i in range(-30, 0)], 30)

    # G1 liquidity - global first, then venue depth
    if global_vol24h is not None and global_vol24h < CFG["min_global_dollar_vol"]:
        return {"pass": False, "why": f"coin illiquid globally (${global_vol24h/1e6:.0f}m/24h)"}
    if dollar_vol < CFG["min_venue_dollar_vol"]:
        return {"pass": False, "why": f"thin on venue (${dollar_vol/1e6:.2f}m/day)"}
    # G2 trend
    if not (close > sma50 and sma50 > sma200):
        return {"pass": False, "why": "not in uptrend (needs close>50SMA>200SMA)"}
    if mode == "position" and close < sma200:
        return {"pass": False, "why": "below 200SMA"}
    # G3 breakout: close above prior-N highest CLOSE (excludes today)
    level = max(c[-(p["donchian"]+1):-1])
    if close <= level:
        return {"pass": False, "why": f"no {p['donchian']}d close breakout"}
    # G4 volume
    ref_vol = avg_vol20 if mode == "swing" else avg_vol50
    vol_ratio = v[-1] / ref_vol if ref_vol else 0
    if vol_ratio < p["vol_mult"]:
        return {"pass": False, "why": f"volume only {vol_ratio:.2f}x avg"}
    # G5 base tightness before the breakout
    bh = max(h[-(p["base_max"]+1):-1]); bl = min(l[-(p["base_max"]+1):-1])
    base_range = (bh - bl) / bl if bl else 99
    if base_range > p["max_base_range"]:
        return {"pass": False, "why": f"base too wide ({base_range*100:.0f}%)"}
    # G6 volatility squeeze
    squeeze = (a14 / a50) if a50 else 99
    if squeeze > 1.25:
        return {"pass": False, "why": "no volatility contraction"}
    # G7 not chasing
    ext = pct(close, level)
    if ext > p["max_extension"]:
        return {"pass": False, "why": f"extended {ext*100:.0f}% past level"}
    r14 = rsi(c, 14)
    if r14 and r14 > CFG["rsi_max"]:
        return {"pass": False, "why": f"RSI {r14:.0f} overbought"}

    # ---- scoring 0-100
    ret30 = pct(c[-1], c[-31])
    rs = ret30 - btc_ret30
    s_vol   = min(vol_ratio / 3.0, 1.0) * 30
    s_base  = max(0.0, 1 - base_range / p["max_base_range"]) * 20
    s_rs    = min(max((rs + 0.10) / 0.40, 0.0), 1.0) * 25
    s_ext   = max(0.0, 1 - ext / p["max_extension"]) * 15
    s_trend = (sum(1 for i in range(-50, 0) if c[i] > (sma(c[:len(c)+i+1], 50) or 1e18)) / 50) * 10
    score = round(s_vol + s_base + s_rs + s_ext + s_trend, 1)

    # ---- trade plan arithmetic (levels, not advice)
    # Plan assumes entry on a pullback INTO the breakout level, not chasing the
    # close. All risk/reward is measured from that planned entry.
    entry_lo, entry_hi = level, level * 1.03
    entry = level * 1.015
    swing_low = min(l[-10:])
    stop = max(level - 1.5 * a14, swing_low * 0.99)
    if stop >= entry * 0.98: stop = entry - 1.5 * a14
    R = entry - stop                      # one unit of risk
    risk_pct = R / entry
    t1, t2 = entry + 2 * R, entry + 4 * R  # targets in R-multiples, so R:R is fixed at 2:1 / 4:1
    rr1 = 2.0

    # G8 the target must be reachable inside the holding window, and the
    # stop must be survivable. Price covers roughly 1 ATR/day, so a T1 more
    # than 6 ATR away is not a 2-10 day trade.
    atr_to_t1 = (t1 - entry) / a14 if a14 else 99
    if atr_to_t1 > 6:
        return {"pass": False, "why": f"T1 is {atr_to_t1:.1f} ATR away, unreachable in window"}
    if risk_pct > 0.15:
        return {"pass": False, "why": f"stop too far ({risk_pct*100:.0f}%)"}

    return {
        "pass": True, "symbol": sym, "mode": mode, "score": score,
        "close": round(close, 6), "breakout_level": round(level, 6),
        "entry_zone": [round(entry_lo, 6), round(entry_hi, 6)],
        "planned_entry": round(entry, 6),
        "stop": round(stop, 6), "risk_pct": round(risk_pct * 100, 2),
        "target1": round(t1, 6), "target2": round(t2, 6),
        "rr_to_t1": 2.0, "rr_to_t2": 4.0, "atr_to_t1": round(atr_to_t1, 1),
        "vol_ratio": round(vol_ratio, 2), "base_range_pct": round(base_range * 100, 1),
        "extension_pct": round(ext * 100, 2), "rsi": round(r14, 1) if r14 else None,
        "atr_pct": round(a14 / close * 100, 2), "rs_vs_btc_30d": round(rs * 100, 1),
        "venue_dollar_vol_m": round(dollar_vol / 1e6, 2),
        "global_vol_24h_m": round(global_vol24h / 1e6, 0) if global_vol24h else None,
        "score_parts": {"volume": round(s_vol,1), "base": round(s_base,1),
                        "rel_strength": round(s_rs,1), "not_extended": round(s_ext,1),
                        "trend": round(s_trend,1)},
    }

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
    for coin in universe:
        sym = coin["symbol"]
        rows, venue = get_candles(sym)
        if not rows:
            failed.append(sym); continue
        for mode in ("swing", "position"):
            r = evaluate(sym, rows, mode, btc_ret30, coin.get("vol24h"))
            if r.get("pass"):
                r.update({"name": coin["name"], "cmc_rank": coin["rank"],
                          "venue": venue, "chart": tv_link(sym, venue)})
                cands.append(r)
            else:
                rejected.setdefault(sym, {})[mode] = r["why"]

    cands.sort(key=lambda x: -x["score"])
    swing = [c for c in cands if c["mode"] == "swing"][:CFG["top_n"]]
    posn  = [c for c in cands if c["mode"] == "position"][:CFG["top_n"]]

    return {
        "generated_at": started,
        "market_regime": regime,
        "btc_30d_return_pct": round(btc_ret30 * 100, 1),
        "universe_scanned": len(universe),
        "no_price_data": failed,
        "counts": {"swing_setups": len([c for c in cands if c['mode']=='swing']),
                   "position_setups": len([c for c in cands if c['mode']=='position'])},
        "swing_top": swing,
        "position_top": posn,
        "all_candidates": cands,
        "rejection_reasons": rejected,
    }

# ---------------------------------------------------------------- self test
def selftest():
    """Synthetic data proves the gates fire correctly without any network."""
    import random
    random.seed(7)

    def build(kind):
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

    expect = {"clean": True, "novol": False, "nobreak": False, "chased": False}
    ok = True
    print("  case      expect  got     detail")
    for kind, want in expect.items():
        r = evaluate("TEST", build(kind), "swing", 0.0)
        got = r.get("pass", False)
        flag = "OK " if got == want else "FAIL"
        if got != want: ok = False
        detail = f"score {r['score']}, vol {r['vol_ratio']}x, R:R {r['rr_to_t1']}, risk {r['risk_pct']}%" \
                 if got else r["why"]
        print(f"  {kind:9} {str(want):6} {str(got):6} {flag}  {detail}")
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
