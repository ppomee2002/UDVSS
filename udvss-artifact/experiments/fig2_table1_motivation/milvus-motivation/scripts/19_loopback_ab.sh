#!/usr/bin/env bash
# Loopback A/B -- delegator->worker RPC over 127.0.0.1 vs over the pod network.
# Design, source citations and the verification contract: docs/07_loopback_ab_plan.md
#
#   ./19_loopback_ab.sh render      # read-only: build the LO manifest from the chart's live Deployments
#   ./19_loopback_ab.sh preflight   # read-only: the runtime preconditions the design depends on
#   ./19_loopback_ab.sh up          # MUTATES: switch the cluster to the LO arm
#   ./19_loopback_ab.sh verify      # read-only (+1 ephemeral debug container): ss / tcpdump / sessions
#   ./19_loopback_ab.sh ab          # MUTATES: interleaved L0 / L1b / LO rounds, rows -> runs_m2.csv
#   ./19_loopback_ab.sh restore     # MUTATES: remove LO, back to L0
#
# Arms (worker QueryNode on 109 in all three, same container resources):
#   L0   delegator 110 (own pod)   worker 109 (own pod)   delegator->worker crosses machines
#   L1b  delegator 109 (own pod)   worker 109 (own pod)   same host, still pod eth0/veth/CNI
#   LO   delegator 109 + worker 109 in ONE pod           127.0.0.1, never touches eth0
#
#   E2E(L1b) - E2E(LO) = pod-network (veth/bridge/CNI) part of the hop, same CPU
#   E2E(L0)  - E2E(LO) = whole hop incl. the machine crossing (delegator CPU differs: 110 vs 109)
set -uo pipefail
: "${MM_ROOT:?source env.sh and env.local.sh first}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
M2_SOURCE_ONLY=1 . "$HERE/15_motivation2.sh"

LB_ARMS="${LB_ARMS:-L0 L1b LO}"
LB_ROUNDS="${LB_ROUNDS:-2}"
LB_ROUND_OFFSET="${LB_ROUND_OFFSET:-0}"
LB_S=16; LB_CHUNKS=4            # S=16, N=1 (asserted by 07 inside row), M=4 -- fixed, not knobs
LOOP_QN_PORT="${MM_LOOP_QN_PORT:-21133}"
LOOP_QN_METRICS="${MM_LOOP_QN_METRICS_PORT:-9092}"
LOOP_DEPLOY="${MM_RELEASE}-snqn-loop"
LOOP_DIR="$MM_RESULTS/loopback"
LOOP_YAML="$LOOP_DIR/snqn-loop.yaml"
CLUSTER_DOMAIN="${MM_CLUSTER_DOMAIN:-cluster.local}"
LOOP_FQDN="qn-loop.${MM_NS}.svc.${CLUSTER_DOMAIN}"
DBG_IMAGE="${DBG_IMAGE:-nicolaka/netshoot:v0.13}"
mkdir -p "$LOOP_DIR"

k () { kubectl -n "$MM_NS" "$@"; }

loop_pod () {   # name of the Running, non-terminating LO pod, or empty
  k get pods -l component=snqn-loop \
    -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}{"|"}{.metadata.name}{"|"}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk -F'|' '$1=="" && $3=="Running"{print $2; exit}'
}
chart_pod () {  # $1 component
  k get pods -l "component=$1" \
    -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}{"|"}{.metadata.name}{"|"}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk -F'|' '$1=="" && $3=="Running"{print $2; exit}'
}

# The delegator's side of the netns: LO pod's streamingnode container, or the chart SN pod.
deleg_exec () {
  local p; p=$(loop_pod)
  if [ -n "$p" ]; then k exec "$p" -c streamingnode -- "$@"
  else k exec "$(chart_pod streamingnode)" -- "$@"; fi
}

# 15's wait_quiet reads component=streamingnode, which does not exist in LO.
wait_quiet () {
  local a b kbs
  for _ in $(seq 1 30); do
    a=$(deleg_exec sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    sleep 10
    b=$(deleg_exec sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    kbs=$(( (${b:-0} - ${a:-0}) / 10 / 1024 ))
    [ "$kbs" -lt 40 ] && { echo "  delegator netns idle ~${kbs} KB/s -- quiet"; return 0; }
  done
  echo "  !! never went quiet; the network columns of this block are suspect"
}

# ------------------------------------------------------------------ render
do_render () {
  say "render LO manifest from the chart's live streamingnode/querynode Deployments"
  $PY "$MM_ROOT/scripts/18_render_loopback.py" --out "$LOOP_YAML" \
      --qn-port "$LOOP_QN_PORT" --qn-metrics-port "$LOOP_QN_METRICS" \
      --cluster-domain "$CLUSTER_DOMAIN" --node-role query || die "render failed"
  echo "  review before 'up':  $LOOP_YAML"
}

# ------------------------------------------------------------------ preflight (read-only)
# Every item here is a precondition of docs/07 section 3. A FAIL means the design
# does not hold on this cluster as-is; do not run 'up'.
do_preflight () {
  local rc=0 qn sn mc v
  say "preflight (read-only)"
  qn=$(chart_pod querynode); sn=$(chart_pod streamingnode); mc=$(chart_pod mixcoord)
  [ -n "$qn" ] && [ -n "$sn" ] && [ -n "$mc" ] || die "need running querynode, streamingnode, mixcoord pods"

  echo "[1] image resolver order (hostAliases must win over DNS)"
  v=$(k exec "$qn" -- sh -c 'grep -E "^hosts:" /etc/nsswitch.conf 2>/dev/null || echo MISSING')
  echo "    querynode /etc/nsswitch.conf: $v"
  case "$v" in
    *"files"*"dns"*) echo "    OK   files before dns" ;;
    MISSING) echo "    WARN no nsswitch.conf: Go defaults are version-dependent -> rely on [verify] socket check"; rc=1 ;;
    *) echo "    FAIL dns before files: the FQDN would resolve to the pod IP, not 127.0.0.1"; rc=1 ;;
  esac

  echo "[2] cluster domain (FQDN = $LOOP_FQDN)"
  v=$(k exec "$mc" -- sh -c 'grep -E "^search" /etc/resolv.conf' 2>/dev/null)
  echo "    mixcoord resolv.conf: $v"
  echo "$v" | grep -q "svc.${CLUSTER_DOMAIN}" && echo "    OK" || { echo "    FAIL set MM_CLUSTER_DOMAIN"; rc=1; }

  echo "[3] mixcoord resolves service names through cluster DNS (getent present in image)"
  v=$(k exec "$mc" -- getent hosts "kubernetes.default.svc.${CLUSTER_DOMAIN}" 2>/dev/null)
  [ -n "$v" ] && echo "    OK   $v" || { echo "    FAIL getent/DNS unusable in mixcoord"; rc=1; }

  echo "[4] listeners of today's processes (the LO pod must not collide)"
  for p in "$sn" "$qn"; do
    echo "    $p:"
    k exec "$p" -- cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | $PY "$MM_ROOT/scripts/lib/proctcp.py" listen | sed 's/^/      /'
  done
  echo "    expected: streamingnode {22222, 21123, 9091}  querynode {21123, 9091}"
  echo "    LO moves the worker to {$LOOP_QN_PORT, $LOOP_QN_METRICS}. Any OTHER port listed on both is a collision -> FAIL."

  echo "[5] ephemeral debug containers (for ss/tcpdump in 'verify')"
  v=$(kubectl version -o json 2>/dev/null | $PY -c 'import json,sys; d=json.load(sys.stdin)["serverVersion"]; print(d["major"]+"."+d["minor"])')
  echo "    server $v (needs >= 1.25);  image $DBG_IMAGE must be pullable on $MM_NODE_QUERY"

  echo "[6] 109 headroom for LO = streamingnode + querynode requests on one node (same as L1b)"
  node_headroom "$MM_NODE_QUERY"
  [ "$rc" = 0 ] && echo "  PREFLIGHT OK" || echo "  PREFLIGHT HAS FAILURES -- read docs/07 section 3"
  return $rc
}

# ------------------------------------------------------------------ placement
lo_down () {
  if k get deploy "$LOOP_DEPLOY" >/dev/null 2>&1; then
    echo "  removing $LOOP_DEPLOY"
    k delete deploy "$LOOP_DEPLOY" --wait=true --timeout=300s >/dev/null
    for _ in $(seq 1 30); do [ -z "$(k get pods -l component=snqn-loop --no-headers 2>/dev/null)" ] && break; sleep 5; done
  fi
}

place_lo () {
  [ -f "$LOOP_YAML" ] || do_render
  say "---- placing LO   (delegator+worker in one pod on $MM_NODE_QUERY, 127.0.0.1)"
  # chart SN and QN to zero FIRST: two StreamingNodes would split the 16 channels,
  # two workers would split the 64 segments (N would no longer be 1).
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" -f "$VALUES" \
       --set streamingNode.replicas=0 --set queryNode.replicas=0 2>&1 | grep -E "^REVISION|Error" || true
  for _ in $(seq 1 60); do
    [ -z "$(k get pods -l 'component in (querynode,streamingnode)' --no-headers 2>/dev/null)" ] && break
    sleep 5
  done
  k apply -f "$LOOP_YAML" | sed 's/^/  /'
  local p="" i
  for i in $(seq 1 60); do
    p=$(loop_pod)
    if [ -n "$p" ] && [ "$(k get pod "$p" -o jsonpath='{.status.containerStatuses[*].ready}')" = "true true" ]; then break; fi
    p=""; sleep 10
  done
  [ -n "$p" ] || { echo "  !! LO pod never became Ready 2/2"; k get pods -l component=snqn-loop -o wide; return 1; }
  local node; node=$(k get pod "$p" -o jsonpath='{.spec.nodeName}')
  [ "$node" = "$MM_NODE_QUERY" ] || { echo "  !! LO pod on $node, want $MM_NODE_QUERY"; return 1; }
  ARM_PLACE="deleg=${node} worker=${node} transport=loopback"
  echo "  placed: $p on $node (2/2 Ready)"
  refresh_uri
}

place_arm () {   # $1 arm
  case "$1" in
    LO) place_lo ;;
    L0|L1b) lo_down; place "$1" "$VALUES" ;;
    *) echo "  !! unknown arm $1"; return 1 ;;
  esac
}

do_restore () {
  say "restoring L0"
  lo_down
  place L0 "$VALUES" || printf '\033[31m  !! RESTORE FAILED -- do not run other stages until placement is L0\033[0m\n'
}

# ------------------------------------------------------------------ verify
# Runs inside an ephemeral netshoot container that shares the LO pod's netns and,
# via --target, the streamingnode container's PID namespace (so ss -p can name the
# delegator process). @PORT@ is the worker's port.
dbg_script () {
  cat <<'DBG'
echo '== ss -tnp  delegator side (dport @PORT@)'; ss -tnp state established '( dport = :@PORT@ )'
echo '== ss -tn   worker side    (sport @PORT@)'; ss -tn  state established '( sport = :@PORT@ )'
timeout 30 tcpdump -nn -i lo   -w /tmp/lo.pcap   'tcp port @PORT@' 2>/dev/null &
timeout 30 tcpdump -nn -i eth0 -w /tmp/eth0.pcap 'tcp port @PORT@' 2>/dev/null &
wait
peers () { tcpdump -nn -r "$1" 2>/dev/null | awk '{split($3,a,"."); split($5,b,".");
  print a[1]"."a[2]"."a[3]"."a[4]" > "b[1]"."b[2]"."b[3]"."b[4]}' | sort | uniq -c | sort -rn | head; }
echo "== tcpdump -i lo   packets: $(tcpdump -nn -r /tmp/lo.pcap 2>/dev/null | wc -l)"; peers /tmp/lo.pcap
echo "== tcpdump -i eth0 packets: $(tcpdump -nn -r /tmp/eth0.pcap 2>/dev/null | wc -l)"; peers /tmp/eth0.pcap
DBG
}

burst () {   # $1 = collection, $2 = queries -- search traffic while a capture runs
  MM_URI="$MM_URI" $PY - "$1" "$2" <<'PYX' >/dev/null 2>&1
import os, sys, numpy as np
from pymilvus import MilvusClient
c = MilvusClient(uri=os.environ["MM_URI"])
v = np.random.rand(96).astype("float32").tolist()
for _ in range(int(sys.argv[2])):
    c.search(sys.argv[1], data=[v], limit=10, anns_field="vector", search_params={"ef": 64})
PYX
}

do_verify () {
  local coll="mm_${MM_DATASET}_s${LB_S}_c${LB_CHUNKS}" p ip mc out dbg rc=0
  p=$(loop_pod); mc=$(chart_pod mixcoord)
  [ -n "$p" ] || die "LO pod not running -- run 'up' first (verify is for the LO arm)"
  ip=$(k get pod "$p" -o jsonpath='{.status.podIP}')
  out="$LOOP_DIR/verify-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$out"
  say "verify LO transport   pod=$p ip=$ip  -> $out"

  echo "[a] what the worker advertised (etcd session address)"
  k logs "$p" -c querynode 2>/dev/null | grep -m1 "QueryNode init session" | tee "$out/session.txt" | sed 's/^/    /'
  grep -qF "${LOOP_FQDN}:${LOOP_QN_PORT}" "$out/session.txt" \
    && echo "    OK   ${LOOP_FQDN}:${LOOP_QN_PORT}" || { echo "    FAIL advertised address is not the loopback FQDN"; rc=1; }
  k logs "$p" -c streamingnode 2>/dev/null | grep -m1 "QueryNode init session" | sed 's/^/    delegator: /'

  echo "[b] name resolution per side"
  echo "    in pod (streamingnode): $(k exec "$p" -c streamingnode -- getent hosts "$LOOP_FQDN" 2>&1)"
  echo "    in mixcoord           : $(k exec "$mc" -- getent hosts "$LOOP_FQDN" 2>&1)"

  echo "[c] sockets from the netns' own /proc (no tools needed; authoritative)"
  burst "$coll" 50
  k exec "$p" -c streamingnode -- cat /proc/net/tcp /proc/net/tcp6 \
    | $PY "$MM_ROOT/scripts/lib/proctcp.py" established "$LOOP_QN_PORT" | tee "$out/proc_tcp.txt" | sed 's/^/    /'
  grep -q "^VERDICT OK" "$out/proc_tcp.txt" || rc=1

  echo "[d] ss -tnp + tcpdump -i lo / -i eth0 (ephemeral container $DBG_IMAGE, target=streamingnode)"
  dbg="dbg$(date +%H%M%S)"
  k debug "$p" --image="$DBG_IMAGE" --target=streamingnode --profile=netadmin -c "$dbg" \
      -- sh -c "$(dbg_script | sed "s/@PORT@/$LOOP_QN_PORT/g")" >/dev/null 2>&1 \
    || echo "    !! kubectl debug failed (k8s version / image pull / profile) -- [c] still stands"
  sleep 5; burst "$coll" 400      # 400 searches x 16 shards of delegator->worker RPCs inside the capture
  sleep 30
  k logs "$p" -c "$dbg" 2>/dev/null | tee "$out/ss_tcpdump.txt" | sed 's/^/    /'
  echo "    mixcoord pod IP for reference: $(k get pod "$mc" -o jsonpath='{.status.podIP}')"
  echo "    PASS criteria: lo packets >> 0 and scale with the burst; every eth0 peer is mixcoord;"
  echo "                   ss shows 127.0.0.1:* -> 127.0.0.1:$LOOP_QN_PORT owned by the milvus (streamingnode) pid"

  echo "[e] both metrics endpoints answer (scraped from here, as 08_bench does)"
  for port in 9091 "$LOOP_QN_METRICS"; do
    if $PY -c "import urllib.request,sys; urllib.request.urlopen('http://$ip:$port/healthz', timeout=5)" 2>/dev/null
    then echo "    OK   $ip:$port/healthz"; else echo "    FAIL $ip:$port/healthz"; rc=1; fi
  done
  [ "$rc" = 0 ] && echo "  VERIFY OK (see $out)" || echo "  VERIFY FAILED (see $out) -- LO rows are not valid"
  return $rc
}

# ------------------------------------------------------------------ A/B
stage_ab () {
  say "########## loopback A/B: $LB_ARMS   S=$LB_S N=1 M=$LB_CHUNKS   (trace OFF)"
  guard
  local coll="mm_${MM_DATASET}_s${LB_S}_c${LB_CHUNKS}" arm i rn rc=0
  trap 'do_restore' INT TERM
  [ -f "$LOOP_YAML" ] || do_render
  for i in $(seq 1 "$LB_ROUNDS"); do
    rn=$(( i + LB_ROUND_OFFSET ))
    say "##### round $rn  ($i / $LB_ROUNDS)"
    for arm in $LB_ARMS; do
      place_arm "$arm" || { rc=1; echo "  !! skipping $arm round $rn"; continue; }
      ensure_coll "$coll" "$LB_S" "$LB_CHUNKS"
      wait_queryable "$coll" || { rc=1; continue; }
      # LO rows are only valid if the transport is proven loopback in THIS placement.
      if [ "$arm" = LO ]; then do_verify > "$LOOP_DIR/verify_k${rn}.log" 2>&1 \
          || { rc=1; echo "  !! LO verify failed round $rn -- row skipped (see $LOOP_DIR/verify_k${rn}.log)"; continue; }
      fi
      wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"
      row "M2LB_${arm}_k${rn}" "$coll" "$LB_S" 1 "$LB_CHUNKS" \
          --output-fields "" \
          --notes "loopback A/B arm=$arm round=$rn ${ARM_PLACE:-}"
    done
  done
  trap - INT TERM
  do_restore
  echo "  report: $PY $MM_ROOT/scripts/20_report_loopback.py"
  return $rc
}

case "${1:-}" in
  render)    do_render ;;
  preflight) do_preflight ;;
  up)        guard; place_lo ;;
  verify)    do_verify ;;
  ab)        stage_ab ;;
  restore)   do_restore ;;
  *) die "usage: $0 {render|preflight|up|verify|ab|restore}" ;;
esac
