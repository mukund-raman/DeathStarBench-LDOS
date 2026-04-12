import os
import sys
import time
import subprocess
import torch

base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(base_dir)

from data_collection.preprocess import TimeSeriesPreprocessor
from models.autoencoder import AdditiveAutoencoder
from data_collection.anomaly_injector import AnomalyInjector
from main import collect_real_time_60s_trace, parse_offline_data_directories

def setup_ssh_agent():
    try:
        if 'SSH_AUTH_SOCK' not in os.environ:
            print("SSH Agent not found in environment. Starting a new agent...")
            agent_out = subprocess.check_output(['ssh-agent', '-s']).decode()
            for line in agent_out.splitlines():
                if 'SSH_AUTH_SOCK' in line:
                    sock_val = line.split('SSH_AUTH_SOCK=')[1].split(';')[0]
                    os.environ['SSH_AUTH_SOCK'] = sock_val
                if 'SSH_AGENT_PID' in line:
                    pid_val = line.split('SSH_AGENT_PID=')[1].split(';')[0]
                    os.environ['SSH_AGENT_PID'] = pid_val
            subprocess.run(['ssh-add', '/users/mkraman/.ssh/id_rsa'], check=True)
        else:
            print("SSH Identity already available.")
    except Exception as e:
        print(f"Error connecting to agent: {e}")

def get_node_sum(model, E_N_max, trace_tensor, services, target_node_idx):
    with torch.no_grad():
        E_vectors = model.encode(trace_tensor).squeeze(0).cpu() # M x d
    
    current_placements = {}
    import json
    for svc in services:
        cfg = f"{base_dir}/socialNetwork/kubernetes/{svc}/deployment.json"
        if os.path.exists(cfg):
            with open(cfg, 'r') as f:
                dj = json.load(f)
                if 'nodeSelector' in dj['spec']['template']['spec']:
                    hostname = dj['spec']['template']['spec']['nodeSelector'].get('kubernetes.io/hostname', '')
                    if '198' in hostname: current_placements[svc] = 1 
                    elif '199' in hostname: current_placements[svc] = 4
                    elif '215' in hostname: current_placements[svc] = 3
                    elif '216' in hostname: current_placements[svc] = 2
                    elif '218' in hostname: current_placements[svc] = 0
    
    e_sum_n = torch.zeros_like(E_N_max)
    for i_name, n_assigned in current_placements.items():
        if n_assigned == target_node_idx:
            if i_name in services:
                i_idx = services.index(i_name)
                e_sum_n += E_vectors[i_idx]
            
    diff = e_sum_n - E_N_max
    v_n = torch.clamp(diff, min=0.0)
    return torch.norm(v_n, p=1).item()

def measure_latency():
    script = "/users/mkraman/DeathStarBench-LDOS/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"
    cmd = f"/users/mkraman/DeathStarBench-LDOS/wrk2/wrk -D exp -t 2 -c 100 -d 20 -L -s {script} http://localhost:32000/wrk2-api/mixed-workload -R 500"
    out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
    import re
    match = re.search(r'99\.000%\s+([\d\.]+)(ms|us|s|m)', out)
    if match:
        val = float(match.group(1))
        unit = match.group(2)
        if unit == "us": val /= 1000.0
        elif unit == "s": val *= 1000.0
        elif unit == "m": val *= 60000.0
        return val
    return -1.0

if __name__ == "__main__":
    setup_ssh_agent()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    preprocessor = TimeSeriesPreprocessor()
    
    data_list, services = parse_offline_data_directories(preprocessor)
    F_input = 13 if len(data_list) == 0 else data_list[0]['M_tensor'].shape[-1]
    
    model = AdditiveAutoencoder(input_size=F_input).to(device)
    model.eval()
    import glob
    ckpt_dir = os.path.join(base_dir, "results/checkpoints")
    latest_model = max(glob.glob(os.path.join(ckpt_dir, "*.pth")), key=os.path.getctime)
    model.load_state_dict(torch.load(latest_model, map_location=device))
    
    E_N_max = model.get_max_node_tensor(device=device).cpu()
    
    injector = AnomalyInjector()
    # Testing exclusively on the INGRESS NODE to capture end-to-end P99 properly
    target_node = "clnode199.clemson.cloudlab.us"
    target_idx = 4
    
    print("\n[CLEANING STATE]")
    injector.remove_all_stress(target_node)
    
    print("\n[BASELINE PHASE]")
    trace_base = collect_real_time_60s_trace(services, preprocessor, device)
    base_l1 = get_node_sum(model, E_N_max, trace_base, services, target_idx)
    base_latency = measure_latency()
    print(f"--> Baseline Latency: {base_latency:.2f} ms")
    print(f"--> Baseline L1: {base_l1:.2f}")

    anomalies = [
        ("CPU", injector.apply_cpu_stress),
        ("Memory", injector.apply_mem_stress),
        ("Disk I/O", injector.apply_disk_stress),
        ("Network", injector.apply_net_delay)
    ]

    results = []

    for name, func in anomalies:
        print(f"\n[{name} ANOMALY PHASE]")
        func(target_node, duration=150)
        time.sleep(15) 
        
        trace_anom = collect_real_time_60s_trace(services, preprocessor, device)
        anom_l1 = get_node_sum(model, E_N_max, trace_anom, services, target_idx)
        anom_latency = measure_latency()
        
        injector.remove_all_stress(target_node)
        time.sleep(15) 
        
        multi = (anom_latency / base_latency) if base_latency > 0 else 0
        l1_diff = anom_l1 - base_l1
        
        print(f"  --> {name} Latency: {anom_latency:.2f} ms ({multi:.2f}x)")
        print(f"  --> {name} L1: {anom_l1:.2f} (Spike: +{l1_diff:.2f})")
        results.append((name, multi, anom_l1))

    print("\n\n=== FINAL CALIBRATION OUTPUT ===")
    print(f"Baseline Latency: {base_latency:.2f} ms | Baseline L1: {base_l1:.2f}")
    thresholds = []
    for name, multi, l1 in results:
        print(f"{name: <10} | Latency Spike: {multi:.2f}x | L1 Score: {l1:.2f}")
        if l1 > base_l1:
            thresholds.append(l1)
            
    if thresholds:
        optimal = base_l1 + ((min(thresholds) - base_l1) / 2.0)
        print(f"\n>>> RECOMMENDED L1 THRESHOLD: {optimal:.2f} <<<")
    else:
        print("\n>>> FAILED TO DETECT SATURATIONS <<<")
