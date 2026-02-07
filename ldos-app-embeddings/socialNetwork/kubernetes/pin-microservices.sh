#!/bin/bash

# Given either a config file or a list of microservices and nodes, pin them
# to the specified nodes in the current cluster.
# Usage: pin-microservices.sh (<config-file> | <microservice1> <node1> <microservice2> <node2> ...)
# Example: pin-microservices.sh configs/config0.yml

set -e

# Function to get node name from simple name (node0 -> node0.app-embeddings...)
get_full_node_name() {
    # Assuming the simple name is the prefix of the hostname
    local simple_name=$1
    local full_name=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep -E "^${simple_name}(\.|$)")
    if [ -z "$full_name" ]; then
        echo "Error: Could not find Kubernetes node matching '${simple_name}'" >&2
        return 1
    fi
    echo "$full_name" | head -n1
}

pin_service() {
    local service=$1
    local node_simple=$2
    local node_full=$(get_full_node_name "$node_simple")
    if [ -z "$node_full" ]; then exit 1; fi

    # Patch the deployment to pin it to the specified node
    echo "Pinning $service to $node_full ($node_simple)..."
    kubectl patch deployment "$service" -p "{\"spec\": {\"template\": {\"spec\": {\"nodeSelector\": {\"kubernetes.io/hostname\": \"$node_full\"}}}}}"
}


if [ "$#" -eq 1 ] && [ -f "$1" ]; then
    # Use python to parse YAML and output "service node" pairs
    CONFIG_FILE=$1
    echo "Applying configuration from $CONFIG_FILE..."
    python3 -c "
import sys, yaml

try:
    with open('$CONFIG_FILE', 'r') as f:
        data = yaml.safe_load(f)
        
    placements = data.get('node-placements', [])
    for entry in placements:
        for node, services in entry.items():
            for service in services:
                print(f'{service} {node}')
except Exception as e:
    print(f'Error parsing YAML: {e}', file=sys.stderr)
    sys.exit(1)
" | while read service node; do
        pin_service "$service" "$node"
    done

elif [ "$#" -ge 2 ] && [ $(($# % 2)) -eq 0 ]; then
    # Even number of arguments: pairs of service node
    while [ "$#" -gt 0 ]; do
        service=$1
        node=$2
        pin_service "$service" "$node"
        shift 2
    done
else
    echo "Usage: $0 (<config-file> | <microservice1> <node1> <microservice2> <node2> ...)"
    exit 1
fi

echo "All services pinned. Waiting for deployment..."