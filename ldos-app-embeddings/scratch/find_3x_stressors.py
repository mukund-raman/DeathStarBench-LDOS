import subprocess
import time
import re
import os

# CONFIGURATION
INGRESS_NODE = "clnode199.clemson.cloudlab.us" # node4
RPS = 1000
WRK_BINARY = "/users/mkraman/DeathStarBench-LDOS/wrk2/wrk"
SCRIPT = "/users/mkraman/DeathStarBench-LDOS/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"
URL = "http://localhost:32000/wrk2-api/mixed-workload"

def measure_latency():
    cmd = f"{WRK_BINARY} -D exp -t 2 -c 100 -d 15 -L -s {SCRIPT} {URL} -R {RPS}"
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
    except: pass
    return -1.0

def run_ssh(cmd, node=INGRESS_NODE):
    ssh_cmd = f"ssh -o StrictHostKeyChecking=no mkraman@{node} \"{cmd}\""
    subprocess.run(ssh_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def clean_stress(node=INGRESS_NODE):
    run_ssh("sudo pkill stress-ng; sudo pkill sysbench; sudo tc qdisc del dev docker0 root netem 2>/dev/null; sudo tc qdisc del dev cni0 root netem 2>/dev/null; true", node)

def test_config(name, stress_cmd, node=INGRESS_NODE):
    print(f"\n--- Testing: {name} on {node} ---")
    clean_stress(node)
    time.sleep(5)
    
    base = measure_latency()
    print(f"  Baseline: {base:.2f}ms")
    
    # Start stress
    ssh_cmd = f"nohup {stress_cmd} > /dev/null 2>&1 &"
    run_ssh(ssh_cmd, node)
    time.sleep(10) # Wait for saturation
    
    anom = measure_latency()
    clean_stress(node)
    
    multi = anom / base if base > 0 else 0
    print(f"  Anomalous: {anom:.2f}ms ({multi:.2f}x)")
    return multi

if __name__ == "__main__":
    print(f"Starting Refined Intensity Search @ {RPS} RPS")
    
    # Target 1: Ingress node network sweet spot
    print("\n--- Phase 1: Ingress Network Sweep ---")
    for delay in [20, 25, 30]:
        cmd = f"sudo tc qdisc add dev docker0 root netem delay {delay}ms; sudo tc qdisc add dev cni0 root netem delay {delay}ms"
        test_config(f"Net Delay {delay}ms", cmd, INGRESS_NODE)
        time.sleep(10)

    # Target 2: Resource combos on Ingress
    print("\n--- Phase 2: Ingress Resource Contention ---")
    combo_cmd = "sudo stress-ng --cpu 0 --vm 64 --vm-bytes 90% --timeout 60s"
    test_config("CPU/Mem Extreme", combo_cmd, INGRESS_NODE)
    
    print("\nSearch Complete.")
