"""Batch-boundary seam detector (v2).

Two physical invariants distinguish a real batch seam from anatomy:
  1. STEP, not slope: the registration curve dy(c) has an abrupt step at a seam;
     smooth anatomy varies continuously  -> use local curvature (2nd difference).
  2. RIGID across rows: a batch seam shifts the WHOLE column by the same dy, so
     the step appears identically in the top, middle AND bottom thirds. A curved
     edge (mandibular ramus/condyle) lives in only some rows, so its apparent
     step disagrees between bands  -> require CONSENSUS across the 3 bands.
Seam strength = curvature common to all three bands (min-if-same-sign). Anatomy,
being band-localized, collapses to ~0; a rigid seam survives.
"""
import numpy as np
from PIL import Image

def _dy(strip_l, strip_r):
    n = len(strip_l)
    if n < 50: return None, 0.0
    corr = {}
    for s in range(-12, 13):
        if s < 0: a, b = strip_l[:n+s], strip_r[-s:]
        elif s > 0: a, b = strip_l[s:], strip_r[:n-s]
        else: a, b = strip_l, strip_r
        if len(a) < 40: continue
        ac, bc = a - a.mean(), b - b.mean()
        d = float(np.sqrt((ac**2).sum() * (bc**2).sum()))
        if d < 1e-6: continue
        corr[s] = float((ac*bc).sum()/d)
    if not corr: return None, 0.0
    bs = max(corr, key=corr.get); bv = corr[bs]
    if bs-1 in corr and bs+1 in corr:
        y0,y1,y2 = corr[bs-1],corr[bs],corr[bs+1]; den = y0-2*y1+y2
        if abs(den) > 1e-9: bs = bs + max(-0.5,min(0.5,0.5*(y0-y2)/den))
    return float(bs), bv

def _band_dy(img, c, W, r0, r1):
    d,_ = _dy(img[r0:r1, c-W:c].mean(1), img[r0:r1, c:c+W].mean(1)); return d

def seam_profile(img, W=8, grid=6):
    h, w = img.shape
    top=(int(h*0.15),int(h*0.40)); mid=(int(h*0.40),int(h*0.63)); bot=(int(h*0.63),int(h*0.88))
    cols = list(range(2*W, w-2*W, grid))
    dt=[]; dm=[]; db=[]
    for c in cols:
        dt.append(_band_dy(img,c,W,*top)); dm.append(_band_dy(img,c,W,*mid)); db.append(_band_dy(img,c,W,*bot))
    def arr(x): return np.array([np.nan if v is None else v for v in x], float)
    dt,dm,db = arr(dt),arr(dm),arr(db)
    span = max(1, int(round(W/grid)))
    def curv(d,i):
        a,b,cc=d[i-span],d[i],d[i+span]
        if np.isnan(a) or np.isnan(b) or np.isnan(cc): return np.nan
        return b-0.5*(a+cc)
    seam = np.full(len(cols), np.nan)
    for i in range(span, len(cols)-span):
        ct,cm,cb = curv(dt,i),curv(dm,i),curv(db,i)
        if np.isnan(ct) or np.isnan(cm) or np.isnan(cb): continue
        # consensus: same sign across all three bands -> rigid seam; take the
        # magnitude they agree on (min |.|). Disagreement (anatomy) -> ~0.
        s = np.sign([ct,cm,cb])
        if s[0]==s[1]==s[2] and s[0]!=0:
            seam[i] = min(abs(ct),abs(cm),abs(cb))
        else:
            seam[i] = 0.0
    return np.array(cols), seam

def score(path):
    img = np.asarray(Image.open(path)).astype(np.float64)
    cols, seam = seam_profile(img)
    v = seam[~np.isnan(seam)]; cc = cols[~np.isnan(seam)]
    if not len(v): return {"max":0.0,"p95":0.0,"n_gt1_5":0,"worst_col":-1}
    return {"max":round(float(v.max()),1),"p95":round(float(np.percentile(v,95)),2),
            "sum":round(float(v.sum()),1),"n_gt1_5":int((v>1.5).sum()),
            "worst_col":int(cc[int(np.argmax(v))])}

if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]: print(p.split('/')[-1], score(p))
