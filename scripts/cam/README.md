# CAM tools (Fusion -> UCCNC, Stepcraft M1000)

Read-only analysis and splitting scripts for Fusion-posted `.nc` programs. Built by Wesley on 2026-11-09 to 11-11.
Method and brief: vault `AI Teams/Business/Operations/CAM Analysis Method.md`.

- `split_by_tool.py SRC.nc OUTDIR`: splits a program into one file per tool for manual tool changes. Never modifies SRC; refuses a non-empty OUTDIR (except `superseded-*`). Verify with `verify_split2.py`.
- `cam_analyze.py PROGRAM.nc [--rapid MM_PER_MIN] [--grid MM] [--json OUT]`: per-operation distances, times, segment stats, Z/XY extents, retracts, entries, and a 2.5D height-map lower bound on air cutting.
- `sim_compare.py`, `sim_buckets.py`, `ledge_check.py`, `engage.py`, `radial_check.py`, `levels*.py`, `arc_centers.py`, `rebuild_old.py`: comparison and diagnostic helpers from the TrayDownLt re-post review. Some hard-code TrayDownLt paths; adapt before reuse.
