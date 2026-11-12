"""Final-material comparison of two posts of the same part, in the PART frame.
Each program is simulated on a 2.5D height map (unknown stock = +inf, flat mills as discs,
T3 as 45 deg half-angle V). The new program's Y is shifted by -DY to undo the WCS shift.
Reports cells where the final surface differs by more than TOL, by region and by op/line."""
import re,math,sys
from array import array
from collections import Counter,defaultdict
DY=float(sys.argv[3]); G=0.1; TOL=0.05; INF=1e9
W=re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
def moves(path,dy):
    L=open(path).read().split("\n"); first=next(i for i,l in enumerate(L) if l.strip() and not l.startswith("("))
    tt={}
    for l in L[:first]:
        m=re.match(r"^\(T(\d+) D=([\d.]+).*?(TAPER=([\d.]+)deg)?",l)
        if m: tt[int(m.group(1))]=(float(m.group(2)),bool(re.search("TAPER",l)))
    op=None;tool=None;pos=[None]*3;mot=None;blank=True;out=[]
    for i,l in enumerate(L):
        s=l.strip()
        if not s: blank=True; continue
        if s.startswith("("):
            if i>first and blank: op=s[1:-1]
            blank=False; continue
        blank=False
        m=re.match(r"^T(\d+) M6",s)
        if m: tool=int(m.group(1)); continue
        if "G53" in s: pos[2]=None; continue
        w={k:float(v) for k,v in W.findall(s)}
        for g in re.findall(r"G(\d+)",s):
            if int(g) in (0,1,2,3): mot=int(g)
        if not any(c in w for c in "XYZ"): continue
        n=[w.get("X",pos[0]),w.get("Y",pos[1]),w.get("Z",pos[2])]
        if mot in (1,2,3) and pos[0] is not None and pos[2] is not None:
            pts=[]
            if mot==1:
                d=math.hypot(n[0]-pos[0],n[1]-pos[1]); k=max(1,int(math.ceil(max(d,abs(n[2]-pos[2]))/G)))
                pts=[(pos[0]+(n[0]-pos[0])*j/k,pos[1]+(n[1]-pos[1])*j/k,pos[2]+(n[2]-pos[2])*j/k) for j in range(1,k+1)]
            else:
                cx,cy=pos[0]+w.get("I",0),pos[1]+w.get("J",0);r=math.hypot(pos[0]-cx,pos[1]-cy)
                a0=math.atan2(pos[1]-cy,pos[0]-cx);a1=math.atan2(n[1]-cy,n[0]-cx);sw=(a1-a0) if mot==3 else (a0-a1)
                if sw<=1e-9: sw+=2*math.pi
                sg=1 if mot==3 else -1; k=max(2,int(math.ceil(r*sw/G)))
                pts=[(cx+r*math.cos(a0+sg*sw*j/k),cy+r*math.sin(a0+sg*sw*j/k),pos[2]+(n[2]-pos[2])*j/k) for j in range(1,k+1)]
            out.append((op,tool,i+1,[(x,y-dy,z) for x,y,z in pts]))
        pos=n
    return tt,out
X0,Y0,NX,NY=20.0,-332.0,2000,1600
def offsets(D,v):
    R=D/2; rc=int(math.ceil(R/G)); o=[]
    for j in range(-rc,rc+1):
        for i in range(-rc,rc+1):
            r=math.hypot(i*G,j*G)
            if r<=R: o.append((i,j,r if v else 0.0))
    return o
def simulate(path,dy):
    tt,mv=moves(path,dy)
    hm=[array('d',[INF])*NX for _ in range(NY)]; who=[array('i',[0])*NX for _ in range(NY)]
    offc={t:offsets(*tt[t]) for t in tt}; opname={}
    for op,tool,ln,pts in mv:
        offs=offc[tool]; opname[ln]=op
        for x,y,z in pts:
            ci=int(round((x-X0)/G)); cj=int(round((y-Y0)/G))
            for di,dj,dz in offs:
                row=hm[cj+dj]; zc=z+dz
                if row[ci+di]>zc:
                    row[ci+di]=zc; who[cj+dj][ci+di]=ln
    return hm,who,opname
ho,wo,no=simulate(sys.argv[1],0.0)
hn,wn,nn=simulate(sys.argv[2],DY)
import pickle
pickle.dump((ho,wo,no,hn,wn,nn),open("simcache.pkl","wb"))
FACE=(36.575,201.975,-308.175,-193.775); OUT=(33.275,205.275,-311.475,-190.475)
def region(x,y):
    inside=lambda b:b[0]<=x<=b[1] and b[2]<=y<=b[3]
    if inside((FACE[0]+0.3,FACE[1]-0.3,FACE[2]+0.3,FACE[3]-0.3)): return "face (Z34 top)"
    if inside(FACE): return "face edge band (0.3 mm)"
    if inside(OUT): return "ledge band (between outer wall and face wall)"
    return "outside outer wall (waste)"
left=defaultdict(lambda:[0,0.0,1e9,-1e9,1e9,-1e9,Counter()])   # new higher than old: material left
deeper=defaultdict(lambda:[0,0.0,1e9,-1e9,1e9,-1e9,Counter()]) # new lower than old: new excursion
for j in range(NY):
    for i in range(NX):
        a=ho[j][i]; b=hn[j][i]
        if a>=INF and b>=INF: continue
        x=X0+i*G; y=Y0+j*G
        if a>=INF: a_=INF
        if b>a+TOL:
            e=left[region(x,y)]; e[0]+=1; e[1]=max(e[1],(b-a) if b<INF else 99)
            e[2]=min(e[2],x);e[3]=max(e[3],x);e[4]=min(e[4],y);e[5]=max(e[5],y); e[6][no.get(wo[j][i],"?")]+=1
        elif b<a-TOL:
            e=deeper[region(x,y)]; e[0]+=1; e[1]=max(e[1],(a-b) if a<INF else 99)
            e[2]=min(e[2],x);e[3]=max(e[3],x);e[4]=min(e[4],y);e[5]=max(e[5],y); e[6][(nn.get(wn[j][i],"?"),wn[j][i])]+=1
cell=G*G
print("== MATERIAL LEFT by NEW that OLD removed (new surface higher by >0.05 mm); old op that removed it")
for r,e in left.items():
    print(f"  {r}: area {e[0]*cell:.1f} mm2, max height diff {e[1]:.2f} mm, X {e[2]:.2f}..{e[3]:.2f} Y {e[4]:.2f}..{e[5]:.2f}, old ops {dict(e[6].most_common(4))}")
print("== NEW CUTS DEEPER or WHERE OLD NEVER CUT (new surface lower by >0.05 mm; 99 = old never cut); new op, line")
for r,e in deeper.items():
    lines=Counter(); ops=Counter()
    for (o,l),c in e[6].items(): ops[o]+=c; lines[l]+=c
    print(f"  {r}: area {e[0]*cell:.1f} mm2, max depth diff {e[1]:.2f} mm, X {e[2]:.2f}..{e[3]:.2f} Y {e[4]:.2f}..{e[5]:.2f}, new ops {dict(ops)}, top lines {lines.most_common(6)}")
# ledge floor heights in new, by side
for tag,hm in (("OLD",ho),("NEW",hn)):
    for name,(x,y) in {"x-min side":(34.9,-250.8),"x-max side":(203.6,-250.8),"y-min side":(119.3,-309.8),"y-max side":(119.3,-192.1),"face centre":(119.3,-250.8)}.items():
        i=int(round((x-X0)/G)); j=int(round((y-Y0)/G)); print(f"  {tag} final Z at ledge/face probe {name} ({x},{y}): {hm[j][i]:.3f}")
