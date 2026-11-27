"""Decode /proc/net/tcp + /proc/net/tcp6 read from inside a pod (stdin).

No tools are needed in the Milvus image -- `cat` is enough -- which makes this the
authoritative socket check for the loopback arm. ss/tcpdump in an ephemeral
container are corroboration.

  cat /proc/net/tcp /proc/net/tcp6 | proctcp.py listen
  cat /proc/net/tcp /proc/net/tcp6 | proctcp.py established 21133

`established PORT` splits the netns' ESTABLISHED sockets on PORT into
  outbound  remote port == PORT  (this netns is the CLIENT: the delegator)
  inbound   local  port == PORT  (this netns is the SERVER: the worker)
and prints VERDICT OK only if there is at least one outbound socket and every
outbound socket's peer is 127.0.0.1.
"""
from __future__ import annotations

import socket
import struct
import sys
from typing import Iterator, Tuple

LISTEN, ESTABLISHED = "0A", "01"


def _addr(hexaddr: str) -> Tuple[str, int]:
    ip_hex, port_hex = hexaddr.split(":")
    port = int(port_hex, 16)
    if len(ip_hex) == 8:
        return socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(ip_hex, 16))), port
    raw = b"".join(struct.pack("<I", int(ip_hex[i:i + 8], 16)) for i in range(0, 32, 8))
    ip = socket.inet_ntop(socket.AF_INET6, raw)
    if ip.startswith("::ffff:") and "." in ip:
        ip = ip[7:]
    return ip, port


def rows(text: str) -> Iterator[Tuple[Tuple[str, int], Tuple[str, int], str]]:
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or f[0] == "sl":
            continue
        yield _addr(f[1]), _addr(f[2]), f[3]


def is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "listen"
    text = sys.stdin.read()
    if mode == "listen":
        ports = sorted({l[1] for l, _, st in rows(text) if st == LISTEN})
        print("listening:", " ".join(map(str, ports)))
        return 0
    port = int(sys.argv[2])
    out_, in_ = [], []
    for l, r, st in rows(text):
        if st != ESTABLISHED:
            continue
        if r[1] == port:
            out_.append((l, r))
        elif l[1] == port:
            in_.append((l, r))
    print(f"outbound to :{port} (delegator -> worker): {len(out_)}")
    for l, r in out_:
        print(f"  {l[0]}:{l[1]} -> {r[0]}:{r[1]}  {'loopback' if is_loopback(r[0]) else 'NOT LOOPBACK'}")
    print(f"inbound on :{port} (peers dialing the worker): {len(in_)}")
    for l, r in in_:
        print(f"  {r[0]}:{r[1]} -> {l[0]}:{l[1]}  {'loopback' if is_loopback(r[0]) else 'via eth0'}")
    bad = [r for _, r in out_ if not is_loopback(r[0])]
    if not out_:
        print("VERDICT FAIL no delegator->worker connection (run a query first)")
        return 1
    if bad:
        print(f"VERDICT FAIL {len(bad)} delegator->worker connection(s) not on 127.0.0.1")
        return 1
    print("VERDICT OK every delegator->worker connection is 127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
