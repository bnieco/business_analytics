"""Cluster old-vs-new final-surface differences into 2 mm buckets to separate real changes
from grid artifacts at vertical walls (the 0.507 mm shift is not a multiple of the 0.1 mm grid)."""
import pickle,sys
from collections import defaultdict,Counter
ho,wo,no,hn,wn,nn=pickle.load(open("simcache.pkl","rb"))
G=0.1;X0,Y0=20.0,-332.0;INF=1e9;TH=float(sys.argv[1]) if len(sys.argv)>1 else 0.15
B=defaultdict(lambda:[0,0,0.0,0.0,Counter(),Counter()])
for j in range(len(ho)):
    for i in range(len(ho[0])):
        a=ho[j][i];b=hn[j][i]
        if a>=INF and b>=INF: continue
        k=(int((X0+i*G)//2)*2,int((Y0+j*G)//2)*2)
        if b>a+TH: e=B[k]; e[0]+=1; e[2]=max(e[2],min(b-a,99)); e[4][no.get(wo[j][i],'?')]+=1
        elif b<a-TH: e=B[k]; e[1]+=1; e[3]=max(e[3],min(a-b,99)); e[5][(nn.get(wn[j][i],'?'),wn[j][i])]+=1
MIN=50  # cells = 0.5 mm2 per 2x2 mm bucket
left=[(k,v) for k,v in B.items() if v[0]>=MIN]; deep=[(k,v) for k,v in B.items() if v[1]>=MIN]
print("LEFT-HIGH buckets >=0.5mm2:",len(left),"area",sum(v[0] for k,v in left)*G*G,"mm2")
byop=Counter()
for k,v in left: byop.update(v[4])
print("  by old op that had removed it:",dict(byop))
xs=[k[0] for k,v in left]; ys=[k[1] for k,v in left]
if left: print("  extent X",min(xs),max(xs)+2,"Y",min(ys),max(ys)+2," max diff",max(v[2] for k,v in left))
# non-ledge left buckets (inside face, x 39..199, y -305..-197)
inner=[(k,v) for k,v in left if 39<=k[0]<=197 and -305<=k[1]<=-198]
print("  buckets inside the face interior:",[(k,v[0],round(v[2],2),dict(v[4])) for k,v in inner][:12])
print("DEEPER buckets >=0.5mm2:",len(deep))
for k,v in sorted(deep)[:30]: print("  ",k,v[1],"cells max",round(v[3],2),v[5].most_common(3))
