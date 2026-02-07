#!/bin/bash

# Wrapper script to run Bayesian Optimization for node placement using the
# Python script. This approach uses Gaussian Process-based Bayesian
# Optimization with scikit-optimize to minimize the average P99 end-to-end
# latency of mixed-workload.

# Usage: bayes-search-experiment.sh <num-calls> <num-random-starts>
# Example: bayes-search-experiment.sh 30 10

set -e

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CONFIG_DIR="$DIR/configs/bayes-search"
RESULT_DIR="$DIR/results/bayes-search"

# Start the SSH agent
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY"

# Ensure Python dependencies are installed
if ! python3 -c "import skopt" &> /dev/null; then
    echo "Dependencies not found. Installing scikit-optimize, pyyaml, numpy..."
    pip3 install scikit-optimize pyyaml numpy
fi

# Get list of nodes and microservices from Kubernetes
echo "Fetching nodes and services..."
NODES=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | cut -c1-5 | tr '\n' ' ')
MICROSERVICES=$(kubectl get deployments -o jsonpath='{.items[*].metadata.name}')
NUM_CALLS=${1:-30}
NUM_RANDOM_STARTS=${2:-10}

echo "=================================================="
echo "Starting Bayesian Optimization Experiment"
echo "Nodes: $NODES"
echo "Services found: $(echo $MICROSERVICES | wc -w)"
echo "Config Directory: $CONFIG_DIR"
echo "Result Directory: $RESULT_DIR"
echo "Optimization: $NUM_CALLS iterations ($NUM_RANDOM_STARTS random starts)"
echo "=================================================="

mkdir -p "$CONFIG_DIR"
mkdir -p "$RESULT_DIR"

# Run the Python optimization script
python3 "$DIR/bayes_optimization.py" \
    --nodes $NODES \
    --services $MICROSERVICES \
    --config-dir "$CONFIG_DIR" \
    --result-dir "$RESULT_DIR" \
    --n-calls "$NUM_CALLS" \
    --n-random-starts "$NUM_RANDOM_STARTS"

echo "Experiments completed."
echo "Check $CONFIG_DIR/best-bayes-config.yml for the optimal configuration."
echo "Full history available in $CONFIG_DIR/bayes-history.json"