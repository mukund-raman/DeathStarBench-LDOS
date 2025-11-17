#!/usr/bin/env bash

set -euo pipefail

# This script prepares a 5-node Kubernetes cluster for the social network benchmark.
# - Current machine is the control-plane node.
# - The 4 worker nodes are the same as in the Docker Swarm experiment script.

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

WORKER_NODES=(
  "c220g5-111219.wisc.cloudlab.us"  # node1
  "c220g5-111226.wisc.cloudlab.us"  # node2
  "c220g5-111205.wisc.cloudlab.us"  # node3
  "c220g5-111228.wisc.cloudlab.us"  # node4
)

K8S_VERSION="${K8S_VERSION:-1.30.0-00}"
POD_CIDR="${POD_CIDR:-10.244.0.0/16}"
REMOTE_APP_DIR="~/socialNetwork"

log() { echo "[k8s-snet-install] $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }; }

ensure_local_prereqs() {
  need_cmd ssh
  need_cmd scp
}

_ssh_opts() {
  printf '%s' "-T -i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes \
    -o PreferredAuthentications=publickey \
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=120"
}

ssh_cmd() {
  local host="$1"; shift
  log "SSH -> ${host}: $*"
  ssh $(_ssh_opts) "${SSH_USER}@${host}" "$@"
}

scp_to() {
  local src="$1" dst_host="$2" dst_path="$3"
  scp -i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -r "$src" "${SSH_USER}@${dst_host}:$dst_path"
}

install_k8s_prereqs_remote() {
  local host="$1"
  log "Installing container runtime and Kubernetes binaries on $host"
  ssh_cmd "$host" "bash -s" <<'REMOTE'
set -eux

disable_swap() {
  sudo swapoff -a || true
  sudo sed -i.bak '/ swap / s/^/#/' /etc/fstab || true
}

setup_kernel_modules() {
  sudo modprobe overlay || true
  sudo modprobe br_netfilter || true
  cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
  cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
  sudo sysctl --system
}

install_containerd() {
  sudo apt-get update -y
  sudo apt-get install -y containerd
  sudo mkdir -p /etc/containerd
  containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
  sudo systemctl restart containerd
  sudo systemctl enable containerd
}

install_kube_tools() {
  sudo apt-get update -y
  sudo apt-get install -y apt-transport-https ca-certificates curl
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /" | sudo tee /etc/apt/sources.list.d/kubernetes.list
  sudo apt-get update -y
  sudo apt-get install -y kubelet kubeadm kubectl
  sudo apt-mark hold kubelet kubeadm kubectl
}

disable_swap
setup_kernel_modules
install_containerd
install_kube_tools
REMOTE
}

init_control_plane() {
  log "Initializing control-plane on local node"
  sudo kubeadm init --pod-network-cidr="${POD_CIDR}" || true

  mkdir -p "$HOME/.kube"
  sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
  sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"

  # Install flannel CNI for pod networking
  kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/master/Documentation/kube-flannel.yml
}

get_join_command() {
  kubeadm token create --print-join-command 2>/dev/null || kubeadm token create --print-join-command
}

join_worker_remote() {
  local host="$1" join_cmd="$2"
  log "Joining worker $host to cluster"
  ssh_cmd "$host" "sudo $join_cmd"
}

wait_for_nodes_ready() {
  log "Waiting for all nodes to be Ready"
  for _ in $(seq 1 60); do
    if kubectl get nodes 2>/dev/null | awk 'NR>1 {print $2}' | grep -qv 'Ready'; then
      sleep 5
    else
      kubectl get nodes
      return 0
    fi
  done
  log "Warning: not all nodes reached Ready state within timeout"
  kubectl get nodes || true
}

deploy_social_network_manifests() {
  log "Deploying social network Kubernetes manifests"
  local ROOT
  ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
  kubectl apply -f "${ROOT}/socialNetwork/kubernetes/all.yaml"
}

main() {
  ensure_local_prereqs

  # Prepare remote worker nodes
  for n in "${WORKER_NODES[@]}"; do
    install_k8s_prereqs_remote "$n"
  done

  # Prepare local control-plane node
  install_k8s_prereqs_remote "127.0.0.1"

  # Initialize cluster on control-plane
  init_control_plane

  # Join workers
  local join_cmd
  join_cmd="$(get_join_command)"
  for w in "${WORKER_NODES[@]}"; do
    join_worker_remote "$w" "$join_cmd"
  done

  wait_for_nodes_ready
  deploy_social_network_manifests

  log "Kubernetes social network cluster setup complete."
}

main "$@"

