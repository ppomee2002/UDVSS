#!/bin/bash
# auto-resume the Vespa feed after the coding test (ends before 16:30 KST = 07:30Z)
until [ "$(date -u +%H%M)" -ge 0730 ]; do sleep 20; done
bash /home/dev_path/experiments/deep1b-1b/scripts/resume_after_exam.sh
