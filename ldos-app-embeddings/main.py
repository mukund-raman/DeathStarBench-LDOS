import argparse
import logging
import os
import sys
import time
import torch
import threading
import glob
import pandas as pd
import numpy as np
import random
import json
import re
import datetime
import subprocess
import traceback

# Add project root to python execution path for inter-module imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data_collection.preprocess import TimeSeriesPreprocessor
from data_collection.collector import setup_ssh_agent, WORKER_NODES, SSH_USER, SSH_KEY, REMOTE_AGENT_PATH, REMOTE_METRICS_PATH, AGENT_SCRIPT
from training.dataset import AdditiveEmbeddingDataset
from training.train import run_training_pipeline, optimize_hyperparameters
from evaluation.online_gradient_router import OnlineGradientRouter
from evaluation.cbo_optimizer import ConstrainedBayesianOptimizer
from evaluation.robustness_evaluator import RobustnessEvaluator
from evaluation.bottleneck_monitor import BottleneckMonitor
from data_collection.anomaly_injector import AnomalyInjector
from models.autoencoder import AdditiveAutoencoder

# Global Logger Initialization
logging.basicConfig(level=logging.INFO, format='%(asctime)s - Orchestrator - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("Main")

def execute_wrk2_workload_generator(base_dir, exp_duration, start_time, latency_container):
    """
    Simulates live user traffic sending requests to the system.
    Runs constantly during the online experiment at 1000 RPS, acting as
    the global pulse parsing the exact P99 latency metric for the cluster
    so that optimizers and robustness evaluation share a single ground truth.
    """
    wrk_binary = os.path.join(base_dir, "../wrk2/wrk")
    # Correct path to social-network script
    script = os.path.join(base_dir, "../socialNetwork/wrk2/scripts/social-network/mixed-workload.lua")
    url = "http://localhost:32000/wrk2-api/mixed-workload"
    
    while (time.time() - start_time) < exp_duration:
        rps = 1000 # Escalated baseline for sensitivity
        interval = 30 # Balanced interval for quicker updates
        logger.info(f"Dynamic Workload Thread: Stressing cluster with {rps} RPS for {interval}s")
        
        cmd = [wrk_binary, "-D", "exp", "-t", "4", "-c", "64", "-d", f"{interval}s", "-L", "-s", script, url, "-R", str(rps)]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        found_p99 = False
        # Parse standard output precisely to find the new P99 metric
        for line in res.stdout.splitlines():
            if "99.000%" in line:
                try:
                    val = line.split()[-1]
                    p99_val = float(val.replace('ms','').replace('us','').replace('s','').replace('m','').strip())
                    if 'us' in val: p99_val /= 1000.0
                    elif 'ms' in val: pass
                    elif 's' in val and 'ms' not in val: p99_val *= 1000.0
                    elif 'm' in val and 'ms' not in val: p99_val *= 60000.0
                    
                    if latency_container and latency_container[0] == 999.0:
                        latency_container.clear()
                    latency_container.append(p99_val)
                    latency_container[:] = latency_container[-10:] # Only keep the last 10 values
                    formatted_container = [round(x, 2) for x in latency_container]
                    logger.info(f"Global latest_p99_latency naturally updated to: {p99_val:.2f}ms | Container: {formatted_container}")
                    found_p99 = True
                except Exception as e:
                    logger.error("Failed to parse integrated P99 latency: " + str(e))
                    
        # If output was not retrieved successfully, delay to prevent infinite spin
        if not found_p99:
            logger.error(f"Failed to fetch P99 latency. stdout: {res.stdout.strip()} | stderr: {res.stderr.strip()}")
            time.sleep(10)

def collect_real_time_60s_trace(services, preprocessor, device):
    """
    Connects to all worker nodes via SSH to collect actual system metrics
    from the past 60 seconds. Returns the processed metrics as a padded
    tensor ready for the model.
    """
    active_nodes = []
    
    # 1. Start continuous data collection loops on worker nodes
    try:
        for node in WORKER_NODES:
            cmd = f"nohup sudo python3 {REMOTE_AGENT_PATH} --output {REMOTE_METRICS_PATH} > /dev/null 2>&1 & echo $!"
            ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}", cmd]
            proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _ = proc.communicate()
            if stdout.decode().strip(): 
                active_nodes.append((node, stdout.decode().strip()))
    except: 
        logger.error("Failed executing trace connections in worker nodes!")
    
    # Wait while telemetry samples accumulate
    time.sleep(60)
    
    # 2. Halt collection
    for node, pid in active_nodes:
        subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}", f"sudo kill {pid}"])
        
    # 3. Pull result CSV files from worker nodes into a single dataframe
    dfs = []
    for node, pid in active_nodes:
        local_temp = f"/tmp/metrics-{node}.csv"
        try:
            ssh_copy = ["scp", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}:{REMOTE_METRICS_PATH}", local_temp]
            subprocess.run(ssh_copy, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            dfs.append(pd.read_csv(local_temp))
            os.remove(local_temp)
        except: 
            logger.error(f"Failed to collect metrics from node {node}")
    if not dfs: 
        logger.error("Failed to collect metrics from any node!")
        return None
    
    # Pad or truncate the trace data to exactly 60 seconds
    df = pd.concat(dfs, ignore_index=True)
    M_tensor, _, _ = preprocessor.preprocess(df)
    T_len = M_tensor.shape[1]
    padded = np.zeros((len(services), 60, M_tensor.shape[2]))
    if T_len >= 60:
        padded = M_tensor[:, :60, :]
    elif T_len > 0:
        padded[:, :T_len, :] = M_tensor
        for i in range(T_len, 60): # Forward fill missing seconds at the end
            padded[:, i, :] = M_tensor[:, -1, :]
            
    # Return the tensor formatted for the neural network
    return torch.tensor(padded).float().to(device)

def background_loops(args, start_time, preprocessor, device, model, router, cbo, monitor, services, latency_container):
    """
    Runs continuously in the background, collecting metrics every minute 
    and executing mathematical placement optimizers (Phase 3, 4, and 6)
    while passively responding to Phase 5 anomaly injections.
    """
    cycle_count = 0
    while (time.time() - start_time) < args.exp_duration:
        # Gate evaluation threading ensuring the underlying cluster is tracking < 100ms
        if not latency_container or latency_container[-1] == 999.0 or latency_container[-1] > 100.0:
            logger.info("Background Loop parked waiting for cluster latency stabilization (<100ms)...")
            time.sleep(5)
            continue
            
        logger.info(f"--- Logical Loop Execution Step: {cycle_count} ---")
        
        # Collect metrics from all nodes for the past 60s via SSH
        x_test = collect_real_time_60s_trace(services, preprocessor, device)
        if x_test is None:
            logger.warning("Trace collection failed, skipping this cycle.")
            cycle_count += 1
            continue
            
        # Calculate autoencoder embeddings for each service
        with torch.no_grad():
            e_i = model.encode(x_test).squeeze(0).cpu() # Matrix of service embeddings (M, d)
        E_dict_list = [e_i[i] for i in range(len(services))]
        
        # Run active optimizers with physical cluster feedback
        current_p99 = latency_container[-1]
        if args.optimizer_mode == "router": 
            router.loop(E_dict_list, current_p99=current_p99)
        elif args.optimizer_mode == "cbo": 
            cbo.loop(E_dict_list, current_p99=current_p99)
        
        # Bottleneck detection
        if not args.no_bottleneck: 
            monitor.process_minute(E_dict_list, is_healthy=(cycle_count < 10))
            
        cycle_count += 1


def parse_offline_data_directories(preprocessor):
    """
    Matches historical trace CSV files from Phase 1 data collection with their 
    corresponding JSON configuration files to extract the P99 latency results.
    """
    data_list = []
    data_files = glob.glob('data_collection/data/collection*/*.txt')
    if not data_files:
        raise FileNotFoundError("No trace metrics found! Ensure data collection completed successfully.")
    
    global_services = None
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # PASS 1: Read valid datasets to compute absolute structural limits natively
    valid_records = []
    all_dfs = []
    for file in data_files:
        try:
            with open(file, 'r') as f:
                first_line = f.readline()
            if not (first_line.startswith('# ') and first_line.strip().endswith('.json')): continue
            json_path = first_line.strip().replace('# ', '', 1)
            
            if not os.path.isabs(json_path) and json_path.startswith("socialNetwork"):
                json_path = os.path.join(base_dir, json_path)
            if not os.path.exists(json_path): continue
            
            with open(json_path, 'r') as f:
                jdata = json.load(f)
                
            df = pd.read_csv(file, comment='#')
            if len(df) == 0: continue
            
            all_dfs.append(df)
            valid_records.append({'file': file, 'df': df, 'jdata': jdata})
        except Exception as e:
            logger.error(f"Failed to pre-parse offline data for {file}: {e}")
            
    if not valid_records:
        raise RuntimeError("No valid parseable data combinations found!")
        
    # Standardize all sequence boundary constraints (0.0 to 1.0) natively across trace datasets
    preprocessor.compute_global_bounds(valid_records)
    
    # PASS 2: Construct actual autoencoder tensor vectors mapping standardized limits
    for record in valid_records:
        file = record['file']
        df = record['df']
        jdata = record['jdata']
        
        try:
            workload_data = jdata.get("mixed-workload", [])
            if not workload_data: continue
            runs = workload_data if isinstance(workload_data, list) else [workload_data]
            
            p99_vals = []
            for r in runs:
                p99_str = str(r.get("p99", ""))
                if not p99_str: continue
                val_str = p99_str.replace("ms", "").replace("us", "").replace("s", "").replace("m", "").strip()
                val = float(val_str)
                if "us" in p99_str: val /= 1000.0
                elif "ms" in p99_str: pass
                elif "s" in p99_str and "m" not in p99_str: val *= 1000.0
                elif "m" in p99_str: val *= 60000.0
                p99_vals.append(val)
                
            if not p99_vals: continue
            p99_latency_val = sum(p99_vals) / len(p99_vals)
            
            placements_dict = jdata.get("placements", {})
            if not placements_dict: continue
            
            services = []
            for node_services in placements_dict.values():
                services.extend(node_services)
            services = sorted(list(set(services)))
            
            M_tensor, _, services_df = preprocessor.preprocess(df, sorted(services))
            if global_services is None:
                global_services = services_df
                
            placement_tensor = np.zeros(len(global_services), dtype=int)
            for node_name, node_services in placements_dict.items():
                node_idx = int(re.search(r'\d+', node_name).group()) - 1 if re.search(r'\d+', node_name) else 0
                for svc in node_services:
                    if svc in global_services:
                        placement_tensor[global_services.index(svc)] = node_idx
                        
            data_list.append({'M_tensor': M_tensor, 'placement': placement_tensor, 'p99_latency': p99_latency_val})
        except Exception as e:
            logger.error(f"Failed Phase 2 bounding mapping on {file}: {e}")
            
    if not data_list:
        raise ValueError("No valid offline data samples were parsed successfully.")
    
    return data_list, global_services

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Additive Autoencoder Application Embeddings Pipeline")
    
    # Options to skip certain phases of the experiment
    parser.add_argument("--no-data", action="store_true", help="Skip Phase 1 Data Collection")
    parser.add_argument("--no-train", action="store_true", help="Skip Phase 2 Model Training Validation")
    parser.add_argument("--optimizer-mode", choices=["cbo", "router", "none"], default="none", help="Choose Active Optimizer (Phase 3 or 4)")
    parser.add_argument("--no-eval", action="store_true", help="Skip Phase 5 Robustness RTO Evaluation testing")
    parser.add_argument("--no-bottleneck", action="store_true", help="Skip Phase 6 Bottleneck EMA Cosine Detection metrics")
    
    # Options to control experiment execution
    parser.add_argument("--run-optuna", action="store_true", help="Run Optuna hyperparameter space analysis")
    parser.add_argument("--exp-duration", type=int, default=300, help="Experiment evaluation duration in seconds")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Initialized Application Embeddings pipeline running on {device}")
    
    # Log structure initialization
    exp_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    exp_dir = os.path.join(base_dir, f"results/experiments/run_{exp_timestamp}")
    os.makedirs(os.path.join(base_dir, "results/checkpoints"), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "results/evaluations"), exist_ok=True)
    os.makedirs(exp_dir, exist_ok=True)
    
    # Establish dynamic file handler pointing directly into current run folder
    file_handler = logging.FileHandler(os.path.join(exp_dir, "experiment.log"), mode='a')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - Orchestrator - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(file_handler)
    
    experiment_summary = {
        "timestamp": exp_timestamp,
        "args": vars(args),
        "phases_executed": {}
    }

    # Step 0: Setup SSH Agent
    setup_ssh_agent()
    
    # Phase 1: Trace Collection
    preprocessor = TimeSeriesPreprocessor()
    if not args.no_data:
        logger.info("=== Phase 1: Data Collection & Anomalies ===")
        injector = AnomalyInjector()
        logger.info("Scheduling metrics initialized.")

    logger.info("Preprocessing traces and converting to rates of change...")
    data_list = []
    services = []
    try:
        # Match trace outputs with target latency metrics
        data_list, services = parse_offline_data_directories(preprocessor)
        logger.info(f"Successfully processed {len(data_list)} offline execution samples.")
        experiment_summary["phases_executed"]["1_data_collection"] = {
            "status": "executed" if not args.no_data else "bypassed",
            "offline_samples": len(data_list)
        }
    except Exception as e:
        logger.error(f"Preprocessing failed: {e}")
        sys.exit(1)

    # Phase 2: Additive Autoencoder Network Training
    dataset = AdditiveEmbeddingDataset(data_list)
    F_input = 13 if len(dataset) == 0 else dataset[0]['x'].shape[-1] # Get feature dimension size

    if not args.no_train:
        logger.info("=== Phase 2: Training Additive Autoencoder & Mega Loss ===")
        # Run hyperparameter tuning if requested
        if args.run_optuna:
            optimize_hyperparameters(dataset, n_trials=5, device=device)
            
        model = run_training_pipeline(dataset, epochs=2, device=device, timestamp=exp_timestamp)
        experiment_summary["phases_executed"]["2_training"] = {"status": "completed", "epochs": 2}
    else:
        # Load the latest checkpoint if possible, otherwise create a new model
        model = AdditiveAutoencoder(input_size=F_input).to(device)
        model.eval()
        ckpt_dir = os.path.join(base_dir, "results/checkpoints")
        pth_files = glob.glob(os.path.join(ckpt_dir, "*.pth"))
        if pth_files:
            latest_model = max(pth_files, key=os.path.getctime)
            logger.info(f"Bypassing training phase. Loading latest pretrained model.")
            model.load_state_dict(torch.load(latest_model, map_location=device))
            experiment_summary["phases_executed"]["2_training"] = {"status": "bypassed_loaded", "model": os.path.basename(latest_model)}
        else:
            logger.warning("Bypassing training, but no checkpoint found. Using untrained weights.")
            experiment_summary["phases_executed"]["2_training"] = {"status": "bypassed_untrained"}

    # Calculate the E_N_max constraint tensor (maximum node capacity)
    logger.info("Evaluating node limit constraint vectors (E_N_max)...")
    E_N_max = model.get_max_node_tensor(device=device).cpu()

    injector = AnomalyInjector()
    
    # Optional Empirical Calibration to determine singular L1 Threshold
    l1_threshold = 1.5 # Default fallback
    if args.optimizer_mode == "router":
        logger.info("Initializing mandatory empirical calibration for Gradient Router threshold...")
        # Clear any stressors first
        ingress_node = injector.get_ingress_node()
        injector.remove_all_stress(ingress_node)
        
        l1_threshold = injector.calibrate_thresholds(
            services=services,
            preprocessor=preprocessor,
            model=model,
            E_N_max=E_N_max,
            collect_trace_func=collect_real_time_60s_trace
        )

    # Shared state between background generation loop and outer evaluations
    latency_container = [999.0] 
    
    # Phases 3 & 4: Online Execution for Gradient Router and Constrained BO
    router = OnlineGradientRouter(service_names=services, E_N_max=E_N_max, measure_latency_fn=lambda d: latency_container[-1], timestamp=exp_timestamp, l1_threshold=l1_threshold)
    cbo = ConstrainedBayesianOptimizer(service_names=services, E_N_max=E_N_max, timestamp=exp_timestamp)
    
    experiment_summary["phases_executed"]["3_gradient_router"] = {
        "status": "enabled" if args.optimizer_mode == "router" else "bypassed",
        "calibrated_l1_threshold": l1_threshold if args.optimizer_mode == "router" else None
    }
    experiment_summary["phases_executed"]["4_cbo_optimizer"] = {
        "status": "enabled" if args.optimizer_mode == "cbo" else "bypassed",
        "gp_fit": "completed" if (not args.no_train and args.optimizer_mode == "cbo") else "bypassed"
    }
    
    if not args.no_train and args.optimizer_mode == "cbo":
        # Precompute offline embeddings so CBO can perform its GP fit
        for data in data_list:
            T_offline = min(data['M_tensor'].shape[1], 60)
            offline_padded = np.zeros((len(services), 60, data['M_tensor'].shape[2]))
            offline_padded[:, :T_offline, :] = data['M_tensor'][:, :T_offline, :]
            for i in range(T_offline, 60):
                offline_padded[:, i, :] = data['M_tensor'][:, T_offline-1, :]
                
            x_test_Offline = torch.tensor(offline_padded).float().to(device).unsqueeze(0)
            with torch.no_grad():
                e_i = model.encode(x_test_Offline).squeeze(0).cpu()
            data['E_dict'] = [e_i[i] for i in range(len(services))]
        cbo.fit_offline(data_list) # GP model fitting

    injector = AnomalyInjector()
    
    # Phases 5 & 6: Set up Robustness Evaluator and Bottleneck Identifier
    evaluator = RobustnessEvaluator(injector=injector, measure_latency_fn=lambda dur: latency_container[-1], timestamp=exp_timestamp)
    monitor = BottleneckMonitor(service_names=services, timestamp=exp_timestamp)

    # Start Execution Subprocess Threadings
    logger.info(f"=== Beginning Phase 3-6 continuous online evaluation for {args.exp_duration}s ===")
    start_time = time.time()
    
    # Ensure remote agent script is deployed on all nodes before online trace collection
    agent_src = os.path.join(base_dir, "data_collection", "agent.py")
    for node in WORKER_NODES:
        subprocess.run(["scp", "-o", "StrictHostKeyChecking=no", agent_src, f"{SSH_USER}@{node}:{REMOTE_AGENT_PATH}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    
    # 1. Start Workload Generator thread to simulate live user traffic
    wg_thread = threading.Thread(target=execute_wrk2_workload_generator, args=(base_dir, args.exp_duration, start_time, latency_container), daemon=True)
    wg_thread.start()

    # 2. Start the online cyclic evaluation thread
    loop_thread = threading.Thread(target=background_loops, args=(args, start_time, preprocessor, device, model, router, cbo, monitor, services, latency_container))
    loop_thread.start()

    if not args.no_eval:
        logger.info("Waiting for tracking metrics stabilization before launching Robustness Injection...")
        while not latency_container or latency_container[-1] == 999.0 or latency_container[-1] > 100.0:
            time.sleep(5)
            
        logger.info("Proceeding to Robustness Target Injection Evaluation...")
        
        # 3. Robustness Evaluator runs sequentially inside the main thread blocking completion
        eval_stats = evaluator.evaluate()
        experiment_summary["phases_executed"]["5_robustness_eval"] = {
            "status": "evaluated",
            "results": eval_stats
        }
    else:
        experiment_summary["phases_executed"]["5_robustness_eval"] = {"status": "bypassed"}
        
    experiment_summary["phases_executed"]["6_bottleneck_monitor"] = {
        "status": "bypassed" if args.no_bottleneck else "enabled",
        "results": monitor.ema_results if not args.no_bottleneck else None
    }

    # Wait for the experiment duration block
    loop_thread.join()
    
    experiment_summary["status"] = "success"
    summary_path = os.path.join(exp_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(experiment_summary, f, indent=4)
        
    logger.info("=== Pipeline Execution Complete ===")
    logger.info(f"Experiment summary configuration persisted successfully to {summary_path}")