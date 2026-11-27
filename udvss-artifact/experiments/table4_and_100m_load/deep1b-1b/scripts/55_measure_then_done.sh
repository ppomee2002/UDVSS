#!/bin/bash
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
bash $S/50_measure.sh && log "=== 1B ALL DONE ===" || log "FATAL: 50 failed"
