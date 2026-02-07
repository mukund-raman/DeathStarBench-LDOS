#!/bin/bash

# Given a config file, test the configs by pinning the microservices to the
# nodes (using pin-microservices.sh) and running the experiment (using
# k8s-snet-default-experiment.sh).
# Usage: test-configs.sh <config-file>... | --all [-n|--num-runs <count>]
# Example: test-configs.sh configs/config0.yml
# Example: test-configs.sh configs/config0.yml configs/config1.yml
# Example: test-configs.sh configs/config*.yml
# Example: test-configs.sh --all
# Example: test-configs.sh configs/config0.yml -n 5
# Example: test-configs.sh configs/config0.yml configs/config1.yml --num-runs 3
# Example: test-configs.sh --all --num-runs 3
# Example: test-configs.sh configs/config[^0].yml --num-runs 3

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


# Function to get the next available versioned path
get_next_version() {
    local base_path=$1
    local is_dir=$2 # 1 if directory, 0 if file
    local extension=""
    
    if [ "$is_dir" -eq 0 ]; then
        extension=".json"
    fi
    
    # Check base version (run/version 1)
    local candidate="${base_path}${extension}"
    if [ ! -e "$candidate" ]; then
        echo "$candidate"
        return
    fi
    
    # Start checking from version 2
    local version=2
    while true; do
        candidate="${base_path}-${version}${extension}"
        if [ ! -e "$candidate" ]; then
            echo "$candidate"
            return
        fi
        ((version++))
    done
}

run_experiment() {
    local config_file=$1
    local run_number=$2
    local output_dest=$3 # File path if NUM_RUNS=1, Directory path if NUM_RUNS>1
    local config_name=$(basename "$config_file" .yml)
    local output_file
    
    # Determine actual output file path
    if [ "$NUM_RUNS" -eq 1 ]; then
        output_file="$output_dest"
    else
        # Multiple runs: ensure directory exists and append run number
        mkdir -p "$output_dest"
        output_file="$output_dest/run${run_number}.json"
    fi
    
    echo "=================================================="
    if [ "$NUM_RUNS" -eq 1 ]; then
        echo "Testing config: $config_name ($config_file)"
    else
        echo "Testing config: $config_name ($config_file) - Run $run_number/$NUM_RUNS"
    fi
    echo "Output: $output_file"
    echo "=================================================="
    
    # Pin microservices first
    "$DIR/pin-microservices.sh" "$config_file"

    # Restart all deployments to ensure a clean state (flushes ephemeral storage/cache)
    echo "Restarting all deployments to clear state..."
    kubectl get deployments -o name | xargs -I {} kubectl rollout restart {}

    # Wait for all deployments to be ready
    echo "Waiting for all deployments to be ready..."
    kubectl get deployments -o name | xargs -I {} kubectl rollout status {} --timeout=300s

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
CONFIG_FILES=()
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
            echo "Usage: test-configs.sh <config-file>... | --all [-n|--num-runs <count>]"
            exit 1
            ;;
        *)
            CONFIG_FILES+=($1)
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
            config_name=$(basename "$config_file" .yml)
            base_output_path="$RESULTS_DIR/results-${config_name}"
            target_path=$(get_next_version "$base_output_path" $(( NUM_RUNS != 1 )))
            for ((i=1; i<=NUM_RUNS; i++)); do
                run_experiment "$config_file" "$i" "$target_path"
            done
        fi
    done
elif [ ${#CONFIG_FILES[@]} -gt 0 ]; then
    echo "Running ${#CONFIG_FILES[@]} config(s) ($NUM_RUNS run(s) each)..."
    for config_file in "${CONFIG_FILES[@]}"; do
        if [ -f "$config_file" ]; then
            config_name=$(basename "$config_file" .yml)
            base_output_path="$RESULTS_DIR/results-${config_name}"
            target_path=$(get_next_version "$base_output_path" $(( NUM_RUNS != 1 )))
            for ((i=1; i<=NUM_RUNS; i++)); do
                run_experiment "$config_file" "$i" "$target_path"
            done
        else
            echo "Warning: Config file not found: $config_file"
        fi
    done

# Describe script usage if no proper arguments are provided
else
    echo "Usage: test-configs.sh <config-file>... | --all [-n|--num-runs <count>]"
    echo ""
    echo "Options:"
    echo "  -n, --num-runs <count>    Run each config <count> times (default: 1)"
    echo ""
    echo "Examples:"
    echo "  test-configs.sh configs/config0.yml"
    echo "  test-configs.sh configs/config0.yml configs/config1.yml"
    echo "  test-configs.sh configs/config*.yml"
    echo "  test-configs.sh configs/config0.yml -n 5"
    echo "  test-configs.sh configs/config0.yml configs/config1.yml --num-runs 3"
    echo "  test-configs.sh --all"
    echo "  test-configs.sh --all --num-runs 3"
    exit 1
fi