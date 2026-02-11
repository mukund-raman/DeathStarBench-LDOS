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
    node_mapping = {n: [] for n in context.nodes[1:]}
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

# Save the history to a JSON file atomically to prevent corruption.
def save_history():
    history_path = os.path.join(context.config_dir, "bayes-history.json")
    temp_path = history_path + ".tmp"
    with open(temp_path, 'w') as f:
        json.dump(context.history, f, indent=2)
    os.replace(temp_path, history_path)

# Run the experiment for a given config file using test-configs.sh
def run_experiment(config_file, result_parent_dir):
    script = os.path.join(context.scripts_dir, "test-configs.sh")
    
    # Ensure expected output directory is clean to avoid versioning complications
    config_name = os.path.splitext(os.path.basename(config_file))[0]
    expected_result_dir = os.path.join(result_parent_dir, f"results-{config_name}")
    if os.path.exists(expected_result_dir):
        import shutil
        shutil.rmtree(expected_result_dir)

    print(f"[{context.iteration}] Running experiment (3 runs) for {config_file}...")
    subprocess.check_call([
        script, 
        config_file, 
        "--num-runs", "3", 
        "--output", result_parent_dir
    ])
    return expected_result_dir

# Parses result directory and returns the objective value (avg P99 latency).
def parse_results(result_dir):
    if not os.path.exists(result_dir):
        print(f"Error: Result directory {result_dir} not found.")
        return float('inf')
    
    # Iterate over run*.json files
    p99_latencies = []
    try:
        files = [f for f in os.listdir(result_dir) if f.startswith("run") and f.endswith(".json")]
        if not files:
            print(f"Warning: No result files found in {result_dir}")
            return float('inf')
        for filename in files:
            filepath = os.path.join(result_dir, filename)
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            # Extract p99 from mixed-workload
            workload_data = data.get("mixed-workload", [])
            if isinstance(workload_data, dict):
                 workload_data = [workload_data]
            for entry in workload_data:
                val_str = entry.get("p99")
                if val_str:
                    try:
                        # Convert units
                        val = float('inf')
                        if isinstance(val_str, (int, float)):
                            val = float(val_str)
                        elif val_str.endswith("ms"):
                            val = float(val_str.replace("ms", ""))
                        elif val_str.endswith("us"):
                            val = float(val_str.replace("us", "")) / 1000.0
                        elif val_str.endswith("s"):
                            val = float(val_str.replace("s", "")) * 1000.0
                        else:
                            val = float(val_str)
                        
                        p99_latencies.append(val)
                    except ValueError:
                        pass
        
        if not p99_latencies:
            print("Warning: No valid P99 latencies found.")
            return float('inf')
        
        # Calculate average P99 end-to-end latency
        avg_p99 = sum(p99_latencies) / len(p99_latencies)
        print(f"[{context.iteration}] Parsed Avg P99 Latency: {avg_p99:.2f} ms")
        return avg_p99
    except Exception as e:
        print(f"Error parsing results: {e}")
        return float('inf')

# The objective function for Bayesian Optimization.
def objective_function(x):
    """
    x: list of node indices (integers).
    Returns: Average P99 End-to-End Latency (float).
    """
    context.iteration += 1
    
    # Determine file paths
    config_filename = f"bayes-config-{context.iteration:03d}.yml"
    config_path = os.path.join(context.config_dir, config_filename)

    # Save config
    save_config(x, config_path)
    
    # Run experiment via script
    score = float('inf')
    actual_result_dir = ""
    try:
        # run_experiment now returns the directory it used
        actual_result_dir = run_experiment(config_path, context.result_dir)
        score = parse_results(actual_result_dir)
    except subprocess.CalledProcessError as e:
        print(f"Experiment failed: {e}")

    # Parse results, store history, and return score
    context.history.append({
        "iteration": context.iteration,
        "config": [int(val) for val in x],
        "score": float(score),
        "config_file": config_path,
        "result_path": actual_result_dir 
    })
    
    # Save history incrementally
    save_history()
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
    save_history()
    history_path = os.path.join(context.config_dir, "bayes-history.json")
    print(f"History saved to {history_path}")