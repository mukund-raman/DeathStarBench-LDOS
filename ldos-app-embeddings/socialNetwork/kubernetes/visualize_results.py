import argparse
import sys
import json
import os
import matplotlib.pyplot as plt
import numpy as np

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
                
                # Extract average median latency by averaging the e2e_median
                # across the runs (if available) and across the workloads
                workloads = ["compose-post", "read-home-timelines", "read-user-timelines", "mixed-workload"]
                total_latency = 0
                count = 0
                for workload in workloads:
                    runs = data.get(workload, [])
                    for run in runs:
                        try:
                            lat = float(run.get("e2e_median", 0))
                            total_latency += lat
                            count += 1
                        except (ValueError, TypeError):
                            pass
                avg_median_latency = (total_latency / count) if count > 0 else 0
                
                # Placement ID from filename: rand-search-000 -> P000
                placement_id = "P" + filename.replace("rand-search-", "") \
                    .replace("results-", "").replace(".json", "")
                if "config" in placement_id:
                     placement_id = placement_id.replace("config", "")

                # Add data point to list 
                data_points.append({
                    "id": placement_id,
                    "latency": avg_median_latency,
                    "node_counts": node_counts,
                    "total_pods": total_pods
                })
            except Exception as e:
                print(f"Skipping {filename}: {e}")

    # Sort by latency and verify data points
    data_points.sort(key=lambda x: x["latency"])
    if not data_points:
        print("No valid data found.")
        sys.exit(0)

    # Prepare for plotting
    ids = [d["id"] for d in data_points]
    unique_nodes = sorted(list(all_nodes))
    
    # Use a qualitative colormap to distinguish nodes clearly
    colors_map = plt.get_cmap('tab10') 
    node_to_color = {node: colors_map(i % 10) for i, node in enumerate(unique_nodes)}

    # Set figure size and initialize bottom array for stacking bars
    plt.figure(figsize=(12, 6))
    bottoms = np.zeros(len(data_points))
    
    # Plot each node's contribution as a segment of the bar
    for node in unique_nodes:
        segment_heights = []
        for d in data_points:
            # Calculate height proportional to microservice count on this node
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
    plt.ylabel('Avg Median End-to-End Latency (ms)')
    plt.title('Performance by Placement and Microservice Distribution')
    plt.xticks(rotation=45)
    plt.legend(title="Node Distribution", bbox_to_anchor=(1.05, 1), loc='upper left')
    plt.tight_layout()
    
    output_image = os.path.join(results_dir, \
        f"{os.path.basename(results_dir.rstrip('/'))}-dist-graph.png")
    plt.savefig(output_image, bbox_inches='tight')
    print(f"Graph saved to {output_image}")