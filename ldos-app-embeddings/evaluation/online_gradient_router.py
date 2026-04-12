import torch
import numpy as np
import time
import subprocess
import logging
import datetime
import json
import os
import random
import argparse

logger = logging.getLogger("GradientRouter")

class OnlineGradientRouter:
    """
    Implements Loop A (Gradient Routing & Swapping).
    Shifts 1% of traffic to measure actual latency changes and find the best way to move services off overloaded nodes.
    """
    def __init__(self, service_names, E_N_max, num_nodes=5, measure_latency_fn=None, pin_script_path="socialNetwork/kubernetes/pin-microservices.sh", timestamp=None, l1_threshold=1.5):
        self.service_names = service_names
        self.E_N_max = E_N_max # Tensor shape: (d,) 
        self.num_nodes = num_nodes
        self.measure_latency_fn = measure_latency_fn
        self.pin_script_path = pin_script_path
        self.l1_threshold = l1_threshold
        
        # Output Logging state variables
        self.timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.eval_results = []
        
        # Current active placements
        self.current_placements = {s: 0 for s in service_names} # Baseline starts at 0
        self.running = True

    # Evaluation Log Saving
    def save_results(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eval_dir = os.path.join(base_dir, "results", "evaluations")
        os.makedirs(eval_dir, exist_ok=True)
        filename = os.path.join(eval_dir, f"router_{self.timestamp}.json")
        with open(filename, 'w') as f:
            json.dump(self.eval_results, f, indent=4)
        logger.info(f"Gradient Router evaluations saved to {filename}")

    # Core Logic Mechanics
    def calculate_violation_vector(self, e_sum_n):
        """
        Calculates the Violation Vector for a given node embedding sum.
        V_n = max(0, sum(E_i) - E_N_max)
        """
        # Element-wise diff
        diff = e_sum_n - self.E_N_max
        # Element-wise max to bind values at constraint ceilings
        v_n = torch.clamp(diff, min=0.0)
        return v_n

    def measure_latency(self, duration_s):
        """
        Measures the physical cluster's current End-To-End P99 Latency.
        Accesses the global asynchronous wrk2 pulse value.
        """
        return self.measure_latency_fn(duration_s)
        
    def _get_node_hostname(self, node_idx):
        import subprocess, re
        # Convert integer index back to specific node physical hostname
        out = subprocess.check_output("kubectl get nodes -o jsonpath='{.items[*].metadata.name}'", shell=True, text=True)
        nodes = out.strip().split()
        node_map = {}
        for n in nodes:
            match = re.search(r'\d+', n)
            if match:
                idx = int(match.group()) - 1
                node_map[idx] = n
        if node_idx in node_map:
            return node_map[node_idx]
        return nodes[node_idx % len(nodes)]

    def _apply_istio_resources(self, service, source_idx, target_idx, percent):
        source_host = self._get_node_hostname(source_idx)
        target_host = self._get_node_hostname(target_idx)
        logger.info(f"Setting up Istio VirtualService & DestinationRule for {service}: 99% {source_host}, {percent}% {target_host}")
        
        import json, subprocess
        clone_name = f"{service}-target-clone"
        try:
            dp_json = json.loads(subprocess.check_output(f"kubectl get deployment {service} -n default -o json", shell=True, text=True))
            dp_json['metadata']['name'] = clone_name
            for key in ['resourceVersion', 'uid', 'creationTimestamp', 'generation']:
                if key in dp_json['metadata']: del dp_json['metadata'][key]
            
            # Label subsets
            dp_json['spec']['template']['metadata']['labels']['routing-subset'] = 'target'
            subprocess.run(f"kubectl patch deployment {service} -n default -p '{{\"spec\": {{\"template\": {{\"metadata\": {{\"labels\": {{\"routing-subset\": \"source\"}}}}}}}}}}'", shell=True)
            
            # Pin clone to target host
            if 'nodeSelector' not in dp_json['spec']['template']['spec']:
                dp_json['spec']['template']['spec']['nodeSelector'] = {}
            dp_json['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] = target_host
            
            with open('/tmp/clone.json', 'w') as f: json.dump(dp_json, f)
            subprocess.run("kubectl apply -f /tmp/clone.json -n default", shell=True)
            
            dr_yaml = f"""
apiVersion: networking.istio.io/v1alpha3
kind: DestinationRule
metadata:
  name: {service}-dr
spec:
  host: {service}
  subsets:
  - name: source
    labels:
      routing-subset: source
  - name: target
    labels:
      routing-subset: target
"""
            with open(f'/tmp/{service}-dr.yaml', 'w') as f: f.write(dr_yaml)
            subprocess.run(f"kubectl apply -f /tmp/{service}-dr.yaml -n default", shell=True)
            
            source_weight = 100 - percent
            vs_yaml = f"""
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: {service}-vs
spec:
  hosts:
  - {service}
  http:
  - route:
    - destination:
        host: {service}
        subset: source
      weight: {source_weight}
    - destination:
        host: {service}
        subset: target
      weight: {percent}
"""
            with open(f'/tmp/{service}-vs.yaml', 'w') as f: f.write(vs_yaml)
            subprocess.run(f"kubectl apply -f /tmp/{service}-vs.yaml -n default", shell=True)
        except Exception as e:
            logger.error(f"Failed to apply Istio overlay: {e}")

    def _remove_istio_resources(self, service):
        import subprocess
        logger.info(f"Tearing down Istio overlay for {service}")
        subprocess.run(f"kubectl delete virtualservice {service}-vs -n default --ignore-not-found", shell=True, stdout=subprocess.DEVNULL)
        subprocess.run(f"kubectl delete destinationrule {service}-dr -n default --ignore-not-found", shell=True, stdout=subprocess.DEVNULL)
        subprocess.run(f"kubectl delete deployment {service}-target-clone -n default --ignore-not-found", shell=True, stdout=subprocess.DEVNULL)
        subprocess.run(f"kubectl patch deployment {service} -n default --type json -p='[{{\"op\": \"remove\", \"path\": \"/spec/template/metadata/labels/routing-subset\"}}]' 2>/dev/null || true", shell=True)
        
    def _route_traffic(self, service, source_node, target_node, percent):
        if percent > 0:
            logger.info(f"-> TRAFFIC SHIFT: Migrating {percent}% of {service} to Node {target_node} via Istio Mesh.")
            self._apply_istio_resources(service, source_node, target_node, percent)
        else:
            logger.info(f"<- REVERT: Returning {service} to Origin Node.")
            self._remove_istio_resources(service)

    def _execute_full_placement(self, service, node_idx):
        self.current_placements[service] = node_idx
        host = self._get_node_hostname(node_idx)
        logger.info(f"Executing Full Physical Migration of {service} to Node {host}...")
        try:
            import subprocess
            subprocess.run(f"kubectl patch deployment {service} -n default -p '{{\"spec\": {{\"template\": {{\"spec\": {{\"nodeSelector\": {{\"kubernetes.io/hostname\": \"{host}\"}}}}}}}}}}'", shell=True, check=True)
            logger.info(f"Service {service} successfully physically migrated to {host}.")
        except Exception as e:
            logger.error(f"Failed to physically apply placement for {service}: {e}")

    # Primary Execution Loop
    def loop(self, E_dict, current_p99=999.0):
        """
        Main loop. Checks all nodes to see if any are overloaded based on
        sum of service embeddings. Called every 60 seconds.

        E_dict: Dictionary/List mapping service index `i` to its latent emb E_i.
        current_p99: Current P99 latency.
        """
        # Identify Overloads
        node_sums = {}
        V_vectors = {}
        overloaded_nodes = []
        healthy_nodes = []
        for n in range(self.num_nodes):
            # Sum all service embeddings on the current node
            e_sum_n = torch.zeros_like(self.E_N_max)
            for i_name, n_assigned in self.current_placements.items():
                if n_assigned == n:
                    i_idx = self.service_names.index(i_name)
                    e_sum_n += E_dict[i_idx]
            node_sums[n] = e_sum_n
            v_n = self.calculate_violation_vector(e_sum_n)
            V_vectors[n] = v_n
            
            # Use the L1 norm to measure how badly the node is overloaded
            norm_L1 = torch.norm(v_n, p=1).item() 
            if norm_L1 >= self.l1_threshold:
                logger.warning(f"Gradient Router: Node {n} OVERLOADED (L1={norm_L1:.2f} >= threshold={self.l1_threshold:.2f})")
                overloaded_nodes.append((n, norm_L1))
            else:
                if norm_L1 > (self.l1_threshold * 0.5):
                    logger.info(f"Gradient Router: Node {n} near-violation (L1={norm_L1:.2f}, threshold={self.l1_threshold:.2f})")
                healthy_nodes.append(n)
                
        if not overloaded_nodes:
            logger.info("Gradient Router: All nodes healthy. No migration necessary.")
            return
            
        overloaded_nodes.sort(key=lambda x: x[1], reverse=True)
        node_A = overloaded_nodes[0][0]
        norm_V_A = overloaded_nodes[0][1]
        logger.warning(f"Gradient Router: Node {node_A} is overloaded! (Violation L1: {norm_V_A:.2f})")
        
        # Sort services on the overloaded node from heaviest to lightest
        services_on_A = [s for s, n in self.current_placements.items() if n == node_A]
        service_heaviness = []
        for s in services_on_A:
            i_idx = self.service_names.index(s)
            e_i = E_dict[i_idx]
            h = torch.norm(e_i, p=1).item()
            service_heaviness.append((s, h))
        service_heaviness.sort(key=lambda x: x[1], reverse=True)
        
        for m_A_tuple in service_heaviness:
            m_A = m_A_tuple[0]
            m_A_idx = self.service_names.index(m_A)
            
            lat_curr = current_p99
            logger.info(f"Trying to move the heaviest service '{m_A}'. Baseline Physical P99 = {lat_curr:.2f}ms")
            
            best_gradient = 0.0
            target_node = None
            swap_target = None
            
            candidate_nodes = [n for n in range(self.num_nodes) if n != node_A]
            
            # Evaluate ALL nodes conditionally
            for node_B in candidate_nodes:
                norm_V_B = torch.norm(V_vectors[node_B], p=1).item()
                
                if norm_V_B == 0:
                    # 1. Healthy Node Candidate -> Evaluate Evacuation Cost
                    logger.info(f"Testing Evacuation of {m_A} to healthy Node {node_B}")
                    self._route_traffic(m_A, node_A, node_B, percent=1)
                    
                    time.sleep(60) # Wait for the traffic shift to affect latencies
                    lat_test = self.measure_latency(10)
                    
                    grad = (lat_test - lat_curr) / 0.01 # Standard gradient approximation
                    logger.info(f"Evacuation empirical gradient: {grad:.2f}")
                    
                    if grad < best_gradient:
                        best_gradient = grad
                        target_node = node_B
                        
                    self._route_traffic(m_A, node_B, node_A, percent=0) # Revert
                    
                else:
                    # 2. Overloaded Node Candidate -> Test a service swap
                    lowest_joint_cost = norm_V_A + norm_V_B
                    best_m_B = None
                    
                    services_on_B = [s for s, n in self.current_placements.items() if n == node_B]
                    
                    # Find the best service to swap with mathematically
                    for m_B in services_on_B:
                        m_B_idx = self.service_names.index(m_B)
                        
                        # Use additive embeddings to predict the violation on both nodes after the swap (fast vector math)
                        test_sum_A = node_sums[node_A] - E_dict[m_A_idx] + E_dict[m_B_idx]
                        test_sum_B = node_sums[node_B] - E_dict[m_B_idx] + E_dict[m_A_idx]
                        
                        v_A_test = self.calculate_violation_vector(test_sum_A)
                        v_B_test = self.calculate_violation_vector(test_sum_B)
                        
                        cost = torch.norm(v_A_test, p=1).item() + torch.norm(v_B_test, p=1).item()
                        
                        if cost < lowest_joint_cost:
                            lowest_joint_cost = cost
                            best_m_B = m_B
                            
                    if best_m_B is not None:
                        logger.info(f"Found best swap candidate: {m_A} <-> {best_m_B}. Testing traffic shift...")
                        self._route_traffic(m_A, node_A, node_B, percent=1)
                        self._route_traffic(best_m_B, node_B, node_A, percent=1)
                        
                        time.sleep(60) 
                        lat_test = self.measure_latency(10)
                        
                        grad = (lat_test - lat_curr) / 0.01
                        logger.info(f"Swap empirical gradient: {grad:.2f}")
                        
                        if grad < best_gradient:
                            best_gradient = grad
                            target_node = node_B
                            swap_target = best_m_B
                            
                        self._route_traffic(m_A, node_B, node_A, percent=0)
                        self._route_traffic(best_m_B, node_A, node_B, percent=0)
                        
            # Immediate Execution Step
            if best_gradient < 0:
                logger.info(f"Found a good mitigation action! (Gradient = {best_gradient:.2f})")
                
                # Append to metric records
                self.eval_results.append({
                    "cycle_time": time.time(),
                    "overloaded_node": node_A,
                    "target_node": target_node,
                    "evacuated_service": m_A,
                    "swap_service": swap_target,
                    "gradient": best_gradient,
                    "action": "swap" if swap_target else "evacuation"
                })
                
                if swap_target is None:
                    # Move the service completely
                    self._execute_full_placement(m_A, target_node)
                else:
                    # Swap the services completely
                    self._execute_full_placement(m_A, target_node)
                    self._execute_full_placement(swap_target, node_A)
                
                self.save_results()
                break # Stop checking after finding a good mitigation action

# Standalone Testing Executions
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Online Gradient Router Execution")
    parser.add_argument("--services", type=str, nargs='+', required=True, help="List of service names")
    parser.add_argument("--en-max", type=float, default=5.0, help="Node limit capacity constraint")
    parser.add_argument("--dim", type=int, default=64, help="Embedding dimension size")
    args = parser.parse_args()
    
    E_N_max = torch.ones(args.dim) * args.en_max
    router = OnlineGradientRouter(args.services, E_N_max)
    
    # Creates random synthetic embeddings if running as a standalone for testing
    mock_E_dict = [torch.rand(args.dim) for _ in range(len(args.services))]
    router.loop(mock_E_dict)
    router.save_results()
    logger.info("Standalone Gradient Router execution completed.")
