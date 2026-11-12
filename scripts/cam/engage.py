"""Per-line engagement on a program: for each cut line, the max axial depth of material removed
(height before minus tool tip Z) and the radial width removed perpendicular to travel (approx: number
of engaged cells across the disc / grid). Uses sim_compare's simulate core."""
import sys,math
from array import array
src=open("sim_compare.py").read().split("ho,wo,no=simulate")[0]
sys.argv=["x","a","b","0"]; exec(src)
tt,mv=moves(sys_argv_file:=__import__("os").environ["PROG"],0.0)
hm=[array('d',[INF])*NX for _ in range(NY)]
offc={t:offsets(*tt[t]) for t in tt}
lo,hi=int(__import__("os").environ["LO"]),int(__import__("os").environ["HI"])
stats={}
for op,tool,ln,pts in mv:
    offs=offc[tool]
    for x,y,z in pts:
        ci=int(round((x-X0)/G)); cj=int(round((y-Y0)/G))
        dmax=0.0; cells=0
        for di,dj,dz in offs:
            row=hm[cj+dj]; zc=z+dz; h=row[ci+di]
            if h>zc+0.02:
                if lo<=ln<=hi:
                    dmax=max(dmax,min(h,40.0)-zc); cells+=1
                row[ci+di]=zc
        if lo<=ln<=hi:
            s=stats.setdefault(ln,[0.0,0,0]); s[0]=max(s[0],dmax); s[1]=max(s[1],cells); s[2]+=1
for ln,(d,c,n) in sorted(stats.items()):
    print(f"line {ln}: max axial depth removed {d:.2f} mm, max engaged cells per sample {c} (= {c*G*G:.1f} mm2 of disc)")
