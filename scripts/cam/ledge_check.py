"""Ledge / face-wall finish check, in the PART frame (new Y shifted by -dy).
Face wall (Z34 face edge) and outer wall positions come from the old finishing loops:
 face wall: old Contour4 finishing loop centre +-3.175 -> X 36.575 / 201.975, Y -308.175 / -193.775
 outer wall: old Contour3 finishing loop centre -> X 33.275 / 205.275, Y -311.475 / -190.475
Reports, per op, the closest any tool edge gets to each wall on the straight sides, the Z range,
and what lowest Z is reached over the ledge band (between outer and face walls)."""
import re,math,sys
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
R=3.175
FACE=dict(xmin=36.575,xmax=201.975,ymin=-308.175,ymax=-193.775)
OUT=dict(xmin=33.275,xmax=205.275,ymin=-311.475,ymax=-190.475)
def pts(path,dy):
    L=open(path).read().split("\n"); first=next(i for i,l in enumerate(L) if l.strip() and not l.startswith("("))
    op=None;pos=[None]*3;mot=None;blank=True;out=[]
    for i,l in enumerate(L):
        s=l.strip()
        if not s: blank=True; continue
        if s.startswith("("):
            if i>first and blank: op=s[1:-1]
            blank=False; continue
        blank=False
        if "G53" in s: pos[2]=None; continue
        w={k:float(v) for k,v in W.findall(s)}
        for g in re.findall(r"G(\d+)",s):
            if int(g) in (0,1,2,3): mot=int(g)
        if not any(c in w for c in "XYZ"): continue
        n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
        if mot in (1,2,3) and pos[0] is not None and pos[2] is not None:
            if mot==1:
                d=math.hypot(n[0]-pos[0],n[1]-pos[1]); k=max(1,int(d/0.2))
                for j in range(1,k+1):
                    t=j/k; out.append((op,pos[0]+(n[0]-pos[0])*t,pos[1]+(n[1]-pos[1])*t-dy,pos[2]+(n[2]-pos[2])*t,i+1))
            else:
                cx,cy=pos[0]+w.get("I",0),pos[1]+w.get("J",0);r=math.hypot(pos[0]-cx,pos[1]-cy)
                a0=math.atan2(pos[1]-cy,pos[0]-cx);a1=math.atan2(n[1]-cy,n[0]-cx);sw=(a1-a0) if mot==3 else (a0-a1)
                if sw<=1e-9: sw+=2*math.pi
                sg=1 if mot==3 else -1; k=max(2,int(r*sw/0.2))
                for j in range(1,k+1):
                    t=j/k; a=a0+sg*sw*t; out.append((op,cx+r*math.cos(a),cy+r*math.sin(a)-dy,pos[2]+(n[2]-pos[2])*t,i+1))
        pos=n
    return out
def report(tag,path,dy):
    P_=pts(path,dy); ops=sorted(set(p[0] for p in P_),key=lambda o:min(p[4] for p in P_ if p[0]==o))
    print(f"##### {tag} (Y shifted by {-dy:+.3f} into part frame)")
    for o in ops:
        if "Chamfer" in o or "Magnet" in o: continue
        Q=[p for p in P_ if p[0]==o and p[3]<34.0-1e-6]
        # straight-side samples: x-min side of face wall, tool on ledge side (x < face) and Y in mid band
        side=[p for p in Q if -290<p[2]<-212 and p[1]<FACE["xmin"]]
        if side:
            best=max(side,key=lambda p:p[1]+R)
            gap_face=FACE["xmin"]-(best[1]+R)
            # lowest Z over the ledge band x in (OUT.xmin, FACE.xmin): any tool disc covering x=35 (ledge middle)
            cover=[p for p in side if abs(p[1]-34.925)<=R]
            zl=min(p[3] for p in cover) if cover else None
            print(f"  {o}: closest tool edge to FACE wall (x-min side, below Z34) = {gap_face:+.3f} mm at line {best[4]} (Z{best[3]:.3f}); lowest Z over ledge middle = {zl}")
        else:
            print(f"  {o}: no cut below Z34 on the x-min side band")
report("OLD","tdl_old.txt",0.0)
report("NEW","tdl_new.txt",0.507)
