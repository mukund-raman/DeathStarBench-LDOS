import time
import logging
import datetime
import json
import os
import random
import argparse
import subprocess
import sys

# Needs AnomalyInjector to perform real testing
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_collection.anomaly_injector import AnomalyInjector

logger = logging.getLogger("RobustnessEvaluator")

class RobustnessEvaluator:
    """
    Implements Phase 5: Robust Evaluation Framework.
    Injects anomalies and measures the time it takes the system to
    automatically recover (Recovery Time Objective - RTO).
    """
    def __init__(self, injector, measure_latency_fn, monitor_callback=None, timestamp=None):
        self.injector = injector
        self.measure_latency_fn = measure_latency_fn
        self.monitor_callback = monitor_callback
        
        self.timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.eval_results = []
        
        # The anomalies to test - dynamically targeted
        self.anomalies = [
            ("Network", self.injector.get_ingress_node()),
            ("CPU", self.injector.get_ingress_node()),
            ("Memory", self.injector.get_ingress_node()),
            ("Disk I/O", self.injector.get_ingress_node())
        ]
    
    # Evaluation Log Saving
    def save_results(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eval_dir = os.path.join(base_dir, "results", "evaluations")
        os.makedirs(eval_dir, exist_ok=True)
        filename = os.path.join(eval_dir, f"robustness_{self.timestamp}.json")
        with open(filename, 'w') as f:
            json.dump(self.eval_results, f, indent=4)
        logger.info(f"Robustness evaluations saved to {filename}")
        
    # Core Logic
    def evaluate(self):
        logger.info("=== Starting Robustness Evaluation ===")
        # Note: 500 RPS workload is naturally handled continuously by the unified `main.py` wg_thread.
        
        for anomaly_type, target_node in self.anomalies:
            logger.info(f"--- Testing {anomaly_type} Anomaly Target on {target_node} ---")
            
            # Shortened 30s stabilization for rapid run
            logger.info("Waiting 30 seconds for stabilization...")
            time.sleep(30)
            
            l_base = self.measure_latency_fn(0)
            logger.info(f"Baseline P99 Latency Recorded: {l_base:.2f}ms")
            
            # Inject the anomaly
            if anomaly_type == "CPU":
                self.injector.apply_cpu_stress(target_node)
            elif anomaly_type == "Memory":
                self.injector.apply_mem_stress(target_node)
            elif anomaly_type == "Network":
                self.injector.apply_net_delay(target_node)
            elif anomaly_type == "Disk I/O":
                self.injector.apply_disk_stress(target_node)
                
            logger.info("Anomaly injected. Waiting for latency to spike > 2.0x baseline...")
            t_spike = None
            
            # Shorter wait for spike in rapid run
            max_wait_spike_time = time.time() + 90
            while time.time() < max_wait_spike_time:
                curr_p99 = self.measure_latency_fn(-1)
                if curr_p99 >= 2.0 * l_base or curr_p99 >= 300.0:
                    t_spike = time.time()
                    logger.warning(f"SPIKE DETECTED at {curr_p99:.2f}ms (>= 2.0x {l_base:.2f}ms). Commencing Recovery tracking!")
                    break
                time.sleep(5)
                
            if t_spike is None:
                logger.error(f"FAIL: Anomaly {anomaly_type} failed to produce sufficient latency spike (>2.0x) after 3 minutes.")
                self.eval_results.append({
                    "anomaly_type": anomaly_type,
                    "target_node": target_node,
                    "baseline_p99": l_base,
                    "recovery_time": 0.0,
                    "status": "FAIL_NO_SPIKE"
                })
                self.save_results()
                # Ensure cleanup regardless
                if hasattr(self.injector, 'remove_all_stress'):
                    self.injector.remove_all_stress(target_node)
                time.sleep(60)
                continue
            
            # Now track successfully injected anomaly for RTO recovery
            success = False
            while True:
                # Check the current latency globally
                curr_p99 = self.measure_latency_fn(-1)
                
                # Trigger mitigation logic if a callback is provided
                if self.monitor_callback:
                    self.monitor_callback()
                    
                # Check if latency has recovered (within 10% of baseline)
                if curr_p99 <= 1.10 * l_base:
                    t_recover = time.time()
                    rto = t_recover - t_spike
                    logger.info(f"PASS: System recovered from {anomaly_type} in {rto:.1f} sec.")
                    success = True
                    self.eval_results.append({
                        "anomaly_type": anomaly_type,
                        "target_node": target_node,
                        "baseline_p99": l_base,
                        "recovery_time": rto,
                        "status": "PASS"
                    })
                    self.save_results()
                    break
                        
                # Timeout if it takes too long to recover
                if (time.time() - t_spike) > 300:
                    logger.error(f"FAIL: Timeout resolving {anomaly_type}. System did not recover within 300s.")
                    self.eval_results.append({
                        "anomaly_type": anomaly_type,
                        "target_node": target_node,
                        "baseline_p99": l_base,
                        "recovery_time": 300.0,
                        "status": "FAIL_TIMEOUT"
                    })
                    self.save_results()
                    break
                    
                time.sleep(10) # Poll every 10 seconds
                
            # Remove anomalies to restore original state
            if hasattr(self.injector, 'remove_all_stress'):
                self.injector.remove_all_stress(target_node)
            logger.info("Removed anomaly. Waiting 2 minutes for the system to stabilize...")
            time.sleep(120)
            
        logger.info("=== Robustness Evaluation Complete ===")
        return self.eval_results

# Standalone Testing Executions
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Robustness Evaluator Execution")
    parser.add_argument("--url", type=str, default="http://localhost:32000", help="Target cluster URL")
    args = parser.parse_args()

    def live_measure_latency(dur=10):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wrk_binary = os.path.join(base_dir, "socialNetwork/wrk2/wrk")
        script = os.path.join(base_dir, "socialNetwork/wrk2/scripts/social-network/mixed-workload.lua")
        cmd = [wrk_binary, "-D", "exp", "-t", "4", "-c", "64", "-d", "10s", "-L", "-s", script, args.url, "-R", "500"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in res.stdout.splitlines():
                if "99.000%" in line:
                    val = line.split()[-1]
                    p99_val = float(val.replace('ms','').replace('us','').replace('s','').replace('m','').strip())
                    if 'us' in val: p99_val /= 1000.0
                    return p99_val
        except Exception as e:
            logger.error(f"Failed to fetch real P99 latency: {e}")
        return 999.0
        
    injector = AnomalyInjector()
    evaluator = RobustnessEvaluator(injector=injector, measure_latency_fn=live_measure_latency)
    
    logger.info("Executing Standalone Phase 5 Robustness Target Injector...")
    evaluator.evaluate()
    logger.info("Standalone Robustness Evaluator execution completed.")
