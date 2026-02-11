#!/bin/bash

# Perform a random search over the placement of microservices in the cluster.
# Store the best config in the best-rsearch-config.yml file in the configs
# directory. Determine the best config by averaging the P99 end-to-end
# latencies of mixed-workload across all runs for a specific placement.

# Usage: rand-search-experiment.sh <num-experiments>
# Example: rand-search-experiment.sh 30

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
echo "Starting experiments (3 runs per config)..."
"$(dirname "$0")/test-configs.sh" "$CONFIGS_DIR"/*.yml --output "$RESULTS_DIR" -n 3

# 3. Determine best config and copy over to best-rsearch-config.yml
best_latency=""
best_config_name=""

# Calculate average P99 end-to-end latency for each config
shopt -s nullglob
for result_dir in "$RESULTS_DIR"/results-config-*; do
    [ -d "$result_dir" ] || continue

    # Extract config number (handling versions like results-config-0-2)
    dirname=$(basename "$result_dir")
    config_name=$(echo "$dirname" | sed -E 's/^results-config-([0-9]+).*/\1/')
    
    # Calculate avg of P99s across all runs in this dir
    avg_p99_latency=$(jq -s -r '
      [
        .[] | ."mixed-workload"[]? | .p99 | select(. != "na" and . != null)
        | if endswith("ms") then rtrimstr("ms") | tonumber
          elif endswith("us") then (rtrimstr("us") | tonumber) / 1000
          elif endswith("s") then (rtrimstr("s") | tonumber) * 1000
          else tonumber end
      ] as $v
      | if ($v | length) > 0 then ($v | add / ($v | length)) else empty end
    ' "$result_dir"/run*.json)
    [[ -z "$avg_p99_latency" ]] && continue # skip if empty
    
    # Update best config if needed
    if [[ -z "$best_latency" ]] || (( $(echo "$avg_p99_latency < $best_latency" | bc -l) )); then
        best_latency=$avg_p99_latency
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