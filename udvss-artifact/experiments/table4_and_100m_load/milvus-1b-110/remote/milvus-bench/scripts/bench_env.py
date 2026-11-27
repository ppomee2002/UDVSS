"""Shared config for the Milvus side of the workload-generality comparison.

Mirrors experiments/workload-bench: same seven datasets, same ground truth
files, same fixed batch of 1024, same C ladder, same arg-max-QPS choice of C,
same fresh repeats for the finals.  What varies here is ef, not M -- the two are
NOT presented as corresponding values; only the resulting recall/QPS planes are
compared.
"""
import os, subprocess

URI = os.environ.get("MVB_URI", "http://127.0.0.1:19530")
ROOT = "/home/dev_path/milvus-bench"
DATASET_DIR = "/home/dev_path/datasets"

# Host side (the Docker daemon and the container live on the host, not in dev1).
# 110 (2026-10-28): runs inside the client container on 110; host commands run locally
# (Milvus volume mounted read-only at /vol, host cgroupfs at /sys/fs/cgroup)
SSH = ["sh", "-c"]
HOST_VOLUMES = "/vol"

# --- fixed conditions -------------------------------------------------------
NQ = 1024              # mirrors Ceph's B=1024 (query vectors per request)
TOPK = 10
METRIC = "L2"
# Index family is chosen by the environment so the IVF sweep can reuse every
# other condition unchanged. MVB_INDEX_TYPE=IVF_FLAT switches the build params,
# the knob (ef -> nprobe) and the results directory together; nothing else moves.
#
# Qdrant and Vespa have no IVF at all (HNSW + exact only), so an IVF run is a
# Milvus-internal comparison, not a three-engine one.
INDEX_TYPE = os.environ.get("MVB_INDEX_TYPE", "HNSW")
# Server defaults, confirmed on this deployment by index-size fingerprinting:
# an index built with no params is byte-for-byte the size of M=30 (122.3 MB on
# 200k x 96), while M=16 gives 100.9 MB and M=18 gives 104.0 MB.  Pinned
# explicitly so the condition is reproducible rather than inherited.
INDEX_PARAMS = {"M": 30, "efConstruction": 360}
KNOB_KEY = "ef"                       # the search-time knob's name in search_params
RESULTS = f"{ROOT}/results"
if INDEX_TYPE == "IVF_FLAT":
    # nlist 1024 is what the Motivation-2 nprobe regression used on this box
    # (~sqrt(N) for the 1M datasets), so the two are directly comparable.
    INDEX_PARAMS = {"nlist": int(os.environ.get("MVB_NLIST", 1024))}
    KNOB_KEY = "nprobe"
    RESULTS = f"{ROOT}/results_ivf"
NUM_SHARDS = 1
REPLICA_NUMBER = 1

# --- variables --------------------------------------------------------------
DATASETS = ["nytimes", "deep1m", "msspacev1m", "glove100", "sift1m", "cohere1m", "gist1m"]
C_LADDER = [10, 20, 40, 60, 80]        # same ladder as Phase 1 on the Ceph side
EF_TUNING = [64, 128]                  # C is tuned at these two, as Ceph tuned M=16/32
EF_CURVE = [10, 16, 32, 64, 128, 256, 512, 1024]   # ef must be >= topk
if INDEX_TYPE == "IVF_FLAT":
    # nprobe, not ef: how many of the nlist posting lists are scanned. 1..nlist,
    # and nprobe == nlist is exhaustive. Two mid-range points for the C tuning,
    # nine for the curve, mirroring the HNSW shape.
    EF_TUNING = [16, 64]
    # up to nlist: nprobe == nlist is exhaustive (recall 1.0). 256 tops out at
    # recall 0.908 on nytimes while the HNSW curve reaches 0.975, so without 512
    # and 1024 the two index families cannot be compared at matched recall.
    EF_CURVE = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

def collection_name(dataset):
    return f"wg_{dataset}"

def queries_for(c):
    """Ceph's rule: rounds = ceil(C*batch*4/query_limit), so queries = C*batch*4."""
    return c * NQ * 4

def host(cmd, timeout=120):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def host_df_gb():
    out = host("df -P /vol | awk 'NR==2{print $4}'")
    return int(out) / 1048576 if out else float("nan")

def host_volume_gb(which="minio"):
    out = host(f"du -sk {HOST_VOLUMES}/{which} 2>/dev/null | cut -f1")
    return int(out) / 1048576 if out else 0.0

_CG = None
def milvus_cgroup():
    """Path of the standalone container's cgroup, found by its cpuset (0-7).

    docker is not reachable from dev1, so the container is identified by the one
    property this experiment sets: it is the only cgroup pinned to 0-7.
    """
    global _CG
    if _CG is None:
        _CG = host(
            "for f in $(find /sys/fs/cgroup -maxdepth 4 -name cpuset.cpus.effective 2>/dev/null); do "
            "  [ \"$(cat $f 2>/dev/null)\" = '" + os.environ.get("MVB_CPUSET", "0-7") + "' ] && dirname $f; done | head -1")
    return _CG

def milvus_cpu_usec():
    cg = milvus_cgroup()
    if not cg:
        return None
    out = host(f"awk '$1==\"usage_usec\"{{print $2}}' {cg}/cpu.stat 2>/dev/null")
    return int(out) if out else None
