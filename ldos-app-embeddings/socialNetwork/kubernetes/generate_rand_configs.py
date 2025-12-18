import argparse
import os
import random
import yaml
import numpy as np

# Generate random placement configurations for microservices in a Kubernetes
# cluster. The configurations are saved in the output directory as YAML files.
# The configurations are generated using a maximin strategy, i.e., select
# placements that are farthest from any existing placement.

# Usage: generate_rand_configs.py
#           --output-dir <output-dir>
#           --num-configs <num-configs>
#           --nodes <node1> <node2> ...
#           --services <service1> <service2> ...
#           --pool-size <pool-size>

# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Generate random placement configurations.")
    parser.add_argument("--output-dir", required=True, help="Directory to save YAML configs")
    parser.add_argument("--num-configs", type=int, default=30, help="Number of configurations to generate")
    parser.add_argument("--nodes", nargs="+", required=True, help="List of node names")
    parser.add_argument("--services", nargs="+", required=True, help="List of microservice names")
    parser.add_argument("--pool-size", type=int, default=10000, help="Size of random pool for sampling")
    return parser.parse_args()

# Calculate Hamming distance between two placement vectors
def get_hamming_distance(p1, p2):
    return np.sum(np.array(p1) != np.array(p2))

# Generate a random placement vector (list of node indices)
def generate_random_placement(num_services, num_nodes):
    return [random.randint(0, num_nodes - 1) for _ in range(num_services)]

# Select placements using Maximin strategy, i.e., select placements that are
# farthest from any existing placement
def maximin_sample(pool, num_to_select, start_configs):
    # Initialize necessary numpy arrays
    selected, pool_arr = np.array(start_configs), np.array(pool)
    min_dists = np.array([get_hamming_distance(p, selected[0]) for p in pool_arr])
    
    # Select placements in an optimized manner by reusing the minimum distance
    # for previously selected placements with the latest selected placement
    while len(selected) < num_to_select:
        new_dists = np.array([get_hamming_distance(p, selected[-1]) for p in pool_arr])
        min_dists = np.minimum(min_dists, new_dists)
        selected = np.vstack([selected, pool_arr[np.argmax(min_dists)]])
    return selected

# Save placement vector as a YAML config file
def save_config(placement, nodes, services, filename):
    # Group services by node
    node_mapping = {n: [] for n in nodes}
    for svc_idx, node_idx in enumerate(placement):
        node_name = nodes[node_idx]
        svc_name = services[svc_idx]
        node_mapping[node_name].append(svc_name)
    
    # Construct YAML structure
    data = {"node-placements": []}
    for node in sorted(node_mapping.keys()):
        data["node-placements"].append({node: node_mapping[node]})
    
    # Write to file
    with open(filename, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)

if __name__ == "__main__":
    # Parse command line arguments and get list of nodes and services
    args = parse_args()
    nodes = args.nodes
    services = args.services
    num_nodes = len(nodes)
    num_services = len(services)
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    
    # Create two starting configs to guide the maximin sampling
    start_configs = [
        [i % num_nodes for i in range(num_services)], # Balanced
        [0] * num_services # All on Node 0
    ]

    # Generate random pool and perform maximin sampling
    print(f"Generating {args.num_configs} configs for {num_services} services on {num_nodes} nodes...")
    pool = [generate_random_placement(num_services, num_nodes) for _ in range(args.pool_size)]
    placements = maximin_sample(pool, args.num_configs, start_configs)
    
    # Save configs
    for i, p in enumerate(placements):
        fname = os.path.join(args.output_dir, f"config-{i:03d}.yml")
        save_config(p, nodes, services, fname)
    print(f"Successfully generated {len(placements)} configurations in {args.output_dir}")
