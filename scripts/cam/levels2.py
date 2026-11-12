import re,math,sys
from collections import defaultdict
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
L=open(sys.argv[1]).read().split("\n")
first=next(i for i,l in enumerate(L) if l.strip() and not l.startswith("("))
op=None;pos=[None]*3;mot=None;blank=True;res={}
for i,l in enumerate(L):
    s=l.strip()
    if not s: blank=True; continue
    if s.startswith("("):
        if i>first and blank: op=s[1:-1]; res[op]=defaultdict(lambda:[0.0,1e9,-1e9,1e9,-1e9,1e9,0])
        blank=False; continue
    blank=False
    if "G53" in s: pos[2]=None; continue
    w={k:float(v) for k,v in W.findall(s)}
    for g in re.findall(r"G(\d+)",s):
        if int(g) in (0,1,2,3): mot=int(g)
    if not any(c in w for c in "XYZ"): continue
    n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
    if mot in (1,2,3) and pos[2] is not None and n[2]==pos[2] and pos[0] is not None:
        e=res[op][n[2]]; 
        if mot==1: e[0]+=math.hypot(n[0]-pos[0],n[1]-pos[1])
        else:
            cx,cy=pos[0]+w.get("I",0),pos[1]+w.get("J",0);r=math.hypot(pos[0]-cx,pos[1]-cy)
            a0=math.atan2(pos[1]-cy,pos[0]-cx);a1=math.atan2(n[1]-cy,n[0]-cx);sw=(a1-a0) if mot==3 else (a0-a1)
            if sw<=1e-9: sw+=2*math.pi
            e[0]+=r*sw
        for x,y in (pos[:2],n[:2]):
            e[1]=min(e[1],x);e[2]=max(e[2],x);e[3]=min(e[3],y);e[4]=max(e[4],y)
        e[5]=min(e[5],i+1); e[6]=max(e[6],i+1)
    pos=n
for op,lv in res.items():
    print("==",op)
    for z,e in sorted(lv.items(),reverse=True):
        if e[0]>1: print(f"  Z{z:<7} {e[0]:8.1f} mm  X {e[1]:.3f}..{e[2]:.3f}  Y {e[3]:.3f}..{e[4]:.3f}  lines {e[5]}-{e[6]}")
