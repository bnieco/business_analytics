import re,glob,os
src="/Volumes/home/CNC/CNCPrograms/TableFixture4/UtilityTrayUpRt.nc"
d="/Volumes/home/CNC/CNCPrograms/TableFixture4/UtilityTrayUpRt"
def L(p):
    r=open(p,"rb").read(); assert r.endswith(b"\r\n") and r.count(b"\r\n")==r.count(b"\n"),p; return r.decode("ascii").split("\r\n")[:-1]
iscode=lambda l: l.strip()!="" and not l.startswith("(")
o=[l for l in L(src) if iscode(l)]
# expected: drop 4 T M6, drop the original footer (last 3), drop the 11-line preamble once per file is re-added
exp=[l for l in o if not re.match(r"^T\d+ M6$",l)]
assert exp[:2]==["G90","G53 G0 Z-1."] and exp[-3:]==["M5","G53 G0 Z-1.","M30"]
exp=exp[2:-3]
files=sorted(glob.glob(d+"/*.nc")); print(files and [os.path.basename(f) for f in files])
cat=[]
for f in files:
    c=[l for l in L(f) if iscode(l)]
    assert c[:2]==["G90","G53 G0 Z-1."],f; assert c[-3:]==["M5","G53 G0 Z-1.","M30"],f
    assert L(f)[-3:]==["M5","G53 G0 Z-1.","M30"],f
    bad=[l for l in c if re.search(r"M0?6\b",l) or re.search(r"T\d",l)]
    print(os.path.basename(f),"lines",len(L(f)),"code",len(c),"M6/T hits",len(bad), "M5",sum(l=="M5" for l in c),"M30",sum(l=="M30" for l in c))
    cat+=c[2:-3]
import difflib
diff=[x for x in difflib.unified_diff(exp,cat,lineterm="",n=0) if x[:1] in "+-" and x[:3] not in("+++","---")]
print("expected motion lines",len(exp),"concatenated",len(cat),"diff lines",len(diff))
# also full-text check: each file body == source slice minus T M6
