#!/bin/bash

# Given a config file, test the configs by pinning the microservices to the
# nodes (using pin-microservices.sh) and running the experiment (using
# k8s-snet-default-experiment.sh).
# Usage: test-configs.sh <config-file> | --all
# Example: test-configs.sh configs/config0.yml
# Example: test-configs.sh --all

set -e

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CONFIGS_DIR="$DIR/configs"
RESULTS_DIR="$DIR/results"
mkdir -p "$RESULTS_DIR"

run_experiment() {
    local config_file=$1
    local config_name=$(basename "$config_file" .yml)
    local output_file="$RESULTS_DIR/results-${config_name}.json"
    
    echo "=================================================="
    echo "Testing config: $config_name ($config_file)"
    echo "Output: $output_file"
    echo "=================================================="
    
    # Pin microservices, wait for stabilization, and run experiment
    "$DIR/pin-microservices.sh" "$config_file"
    echo "Waiting 30s for pods to restart and stabilize..."
    sleep 30
    echo "Running experiment..."
    "$DIR/k8s-snet-default-experiment.sh" "$output_file"
    echo "Finished experiment for $config_name"
}

# Start the SSH agent
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY"

# Run experiment on all configs or a specific config
if [ "$1" == "--all" ]; then
    echo "Running all configs in $CONFIGS_DIR..."
    for config_file in "$CONFIGS_DIR"/config*.yml; do
        if [ -f "$config_file" ]; then
            run_experiment "$config_file"
        fi
    done
elif [ -f "$1" ]; then
    run_experiment "$1"
else
    echo "Usage: test-configs.sh <config-file> | --all"
    exit 1
fi