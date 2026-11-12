"""Rebuild the pre-re-post TrayDownLt.nc from the 4 split files (read-only on them)."""
import glob,re,sys,hashlib
d="/Volumes/home/CNC/CNCPrograms/TableFixture4/TrayDownLt"
hdr=["(TrayDown)","(Machine)","(  vendor: Stepcraft)","(  model: M1000)","(  description: Generic 3-axis)"]
files=sorted(glob.glob(d+"/TrayDownLt-*-T*.nc"))
assert len(files)==4,files
secs=[];tt={}
for f in files:
    L=open(f,"rb").read().decode("ascii").split("\r\n")[:-1]
    tool=int(re.search(r"-T(\d+)\.nc$",f).group(1))
    tt[tool]=L[2]
    body=L[8:-3]
    secs.append(body[:1]+[f"T{tool} M6"]+body[1:])
out=hdr+[tt[t] for t in sorted(tt)]+["G90","G53 G0 Z-1.",""]
for i,s in enumerate(secs):
    out+=s+([""] if i<len(secs)-1 else [])
out+=["M5","G53 G0 Z-1.","M30"]
data=("\r\n".join(out)+"\r\n").encode("ascii")
open(sys.argv[1],"wb").write(data)
print(len(out),"lines sha256",hashlib.sha256(data).hexdigest())
