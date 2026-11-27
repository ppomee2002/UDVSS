#!/bin/bash
# Copies experiment run/plot scripts verbatim (*.sh, *.py) into the release tree,
# grouped by paper figure/table. No content is modified. MANIFEST.tsv records
# source path and sha256 of every copied file.
set -e
DST=/home/dev_path/ceph-udvss-release/udvss-artifact/experiments
E=/home/dev_path/experiments; D=/home/dev_path
rm -rf $DST; mkdir -p $DST
printf 'group\tcopied_path\tsource_path\tsha256\n' > $DST/MANIFEST.tsv
copy() {  # group srcdir
  local g=$1 s=$2 name; name=$(basename $s)
  find $s -type f \( -name '*.sh' -o -name '*.py' \) -not -path '*/.venv/*' \
       -not -path '*/__pycache__/*' -not -name '*.bak*' | sort | while read f; do
    rel=${f#$s/}; mkdir -p "$DST/$g/$name/$(dirname "$rel")"; cp -p "$f" "$DST/$g/$name/$rel"
    printf '%s\t%s\t%s\t%s\n' "$g" "$g/$name/$rel" "$f" "$(sha256sum "$f" | cut -d' ' -f1)" >> $DST/MANIFEST.tsv
  done
}
copy common_plot            $D/lsh-selective-results-20260827
copy fig2_table1_motivation $D/milvus-motivation
copy fig2_table1_motivation $D/milvus-motivation-results-20260922
copy fig3_workload_scale    $E/paper-100m
copy fig3_workload_scale    $E/workload-scale-sq8
copy fig7_vsd_scaling       $E/osd-scale-deep
copy fig8_crush_vs_udvss    $E/crush-exp2
copy fig9_fanout            $E/ceph-s-qn-scaling
copy fig10_onode_budget     $E/latency-94
copy fig11_split_merge      $E/hier-lsh-1m
copy fig12_selective        $E/lsh-aligned-1m
copy table2_table3_memory   $E/memfootprint-sq8
copy table4_and_100m_load   $E/scale-deep
copy table4_and_100m_load   $E/deep1b-1b
copy table4_and_100m_load   $E/milvus-1b-110
echo "files: $(($(wc -l < $DST/MANIFEST.tsv)-1))"
