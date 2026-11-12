#!/usr/bin/env python3
"""Read-only toolpath analysis of a Fusion -> UCCNC (Stepcraft) .nc program.

Usage: cam_analyze.py PROGRAM.nc [--rapid MM_PER_MIN] [--grid MM] [--json OUT.json]

Computes per-operation cut / rapid distances (true helical arc length), time at programmed
feeds, segment-length distribution, Z/XY extents, retract heights, entry (plunge/ramp/helix)
moves, and a conservative 'definite air cutting' estimate from a 2.5D height-map simulation.

Height-map method (conservative, lower bound on air):
  * Stock shape is UNKNOWN, so every cell starts at +infinity (= assume material everywhere).
  * Each cut sample stamps the tool profile (flat end mill disk, or 45 deg half-angle V for the
    chamfer mill) into the map: h = min(h, tipZ + profile(r)).
  * Before stamping, a sample is 'engaged' if any cell under the tool is higher than the
    profile + EPS. A cut sample with no engaged cell is DEFINITELY in air, because it only
    passes through volume this program has already removed.
  * Material that the real stock never had (stock below the assumed top) is NOT counted, so
    true air cutting is >= the reported value.
The original file is opened read-only and never written.
"""
import math, re, sys, json, argparse
from collections import defaultdict, Counter

ap = argparse.ArgumentParser()
ap.add_argument("prog")
ap.add_argument("--rapid", type=float, default=5000.0, help="placeholder G0 rate mm/min")
ap.add_argument("--grid", type=float, default=0.25, help="height-map cell size mm")
ap.add_argument("--json", default=None)
args = ap.parse_args()

raw = open(args.prog, "rb").read().decode("ascii")
lines = raw.replace("\r\n", "\n").split("\n")
if lines and lines[-1] == "":
    lines.pop()

# ---- header tool table ----
tooltab = {}
for l in lines:
    if not l.startswith("("):
        break
    m = re.match(r"^\(T(\d+) D=([\d.]+) CR=([\d.]+)(?: TAPER=([\d.]+)deg)? - ZMIN=([-\d.]+) - (.*)\)$", l)
    if m:
        tooltab[int(m.group(1))] = dict(D=float(m.group(2)), CR=float(m.group(3)),
                                       taper=float(m.group(4)) if m.group(4) else None,
                                       zmin=float(m.group(5)), kind=m.group(6))

WORD = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")

# ---- parse into moves ----
ops = []          # list of dict(name, tool, start, end, S, moves=[])
cur = None
tool = None
S = None
motion = None
F = None
pos = [None, None, None]
after_tc = False
prev_blank = True
first_code = next(i for i, l in enumerate(lines, 1) if l.strip() and not l.startswith("("))
for ln, l in enumerate(lines, 1):
    s = l.strip()
    if s == "":
        prev_blank = True
        continue
    if s.startswith("("):
        if ln > first_code and prev_blank:
            if cur:
                cur["end"] = ln - 1
            cur = dict(name=s[1:-1], tool=tool, start=ln, end=None, S=S, moves=[], desc=None)
            ops.append(cur)
        elif after_tc and cur is not None and cur["desc"] is None:
            cur["desc"] = s[1:-1]
            after_tc = False
        prev_blank = False
        continue
    prev_blank = False
    words = WORD.findall(s)
    w = defaultdict(list)
    for k, v in words:
        w[k].append(float(v))
    gs = [int(v) for v in w.get("G", [])]
    if "T" in w and 6 in [int(v) for v in w.get("M", [])]:
        tool = int(w["T"][0])
        if cur:
            cur["tool"] = tool
        after_tc = True
        continue
    if "S" in w:
        S = w["S"][0]
        if cur:
            cur["S"] = S
    if 53 in gs:
        # machine-coordinate retract: work Z unknown afterwards
        pos[2] = None
        if cur:
            cur.setdefault("g53", []).append(ln)
        continue
    for g in gs:
        if g in (0, 1, 2, 3):
            motion = g
    if "F" in w:
        F = w["F"][0]
    if not any(k in w for k in "XYZ"):
        continue
    nx = w["X"][0] if "X" in w else pos[0]
    ny = w["Y"][0] if "Y" in w else pos[1]
    nz = w["Z"][0] if "Z" in w else pos[2]
    mv = dict(ln=ln, g=motion, F=F, p0=tuple(pos), p1=(nx, ny, nz), raw=s)
    if motion in (2, 3):
        mv["I"] = w["I"][0] if "I" in w else 0.0
        mv["J"] = w["J"][0] if "J" in w else 0.0
    if cur is not None:
        cur["moves"].append(mv)
    pos = [nx, ny, nz]
if cur:
    cur["end"] = len(lines)


def arc_geom(mv):
    x0, y0, z0 = mv["p0"]
    x1, y1, z1 = mv["p1"]
    cx, cy = x0 + mv["I"], y0 + mv["J"]
    r0 = math.hypot(x0 - cx, y0 - cy)
    r1 = math.hypot(x1 - cx, y1 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if mv["g"] == 3:  # CCW
        sweep = a1 - a0
        if sweep <= 1e-9:
            sweep += 2 * math.pi
    else:
        sweep = a0 - a1
        if sweep <= 1e-9:
            sweep += 2 * math.pi
    return cx, cy, r0, r1, a0, sweep


def seglen(mv):
    x0, y0, z0 = mv["p0"]
    x1, y1, z1 = mv["p1"]
    dz = 0.0 if (z0 is None or z1 is None) else z1 - z0
    if mv["g"] in (2, 3):
        cx, cy, r0, r1, a0, sweep = arc_geom(mv)
        r = (r0 + r1) / 2
        mv["radius_err"] = abs(r0 - r1)
        return math.hypot(r * sweep, dz), r * sweep, dz
    if x0 is None:
        return abs(dz), 0.0, dz
    dxy = math.hypot(x1 - x0, y1 - y0)
    return math.sqrt(dxy * dxy + dz * dz), dxy, dz


def samples(mv, step):
    """points along a cut move, including end point, excluding start."""
    x0, y0, z0 = mv["p0"]
    x1, y1, z1 = mv["p1"]
    L, _, _ = seglen(mv)
    n = max(1, int(math.ceil(L / step)))
    pts = []
    if mv["g"] in (2, 3):
        cx, cy, r0, r1, a0, sweep = arc_geom(mv)
        sgn = 1 if mv["g"] == 3 else -1
        for i in range(1, n + 1):
            t = i / n
            a = a0 + sgn * sweep * t
            r = r0 + (r1 - r0) * t
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a), z0 + (z1 - z0) * t))
    else:
        for i in range(1, n + 1):
            t = i / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t))
    return pts


# ---- global extents for height map ----
xs, ys = [], []
for op in ops:
    for mv in op["moves"]:
        if mv["p1"][0] is not None:
            xs.append(mv["p1"][0]); ys.append(mv["p1"][1])
maxR = max(t["D"] for t in tooltab.values()) / 2 + 1
gx0, gy0 = min(xs) - maxR, min(ys) - maxR
G = args.grid
NX = int((max(xs) + maxR - gx0) / G) + 2
NY = int((max(ys) + maxR - gy0) / G) + 2
INF = 1e9
hm = [[INF] * NX for _ in range(NY)]
EPS = float(__import__("os").environ.get("EPS","0.05"))


def profile_offsets(t):
    R = t["D"] / 2
    offs = []
    rc = int(math.ceil(R / G))
    for j in range(-rc, rc + 1):
        for i in range(-rc, rc + 1):
            r = math.hypot(i * G, j * G)
            if r <= R:
                if t["taper"]:
                    dz = r / math.tan(math.radians(t["taper"]))  # V: height above tip at radius r
                else:
                    dz = 0.0
                offs.append((i, j, dz))
    return offs

# ---- per-op analysis ----
report = []
all_cut_hash = Counter()
for op in ops:
    t = tooltab[op["tool"]]
    offs = profile_offsets(t)
    cut_len = cut_t = rap_xy = rap_z = rap_t = 0.0
    seglens = []
    feeds = Counter()
    zmin = INF
    ext = [INF, -INF, INF, -INF]
    retract_heights = Counter()
    rapid_down_heights = Counter()
    entries = []      # dicts
    air_len = air_t = 0.0
    air_moves = 0
    big_rapids = []
    retract_events = []   # (ln_up, z_from, z_top, xy_dist, z_reentry, ln_down)
    i = 0
    mvs = op["moves"]
    # retract event detection: G0 Z-up ... G0 XY ... G0 Z-down
    for k, mv in enumerate(mvs):
        L, dxy, dz = seglen(mv)
        if mv["g"] == 0:
            rap_xy += dxy
            rap_z += abs(dz)
            rap_t += math.sqrt(dxy * dxy + dz * dz) / args.rapid
            if dz > 0:
                retract_heights[mv["p1"][2]] += 1
            if dz < 0:
                rapid_down_heights[mv["p1"][2]] += 1
            if dxy > 0:
                big_rapids.append((dxy, mv["ln"], mv["p0"], mv["p1"]))
            continue
        # cut move
        cut_len += L
        cut_t += L / mv["F"]
        seglens.append(L)
        feeds[mv["F"]] += L
        zmin = min(zmin, mv["p1"][2], mv["p0"][2] if mv["p0"][2] is not None else INF)
        for p in (mv["p0"], mv["p1"]):
            if p[0] is not None:
                ext[0] = min(ext[0], p[0]); ext[1] = max(ext[1], p[0])
                ext[2] = min(ext[2], p[1]); ext[3] = max(ext[3], p[1])
        key = (mv["g"], tuple(round(c, 3) for c in mv["p0"]), tuple(round(c, 3) for c in mv["p1"]))
        all_cut_hash[key] += 1
        # simulation
        engaged_len = 0.0
        pts = samples(mv, G)
        step_len = L / len(pts)
        for (px, py, pz) in pts:
            ci = int(round((px - gx0) / G)); cj = int(round((py - gy0) / G))
            eng = False
            for (di, dj, dz) in offs:
                row = hm[cj + dj]
                h = row[ci + di]
                zc = pz + dz
                if h > zc + EPS:
                    eng = True
                    row[ci + di] = zc
            if not eng:
                air_len += step_len
                air_t += step_len / mv["F"]
            else:
                engaged_len += step_len
        mv["air_frac"] = 1 - engaged_len / L if L else 0
    # entries: after each G0 that goes down, collect descending cut moves
    k = 0
    while k < len(mvs):
        mv = mvs[k]
        if mv["g"] == 0 and seglen(mv)[2] < 0:
            # skip further rapids
            j = k + 1
            while j < len(mvs) and mvs[j]["g"] == 0:
                j += 1
            seq = []
            while j < len(mvs) and mvs[j]["g"] != 0:
                L, dxy, dz = seglen(mvs[j])
                if dz < -1e-6:
                    seq.append((mvs[j], L, dxy, dz))
                    j += 1
                elif seq and dz == 0 and dxy < 1e-9:
                    j += 1
                else:
                    break
            if seq:
                kinds = Counter()
                for (m, L, dxy, dz) in seq:
                    if m["g"] in (2, 3):
                        kinds["helix"] += 1
                    elif dxy < 1e-6:
                        kinds["plunge"] += 1
                    else:
                        kinds["ramp"] += 1
                z_start = seq[0][0]["p0"][2]
                z_end = seq[-1][0]["p1"][2]
                plunge_drop = sum(-dz for (m, L, dxy, dz) in seq if dxy < 1e-6 and m["g"] == 1)
                entries.append(dict(ln=seq[0][0]["ln"], z_start=z_start, z_end=z_end,
                                    kinds=dict(kinds), plunge_drop=round(plunge_drop, 3),
                                    feeds=sorted(set(m["F"] for (m, *_r) in seq)),
                                    helix_len=round(sum(L for (m, L, dxy, dz) in seq if m["g"] in (2, 3)), 2)))
            k = j
        else:
            k += 1
    # retract events (cut -> G0 up ... G0 down -> cut)
    k = 0
    while k < len(mvs):
        if mvs[k]["g"] == 0 and seglen(mvs[k])[2] > 0 and k > 0 and mvs[k - 1]["g"] != 0:
            j = k
            ztop = mvs[k]["p0"][2]
            zfrom = mvs[k]["p0"][2]
            xy0 = mvs[k]["p0"][:2]
            zd = zu = 0.0
            while j < len(mvs) and mvs[j]["g"] == 0:
                L, dxy, dz = seglen(mvs[j])
                ztop = max(ztop, mvs[j]["p1"][2])
                if dz > 0: zu += dz
                else: zd += -dz
                j += 1
            if j < len(mvs):
                xy1 = mvs[j]["p0"][:2]
                zre = mvs[j]["p0"][2]
                d = math.hypot(xy1[0] - xy0[0], xy1[1] - xy0[1])
                # safe height along the rapid path from height map (known-removed material only)
                n = max(1, int(d / G))
                Rc = t["D"] / 2
                hmax = max(zfrom, zre)
                for s_ in range(n + 1):
                    px = xy0[0] + (xy1[0] - xy0[0]) * s_ / n
                    py = xy0[1] + (xy1[1] - xy0[1]) * s_ / n
                    ci = int(round((px - gx0) / G)); cj = int(round((py - gy0) / G))
                    for (di, dj, dz) in offs:
                        hmax = max(hmax, hm[cj + dj][ci + di])
                retract_events.append(dict(ln=mvs[k]["ln"], z_from=zfrom, z_top=ztop, z_re=zre,
                                           xy=round(d, 2), z_travel=round(zu + zd, 2),
                                           path_max_material=None if hmax >= INF else round(hmax, 3)))
            k = j
        else:
            k += 1
    runs = []
    cur_run = None
    for mv in mvs:
        if mv["g"] == 0:
            continue
        L_ = seglen(mv)[0]
        if mv.get("air_frac", 0) > 0.95:
            if cur_run is None:
                cur_run = [mv["ln"], mv["ln"], 0.0, 0.0]
            cur_run[1] = mv["ln"]; cur_run[2] += L_; cur_run[3] += L_ / mv["F"]
        else:
            if cur_run: runs.append(cur_run)
            cur_run = None
    if cur_run: runs.append(cur_run)
    runs.sort(key=lambda r: -r[2])
    big_rapids.sort(reverse=True)
    n = len(seglens)
    rep = dict(op=op["name"], tool=op["tool"], lines=f"{op['start']}-{op['end']}", S=op["S"],
               desc=op["desc"],
               feeds={str(k): round(v, 1) for k, v in sorted(feeds.items())},
               cut_mm=round(cut_len, 1), cut_min=round(cut_t, 2),
               rapid_xy_mm=round(rap_xy, 1), rapid_z_mm=round(rap_z, 1), rapid_min=round(rap_t, 2),
               n_cut_moves=n,
               pct_lt_0_5=round(100 * sum(1 for x in seglens if x < 0.5) / n, 1) if n else 0,
               pct_lt_0_1=round(100 * sum(1 for x in seglens if x < 0.1) / n, 1) if n else 0,
               median_seg=round(sorted(seglens)[n // 2], 3) if n else 0,
               zmin=round(zmin, 3), header_zmin=t["zmin"],
               xy_ext=[round(e, 3) for e in ext],
               retract_heights={str(k): v for k, v in sorted(retract_heights.items())},
               rapid_down_heights={str(k): v for k, v in sorted(rapid_down_heights.items())},
               n_retract_events=len(retract_events),
               retract_events=retract_events,
               entries=entries,
               air_mm=round(air_len, 1), air_min=round(air_t, 2),
               air_runs=[(r[0], r[1], round(r[2], 1), round(r[3] * 60, 1)) for r in runs[:8]],
               top_rapids=[(round(d, 1), ln) for d, ln, a, b in big_rapids[:5]])
    report.append(rep)

dups = sum(c - 1 for c in all_cut_hash.values() if c > 1)
out = dict(file=args.prog, rapid_rate=args.rapid, grid=G, tools=tooltab, ops=report,
           duplicate_cut_segments=dups,
           total_cut_min=round(sum(r["cut_min"] for r in report), 2),
           total_rapid_min=round(sum(r["rapid_min"] for r in report), 2))
if args.json:
    json.dump(out, open(args.json, "w"), indent=1, default=str)
for r in report:
    r2 = {k: v for k, v in r.items() if k not in ("retract_events", "entries")}
    print(json.dumps(r2, default=str))
    print("  entries:", len(r["entries"]), Counter(json.dumps(e["kinds"], sort_keys=True) for e in r["entries"]))
print("dup cut segments:", dups, "| total cut min", out["total_cut_min"], "| rapid min", out["total_rapid_min"])
