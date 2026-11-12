import re,math,sys
from collections import defaultdict
L=open(sys.argv[1]).read().replace("\r\n","\n").split("\n")
ops=[(13,"Adaptive3"),(2523,"Flat3"),(2976,"2D Contour4"),(3028,"2D Contour3"),(3266,"MagnetHoles"),(3529,"2D Chamfer7"),(99999,"end")]
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
pos=[None,None,None];mot=None
for k in range(len(ops)-1):
    a,name=ops[k];b=ops[k+1][0]
    lv=defaultdict(lambda:[0.0,1e9,-1e9,1e9,-1e9])
    for ln in range(a,min(b,len(L)+1)):
        s=L[ln-1].strip()
        if not s or s.startswith("(") or "G53" in s: continue
        w=dict((x,float(v)) for x,v in W.findall(s))
        for g in re.findall(r"G(\d+)",s):
            if int(g) in (0,1,2,3): mot=int(g)
        if not any(c in w for c in "XYZ"): continue
        n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
        if mot in (1,2,3) and pos[2] is not None and n[2]==pos[2] and pos[0] is not None:
            e=lv[n[2]]; e[0]+=math.hypot(n[0]-pos[0],n[1]-pos[1])
            for x,y in (pos[:2],n[:2]):
                e[1]=min(e[1],x);e[2]=max(e[2],x);e[3]=min(e[3],y);e[4]=max(e[4],y)
        pos=n
    print("==",name)
    for z,e in sorted(lv.items(),reverse=True):
        if e[0]>1: print(f"  Z{z:<8} flat-cut {e[0]:8.1f} mm  X {e[1]:.2f}..{e[2]:.2f}  Y {e[3]:.2f}..{e[4]:.2f}")
