"""List distinct arc centres (X,Y,radius) per op, to detect geometry shifts between two posts."""
import re,sys
from collections import Counter
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
def centers(path):
    L=open(path,"rb").read().decode("ascii").replace("\r\n","\n").split("\n")
    first=next(i for i,l in enumerate(L) if l.strip() and not l.startswith("("))
    op=None;pos=[None,None,None];mot=None;out={};blank=True
    for i,l in enumerate(L):
        s=l.strip()
        if not s: blank=True; continue
        if s.startswith("("):
            if i>first and blank: op=s[1:-1]; out[op]=Counter()
            blank=False; continue
        blank=False
        if "G53" in s: continue
        w={k:float(v) for k,v in W.findall(s)}
        for g in re.findall(r"G(\d+)",s):
            if int(g) in (0,1,2,3): mot=int(g)
        if not any(c in w for c in "XYZ"): continue
        n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
        if mot in (2,3) and op:
            cx=pos[0]+w.get("I",0); cy=pos[1]+w.get("J",0); r=((pos[0]-cx)**2+(pos[1]-cy)**2)**.5
            out[op][(round(cx,3),round(cy,3),round(r,3))]+=1
        pos=n
    return out
o=centers(sys.argv[1]); n=centers(sys.argv[2])
for op in sorted(set(o)|set(n)):
    oc=set(c for c,k in o.get(op,{}).items() if c[2]>2); nc=set(c for c,k in n.get(op,{}).items() if c[2]>2)
    print("==",op,"old big-arc centres",sorted(oc)[:6],"\n   new",sorted(nc)[:6])
    oh=sorted(set((c[0],c[1]) for c in o.get(op,{}) if c[2]<1.5)); nh=sorted(set((c[0],c[1]) for c in n.get(op,{}) if c[2]<1.5))
    if oh or nh: print("   small-arc centres old",oh[:8],"\n   new",nh[:8])
