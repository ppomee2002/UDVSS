#!/usr/bin/env bash
# kubectl on the k3s control plane (node94) -- the kubeconfig stays there.
# stdin is forwarded, so `kc.sh apply -f -` works.
exec ssh -o BatchMode=yes -o ServerAliveInterval=30 -p 31234 ubuntu@k8s-gw \
  "KUBECONFIG=/home/ubuntu/.kube/config kubectl $(printf '%q ' "$@")"
