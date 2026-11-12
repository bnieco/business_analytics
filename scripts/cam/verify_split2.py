import re,glob,os,sys,difflib
D="/Volumes/home/CNC/CNCPrograms/TableFixture4"
def L(p):
    r=open(p,"rb").read(); assert r.endswith(b"\r\n") and r.count(b"\r\n")==r.count(b"\n")==r.count(b"\r"),p
    return r.decode("ascii").split("\r\n")[:-1]
iscode=lambda l: l.strip()!="" and not l.startswith("(")
for base in sys.argv[1:]:
    src=L(f"{D}/{base}.nc"); o=[l for l in src if iscode(l)]
    exp=[l for l in o if not re.match(r"^T\d+ M6$",l)]
    assert exp[:2]==["G90","G53 G0 Z-1."] and exp[-3:]==["M5","G53 G0 Z-1.","M30"]
    exp=exp[2:-3]; cat=[]
    print(f"##### {base}")
    for f in sorted(glob.glob(f"{D}/{base}/*.nc")):
        fl=L(f); c=[l for l in fl if iscode(l)]
        m=re.match(r"\(Source: .*, lines (\d+)-(\d+)\)",fl[1]); a,b=int(m.group(1)),int(m.group(2))
        sl=src[a-1:b]
        if b==len(src): sl=sl[:-3]
        body_ok = fl[8:-3]==[l for l in sl if not re.match(r"^T\d+ M6$",l)]
        bad=[l for l in c if re.search(r"M0?6\b",l) or re.search(r"T\d",l)]
        g43=[l for l in c if l.startswith("G43")]; sp=[l for l in c if re.match(r"^S\d+ M3$",l)]
        ok = c[:2]==["G90","G53 G0 Z-1."] and fl[-3:]==["M5","G53 G0 Z-1.","M30"] and fl[6:8]==["G90","G53 G0 Z-1."]
        print(f"{os.path.basename(f)}: lines {len(fl)}, src {a}-{b}, start/end ok {ok}, body verbatim {body_ok}, M6/T hits {len(bad)}, G43 {g43}, spindle {sp}, M5 x{c.count('M5')}, M30 x{c.count('M30')}, header-name-comment-of-source-present {src[0] in fl[:6]}")
        cat+=c[2:-3]
    diff=[x for x in difflib.unified_diff(exp,cat,lineterm="",n=0) if x[:1] in "+-" and x[:3] not in("+++","---")]
    print(f"motion lines expected {len(exp)} concatenated {len(cat)} diff {len(diff)}")
