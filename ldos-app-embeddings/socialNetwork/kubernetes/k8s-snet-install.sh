#!/usr/bin/env bash

set -euo pipefail # Exit early on errors

# This script prepares a 5-node Kubernetes cluster for the social network
# benchmark.
# - Current machine is the control-plane node.
# - The 4 worker nodes are the same as in Docker Swarm experiment script.

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

# Path variables
ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
SOCIAL_DIR="${ROOT_DIR}/socialNetwork"
WRK2_DIR="${ROOT_DIR}/wrk2"

# Remote worker nodes (control node is the current local machine)
WORKER_NODES=(
  "c220g5-111219.wisc.cloudlab.us"  # node1
  "c220g5-111226.wisc.cloudlab.us"  # node2
  "c220g5-111205.wisc.cloudlab.us"  # node3
  "c220g5-111228.wisc.cloudlab.us"  # node4
)

# Constants
K8S_VERSION="${K8S_VERSION:-1.30.0-00}" # Kubernetes version to be used
POD_CIDR="${POD_CIDR:-10.244.0.0/16}" # Pod CIDR (range of IP addresses)
REMOTE_APP_DIR="~/socialNetwork" # Path where project will be copied on remote
INSTALL_PREREQS=false # Flag for whether to attempt installing k8s on remote
FORCE_RESET=false # Forcibly tear down and recreate cluster even if it exists

# Log every bash command run for debugging purposes
log() { echo "[k8s-snet-install] $*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2; exit 1;
  };
}

# Ensure that prereqs are met for installation
ensure_local_prereqs() {
  need_cmd ssh
  need_cmd scp
}

# No TTY, force publickey, non-interactive (fail instead of hanging)
_ssh_opts() {
  printf '%s' "-T -i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes \
    -o PreferredAuthentications=publickey \
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=120"
}

# Command to SSH into provided host
ssh_cmd() {
  local host="$1"; shift
  log "SSH -> ${host}: $*"
  ssh $(_ssh_opts) "${SSH_USER}@${host}" "$@"
}

# Copy file from src to dest
scp_to() {
  local src="$1" dst_host="$2" dst_path="$3"
  scp -i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -r "$src" "${SSH_USER}@${dst_host}:$dst_path"
}

# Install kubernetes prereqs on remote host
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
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key \
    | sudo gpg --dearmor --batch --yes --no-tty -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
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

# Check if an existing cluster is healthy (API reachable, at least one ready node)
cluster_healthy() {
  if ! command -v kubectl >/dev/null 2>&1; then
    return 1
  fi
  if ! kubectl version --request-timeout='5s' >/dev/null 2>&1; then
    return 1
  fi
  if kubectl get nodes >/dev/null 2>&1 && \
     kubectl get nodes | awk 'NR>1 && $2=="Ready" {found=1} END {exit !found}'; then
    return 0
  fi
  return 1
}

# Reset Kubernetes state on a node so kubeadm init/join can be re-run cleanly
reset_node_remote() {
  local host="$1"
  log "Resetting Kubernetes state on $host"
  ssh_cmd "$host" "bash -s" <<'REMOTE'
set -eux
sudo kubeadm reset -f || true
sudo systemctl stop kubelet || true
sudo systemctl stop containerd || true
sudo rm -rf /etc/cni/net.d /var/lib/cni /var/lib/kubelet /etc/kubernetes /var/lib/etcd || true
sudo systemctl start containerd || true
sudo systemctl start kubelet || true
REMOTE
}

# Initialize Kubernetes control plane on the local node (manager)
init_control_plane() {
  log "Initializing control-plane on local node"
  sudo kubeadm init --pod-network-cidr="${POD_CIDR}" || true

  # Set up Kubernetes config
  mkdir -p "$HOME/.kube"
  sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
  sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"

   # Ensure non-root can also read the admin kubeconfig
   # (for kubectl calls in this script)
  sudo chmod 644 /etc/kubernetes/admin.conf || true

  # Wait for API server to respond before installing CNI (~1 min timeout)
  log "Waiting for API server to become reachable"
  # Allow up to ~5 minutes for the control-plane components to stabilize
  for _ in $(seq 1 30); do
    if KUBECONFIG="$HOME/.kube/config" kubectl version --request-timeout='5s' >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done

  # Install flannel CNI for pod networking (with retry)
  log "Applying Flannel CNI manifest"
  for _ in $(seq 1 10); do
    if KUBECONFIG="$HOME/.kube/config" kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/master/Documentation/kube-flannel.yml --validate=false; then
      break
    fi
    log "Flannel apply failed, retrying in 5s..."
    sleep 5
  done
}

# Join command for worker nodes
get_join_command() {
  kubeadm token create --print-join-command 2>/dev/null || kubeadm token create --print-join-command
}

# Joins the given remote host to the Kubernetes cluster
join_worker_remote() {
  local host="$1" join_cmd="$2"
  log "Joining worker $host to cluster"
  ssh_cmd "$host" "sudo $join_cmd"
}

# Wait for all nodes in the cluster to be ready for use (~1 min timeout)
wait_for_nodes_ready() {
  log "Waiting for all nodes to be Ready"
  for _ in $(seq 1 12); do
    if kubectl get nodes 2>/dev/null | awk 'NR>1 {print $2}' | grep -qvx 'Ready'; then
      sleep 5
    else
      kubectl get nodes
      return 0
    fi
  done
  log "Warning: not all nodes reached Ready state within timeout"
  kubectl get nodes || true
}

# Deploy the Kubernetes configs for all microservices from all.yaml
deploy_social_network_manifests() {
  log "Deploying social network Kubernetes manifests"
  local ROOT
  ROOT=$(cd $(pwd)/../../.. && pwd)
  kubectl apply -f "${ROOT}/socialNetwork/kubernetes/all.yaml"
}

# Run necessary steps for installation and setup of k8s cluster
main() {
  ensure_local_prereqs

  # Start the SSH agent
  eval "$(ssh-agent -s)"
  ssh-add "$SSH_KEY"

  # Only install Kubernetes prereqs if flag set to true
  if [ "${INSTALL_PREREQS}" = "true" ]; then
    # Prepare remote worker nodes
    for n in "${WORKER_NODES[@]}"; do
      install_k8s_prereqs_remote "$n"
    done

    # Prepare local control-plane node
    install_k8s_prereqs_remote "127.0.0.1"
  fi

  # If an existing healthy cluster is present, just deploy manifests and exit
  if [ -f /etc/kubernetes/admin.conf ]; then
    mkdir -p "$HOME/.kube"
    sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config" 2>/dev/null || true
    sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config" 2>/dev/null || true
  fi
  if cluster_healthy; then
    log "Existing Kubernetes cluster detected and healthy; skipping kubeadm init/join."
    deploy_social_network_manifests
    log "Social network manifests applied on existing cluster."
    exit 0
  fi

  # If a cluster appears to exist but is unhealthy, do NOT auto-reset it
  # unless the user explicitly opts in via FORCE_RESET=true. This prevents
  # accidentally tearing down a running cluster when re-running the script.
  if [ -f /etc/kubernetes/admin.conf ] && [ "${FORCE_RESET}" != "true" ]; then
    log "Existing Kubernetes cluster config detected but not healthy."
    log "Leaving cluster state untouched. To rebuild from scratch, re-run with FORCE_RESET=true."
    exit 1
  fi

  # No existing healthy cluster (or explicit FORCE_RESET): reset all nodes
  # and perform fresh init/join.
  log "No healthy cluster detected; resetting all nodes and performing fresh initialization."
  reset_node_remote "127.0.0.1"
  for n in "${WORKER_NODES[@]}"; do
    reset_node_remote "$n"
  done

  # Initialize cluster on control-plane
  init_control_plane

  # Join workers
  local join_cmd
  join_cmd="$(get_join_command)"
  for w in "${WORKER_NODES[@]}"; do
    join_worker_remote "$w" "$join_cmd"
  done

  # Wait and deploy microservice Kubernetes configs
  wait_for_nodes_ready
  deploy_social_network_manifests

  log "Kubernetes social network cluster setup complete."
}

main "$@"
