"""Part 2: test (a) the INTRO blowup mechanism specifically, and (b) strategist's
proposed fix (absolute vocals_rms floor ANDed with the ratio) at several floor values.

Caches envelopes so the 3.8 GB of WAV is read once.
"""

import glob
import json
import os
import wave

import numpy as np

HOP_S, ON, OFF, MERGE, MINLEN = 0.5, 0.10, 0.05, 1.5, 1.0
STEMS = ("vocals", "drums", "bass", "other")
SCRATCH = "/private/tmp/claude-502/-Users-dev-Music-music-dj-tools/31adbe50-f905-4cda-8e46-3083bd0f8ced/scratchpad"
ENV_CACHE = f"{SCRATCH}/envelopes.json"


def read_mono(path):
    with wave.open(path, "rb") as w:
        sr, nch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dt = {2: np.int16, 4: np.int32}[sw]
    a = np.frombuffer(raw, dtype=dt).astype(np.float32) / float(np.iinfo(dt).max)
    return a.reshape(-1, nch).mean(axis=1), sr


def rms_env(mono, sr):
    hop = int(sr * HOP_S)
    n = len(mono) // hop
    return np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1))


def build_envelopes():
    out = {}
    for d in sorted(glob.glob("data/state/stems/*")):
        sid = os.path.basename(d)
        if not os.path.isfile(f"data/state/vocal-cache/{sid}.json"):
            continue
        monos = {}
        for s in STEMS:
            monos[s], sr = read_mono(f"{d}/{s}.wav")
        n = min(len(m) for m in monos.values())
        v = rms_env(monos["vocals"][:n], sr)
        m = rms_env(sum(monos[s][:n] for s in STEMS), sr)
        k = min(len(v), len(m))
        out[sid] = {"v": v[:k].tolist(), "m": m[:k].tolist()}
        print(f"  read {sid[:8]} ({k} frames)")
    json.dump(out, open(ENV_CACHE, "w"))
    return out


def regions(ratio):
    spans, on, start = [], False, 0
    for i, val in enumerate(ratio):
        if not on and val >= ON:
            on, start = True, i
        elif on and val < OFF:
            on = False
            spans.append((start * HOP_S, i * HOP_S))
    if on:
        spans.append((start * HOP_S, len(ratio) * HOP_S))
    merged = []
    for s, e in spans:
        if merged and s - merged[-1][1] < MERGE:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= MINLEN]


def cov(spans, dur):
    return 100.0 * sum(e - s for s, e in spans) / dur


envs = json.load(open(ENV_CACHE)) if os.path.exists(ENV_CACHE) else build_envelopes()

print("\n=== TEST A: is the INTRO quiet on tracks whose first region starts at 0.00s? ===")
print(f"{'sid':9} {'first@0':>8} {'intro5s_dBFS':>13} {'track_med_dBFS':>15} {'intro-med':>10} {'ratio@0-5s':>11}")
print("-" * 74)
intro_rows = []
for sid, e in sorted(envs.items()):
    v, m = np.array(e["v"]), np.array(e["m"])
    r = np.where(m > 1e-6, v / np.maximum(m, 1e-12), 0.0)
    sp = regions(r)
    first0 = bool(sp and sp[0][0] == 0.0)
    n5 = int(5 / HOP_S)
    intro_db = 20 * np.log10(max(float(m[:n5].mean()), 1e-9))
    med_db = 20 * np.log10(max(float(np.median(m)), 1e-9))
    intro_rows.append((sid, first0, intro_db, med_db, float(r[:n5].mean())))
    print(f"{sid[:8]:9} {str(first0):>8} {intro_db:>13.1f} {med_db:>15.1f} {intro_db-med_db:>10.1f} {float(r[:n5].mean()):>11.3f}")

grp1 = [x for x in intro_rows if x[1]]
grp0 = [x for x in intro_rows if not x[1]]
print(f"\nfirst-region-at-0.00s: n={len(grp1)}   otherwise: n={len(grp0)}")
if grp1 and grp0:
    print(f"  mean (intro - track median) dB, first@0 group : {np.mean([x[2]-x[3] for x in grp1]):+.1f} dB")
    print(f"  mean (intro - track median) dB, other group   : {np.mean([x[2]-x[3] for x in grp0]):+.1f} dB")
print("  MECHANISM PREDICTS: first@0 group should have a MUCH QUIETER intro (large negative).")

print("\n=== TEST B: strategist's proposed fix -- absolute vocals_rms floor AND ratio ===")
floors_db = [-60, -55, -50, -45, -40, -35]
print(f"{'sid':9} {'nofloor':>8} " + " ".join(f"{f:>7}" for f in floors_db))
print("-" * 68)
tbl = {}
for sid, e in sorted(envs.items()):
    v, m = np.array(e["v"]), np.array(e["m"])
    r = np.where(m > 1e-6, v / np.maximum(m, 1e-12), 0.0)
    dur = len(r) * HOP_S
    base = cov(regions(r), dur)
    row = []
    for fdb in floors_db:
        thr = 10 ** (fdb / 20.0)
        row.append(cov(regions(np.where(v >= thr, r, 0.0)), dur))
    tbl[sid] = (base, row)
    print(f"{sid[:8]:9} {base:>8.1f} " + " ".join(f"{c:>7.1f}" for c in row))

base_all = np.array([tbl[s][0] for s in tbl])
print(f"\nmedian coverage, no floor: {np.median(base_all):.1f}%")
for i, fdb in enumerate(floors_db):
    c = np.array([tbl[s][1][i] for s in tbl])
    print(f"  floor {fdb:>4} dBFS -> median {np.median(c):>5.1f}%   mean drop {np.mean(base_all-c):>5.1f} pts   tracks changed >5pts: {(base_all-c>5).sum()}/{len(c)}")

print("\n=== TEST C: degenerate ceiling -- what does a passthrough 'separator' score? ===")
print("If vocals == mix, ratio == 1.0 everywhere -> coverage 100% on every track.")
print("So coverage_pct ALONE cannot distinguish a perfect separator from a null one.")
