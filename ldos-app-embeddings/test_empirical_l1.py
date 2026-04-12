import os
import sys
import time
import subprocess
import torch
import torch.nn as nn
import glob

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
            print(f"SSH Agent started (PID: {os.environ.get('SSH_AGENT_PID')})")
            
            # This will prompt the user in stdout/stderr for the passphrase!
            subprocess.run(['ssh-add', '/users/mkraman/.ssh/id_rsa'], check=True)
        else:
            print("SSH Identity already available via existing agent environment.")
            subprocess.run(['ssh-add', '-l'], check=True, stdout=subprocess.DEVNULL)
    except Exception as e:
        print(f"Error connecting to agent: {e}")

def get_node_sum(model, E_N_max, trace_tensor, services, target_node_idx):
    # trace_tensor shape: (M, 60, F)
    # Get M service latent vectors
    with torch.no_grad():
        E_vectors = model.encode(trace_tensor).squeeze(0).cpu() # M x d
    
    # We only care about services actually running on the target_node.
    # We can get standard placements from the socialNetwork divide
    current_placements = {}
    import json
    for svc in services:
        cfg = f"{base_dir}/socialNetwork/kubernetes/{svc}/deployment.json"
        if os.path.exists(cfg):
            with open(cfg, 'r') as f:
                dj = json.load(f)
                if 'nodeSelector' in dj['spec']['template']['spec']:
                    hostname = dj['spec']['template']['spec']['nodeSelector'].get('kubernetes.io/hostname', '')
                    if '198' in hostname: current_placements[svc] = 1 # clnode198 is usually node 1 or 0
                    elif '199' in hostname: current_placements[svc] = 3
                    elif '215' in hostname: current_placements[svc] = 4
                    elif '216' in hostname: current_placements[svc] = 2
                    elif '218' in hostname: current_placements[svc] = 0
    
    # Sum embeddings for clnode198
    e_sum_n = torch.zeros_like(E_N_max)
    for i_name, n_assigned in current_placements.items():
        if n_assigned == target_node_idx:
            i_idx = services.index(i_name)
            e_sum_n += E_vectors[i_idx]
            
    # Calculate variation -> v_n = max(0, e_sum_n - E_N_max)
    diff = e_sum_n - E_N_max
    v_n = torch.clamp(diff, min=0.0)
    l1_norm = torch.norm(v_n, p=1).item()
    return l1_norm

if __name__ == "__main__":
    setup_ssh_agent()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    preprocessor = TimeSeriesPreprocessor()
    
    data_list, services = parse_offline_data_directories(preprocessor)
    F_input = 13 if len(data_list) == 0 else data_list[0]['M_tensor'].shape[-1]
    
    model = AdditiveAutoencoder(input_size=F_input).to(device)
    model.eval()
    ckpt_dir = os.path.join(base_dir, "results/checkpoints")
    latest_model = max(glob.glob(os.path.join(ckpt_dir, "*.pth")), key=os.path.getctime)
    model.load_state_dict(torch.load(latest_model, map_location=device))
    
    E_N_max = model.get_max_node_tensor(device=device).cpu()
    
    injector = AnomalyInjector()
    target_node = "clnode218.clemson.cloudlab.us"
    target_idx = 0
    
    print("\n[STEP 0] Ensuring clean network state before evaluations...")
    injector.remove_all_stress(target_node)
    
    print("\n[STEP 1] Generating Peace-Time Latent Baseline Offset...")
    # Wait 30 seconds for stability
    time.sleep(30) 
    print("Collecting 60s normal trace...")
    trace_base = collect_real_time_60s_trace(services, preprocessor, device)
    base_l1 = get_node_sum(model, E_N_max, trace_base, services, target_node_idx=target_idx)
    
    print(f"--> NORMAL PEACE-TIME L1 DEVIATION: {base_l1:.2f}")
    
    print(f"\n[STEP 2] Injecting Network Delay Anomaly on {target_node}...")
    injector.apply_net_delay(target_node, duration=150)
    
    # Wait for chaos to settle mechanically
    time.sleep(30)
    print("Collecting 60s anomalous trace...")
    trace_anomaly = collect_real_time_60s_trace(services, preprocessor, device)
    anomaly_l1 = get_node_sum(model, E_N_max, trace_anomaly, services, target_node_idx=target_idx)
    
    print(f"--> ANOMALOUS CPU STRESS L1 DEVIATION: {anomaly_l1:.2f}")
    
    print("\n[STEP 3] Cleaning up anomalies...")
    injector.remove_all_stress(target_node)
    
    print(f"\n--- EMPIRICAL THRESHOLD RESULTS ---")
    print(f"Normal Trace L1 Violation: {base_l1:.2f}")
    print(f"Anomalous Trace L1 Violation: {anomaly_l1:.2f}")
    
    if anomaly_l1 > base_l1:
        optimal = base_l1 + ((anomaly_l1 - base_l1) / 2.0)
        print(f"RECOMMENDED THRESHOLD TO PREVENT FALSE POSITIVES: {optimal:.2f}")
    else:
        print("WARNING: Anomaly L1 did not exceed Base L1! The Autoencoder embeddings failed to saturate!")
