"""Exact radial engagement check: for each point on a later toolpath, the distance to the nearest
earlier toolpath (same 6.35 mm tool diameter, at or below that Z). For equal-diameter tools moving
parallel, this distance IS the radial width of new material that pass removes."""
import re,math,sys
from collections import defaultdict
L=open(sys.argv[1]).read().replace("\r\n","\n").split("\n")
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
def path(a,b):
    pos=[None,None,None];mot=None;segs=[]
    for ln in range(1,len(L)+1):
        s=L[ln-1].strip()
        if not s or s.startswith("(") or "G53" in s:
            if "G53" in s: pos[2]=None
            continue
        w=dict(W.findall(s)); w={k:float(v) for k,v in w.items()}
        for g in re.findall(r"G(\d+)",s):
            if int(g) in (0,1,2,3): mot=int(g)
        if not any(c in w for c in "XYZ"): continue
        n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
        if a<=ln<=b and mot in (1,2,3) and pos[0] is not None and pos[2] is not None:
            if mot==1: pts=[pos,n]
            else:
                cx,cy=pos[0]+w.get("I",0),pos[1]+w.get("J",0); r=math.hypot(pos[0]-cx,pos[1]-cy)
                a0=math.atan2(pos[1]-cy,pos[0]-cx);a1=math.atan2(n[1]-cy,n[0]-cx)
                sw=(a1-a0) if mot==3 else (a0-a1)
                if sw<=1e-9: sw+=2*math.pi
                k=max(2,int(r*sw/0.05)); sg=1 if mot==3 else -1
                pts=[[cx+r*math.cos(a0+sg*sw*i/k),cy+r*math.sin(a0+sg*sw*i/k),pos[2]+(n[2]-pos[2])*i/k] for i in range(k+1)]
            for p,q in zip(pts,pts[1:]): segs.append((p,q,ln))
        pos=n
    return segs
def dens(segs,step=0.25):
    out=[]
    for p,q,ln in segs:
        d=math.hypot(q[0]-p[0],q[1]-p[1]); k=max(1,int(d/step))
        for i in range(k+1):
            t=i/k; out.append((p[0]+(q[0]-p[0])*t,p[1]+(q[1]-p[1])*t,p[2]+(q[2]-p[2])*t,ln))
    return out
def check(name,later,earlier,zlev=None):
    E=dens(earlier,0.1); H=defaultdict(list)
    for x,y,z,ln in E: H[(int(x//2),int(y//2))].append((x,y,z))
    res=[]
    for x,y,z,ln in dens(later):
        if zlev is not None and abs(z-zlev)>1e-6: continue
        best=99
        for i in (-2,-1,0,1,2):
            for j in (-2,-1,0,1,2):
                for (ex,ey,ez) in H.get((int(x//2)+i,int(y//2)+j),()):
                    if ez<=z+0.01: best=min(best,math.hypot(ex-x,ey-y))
        res.append(min(best,99))
    res.sort(); n=len(res)
    if not n: print(name,"no pts"); return
    q=lambda f: res[min(n-1,int(f*n))]
    print(f"{name}: n={n} radial new-material width mm: min {res[0]:.3f} p10 {q(.1):.3f} median {q(.5):.3f} p90 {q(.9):.3f} max {res[-1]:.3f}  (99 = nothing earlier within 4 mm)")
ad=path(13,2522); f3=path(2523,2975); c4=path(2976,3027); c3=path(3028,3265)
check("Contour4 @Z31 vs Flat3+Adaptive",c4,ad+f3,31.0)
for z in (28.5,23.0,17.5,12.0,6.5,1.0):
    check(f"Contour3 @Z{z} vs Adaptive only",c3,ad,z)
check("Flat3 ledge @Z31 vs Adaptive",f3,ad,31.0)
