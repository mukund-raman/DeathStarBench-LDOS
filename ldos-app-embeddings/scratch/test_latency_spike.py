import sys
import os
import time
import subprocess
import re

# Add path so we can import AnomalyInjector
sys.path.append("/users/mkraman/DeathStarBench-LDOS/ldos-app-embeddings")
from data_collection.anomaly_injector import AnomalyInjector

node = "clnode198.clemson.cloudlab.us"
injector = AnomalyInjector()

def measure_latency():
    base_url = "http://130.127.133.16:32000"
    script = "/users/mkraman/DeathStarBench-LDOS/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"
    duration = 20
    cmd = f"/users/mkraman/DeathStarBench-LDOS/wrk2/wrk -D exp -t 2 -c 100 -d {duration} -L -s {script} {base_url}/wrk2-api/mixed-workload -R 500"
    
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
        print("Error measuring:", e)
    return -1

# Setup SSH Agent globally first to prevent password loops!
os.system("eval `ssh-agent -s` && ssh-add /users/mkraman/.ssh/id_rsa < /dev/null")

print("Measuring Baseline...")
base = measure_latency()
print(f"Baseline: {base} ms")

print("\n--- Injecting CPU Stress (nice -20, 256 threads) ---")
injector.apply_cpu_stress(node, duration=60)
time.sleep(10) # Let it spin up
spike = measure_latency()
print(f"CPU Spiked: {spike} ms")
injector.remove_all_stress(node)
time.sleep(10) # Spin down

print("\n--- Injecting Memory Stress (nice -20, 16 threads, 98% alloc) ---")
injector.apply_mem_stress(node, duration=60)
time.sleep(10)
spike = measure_latency()
print(f"Memory Spiked: {spike} ms")
injector.remove_all_stress(node)
time.sleep(10)

print("\n--- Injecting Network Stress (500ms delay on cni0 and enp24s0f1np1) ---")
injector.apply_net_delay(node, duration=60)
time.sleep(10)
spike = measure_latency()
print(f"Network Spiked: {spike} ms")
injector.remove_all_stress(node)

print("\nTests complete!")
