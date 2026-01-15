#!/bin/bash

# Given a config file, test the configs by pinning the microservices to the
# nodes (using pin-microservices.sh) and running the experiment (using
# k8s-snet-default-experiment.sh).
# Usage: test-configs.sh <config-file> | --all [-n|--num-runs <count>]
# Example: test-configs.sh configs/config0.yml
# Example: test-configs.sh --all
# Example: test-configs.sh configs/config0.yml -n 5
# Example: test-configs.sh --all --num-runs 3

set -e

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CONFIGS_DIR="$DIR/configs"
RESULTS_DIR="$DIR/results"
mkdir -p "$RESULTS_DIR"

# Default number of runs
NUM_RUNS=1

run_experiment() {
    local config_file=$1
    local run_number=$2
    local config_name=$(basename "$config_file" .yml)
    local output_file
    
    # Determine output file path based on number of runs
    if [ "$NUM_RUNS" -eq 1 ]; then
        # Single run: no suffix, save directly in results/
        output_file="$RESULTS_DIR/results-${config_name}.json"
    else
        # Multiple runs: create subdirectory and use run suffix
        local config_results_dir="$RESULTS_DIR/results-${config_name}"
        mkdir -p "$config_results_dir"
        output_file="$config_results_dir/run${run_number}.json"
    fi
    
    echo "=================================================="
    if [ "$NUM_RUNS" -eq 1 ]; then
        echo "Testing config: $config_name ($config_file)"
    else
        echo "Testing config: $config_name ($config_file) - Run $run_number/$NUM_RUNS"
    fi
    echo "Output: $output_file"
    echo "=================================================="
    
    # Pin microservices, wait for stabilization, and run experiment
    "$DIR/pin-microservices.sh" "$config_file"
    echo "Waiting 30s for pods to restart and stabilize..."
    sleep 30
    echo "Running experiment..."
    "$DIR/k8s-snet-default-experiment.sh" "$output_file"
    
    if [ "$NUM_RUNS" -eq 1 ]; then
        echo "Finished experiment for $config_name"
    else
        echo "Finished experiment for $config_name (run $run_number)"
    fi
}

# Start the SSH agent
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY"

# Parse arguments
MODE=""
CONFIG_FILE=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --all)
            MODE="all"
            shift
            ;;
        -n|--num-runs)
            NUM_RUNS="$2"
            shift 2
            ;;
        -*)
            echo "Unknown option: $1"
            echo "Usage: test-configs.sh <config-file> | --all [-n|--num-runs <count>]"
            exit 1
            ;;
        *)
            if [ -z "$CONFIG_FILE" ]; then
                CONFIG_FILE="$1"
            fi
            shift
            ;;
    esac
done

# Ensure NUM_RUNS is a positive integer
if ! [[ "$NUM_RUNS" =~ ^[0-9]+$ ]] || [ "$NUM_RUNS" -lt 1 ]; then
    echo "Error: --num-runs must be a positive integer (got: $NUM_RUNS)"
    exit 1
fi

# Run experiments
if [ "$MODE" == "all" ]; then
    echo "Running all configs in $CONFIGS_DIR ($NUM_RUNS run(s) each)..."
    for config_file in "$CONFIGS_DIR"/config*.yml; do
        if [ -f "$config_file" ]; then
            for ((i=1; i<=NUM_RUNS; i++)); do
                run_experiment "$config_file" "$i"
            done
        fi
    done
elif [ -f "$CONFIG_FILE" ]; then
    for ((i=1; i<=NUM_RUNS; i++)); do
        run_experiment "$CONFIG_FILE" "$i"
    done

# Describe script usage if no proper arguments are provided
else
    echo "Usage: test-configs.sh <config-file> | --all [-n|--num-runs <count>]"
    echo ""
    echo "Options:"
    echo "  -n, --num-runs <count>    Run each config <count> times (default: 1)"
    echo ""
    echo "Examples:"
    echo "  test-configs.sh configs/config0.yml"
    echo "  test-configs.sh configs/config0.yml -n 5"
    echo "  test-configs.sh --all"
    echo "  test-configs.sh --all --num-runs 3"
    exit 1
fi