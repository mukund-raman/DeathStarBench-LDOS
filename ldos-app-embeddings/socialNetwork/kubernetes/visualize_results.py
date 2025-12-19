import argparse
import json
import os
import matplotlib.pyplot as plt

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Visualize Kubernetes Experiment Results')
    parser.add_argument('results_dir', help='Directory containing result JSON files')
    args = parser.parse_args()

    results_dir = args.results_dir
    if not os.path.exists(results_dir):
        print(f"Error: Directory {results_dir} does not exist.")
        return

    data_points = []

    # Iterate over all json files in the directory
    for filename in os.listdir(results_dir):
        if filename.endswith(".json") and "results-" in filename:
            filepath = os.path.join(results_dir, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                
                # Extract placements to find busiest node
                placements = data.get("placements", {})
                node_counts = {}
                for node, pods in placements.items():
                    node_counts[node] = len(pods)
                busiest_node = max(node_counts, key=node_counts.get) if node_counts else "unknown"

                # Extract average median latency by averaging the e2e_median
                # across the 3 runs (if available) and across the 4 workloads
                # (compose, home, user, mixed)
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
                
                # Placeholder for placement ID - parsing from filename for
                # now: results-config1.json -> P1
                placement_id = "P" + filename.split(".")[0][-1]
                data_points.append({
                    "id": placement_id,
                    "latency": avg_median_latency,
                    "busiest_node": busiest_node
                })

            except Exception as e:
                print(f"Skipping {filename}: {e}")

    # Sort by latency
    data_points.sort(key=lambda x: x["latency"])
    
    if not data_points:
        print("No valid data found.")
        return

    # Preparation for Plotting
    ids = [d["id"] for d in data_points]
    latencies = [d["latency"] for d in data_points]
    busiest_nodes = [d["busiest_node"] for d in data_points]
    unique_nodes = sorted(list(set(busiest_nodes)))
    
    # Simple color map
    colors_map = plt.cm.get_cmap('tab10', len(unique_nodes))
    node_to_color = {node: colors_map(i) for i, node in enumerate(unique_nodes)}
    bar_colors = [node_to_color[n] for n in busiest_nodes]

    # Plotting
    plt.figure(figsize=(10, 6))
    bars = plt.bar(ids, latencies, color=bar_colors)
    
    plt.xlabel('Placement Configuration')
    plt.ylabel('Avg Median End-to-End Latency (ms)')
    plt.title('Performance by Placement Configuration')
    plt.xticks(rotation=45)
    
    # Legend
    handles = [plt.Rectangle((0,0),1,1, color=node_to_color[n]) for n in unique_nodes]
    plt.legend(handles, unique_nodes, title="Busiest Node")    
    plt.tight_layout()
    
    output_image = os.path.join(results_dir, \
        f"{os.path.basename(results_dir.rstrip('/'))}-graph.png")
    plt.savefig(output_image)
    print(f"Graph saved to {output_image}")