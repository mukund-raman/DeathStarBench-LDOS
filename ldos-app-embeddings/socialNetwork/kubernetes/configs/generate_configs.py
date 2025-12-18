import random
import yaml
import os

# Simple script to generate a number of random config files

N = 5
services = [
    "home-timeline-redis",
    "post-storage-memcached",
    "user-timeline-redis",
    "nginx-thrift",
    "text-service",
    "unique-id-service",
    "url-shorten-memcached",
    "url-shorten-mongodb",
    "user-mention-service",
    "user-mongodb",
    "jaeger",
    "media-mongodb",
    "media-service",
    "social-graph-redis",
    "social-graph-service",
    "user-memcached",
    "media-memcached",
    "post-storage-mongodb",
    "url-shorten-service",
    "user-service",
    "user-timeline-service",
    "compose-post-service",
    "home-timeline-service",
    "media-frontend",
    "post-storage-service",
    "social-graph-mongodb",
    "user-timeline-mongodb"
]

nodes = ["node0", "node1", "node2", "node3", "node4"]
output_dir = "/users/mkraman/DeathStarBench-LDOS/ldos-app-embeddings/socialNetwork/kubernetes/configs/"

for i in range(1, N + 1):
    random.shuffle(services)
    
    # Distribute services across nodes
    distributed_services = {node: [] for node in nodes}
    for idx, service in enumerate(services):
        node = nodes[idx % len(nodes)]
        distributed_services[node].append(service)
    
    # formatting output similar to config0.yml
    config_data = {"node-placements": []}
    for node in nodes:
        config_data["node-placements"].append({node: distributed_services[node]})
        
    filename = os.path.join(output_dir, f"config{i}.yml")
    with open(filename, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False)
    
    print(f"Generated {filename}")
