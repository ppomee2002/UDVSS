#!/bin/bash
# int8 cache-shrink retries (user request): the int8 arm ran the pre-fallback code.
cd /home/dev_path/experiments/memfootprint-sq8/ceph
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
ARM=int8 RTAG=main STEPS=2000000 RESUME_N=2000000 RESUME_PLAN="4G:quarter,2G:quarter" $PY -u scripts/run_arm.py
ARM=int8 RTAG=main STEPS=1000000 CAPS=2G HALF_CAPS=2G RESUME_N=0 $PY -u scripts/run_arm.py
