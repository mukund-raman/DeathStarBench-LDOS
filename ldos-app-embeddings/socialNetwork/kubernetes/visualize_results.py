import argparse
import sys
import json
import os
import matplotlib.pyplot as plt
import numpy as np

# Threshold for outliers in microseconds (100 ms)
OUTLIER_THRESHOLD_US = 100000

if __name__ == "__main__":
    # Parse arguments and verify results directory
    parser = argparse.ArgumentParser(description='Visualize Kubernetes Experiment Results')
    parser.add_argument('results_dir', help='Directory containing result JSON files')
    args = parser.parse_args()
    results_dir = args.results_dir
    if not os.path.exists(results_dir):
        print(f"Error: Directory {results_dir} does not exist.")
        sys.exit(1)

    # Iterate over all json files in the directory
    data_points = []
    all_nodes = set()
    for filename in os.listdir(results_dir):
        if filename.endswith(".json"):
            filepath = os.path.join(results_dir, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                
                # Extract placements and count microservices per node
                placements = data.get("placements", {})
                node_counts = {}
                total_pods = 0
                for node, pods in placements.items():
                    count = len(pods)
                    node_counts[node] = count
                    total_pods += count
                    all_nodes.add(node)
                
                # Extract P99 latency (ms) for mixed-workload
                target_workload = "mixed-workload"
                run = data.get(target_workload, [])
                if not run:
                    print(f"Warning: No mixed-workload data in {filename}")
                    continue
                p99_str = run.get("p99")
                p99_latency = 0.0
                if p99_str:
                    try:
                        if isinstance(p99_str, (int, float)):
                            p99_latency = float(p99_str)
                        elif p99_str.endswith("ms"):
                            p99_latency = float(p99_str.replace("ms", ""))
                        elif p99_str.endswith("us"):
                            p99_latency = float(p99_str.replace("us", "")) / 1000.0
                        elif p99_str.endswith("s"):
                            p99_latency = float(p99_str.replace("s", "")) * 1000.0
                        else:
                            p99_latency = float(p99_str)
                    except ValueError:
                         print(f"Warning: Could not parse p99 '{p99_str}' in {filename}")
                
                # Placement ID from filename:
                #   if rand search: rand-search-000.json -> P000
                #   if bayes search: bayes-result-000.json -> P000
                #   if results config: results-config<N>/run<M>.json -> P<N>-<M>
                placement_id = "P"
                if "rand-search" in filename:
                    placement_id += filename.replace("rand-search-", "")
                elif "bayes-result" in filename:
                    placement_id += filename.replace("bayes-result-", "")
                elif "run" in filename and "results-config" in results_dir:
                    placement_id += results_dir[-1] + "-" + filename[3:]
                else:
                    print(f"Error in parsing filename {filename}.")
                    sys.exit(1)
                placement_id = placement_id.replace(".json", "")

                # Add data point to list 
                data_points.append({
                    "id": placement_id,
                    "latency": p99_latency,
                    "node_counts": node_counts,
                    "total_pods": total_pods
                })
            except Exception as e:
                print(f"Skipping {filename}: {e}")

    # Sort by latency and verify data points
    data_points.sort(key=lambda x: x["latency"])
    
    # Filter outliers and use valid points for plotting
    valid_points = []
    outliers = []
    # threshold 200ms
    OUTLIER_THRESHOLD_MS = 200
    for dp in data_points:
        if dp["latency"] > OUTLIER_THRESHOLD_MS:
            outliers.append(dp)
        else:
            valid_points.append(dp)
    
    # Check if we have any valid points
    if not valid_points and not outliers:
         print("No data found.")
         sys.exit(0)
    
    # If no valid points but we have outliers, just plot everything or warn
    if not valid_points:
        print(f"All data points exceeded the threshold ({OUTLIER_THRESHOLD_MS}ms). Plotting all...")
        valid_points = outliers
        outliers = []
        
    data_points = valid_points

    # Prepare for plotting
    ids = [d["id"] for d in data_points]
    latencies = [d["latency"] for d in data_points]
    unique_nodes = sorted(list(all_nodes))
    
    # Use a qualitative colormap
    colors_map = plt.get_cmap('tab10') 
    node_to_color = {node: colors_map(i % 10) for i, node in enumerate(unique_nodes)}

    # Set figure size
    plt.figure(figsize=(12, 6))
    bottoms = np.zeros(len(data_points))
    
    # Plot each node's contribution
    for node in unique_nodes:
        segment_heights = []
        for d in data_points:
             # Scale the height of the bar segment by the node's share of pods
             # The total height of the bar will be the latency
            if d["total_pods"] > 0:
                fraction = d["node_counts"].get(node, 0) / d["total_pods"]
                height = d["latency"] * fraction
            else:
                height = 0
            segment_heights.append(height)
        plt.bar(ids, segment_heights, bottom=bottoms, color=node_to_color[node], label=node)
        bottoms += np.array(segment_heights)
    
    # Add labels and title
    plt.xlabel('Placement Configuration')
    plt.ylabel('Avg P99 Latency (ms)')
    plt.title('P99 Latency for Mixed Workload by Placement')
    plt.xticks(rotation=45)
    plt.legend(title="Node Distribution", bbox_to_anchor=(1.05, 1), loc='upper left')
    
    # Add outlier text
    if outliers:
        outlier_text = f"Outliers (> {OUTLIER_THRESHOLD_MS}ms):\n"
        for dp in outliers[:5]:
            outlier_text += f"{dp['id']}: {dp['latency']:.2f} ms\n"
        if len(outliers) > 5:
             outlier_text += f"... + {len(outliers)-5} more"
        plt.text(1.05, 0.5, outlier_text, transform=plt.gca().transAxes, 
                 verticalalignment='top', fontsize=9, 
                 bbox=dict(boxstyle="round,pad=0.3", facecolor='white', alpha=0.9, edgecolor='gray'))
    plt.tight_layout()
    
    output_image = os.path.join(results_dir, \
        f"{os.path.basename(results_dir.rstrip('/'))}-dist-graph.png")
    plt.savefig(output_image, bbox_inches='tight')
    print(f"Graph saved to {output_image}")