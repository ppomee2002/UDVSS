#!/bin/bash
# Usage: netctr.sh setup | snap FILE | <file-prefix> begin|end
# Byte/packet counters for every non-loopback TCP packet to/from the OSD
# port range on 109 = all client<->OSD traffic (OSD->mon/mgr connections use
# ephemeral source ports and are not counted; OSD<->OSD on 109 goes via lo).
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
snap() {
  r109 "sudo nft -j list counters table inet sqn" | python3 -c '
import json,sys,time
d={}
for o in json.load(sys.stdin)["nftables"]:
    c=o.get("counter")
    if c: d[c["name"]]={"bytes":c["bytes"],"packets":c["packets"]}
d["t"]=time.time(); print(json.dumps(d))' > "$1"
}
case "$1" in
  setup)
    r109 "sudo nft delete table inet sqn 2>/dev/null; sudo nft -f -" <<EOF
table inet sqn {
  counter osd_rx {}
  counter osd_tx {}
  chain in { type filter hook input priority -10; policy accept;
    iifname != "lo" tcp dport $OSD_PORT_MIN-$OSD_PORT_MAX counter name osd_rx
  }
  chain out { type filter hook output priority -10; policy accept;
    oifname != "lo" tcp sport $OSD_PORT_MIN-$OSD_PORT_MAX counter name osd_tx
  }
}
EOF
    ;;
  snap) snap "$2" ;;
  *) snap "$1.$2.json" ;;
esac
