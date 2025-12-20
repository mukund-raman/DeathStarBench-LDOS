#!/bin/bash

# Perform a random search over the placement of microservices in the cluster.
# Store the best config in the best-rsearch-config.yml file in the configs
# directory. Determine the best config by averaging the median end-to-end
# latencies across all actions for a specific placement (e.g., compose-post,
# home-timeline, etc.).

# Usage: rand-search-experiment.sh <num-experiments>
# Example: rand-search-experiment.sh 30

# Result of running ~30 random configs - P17 is best

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

# Directories
CONFIGS_DIR="$(dirname "$0")/configs/rand-search"
RESULTS_DIR="$(dirname "$0")/results/rand-search"

# Get list of all nodes and microservices and number of configs to generate
echo "Fetching nodes and services..."
NODES=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | cut -c1-5 | tr '\n' ' ')
MICROSERVICES=$(kubectl get deployments -o jsonpath='{.items[*].metadata.name}')
NUM_CONFIGS=$1
if [ -z "$NUM_CONFIGS" ]; then
    NUM_CONFIGS=30
fi

# Start the SSH agent
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY"

# 1. Generate configurations using Python script
echo "Generating $NUM_CONFIGS random configurations..."
python3 "$(dirname "$0")/generate_rand_configs.py" \
    --output-dir "$CONFIGS_DIR" \
    --num-configs "$NUM_CONFIGS" \
    --nodes $NODES \
    --services $MICROSERVICES

# 2. Loop through configs and run experiments
echo "Starting experiments..."
for config_file in $(ls "$CONFIGS_DIR"/config-*.yml | sort); do
    config_name=$(basename "$config_file" .yml | sed 's/^config-//')
    result_file="$RESULTS_DIR/rand-search-${config_name}.json"
    
    echo "=================================================="
    echo "Running Configuration: $config_name"
    echo "Config File: $config_file"
    echo "Result File: $result_file"
    echo "=================================================="
    
    # Check if result already exists (skip if resuming)
    if [ -f "$result_file" ]; then
        echo "Result file $result_file already exists. Skipping..."
        continue
    fi

    # Pin Microservices
    echo "[Step 1] Pinning microservices..."
    "$(dirname "$0")/pin-microservices.sh" "$config_file"
    
    # Wait for stabilization (30s)
    echo "[Step 2] Waiting 30s for cluster stabilization..."
    sleep 30
    
    # Run Experiment
    echo "[Step 3] Running benchmark..."
    "$(dirname "$0")/k8s-snet-default-experiment.sh" "$result_file"
    
    echo "Finished $config_name"
done

# 3. Determine best config and copy over to best-rsearch-config.yml
best_latency=""
best_config_name=""

# Calculate average median end-to-end latency for each config
shopt -s nullglob
for result_file in "$RESULTS_DIR"/rand-search-*.json; do
    config_name=$(basename "$result_file" .json | sed 's/^rand-search-//')
    avg_median_latency=$(jq -r '
      [.. | .e2e_median? | select(. != null) | tonumber] as $v
      | if ($v | length) > 0
        then ($v | add / length)
        else empty
        end
    ' "$result_file")
    [[ -z "$avg_median_latency" ]] && continue # skip if empty
    
    # Update best config if needed
    if [[ -z "$best_latency" ]] || (( $(echo "$avg_median_latency < $best_latency" | bc -l) )); then
        best_latency=$avg_median_latency
        best_config_name=$config_name
    fi
done
if [ -z "$best_config_name" ]; then
    echo "Error: Could not determine best configuration. No valid results found."
else
    cp "$CONFIGS_DIR/config-${best_config_name}.yml" "$CONFIGS_DIR/../best-rsearch-config.yml"
    echo "Best configuration: $best_config_name"
    echo "Best configuration saved to $CONFIGS_DIR/../best-rsearch-config.yml"
fi

echo "All experiments completed."