#!/bin/bash

set -euo pipefail

# =========================
# Configuration
# =========================

# Remote worker nodes
WORKER_NODES=(
  "clnode218.clemson.cloudlab.us"  # node1
  "clnode198.clemson.cloudlab.us"  # node2
  "clnode216.clemson.cloudlab.us"  # node3
  "clnode199.clemson.cloudlab.us"  # node4
  "clnode215.clemson.cloudlab.us"  # node5
)

SSH_USER="mkraman"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_TIMEOUT=600

# Flags
DO_CLEANUP_DEPS=false
DO_INSTALL_DEPS=false
DO_RESET_K8S=false
DO_CLUSTER=false
DO_DEPLOY_APP=false
DO_STOP_CLUSTER=false
DO_START_CLUSTER=false

# =========================
# Helper Functions
# =========================

# Logging function
log() { echo "[k8s-install] $*" >&2; }

# SSH options for non-interactive execution
_ssh_opts() {
    echo "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"
}

# Run command on a remote host
run_remote() {
    local host="$1"
    local cmd="$2"
    log "Running on $host: $cmd"
    ssh $(_ssh_opts) "$SSH_USER@$host" "$cmd"
}

# Run command locally
run_local() {
    local cmd="$1"
    log "Running locally: $cmd"
    eval "$cmd"
}

# Setup SSH Agent
setup_ssh_agent() {
    log "Setting up SSH agent..."
        eval "$(ssh-agent -s)"
    ssh-add "$SSH_KEY"
}

# Cleanup dependencies (Docker, K8s binaries)
cleanup_deps_node() {
    local cmd="
        sudo systemctl stop kubelet || true
        sudo systemctl stop docker.socket || true
        sudo systemctl stop docker || true
        sudo apt-mark unhold kubelet kubeadm kubectl || true
        sudo apt-get purge -y docker-ce docker-ce-cli containerd.io kubelet kubeadm kubectl || true
        sudo apt-get autoremove -y || true
        sudo rm -rf /etc/docker /var/lib/docker /var/lib/containerd || true
    "
    if [ "$1" == "localhost" ]; then
        run_local "$cmd"
    else
        run_remote "$1" "$cmd"
    fi
}

# Reset Kubernetes state
reset_k8s_node() {
    local cmd="
        sudo systemctl start docker containerd || true
        sudo kubeadm reset -f || true
        sudo systemctl stop kubelet || true
        sudo systemctl stop docker || true
        sudo rm -rf /etc/cni/net.d || true
        sudo rm -rf /var/lib/etcd || true
        sudo rm -rf /var/lib/kubelet || true
        sudo rm -rf /var/lib/dockershim || true
        sudo rm -rf /var/run/kubernetes || true
        sudo rm -rf \$HOME/.kube || true
        
        # Network cleanup
        sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X || true
        sudo ip link delete cni0 || true
        sudo ip link delete flannel.1 || true
        sudo rm -rf /run/flannel || true
    "
    if [ "$1" == "localhost" ]; then
        run_local "$cmd"
    else
        run_remote "$1" "$cmd"
    fi
}

# Install dependencies
install_dependencies_node() {
    local cmd="
        sudo apt-get update
        sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common gnupg2 build-essential libssl-dev zlib1g-dev luarocks

        # Install Docker
        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
        echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$(lsb_release -cs) stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo apt-get update
        sudo apt-get install -y --allow-downgrades docker-ce docker-ce-cli containerd.io=1.7.29-1~ubuntu.24.04~noble

        # Configure containerd
        sudo mkdir -p /etc/containerd
        sudo rm -f /etc/containerd/config.toml
        containerd config default | sudo tee /etc/containerd/config.toml
        sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml
        sudo systemctl restart containerd

        # Configure Docker daemon
        sudo mkdir -p /etc/docker
        cat <<EOF | sudo tee /etc/docker/daemon.json
            {
            \"exec-opts\": [\"native.cgroupdriver=systemd\"],
            \"log-driver\": \"json-file\",
            \"log-opts\": {
                \"max-size\": \"100m\"
            },
            \"storage-driver\": \"overlay2\"
            }
EOF
        # Fix for Docker socket activation issues
        sudo systemctl stop docker.socket docker.service || true
        sudo systemctl reset-failed docker.socket docker.service || true
        sudo systemctl daemon-reload
        sudo systemctl enable docker
        sudo systemctl start docker.socket
        sudo systemctl start docker.service

        # Install K8s components
        curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.28/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg --yes
        echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.28/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list
        sudo apt-get update
        sudo apt-get install -y kubelet kubeadm kubectl ethtool
        sudo apt-mark hold kubelet kubeadm kubectl
        
        # Load kernel modules required for Kubernetes
        sudo modprobe overlay
        sudo modprobe br_netfilter
        
        # Ensure modules load on boot
        printf 'overlay\nbr_netfilter\n' | sudo tee /etc/modules-load.d/k8s.conf
        
        # Configure sysctl for Kubernetes networking
        printf 'net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\nnet.ipv4.ip_forward = 1\n' | sudo tee /etc/sysctl.d/k8s.conf
        sudo sysctl --system
        
        # Disable swap
        sudo swapoff -a
        sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab
        
        # Install luasocket for wrk2 Lua scripts
        sudo luarocks install luasocket
        
        # Create symbolic links for wrk2's embedded LuaJIT to find luasocket
        # wrk2 looks in /usr/local/share/lua/5.1/ and /usr/local/lib/lua/5.1/
        sudo mkdir -p /usr/local/share/lua/5.1
        sudo mkdir -p /usr/local/lib/lua/5.1
        
        # Link socket.lua and socket directory
        sudo ln -sf /usr/share/lua/5.1/socket.lua /usr/local/share/lua/5.1/socket.lua
        sudo ln -sf /usr/share/lua/5.1/socket /usr/local/share/lua/5.1/socket
        sudo ln -sf /usr/share/lua/5.1/ltn12.lua /usr/local/share/lua/5.1/ltn12.lua
        sudo ln -sf /usr/share/lua/5.1/mime.lua /usr/local/share/lua/5.1/mime.lua
        
        # Link socket binaries
        sudo ln -sf /usr/lib/x86_64-linux-gnu/lua/5.1/socket /usr/local/lib/lua/5.1/socket
        sudo ln -sf /usr/lib/x86_64-linux-gnu/lua/5.1/mime /usr/local/lib/lua/5.1/mime
    "
    if [ "$1" == "localhost" ]; then
        run_local "$cmd"
    else
        run_remote "$1" "$cmd"
    fi
}

# Stop Kubernetes services
stop_cluster_node() {
    local cmd="
        sudo systemctl stop kubelet || true
        sudo systemctl stop docker || true
        sudo systemctl stop containerd || true
    "
    if [ "$1" == "localhost" ]; then
        run_local "$cmd"
    else
        run_remote "$1" "$cmd"
    fi
}

# Start Kubernetes services
start_cluster_node() {
    local cmd="
        sudo systemctl start containerd || true
        sudo systemctl start docker || true
        sudo systemctl start kubelet || true
    "
    if [ "$1" == "localhost" ]; then
        run_local "$cmd"
    else
        run_remote "$1" "$cmd"
    fi
}

# =========================
# Main Execution
# =========================

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "Options:"
    echo "  --cleanup-deps    Remove Docker and K8s binaries"
    echo "  --install-deps    Install Docker and K8s binaries"
    echo "  --reset-k8s       Reset Kubernetes cluster state"
    echo "  --cluster         Initialize control plane and join workers"
    echo "  --deploy-app      Deploy the application"
    echo "  --stop            Stop Kubernetes services (pause cluster)"
    echo "  --start           Start Kubernetes services (resume cluster)"
    echo "  --all             Run all steps (Cleanup -> Install -> Reset -> Setup -> Deploy)"
    echo "  --setup           Setup the cluster (Reset -> Setup -> Deploy)"
    exit 1
}

# Parse arguments
if [ $# -eq 0 ]; then
    usage
fi
while [[ $# -gt 0 ]]; do
    case $1 in
        --cleanup-deps) DO_CLEANUP_DEPS=true; shift ;;
        --install-deps) DO_INSTALL_DEPS=true; shift ;;
        --reset-k8s)    DO_RESET_K8S=true; shift ;;
        --cluster)      DO_CLUSTER=true; shift ;;
        --deploy-app)   DO_DEPLOY_APP=true; shift ;;
        --stop)         DO_STOP_CLUSTER=true; shift ;;
        --start)        DO_START_CLUSTER=true; shift ;;
        --all)
            DO_CLEANUP_DEPS=true
            DO_INSTALL_DEPS=true
            DO_RESET_K8S=true
            DO_CLUSTER=true
            DO_DEPLOY_APP=true
            shift
            ;;
        --setup)
            DO_RESET_K8S=true
            DO_CLUSTER=true
            DO_DEPLOY_APP=true
            shift
            ;;
        *) usage ;;
    esac
done

# 0. Setup SSH Agent
setup_ssh_agent

# 1. Cleanup Dependencies
if [ "$DO_CLEANUP_DEPS" = true ]; then
    log "Cleaning up dependencies on all nodes..."
    cleanup_deps_node "localhost"
    for node in "${WORKER_NODES[@]}"; do
        cleanup_deps_node "$node"
    done
fi

# 2. Install Dependencies
if [ "$DO_INSTALL_DEPS" = true ]; then
    log "Installing dependencies on all nodes..."
    install_dependencies_node "localhost"
    for node in "${WORKER_NODES[@]}"; do
        install_dependencies_node "$node"
    done
fi

# 3. Reset Kubernetes State
if [ "$DO_RESET_K8S" = true ]; then
    log "Ensuring ports are free..."
    # Ports: 6443, 10259, 10257, 2379, 2380
    run_local "sudo fuser -k 6443/tcp 10259/tcp 10257/tcp 2379/tcp 2380/tcp || true"

    log "Resetting Kubernetes state on all nodes..."
    reset_k8s_node "localhost"
    for node in "${WORKER_NODES[@]}"; do
        reset_k8s_node "$node"
    done
fi

# 4. Setup Cluster
if [ "$DO_CLUSTER" = true ]; then
    log "Initializing Control Plane..."
    run_local "sudo systemctl start docker containerd || true"
    run_local "sudo kubeadm init --pod-network-cidr=10.244.0.0/16"

    # Setup kubeconfig
    mkdir -p $HOME/.kube
    sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
    sudo chown $(id -u):$(id -g) $HOME/.kube/config

    # Install Flannel
    log "Installing Flannel CNI..."
    log "Waiting for API server to be ready..."
    for i in {1..60}; do
        if kubectl get nodes &> /dev/null; then
            break
        fi
        echo "Waiting for API server... ($i/60)"
        sleep 2
    done

    # Download and patch Flannel for host-gw
    log "Applying Flannel CNI (host-gw mode)..."
    curl -fsSL -o kube-flannel.yml https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
    sed -i 's/"Type": "vxlan"/"Type": "host-gw"/' kube-flannel.yml
    for i in {1..5}; do
        if kubectl apply -f kube-flannel.yml; then
            break
        fi
        log "Retry applying Flannel ($i/5)..."
        sleep 5
    done
    rm -f kube-flannel.yml

    # Join Workers
    log "Joining Worker Nodes..."
    JOIN_CMD=$(kubeadm token create --print-join-command)
    for node in "${WORKER_NODES[@]}"; do
        run_remote "$node" "sudo $JOIN_CMD"
    done

    # Restart all node services to fix potential NotReady state
    log "Restarting containerd and kubelet on all nodes to ensure readiness..."
    sudo systemctl restart containerd && sudo systemctl restart kubelet
    for node in "${WORKER_NODES[@]}"; do
        run_remote "$node" "sudo systemctl restart containerd && sudo systemctl restart kubelet"
    done

    # Wait for nodes to be ready
    log "Waiting for nodes to be ready..."
    kubectl wait --for=condition=ready node --all --timeout=300s
    log "Nodes are ready."
fi

# 5. Deploy Application
if [ "$DO_DEPLOY_APP" = true ]; then
    log "Deploying Social Network Application..."
    APP_YAML="/users/mkraman/DeathStarBench-LDOS/socialNetwork/kubernetes/all.yaml"
    if [ -f "$APP_YAML" ]; then
        kubectl apply -f "$APP_YAML"
    else
        log "Error: Application YAML not found at $APP_YAML"
        exit 1
    fi

    log "Waiting for pods to be ready..."
    kubectl wait --for=condition=ready pod --all --timeout=300s
    log "Cluster setup complete!"
    kubectl get nodes
    kubectl get pods -o wide
fi

# 6. Stop Cluster
if [ "$DO_STOP_CLUSTER" = true ]; then
    log "Stopping Kubernetes services on all nodes..."
    stop_cluster_node "localhost"
    for node in "${WORKER_NODES[@]}"; do
        stop_cluster_node "$node"
    done
    log "Cluster services stopped."
fi

# 7. Start Cluster
if [ "$DO_START_CLUSTER" = true ]; then
    log "Starting Kubernetes services on all nodes..."
    start_cluster_node "localhost"
    for node in "${WORKER_NODES[@]}"; do
        start_cluster_node "$node"
    done
    log "Cluster services started. Waiting for nodes to be ready..."
    kubectl wait --for=condition=ready node --all --timeout=300s
    log "Nodes are ready."
fi