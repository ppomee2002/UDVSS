"""Cross-file consistency check for the milvus-motivation deliverables.

Read-only. Verifies that the S/N/M column contract, the SNM dataclass, the four
report tables, the docs and the reference YAML all agree. Run it after editing any
of them:  python3 scripts/99_consistency_check.py
"""
import ast, re, sys, os
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(R, "scripts")
fail = []

def ok(m):  print(f"  OK   {m}")
def bad(m): print(f"  FAIL {m}"); fail.append(m)

# ---- 1. columns produced by SNM.as_row() ------------------------------------
src = open(os.path.join(S, "lib/milvus_metrics.py")).read()
as_row = src[src.index("def as_row(self)"):src.index("def measure_snm")]
produced = set(re.findall(r'"([A-Za-z_0-9]+)":', as_row))

# ---- 2. columns set explicitly in 08_bench.py row dict -----------------------
bench = open(os.path.join(S, "08_bench.py")).read()
rowblk = bench[bench.index("    row = {"):bench.index("    for k in (\"proxy_e2e\"")]
explicit = set(re.findall(r'"([A-Za-z_0-9]+)":', rowblk))
bd_keys = set(re.findall(r'"([a-z_0-9]+)"', bench[bench.index('    for k in ("proxy_e2e"'):bench.index('    # ------------- persist') if '    # ------------- persist' in bench else bench.index("    write_csv(")]))
bd_cols = {f"{k}_ms" for k in bd_keys}

# ---- 3. RUNS_CSV_FIELDS ------------------------------------------------------
fields_src = bench.split("RUNS_CSV_FIELDS = [")[1].split("\n]")[0]
fields = [x for x in re.findall(r'"([^"]+)"', fields_src)]
fieldset = set(fields)

print("== column contract: SNM.as_row() -> RUNS_CSV_FIELDS ==")
missing = produced - fieldset
if missing: bad(f"as_row() emits columns absent from RUNS_CSV_FIELDS: {sorted(missing)}")
else: ok(f"all {len(produced)} as_row() columns are declared")

print("== column contract: explicit row keys -> RUNS_CSV_FIELDS ==")
missing = (explicit | bd_cols) - fieldset - {"label"}
if missing: bad(f"row keys absent from RUNS_CSV_FIELDS: {sorted(missing)}")
else: ok("every explicitly-set row key is declared")

print("== unfilled declared fields ==")
unfilled = fieldset - produced - explicit - bd_cols
if unfilled: bad(f"declared but never written: {sorted(unfilled)}")
else: ok("no declared field is left unwritten")

# ---- 4. columns 09_make_tables.py reads --------------------------------------
tables = open(os.path.join(S, "09_make_tables.py")).read()
read_cols = set(re.findall(r'(?:r\.get|f\(r,\s*)[\(\s]*["\']([A-Za-z_0-9]+)["\']', tables))
read_cols |= set(re.findall(r'f\(x,\s*"([A-Za-z_0-9]+)"', tables))
read_cols |= set(re.findall(r'\bf\(r,\s*f"([a-z_]+)\{', tables))
# templated: rx_kb_per_query_{k} / tx_kb_per_query_{k}
for k in ("proxy", "streaming", "query"):
    read_cols.discard("rx_kb_per_query_"); read_cols.discard("tx_kb_per_query_")
    read_cols.add(f"rx_kb_per_query_{k}"); read_cols.add(f"tx_kb_per_query_{k}")
read_cols = {c for c in read_cols if c not in ("rx_kb_per_query_", "tx_kb_per_query_")}
print("== 09_make_tables.py reads only declared columns ==")
unknown = read_cols - fieldset
if unknown: bad(f"09 reads columns that runs.csv never has: {sorted(unknown)}")
else: ok(f"all {len(read_cols)} columns read by 09 are declared")

# ---- 5. 07_probe_snm.py uses only existing SNM attributes --------------------
probe = open(os.path.join(S, "07_probe_snm.py")).read()
snm_attrs = set(re.findall(r'\bsnm\.([a-zA-Z_]+)', probe))
tree = ast.parse(src)
cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SNM")
declared = {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign)}
declared |= {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
print("== 07_probe_snm.py -> SNM attributes ==")
unknown = snm_attrs - declared
if unknown: bad(f"07 uses nonexistent SNM attrs: {sorted(unknown)}")
else: ok(f"all {len(snm_attrs)} SNM attributes used by 07 exist")

bench_attrs = set(re.findall(r'\bsnm\.([a-zA-Z_]+)', bench))
unknown = bench_attrs - declared
print("== 08_bench.py -> SNM attributes ==")
if unknown: bad(f"08 uses nonexistent SNM attrs: {sorted(unknown)}")
else: ok(f"all {len(bench_attrs)} SNM attributes used by 08 exist")

# ---- 6. stale vocabulary anywhere -------------------------------------------
STALE = ["actual_querynodes_participated", "actual_sealed_segments_searched_per_query",
         "actual_growing_segments_searched_per_query", "actual_sealed_segments_per_worker_rpc",
         "M_sealed_per_worker_rpc", "N_actual", "expected_N_from_segment_placement"]
print("== stale identifiers removed everywhere ==")
hits = []
SELF = os.path.basename(__file__)
for root, _, files in os.walk(R):
    if "results" in root or "__pycache__" in root: continue
    for fn in files:
        if not fn.endswith((".py", ".sh", ".md", ".yaml")): continue
        if fn == SELF: continue          # this file names the stale terms on purpose
        fp = os.path.join(root, fn)
        t = open(fp, errors="replace").read()
        for term in STALE:
            if term in t:
                hits.append(f"{os.path.relpath(fp, R)}: {term}")
if hits: bad("stale identifiers still present:\n      " + "\n      ".join(hits))
else: ok("none of the old N/M identifiers remain")

# ---- 7. definition wording present in all four prose files ------------------
print("== definition wording is stated in README + docs/02 + docs/03 + table2 note ==")
checks = {
    "README.md": ["per shard", "N_unique_total", "M_total_searched"],
    "docs/02_snm_semantics.md": ["per shard", "N_unique_total", "M_total_searched"],
    "docs/03_experiment_plan.md": ["actual_N_per_shard_avg", "actual_M_per_worker_per_shard"],
    "scripts/09_make_tables.py": ["per shard", "N_unique_total", "M_total_searched"],
    "scripts/07_probe_snm.py": ["PER SHARD", "N_unique_total", "M_total_searched"],
}
for f, terms in checks.items():
    t = open(os.path.join(R, f), errors="replace").read()
    miss = [x for x in terms if x not in t]
    if miss: bad(f"{f} missing: {miss}")
    else: ok(f"{f}")

# ---- 8. indexNode / woodpecker consistency ----------------------------------
print("== indexNode + woodpecker consistency ==")
import yaml
y = yaml.safe_load(open(os.path.join(R, "values/milvus-3node.reference.yaml")))
if y["indexNode"]["enabled"] is False: ok("reference YAML indexNode.enabled = false")
else: bad("reference YAML indexNode.enabled is not false")
if list(y["woodpecker"].keys()) == ["enabled"]: ok("reference YAML woodpecker has only `enabled`")
else: bad(f"reference YAML woodpecker still carries {list(y['woodpecker'].keys())}")
if y["streaming"]["woodpecker"]["embedded"] is True and y["streaming"]["woodpecker"]["storage"]["type"] == "minio":
    ok("streaming.woodpecker.embedded=true, storage.type=minio")
else: bad("streaming.woodpecker block wrong")
u = yaml.safe_load(y["extraConfigFiles"]["user.yaml"])
if "minSegmentNumRowsToEnableIndex" in u.get("indexCoord", {}).get("segment", {}): ok("minSegmentNumRowsToEnableIndex under indexCoord.segment")
else: bad("minSegmentNumRowsToEnableIndex not under indexCoord.segment")
if "minSegmentNumRowsToEnableIndex" not in u.get("dataCoord", {}).get("segment", {}): ok("not duplicated under dataCoord.segment")
else: bad("still present under dataCoord.segment")

for f, needles in {
    "scripts/01_render_and_check.sh": ['"milvus", "run", "indexnode"', "woodpecker-storage", "etcd", "minio"],
    "scripts/03_verify_placement.sh": ["component=indexnode", "woodpecker", "etcd", "app=minio"],
}.items():
    t = open(os.path.join(R, f)).read()
    miss = [n for n in needles if n not in t]
    if miss: bad(f"{f} missing checks for {miss}")
    else: ok(f"{f} checks indexnode + woodpecker PVC + etcd + minio")

print()
print("CONSISTENCY FAILED" if fail else "CONSISTENCY OK")
sys.exit(1 if fail else 0)
