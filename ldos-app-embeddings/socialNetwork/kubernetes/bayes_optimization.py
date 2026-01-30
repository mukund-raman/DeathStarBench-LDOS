#!/usr/bin/env python3

import argparse
import os
import subprocess
import time
import json
import yaml
from skopt import gp_minimize
from skopt.space import Integer
from skopt.utils import use_named_args

# Global settings holding state for the objective function
class ExperimentContext:
    def __init__(self):
        self.nodes = []
        self.services = []
        self.config_dir = "."
        self.result_dir = "."
        self.scripts_dir = "."
        self.iteration = 0
        self.history = []

context = ExperimentContext()

# Save the placement vector as a YAML configuration file.
def save_config(placement_vector, filename):
    # Group services by node
    node_mapping = {n: [] for n in context.nodes}
    for svc_idx, node_idx in enumerate(placement_vector):
        node_name = context.nodes[node_idx]
        svc_name = context.services[svc_idx]
        node_mapping[node_name].append(svc_name)
    
    # Construct YAML structure
    data = {"node-placements": []}
    for node in sorted(node_mapping.keys()):
        data["node-placements"].append({node: node_mapping[node]})
    
    # Write to file
    with open(filename, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)

# Run the experiment for a given config file.
def run_experiment(config_file, result_file):
    # 1. Pin microservices
    pin_script = os.path.join(context.scripts_dir, "pin-microservices.sh")
    exp_script = os.path.join(context.scripts_dir, "k8s-snet-default-experiment.sh")
    print(f"[{context.iteration}] Pinning services with {config_file}...")
    subprocess.check_call([pin_script, config_file])

    # 2. Wait for stabilization
    print(f"[{context.iteration}] Waiting 30s for stabilization...")
    time.sleep(30)

    # 3. Run benchmark
    print(f"[{context.iteration}] Running benchmark, output to {result_file}...")
    subprocess.check_call([exp_script, result_file])

# Parses result file and returns the objective value (avg median latency).
def parse_results(result_file):
    if not os.path.exists(result_file):
        print(f"Error: Result file {result_file} not found.")
        return float('inf')
    try:
        with open(result_file, 'r') as f:
            data = json.load(f)
        
        # Extract e2e_median field from all workloads
        latencies = []
        for workload, runs in data.items():
            if not isinstance(runs, list):
                continue
            for run in runs:
                val = run.get('e2e_median')
                if val:
                    try:
                        latencies.append(float(val))
                    except ValueError:
                        pass
        if not latencies:
            print("Warning: No valid latencies found in results.")
            return float('inf')
        
        # Return average of latencies
        avg_median_latency = sum(latencies) / len(latencies)
        print(f"[{context.iteration}] Parsed Avg Median Latency: {avg_median_latency}")
        return avg_median_latency
    except Exception as e:
        print(f"Error parsing results: {e}")
        return float('inf')

# The objective function for Bayesian Optimization.
def objective_function(x):
    """
    x: list of node indices (integers).
    Returns: Average Median End-to-End Latency (float).
    """
    context.iteration += 1
    
    # Determine file paths
    config_filename = f"bayes-config-{context.iteration:03d}.yml"
    config_path = os.path.join(context.config_dir, config_filename)
    result_filename = f"bayes-result-{context.iteration:03d}.json"
    result_path = os.path.join(context.result_dir, result_filename)

    # Save config and run experiment
    save_config(x, config_path)
    try:
        run_experiment(config_path, result_path)
    except subprocess.CalledProcessError as e:
        print(f"Experiment failed: {e}")
        return float('inf')

    # Parse results, store history, and return score
    score = parse_results(result_path)
    context.history.append({
        "iteration": context.iteration,
        "config": [int(val) for val in x],
        "score": float(score),
        "config_file": config_path,
        "result_file": result_path
    })
    return score

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Bayesian Optimization for node placement.")
    parser.add_argument("--nodes", nargs="+", required=True, help="List of node names")
    parser.add_argument("--services", nargs="+", required=True, help="List of microservices")
    parser.add_argument("--config-dir", required=True, help="Directory to store configs")
    parser.add_argument("--result-dir", required=True, help="Directory to store results")
    parser.add_argument("--n-calls", type=int, default=30, help="Number of BO iterations")
    parser.add_argument("--n-random-starts", type=int, default=10, help="Number of random initialization points")
    
    args = parser.parse_args()

    # Initialize context
    context.nodes = args.nodes
    context.services = args.services
    context.config_dir = args.config_dir
    context.result_dir = args.result_dir
    context.scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(context.config_dir):
        os.makedirs(context.config_dir)
    if not os.path.exists(context.result_dir):
        os.makedirs(context.result_dir)
    
    num_services = len(context.services)
    num_nodes = len(context.nodes)
    print(f"Starting Bayesian Optimization with {num_services} services on {num_nodes} nodes.")
    print(f"Optimization loop: {args.n_calls} calls, {args.n_random_starts} random starts.")

    # Define search space: one dimension per service, value is node index
    space = [Integer(1, num_nodes - 1) for _ in range(num_services)]

    # Run optimization with Expected Improvement (EI) acquisition function
    res = gp_minimize(
        objective_function,
        space,
        acq_func="EI",
        n_calls=args.n_calls,
        n_random_starts=args.n_random_starts,
        random_state=42,
        verbose=True
    )
    print("Optimization finished.")
    print(f"Best score (latency): {res.fun}")
    print(f"Best config vector: {res.x}")

    # Save best config
    best_config_path = os.path.join(context.config_dir, "best-bayes-config.yml")
    save_config(res.x, best_config_path)
    print(f"Best configuration saved to {best_config_path}")

    # Save full optimization history
    history_path = os.path.join(context.config_dir, "bayes-history.json")
    with open(history_path, 'w') as f:
        json.dump(context.history, f, indent=2)
    print(f"History saved to {history_path}")