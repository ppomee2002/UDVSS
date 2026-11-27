#!/bin/bash
# teardown.sh <arm> : stop this arm's OSD, mon and mgr, then delete its cluster
# dir (64 GiB device). Refuses any path outside /vstart-extra/sd-*.
source /home/dev_path/experiments/scale-deep/ceph/scripts/bench_env.sh
ARM=${1:?usage: teardown.sh <arm>}
load_workload "$ARM"
case "$WL_CLUSTER" in /vstart-extra/sd-*) ;; *) die "refusing to delete $WL_CLUSTER";; esac
echo "=== teardown $ARM ($WL_CLUSTER) ==="
stop_osd "$WL_CLUSTER" || die "osd would not stop"
for d in ceph-mgr ceph-mon; do
  p=$(cluster_daemon_pid "$WL_CLUSTER" $d) || continue
  kill "$p"; for i in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -0 "$p" 2>/dev/null && kill -9 "$p"
  echo "  stopped $d pid $p"
done
for d in crimson-osd ceph-mgr ceph-mon; do
  for p in $(pgrep -x $d); do grep -qa "$WL_CLUSTER/ceph.conf" /proc/$p/cmdline && die "$d $p still running"; done
done
# keep the small text artefacts (conf, osd log tail) next to the results
mkdir -p "$BENCH_ROOT/logs/clusters/$ARM"
cp "$WL_CLUSTER/ceph.conf" "$BENCH_ROOT/logs/clusters/$ARM/" 2>/dev/null
tail -c 2000000 "$WL_CLUSTER/out/osd.0.log" > "$BENCH_ROOT/logs/clusters/$ARM/osd.0.log.tail" 2>/dev/null
rm -rf "$WL_CLUSTER"
echo "  removed $WL_CLUSTER; free on /home/dev_path: $(df -h /home/dev_path | awk 'NR==2{print $4}')"
