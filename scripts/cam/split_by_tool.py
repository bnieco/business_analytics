#!/usr/bin/env python3
"""Split a Fusion/Stepcraft UCCNC .nc program into one program per tool (manual tool change).

Deterministic and re-runnable. Never modifies the source. Refuses to write into a non-empty
output folder.
Usage: split_by_tool.py SRC.nc OUTDIR
"""
import os, re, sys

src, outdir = sys.argv[1], sys.argv[2]
base = os.path.splitext(os.path.basename(src))[0]

raw = open(src, "rb").read()
assert raw.endswith(b"\r\n"), "expected CRLF-terminated final line"
assert raw.count(b"\r\n") == raw.count(b"\n"), "mixed line endings"
raw.decode("ascii")  # assert pure ASCII
lines = raw.decode("ascii").split("\r\n")[:-1]  # 0-based list; line N = lines[N-1]
EOL = "\r\n"

TM6 = re.compile(r"^T(\d+) M6$")
toolchanges = [(i, int(TM6.match(l).group(1))) for i, l in enumerate(lines) if TM6.match(l)]
assert len(toolchanges) >= 1, toolchanges

# Tool-table comments in header (before first G-code line)
tooltable = {}
for l in lines:
    if not l.startswith("("):
        break
    m = re.match(r"^\(T(\d+) ", l)
    if m:
        tooltable[int(m.group(1))] = l

first_code = next(i for i, l in enumerate(lines) if not l.startswith("("))
assert lines[first_code] == "G90" and lines[first_code + 1] == "G53 G0 Z-1.", "unexpected preamble"
assert lines[first_code + 2] == "" and first_code + 4 == toolchanges[0][0], "unexpected gap before first tool"
assert not any(re.match(r"^G(17|20|21)\b", l) for l in lines), "unexpected plane/unit code"
assert sorted(tooltable) == sorted(t for _, t in toolchanges), "tool table / M6 mismatch"
FOOTER = ["M5", "G53 G0 Z-1.", "M30"]
RETRACT = re.compile(r"^G0 Z\d+(\.\d*)?$")  # final clearance retract (height varies per op)
assert lines[-3:] == FOOTER, "unexpected footer"

sections = []
for k, (ti, tool) in enumerate(toolchanges):
    start = ti - 1  # operation comment just before T M6
    assert lines[start].startswith("(") and lines[start - 1] == "", (start, lines[start])
    if k + 1 < len(toolchanges):
        end = toolchanges[k + 1][0] - 3  # last line before blank + op comment of next section
        assert lines[end + 1] == "", end
        assert RETRACT.match(lines[end - 1]) and lines[end] == "G53 G0 Z-1.", lines[end - 1:end + 1]
        body = lines[start:end + 1]
    else:
        end = len(lines) - 1
        assert RETRACT.match(lines[end - 3]), lines[end - 3]
        body = lines[start:end + 1 - 3]  # strip original footer; re-appended below
    # body: drop exactly the T M6 line
    rel = ti - start
    assert body[rel] == f"T{tool} M6"
    desc = body[rel + 1]
    assert desc.startswith("(")
    assert f"G43 H{tool}" in body[rel + 2:rel + 8], "G43 H mismatch"
    body = body[:rel] + body[rel + 1:]
    sections.append(dict(seq=k + 1, tool=tool, a=start + 1, b=end + 1, body=body, desc=desc))

if os.path.exists(outdir):
    # allowed: only 'superseded-*' archive subfolders; anything else -> stop
    other = [e for e in os.listdir(outdir)
             if not (e.startswith("superseded-") and os.path.isdir(os.path.join(outdir, e)))]
    if other:
        sys.exit(f"STOP: {outdir} exists and is not empty: {other}")
else:
    os.mkdir(outdir)

for s in sections:
    name = f"{base}-{s['seq']}-T{s['tool']}.nc"
    out = [
        f"({name})",
        f"(Source: {os.path.basename(src)}, lines {s['a']}-{s['b']})",
        tooltable[s["tool"]],
        s["desc"],
        "(Manual tool change: ATC disabled, no M6 in this file)",
        f"(Uses G43 H{s['tool']} tool length offset from the UCCNC tool table)",
        "G90",
        "G53 G0 Z-1.",
    ] + s["body"] + FOOTER
    data = (EOL.join(out) + EOL).encode("ascii")
    with open(os.path.join(outdir, name), "xb") as f:  # x = never overwrite
        f.write(data)
    print(f"wrote {name}: {len(out)} lines, source lines {s['a']}-{s['b']}")
