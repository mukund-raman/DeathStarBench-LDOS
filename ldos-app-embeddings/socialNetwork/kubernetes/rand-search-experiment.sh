#!bin/bash

# Don't implement for now, but read over this to understand the experiment
# and the expected output.

# Perform a random search over the placement of microservices in the cluster.
# For each placement, run the experiment with k8s-snet-default-experiment.sh
# and collect the results.
# Each result is stored in the results/rsearch directory as JSON in the same
# format as k8s-default-snet-results.json. Make sure to give each result a
# unique name (e.g., k8s-rsearch-0.json, k8s-rsearch-1.json, etc.).
# Store the best config in the best-rsearch-config.yml file in the configs
# directory.
# Determine the best config by averaging the median end-to-end latencies
# across all actions for a specific placement (e.g., compose-post,
# home-timeline, etc.).

# Usage: rand-search-experiment.sh <num-experiments>

# Example: rand-search-experiment.sh 30