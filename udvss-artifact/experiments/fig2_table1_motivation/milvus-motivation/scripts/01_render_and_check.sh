#!/usr/bin/env bash
# Read-only. Renders the chart locally and asserts the findings in docs/01_findings.md
# against YOUR values file. Installs nothing.
set -uo pipefail
: "${MM_ROOT:?source env.sh first}"

OUT="${1:-$MM_RESULTS/rendered/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
R="$OUT/rendered.yaml"

echo "rendering $MM_CHART:$MM_CHART_VERSION with $MM_VALUES ..."
helm template "$MM_RELEASE" "$MM_CHART" \
  --version "$MM_CHART_VERSION" \
  --namespace "$MM_NS" \
  -f "$MM_VALUES" \
  > "$R" || { echo "helm template FAILED"; exit 1; }
echo "  -> $R  ($(grep -c '^kind:' "$R") objects)"

py() { python3 - "$R" "$@"; }

echo
echo "== objects by kind/component =============================================="
python3 - "$R" <<'PY'
import sys, re, collections
txt = open(sys.argv[1]).read()
docs = [d for d in txt.split("\n---\n") if d.strip()]
rows = []
for d in docs:
    kind = re.search(r'^kind:\s*(\S+)', d, re.M)
    name = re.search(r'^\s{2}name:\s*(\S+)', d, re.M)
    comp = re.search(r'^\s+component:\s*"?([\w-]+)"?', d, re.M)
    repl = re.search(r'^\s{2}replicas:\s*(\d+)', d, re.M)
    node = re.search(r'nodeSelector:\s*\n(?:\s+.*\n)*?\s+milvus-role:\s*(\S+)', d)
    if not kind:
        continue
    rows.append((kind.group(1), name.group(1) if name else "-",
                 comp.group(1) if comp else "-",
                 repl.group(1) if repl else "-",
                 node.group(1) if node else "-"))
w = [max(len(r[i]) for r in rows + [("KIND","NAME","COMPONENT","REPL","NODE-ROLE")]) for i in range(5)]
hdr = ("KIND","NAME","COMPONENT","REPL","NODE-ROLE")
print("  " + "  ".join(h.ljust(w[i]) for i,h in enumerate(hdr)))
for r in sorted(rows):
    print("  " + "  ".join(str(c).ljust(w[i]) for i,c in enumerate(r)))
PY

echo
echo "== assertions ============================================================="
fail=0
assert_absent() { if grep -q "$1" "$R"; then printf '  \033[31mFAIL\033[0m %s\n' "$2"; fail=1; else printf '  \033[32mOK\033[0m   %s\n' "$2"; fi; }
assert_present() { if grep -q "$1" "$R"; then printf '  \033[32mOK\033[0m   %s\n' "$2"; else printf '  \033[31mFAIL\033[0m %s\n' "$2"; fail=1; fi; }

# F1/F2 -- woodpecker must be embedded on 2.6.23
if grep -q 'component: "woodpecker"' "$R"; then
  printf '  \033[31mFAIL\033[0m woodpecker StatefulSet IS rendered -> streaming.woodpecker.embedded is false.\n'
  printf '         On Milvus 2.6.23 this also emits woodpecker.storage.type: service, which 2.6.23\n'
  printf '         does not implement. Set streaming.woodpecker.embedded: true. (docs/01_findings.md F2)\n'
  fail=1
else
  printf '  \033[32mOK\033[0m   no woodpecker StatefulSet (expected: embedded WAL on 2.6.23)\n'
fi
assert_absent 'type: service'                 'no woodpecker storage.type=service (3.0-only)'
assert_present 'messageQueue: woodpecker'     'mq.type/messageQueue = woodpecker'
assert_absent  'quorumBufferPools'            'no woodpecker.client.quorum (3.0-only)'
assert_absent  'primaryResourceGroup'         'no streaming.primaryResourceGroup (3.0-only)'

# F3 -- pulsar must not be deployed
if grep -qE 'name: .*(pulsarv3|zookeeper|bookie|broker)' "$R"; then
  printf '  \033[31mFAIL\033[0m pulsarv3 subchart objects present -> set pulsarv3.enabled: false\n'; fail=1
else
  printf '  \033[32mOK\033[0m   no pulsar/zookeeper/bookkeeper objects\n'
fi

# F4 -- image pin
assert_present "milvusdb/milvus:$MM_IMAGE_TAG" "image pinned to $MM_IMAGE_TAG"
assert_absent  'milvusdb/milvus:v3\.'          'no v3.x milvus image leaked in'

# F12 -- the indexnode role does not exist in v2.6.23; DataNode builds indexes
if grep -q '"milvus", "run", "indexnode"' "$R"; then
  printf '  \033[31mFAIL\033[0m an IndexNode Deployment is rendered (args: milvus run indexnode).\n'
  printf '         Milvus v2.6.23 has no indexnode role -- cmd/components/ holds only cdc,\n'
  printf '         data_node, mix_coord, proxy, query_node, streaming_node. The pod will not\n'
  printf '         start. Set indexNode.enabled: false; DataNode does index building.\n'
  fail=1
else
  printf '  \033[32mOK\033[0m   no IndexNode Deployment (DataNode builds indexes on 2.6.x)\n'
fi
assert_present '"milvus", "run", "datanode"'   'DataNode Deployment present (it is the index builder)'

# separate coordinators must stay off -- MixCoordinator covers all of them
assert_absent  '"milvus", "run", "indexcoord"' 'no standalone IndexCoord (MixCoordinator covers it)'
assert_absent  '"milvus", "run", "rootcoord"'  'no standalone RootCoord (MixCoordinator covers it)'

# F7/F8 -- experiment-critical config
assert_present 'autoBalance: false'            'queryCoord.autoBalance disabled'
assert_present 'enableCompaction: false'       'dataCoord.enableCompaction disabled'

# placement
python3 - "$R" <<'PY'
import sys, re
txt = open(sys.argv[1]).read()
# indexnode is deliberately absent on 2.6.23 (F12) and is not in this map.
want = {"proxy": "proxy", "mixcoord": "proxy", "datanode": "proxy",
        "streamingnode": "streaming", "querynode": "query"}
docs = [d for d in txt.split("\n---\n") if d.strip()]
bad = 0
for d in docs:
    k = re.search(r'^kind:\s*(Deployment|StatefulSet)', d, re.M)
    c = re.search(r'^\s+component:\s*"?([\w-]+)"?', d, re.M)
    if not k or not c or c.group(1) not in want:
        continue
    role = re.search(r'milvus-role:\s*(\S+)', d)
    got = role.group(1) if role else None
    exp = want[c.group(1)]
    if got == exp:
        print(f"  \033[32mOK\033[0m   {c.group(1):<14} -> milvus-role={got}")
    else:
        print(f"  \033[31mFAIL\033[0m {c.group(1):<14} -> milvus-role={got} (expected {exp})")
        bad = 1
sys.exit(bad)
PY
[ $? -ne 0 ] && fail=1

echo
echo "== storage distribution (etcd + MinIO are the ONLY storage tiers) =========="
# Embedded Woodpecker has no PVC of its own: the WAL persists into the MinIO bucket,
# so there are exactly two tiers to verify and a woodpecker PVC would be a red flag.
if grep -q 'woodpecker-storage' "$R"; then
  printf '  \033[31mFAIL\033[0m a woodpecker PVC is rendered -- embedded WAL must not claim storage\n'; fail=1
else
  printf '  \033[32mOK\033[0m   no woodpecker PVC (embedded WAL persists into the MinIO bucket)\n'
fi
python3 - "$R" <<'STORAGEPY'
import sys, re
txt = open(sys.argv[1]).read()
docs = [d for d in txt.split("\n---\n") if d.strip()]
for tier in ("etcd", "minio"):
    found = False
    for d in docs:
        if not re.search(r'^kind:\s*StatefulSet', d, re.M):
            continue
        name = re.search(r'^\s{2}name:\s*(\S+)', d, re.M)
        if not name or tier not in name.group(1):
            continue
        found = True
        rep = re.search(r'^\s{2}replicas:\s*(\d+)', d, re.M)
        size = re.search(r'storage:\s*(\S+)', d)
        sc = re.search(r'storageClassName:\s*"?([\w-]+)"?', d)
        anti = "hard" if ("required" in d and "podAntiAffinity" in d) else (
               "soft" if ("preferred" in d and "podAntiAffinity" in d) else "none")
        print(f"  {tier:<6} sts={name.group(1):<30} replicas={rep.group(1) if rep else '?':<3} "
              f"pvc={size.group(1) if size else '-':<6} sc={sc.group(1) if sc else '-':<12} "
              f"anti-affinity={anti}")
    if not found:
        print(f"  {tier:<6} NO StatefulSet rendered -- storage tier missing")
STORAGEPY

echo
echo "== every PVC that will be created =========================================="
grep -B4 -A8 'kind: PersistentVolumeClaim' "$R" | grep -E 'name:|storage:|storageClassName:' | sed 's/^/  /'
grep -A30 'volumeClaimTemplates' "$R" | grep -E '^\s+name:|storage:|storageClassName' | sed 's/^/  (sts) /'

echo
[ "$fail" -ne 0 ] && { echo "RENDER CHECK FAILED"; exit 1; }
echo "RENDER CHECK OK -- rendered manifest kept at $R"
