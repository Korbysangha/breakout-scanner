#!/usr/bin/env python3
"""
Read scan JSON (schema_version 2) and answer the tuning questions offline.

The point of the gate_matrix is that you never have to guess what a threshold
change would do, and never have to refetch a candle to find out.

    python3 analyse.py data/latest_scan.json
    python3 analyse.py data/*.json --mode momentum
"""
import json, sys, glob, collections

GATE_NAMES = {
    "G0": "insufficient history", "G1a": "coin liquid globally",
    "G1b": "venue usable", "G2": "uptrend", "G3": "breakout",
    "G4": "volume", "G5": "base range", "G6": "volatility contraction",
    "G7a": "not extended", "G7b": "RSI", "G8a": "T1 reachable", "G8b": "stop distance",
}

def load(paths):
    out = []
    for p in paths:
        for f in sorted(glob.glob(p)):
            d = json.load(open(f))
            if d.get("schema_version", 1) < 2:
                print(f"  skip {f}: schema v1, no gate matrix"); continue
            out.append((f, d))
    return out

def report(scans, mode):
    print(f"\n{'='*72}\nMODE: {mode}   ({len(scans)} scan days)\n{'='*72}")

    # 1. where things die (first failing gate)
    first = collections.Counter()
    # 2. how often each gate fails at all - the honest strictness measure,
    #    invisible in the old scanner because it stopped at the first failure
    anyfail = collections.Counter()
    total = 0
    one_gate = collections.Counter()
    for _, d in scans:
        for sym, modes in d.get("gate_matrix", {}).items():
            r = modes.get(mode)
            if not r: continue
            total += 1
            f = r["failed"]
            if f:
                first[f[0]] += 1
                for g in f: anyfail[g] += 1
                if len(f) == 1: one_gate[f[0]] += 1
            else:
                first["PASSED"] += 1

    print(f"\nWhere coins die first (n={total} coin-days)")
    for g, c in first.most_common():
        nm = "" if g == "PASSED" else GATE_NAMES.get(g, g)
        print(f"  {g:5} {nm:26} {c:5}  {c/total*100:5.1f}%")

    print(f"\nHow often each gate fails AT ALL (a gate can fail behind another)")
    for g, c in anyfail.most_common():
        nm = GATE_NAMES.get(g, g)
        print(f"  {g:5} {nm:26} {c:5}  {c/total*100:5.1f}%")

    print(f"\nSingle-gate blockers - remove this ONE gate and the coin passes")
    if not one_gate:
        print("  (none - everything failing is failing on 2+ gates)")
    for i, (g, c) in enumerate(one_gate.most_common()):
        nm = GATE_NAMES.get(g, g)
        tag = "  <- highest-value gate to argue with" if i == 0 else ""
        print(f"  {g:5} {nm:26} {c:5}{tag}")

def whatif(scans, mode, field, values, direction):
    """How many MORE coin-days clear a gate at a different threshold, holding
    every other gate fixed. This is the question the old JSON could not answer."""
    print(f"\nWhat-if: {field}  (all other gates held as they are)")
    rows = []
    for _, d in scans:
        for sym, modes in d.get("gate_matrix", {}).items():
            r = modes.get(mode)
            if not r or not r.get("m"): continue
            rows.append((sym, r["failed"], r["m"]))
    gate_of = {"vol_ratio": "G4", "base_range_pct": "G5",
               "extension_pct": "G7a", "rsi": "G7b", "risk_pct": "G8b"}
    g = gate_of[field]
    for thr in values:
        # coins whose ONLY remaining problem would be gone at this threshold
        newly = 0
        for sym, failed, m in rows:
            val = m.get(field)
            if val is None: continue
            passes_now = g not in failed
            passes_then = (val >= thr) if direction == "ge" else (val <= thr)
            if passes_then and not passes_now and set(failed) <= {g}:
                newly += 1
        print(f"  {field} {'>=' if direction=='ge' else '<='} {thr:<6} -> {newly} extra setups")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = "swing"
    if "--mode" in sys.argv: mode = sys.argv[sys.argv.index("--mode") + 1]
    scans = load(args or ["data/latest_scan.json"])
    if not scans:
        print("no v2 scans found - run the new scanner at least once"); sys.exit(1)
    report(scans, mode)
    whatif(scans, mode, "vol_ratio",      [1.0, 1.2, 1.5, 2.0], "ge")
    whatif(scans, mode, "base_range_pct", [25, 40, 50, 70],     "le")
    whatif(scans, mode, "extension_pct",  [10, 15, 20],         "le")
    whatif(scans, mode, "risk_pct",       [15, 20, 25],         "le")
    print("\nReminder: more setups is not better. Every loosening trades a missed")
    print("winner for an unknown number of false signals. Paper only until measured.")
