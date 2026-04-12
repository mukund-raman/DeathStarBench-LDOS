import os
import time
import logging
import subprocess
import threading
import argparse
import re
import json
import torch

logger = logging.getLogger("Orchestrator")

class AnomalyInjector:
    def __init__(self, ssh_user="mkraman", default_nodes=None):
        self.ssh_user = ssh_user
        self.default_nodes = default_nodes or [
            "clnode218.clemson.cloudlab.us",
            "clnode198.clemson.cloudlab.us",
            "clnode216.clemson.cloudlab.us",
            "clnode199.clemson.cloudlab.us",
            "clnode215.clemson.cloudlab.us"
        ]
        self.running_processes = []
        self._installed_nodes = set()
        # Node mapping: K8s node name -> FQDN
        self.node_map = {
            "node0": "clnode198.clemson.cloudlab.us",
            "node1": "clnode218.clemson.cloudlab.us",
            "node2": "clnode216.clemson.cloudlab.us",
            "node3": "clnode215.clemson.cloudlab.us",
            "node4": "clnode199.clemson.cloudlab.us",
            "node5": "clnode224.clemson.cloudlab.us" 
        }

    def get_ingress_node(self):
        """Dynamically identifies the node hosting the nginx-thrift ingress."""
        try:
            cmd = "kubectl get pods -l app=nginx-thrift -o jsonpath='{.items[0].spec.nodeName}'"
            full_node_name = subprocess.check_output(cmd, shell=True, text=True).strip()
            # Extract just the prefix (e.g., node4 from node4.app-embeddings...)
            node_name = full_node_name.split('.')[0]
            fqdn = self.node_map.get(node_name)
            if not fqdn:
                # Direct fallback for clnodeXXX style if it matches node mapping
                return f"cl{node_name}.clemson.cloudlab.us"
            return fqdn
        except Exception as e:
            logger.error(f"Failed to dynamically detect ingress node: {e}")
            return "clnode199.clemson.cloudlab.us"
        
    def _install_dependencies_if_needed(self, node):
        if node not in self._installed_nodes:
            logger.info(f"Checking & installing anomaly tool dependencies on {node}...")
            cmd = "sudo DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y stress-ng sysbench >/dev/null 2>&1"
            self._execute_sync_ssh(node, cmd)
            self._installed_nodes.add(node)        
    # Core SSH Execution Logic
    def _execute_async_ssh(self, node, command):
        """Execute an SSH command asynchronously in the background."""
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", 
            f"{self.ssh_user}@{node}", 
            command
        ]
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.running_processes.append(proc)
        return proc

    def _execute_sync_ssh(self, node, command):
        """Execute an SSH command synchronously."""
        ssh_cmd = f"ssh -o StrictHostKeyChecking=no {self.ssh_user}@{node} '{command}'"
        result = subprocess.run(ssh_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0

    # Anomaly Injection Methods
    def apply_cpu_stress(self, node, duration=120):
        self._install_dependencies_if_needed(node)
        logger.info(f"Injecting Extreme CPU Stress on {node} for {duration}s...")
        # Use all cores plus aggressive matrix math
        cmd = f"sudo nice -n -20 stress-ng --cpu 0 --cpu-method all --matrix-size 512 --timeout {duration}s"
        self._execute_async_ssh(node, cmd)

    def apply_mem_stress(self, node, duration=120):
        self._install_dependencies_if_needed(node)
        logger.info(f"Injecting Extreme Memory Stress on {node} for {duration}s...")
        # Use more VM workers and higher byte count
        cmd = f"sudo nice -n -20 stress-ng --vm 64 --vm-bytes 95% --vm-hang 0 --timeout {duration}s"
        self._execute_async_ssh(node, cmd)

    def apply_net_delay(self, node, duration=120):
        logger.info(f"Injecting 45ms Network Delay on {node}...")
        cmd = (f"sudo tc qdisc del dev docker0 root netem 2>/dev/null; sudo tc qdisc add dev docker0 root netem delay 45ms 5ms distribution normal; "
               f"sudo tc qdisc del dev cni0 root netem 2>/dev/null; sudo tc qdisc add dev cni0 root netem delay 45ms 5ms distribution normal")
        self._execute_sync_ssh(node, cmd)

    def remove_net_delay(self, node):
        logger.info(f"Removing Network Delay on {node}...")
        cmd = "sudo tc qdisc del dev docker0 root netem 2>/dev/null || true; sudo tc qdisc del dev cni0 root netem 2>/dev/null || true"
        self._execute_sync_ssh(node, cmd)

    def apply_disk_stress(self, node, duration=120):
        self._install_dependencies_if_needed(node)
        # 10GB file size to ensure cache misses
        prep_cmd = "sudo sysbench fileio cleanup > /dev/null 2>&1 || true; sudo sysbench fileio --file-total-size=10G prepare > /dev/null 2>&1"
        self._execute_sync_ssh(node, prep_cmd)
        
        logger.info(f"Injecting Aggressive Disk I/O Stress on {node} for {duration}s...")
        cmd = f"sudo sysbench fileio --file-total-size=10G --file-test-mode=rndrw --file-extra-flags=direct --max-requests=0 --time={duration} run"
        self._execute_async_ssh(node, cmd)

    def clean_disk_stress(self, node):
        logger.info(f"Cleaning sysbench files on {node}...")
        cmd = "sudo sysbench fileio cleanup > /dev/null 2>&1 || true"
        self._execute_sync_ssh(node, cmd)

    def remove_all_stress(self, node):
        logger.info(f"Removing all active stress injections on {node}...")
        self.remove_net_delay(node)
        self.clean_disk_stress(node)
        # Kill any lingering stress-ng
        self._execute_sync_ssh(node, "sudo pkill stress-ng || true")

    def get_target_node(self):
        """Dynamically identifies the node with the highest pod count to maximize impact."""
        try:
            # Count pods per node
            cmd = "kubectl get pods -o wide --no-headers | awk '{print $7}' | sort | uniq -c"
            counts = subprocess.check_output(cmd, shell=True, text=True).strip().splitlines()
            
            node_counts = []
            for line in counts:
                parts = line.strip().split()
                if len(parts) == 2:
                    count = int(parts[0])
                    # Full K8s node name
                    full_name = parts[1]
                    node_prefix = full_name.split('.')[0]
                    fqdn = self.node_map.get(node_prefix, f"cl{node_prefix}.clemson.cloudlab.us")
                    node_counts.append((fqdn, count))
            
            if not node_counts:
                return "clnode199.clemson.cloudlab.us" # Safe default
                
            # Sort by count descending
            node_counts.sort(key=lambda x: x[1], reverse=True)
            heaviest = node_counts[0][0]
            logger.info(f"Dynamic Targeting: Identified Heaviest Node {heaviest} with {node_counts[0][1]} pods.")
            return heaviest
        except Exception as e:
            logger.error(f"Failed to dynamically detect heaviest node: {e}")
            return "clnode199.clemson.cloudlab.us"

    def measure_latency(self):
        """Measures E2E P99 latency using wrk2 mixed workload script @ 1000 RPS."""
        script = "/users/mkraman/DeathStarBench-LDOS/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"
        cmd = f"/users/mkraman/DeathStarBench-LDOS/wrk2/wrk -D exp -t 2 -c 100 -d 15 -L -s {script} http://localhost:32000/wrk2-api/mixed-workload -R 1000"
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
            match = re.search(r'99\.000%\s+([\d\.]+)(ms|us|s|m)', out)
            if match:
                val = float(match.group(1))
                unit = match.group(2)
                if unit == "us": val /= 1000.0
                elif unit == "s": val *= 1000.0
                elif unit == "m": val *= 60000.0
                return val
        except Exception as e:
            logger.error(f"Latency measurement failed: {e}")
        return -1.0

    def get_node_sum(self, model, E_N_max, trace_tensor, services, target_node_idx):
        """Calculates current L1 violation sum for a specific node index."""
        with torch.no_grad():
            E_vectors = model.encode(trace_tensor).squeeze(0).cpu() # M x d
        
        current_placements = {}
        for svc in services:
            cfg = f"/users/mkraman/DeathStarBench-LDOS/socialNetwork/kubernetes/{svc}/deployment.json"
            if os.path.exists(cfg):
                with open(cfg, 'r') as f:
                    dj = json.load(f)
                    if 'nodeSelector' in dj['spec']['template']['spec']:
                        hostname = dj['spec']['template']['spec']['nodeSelector'].get('kubernetes.io/hostname', '')
                        if '198' in hostname: current_placements[svc] = 0
                        elif '218' in hostname: current_placements[svc] = 1 
                        elif '216' in hostname: current_placements[svc] = 2
                        elif '215' in hostname: current_placements[svc] = 3
                        elif '199' in hostname: current_placements[svc] = 4
                        elif '224' in hostname: current_placements[svc] = 5
        
        e_sum_n = torch.zeros_like(E_N_max)
        for i_name, n_assigned in current_placements.items():
            if n_assigned == target_node_idx:
                if i_name in services:
                    i_idx = services.index(i_name)
                    e_sum_n += E_vectors[i_idx]
                
        diff = e_sum_n - E_N_max
        v_n = torch.clamp(diff, min=0.0)
        return torch.norm(v_n, p=1).item()

    def calibrate_thresholds(self, services, preprocessor, model, E_N_max, collect_trace_func):
        """
        Integrated calibration: measures baseline and anomalies to derive a singular threshold.
        Targeting 2.5x-3.0x latency spike.
        """
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        target_node = self.get_target_node()
        # Find index for target_node from node_map
        target_idx = -1
        for node_key, fqdn in self.node_map.items():
            if fqdn == target_node:
                target_idx = int(node_key.replace('node', ''))
                break
        
        logger.info(f"=== Starting Empirical Calibration on Ingress Node: {target_node} (Index: {target_idx}) ===")
        self.remove_all_stress(target_node)
        
        logger.info("[Baseline] Measuring initial state...")
        trace_base = collect_trace_func(services, preprocessor, device)
        base_l1 = self.get_node_sum(model, E_N_max, trace_base, services, target_idx)
        base_latency = self.measure_latency()
        logger.info(f"Baseline: Latency={base_latency:.2f}ms, L1={base_l1:.2f}")

        anomalies = [
            ("CPU", self.apply_cpu_stress),
            ("Memory", self.apply_mem_stress),
            ("Disk I/O", self.apply_disk_stress),
            ("Network", self.apply_net_delay)
        ]

        # Cleanup existing stress just in case
        self.remove_all_stress(target_node)
        time.sleep(10)

        thresholds = []
        for name, func in anomalies:
            logger.info(f"[{name}] Injecting stress...")
            func(target_node, duration=30)
            time.sleep(15) # Shorter wait for rapid run
            
            trace_anom = collect_trace_func(services, preprocessor, device)
            anom_l1 = self.get_node_sum(model, E_N_max, trace_anom, services, target_idx)
            anom_latency = self.measure_latency()
            
            self.remove_all_stress(target_node)
            time.sleep(15) # Shorter cooldown
            
            multi = (anom_latency / base_latency) if base_latency > 0 else 0
            logger.info(f"  --> {name}: Latency={anom_latency:.2f}ms ({multi:.2f}x), L1={anom_l1:.2f}")
            if anom_l1 > base_l1:
                thresholds.append(anom_l1)

        if not thresholds:
            logger.error("Calibration failed to detect L1 saturations!")
            return 1.5 # Safe default fallback

        # Recommended threshold: middle ground favoring a bit of headroom
        recommended = base_l1 + ((min(thresholds) - base_l1) * 0.5) 
        logger.info(f"=== Calibration Complete. Recommended Threshold: {recommended:.2f} ===")
        return recommended

    # Timeline Execution
    def run_15_minute_timeline(self, target_nodes=None):
        """
        Executes the mandatory 15-minute anomaly injection schedule defined in Draft 2.
        """
        nodes = target_nodes or self.default_nodes
        
        logger.info("=== Starting 15-Minute Anomaly Injection Timeline ===")
        
        # Min 0-2: Warmup & Baseline
        logger.info("[Min 0-2] Warmup & Baseline. No stressors.")
        time.sleep(120)
        
        # Min 2-4: CPU Stress
        logger.info("[Min 2-4] Launching CPU Stress...")
        for node in nodes:
            self.apply_cpu_stress(node, 120)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 4-6: Memory Stress
        logger.info("[Min 4-6] Launching Memory Stress...")
        for node in nodes:
            self.apply_mem_stress(node, 120)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 6-8: Network Delay
        logger.info("[Min 6-8] Launching Network Delay...")
        for node in nodes:
            self.apply_net_delay(node)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 8-10: Disk I/O Stress
        logger.info("[Min 8-10] Launching Disk I/O Stress...")
        for node in nodes:
            self.apply_disk_stress(node, 120)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 10-12: Multi-Stress (CPU + Network)
        logger.info("[Min 10-12] Launching Multi-Stress (CPU + Network)...")
        for node in nodes:
            self.apply_cpu_stress(node, 120)
            self.apply_net_delay(node)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 12-14: Multi-Stress (Memory + Disk)
        logger.info("[Min 12-14] Launching Multi-Stress (Mem + Disk)...")
        for node in nodes:
            self.apply_mem_stress(node, 120)
            self.apply_disk_stress(node, 120)
        time.sleep(120)
        for node in nodes:
            self.remove_all_stress(node)
            
        # Min 14-15: Cooldown
        logger.info("[Min 14-15] Cooldown Phase...")
        time.sleep(60)
        
        self.kill_all_local_ssh()
        logger.info("=== 15-Minute Anomaly Injection Timeline Completed ===")

# Standalone Testing Executions
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Anomaly Injector CLI Execution")
    parser.add_argument("--timeline", action="store_true", help="Execute the fully automated 15-minute anomaly injection schedule")
    parser.add_argument("--node", type=str, help="Target node for manual stress injection")
    parser.add_argument("--cpu", action="store_true", help="Apply manual CPU stress")
    parser.add_argument("--mem", action="store_true", help="Apply manual Memory stress")
    parser.add_argument("--net", action="store_true", help="Apply manual Network delay")
    parser.add_argument("--clear", action="store_true", help="Remove all anomalies from target node")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    injector = AnomalyInjector()
    
    if args.timeline:
        injector.run_15_minute_timeline()
    elif args.node:
        if args.clear:
            injector.remove_all_stress(args.node)
        else:
            if args.cpu: injector.apply_cpu_stress(args.node)
            if args.mem: injector.apply_mem_stress(args.node)
            if args.net: injector.apply_net_delay(args.node)
    else:
        parser.print_help()
