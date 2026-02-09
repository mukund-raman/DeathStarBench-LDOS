import argparse
import time
import subprocess
import os
import glob
import sys
import atexit

# Constants
WORKER_NODES = [
    "clnode218.clemson.cloudlab.us",
    "clnode198.clemson.cloudlab.us",
    "clnode216.clemson.cloudlab.us",
    "clnode199.clemson.cloudlab.us",
    "clnode215.clemson.cloudlab.us",
]
SSH_USER = "mkraman"
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")
AGENT_SCRIPT = "agent.py"
REMOTE_AGENT_PATH = "/tmp/agent.py"
REMOTE_METRICS_PATH = "/tmp/metrics.csv"

def setup_ssh_agent():
    """Starts ssh-agent and adds the SSH key to the environment."""
    try:
        # Start ssh-agent and parse output to set environment variables
        output = subprocess.check_output(["ssh-agent", "-s"], text=True)
        for line in output.splitlines():
            if "=" in line and ";" in line:
                key_value = line.split(";")[0]
                key, value = key_value.split("=", 1)
                os.environ[key] = value
        
        agent_pid = os.environ.get('SSH_AGENT_PID')
        print(f"SSH Agent started (PID: {agent_pid})")
        
        # Register cleanup
        def cleanup_ssh_agent():
            if agent_pid:
                subprocess.run(["kill", agent_pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(cleanup_ssh_agent)

        # Add SSH Key
        if os.path.exists(SSH_KEY):
            subprocess.run(["ssh-add", SSH_KEY], check=True)
            print(f"Identity added: {SSH_KEY}")
        else:
            print(f"Warning: SSH key not found at {SSH_KEY}")
    except subprocess.CalledProcessError as e:
        print(f"Failed to setup SSH agent: {e}")
    except Exception as e:
        print(f"An error occurred setting up SSH agent: {e}")

def get_next_version(output_dir, config_name):
    """Finds the next available version number for a given config name."""
    # Find existing files matching <config_name>-run<N>.txt
    pattern = os.path.join(output_dir, f"{config_name}-run*.txt")
    existing_files = glob.glob(pattern)
    
    # Determine latest version of config
    max_version = 0
    for f in existing_files:
        try:
            # Extract N from ...-run<N>.txt
            filename = os.path.basename(f)
            base = os.path.splitext(filename)[0] # Remove extension
            run_part = base.replace(f"{config_name}-run", "") # Remove prefix
            if run_part.isdigit():
                v = int(run_part)
                if v > max_version:
                    max_version = v
        except:
            continue
    return max_version + 1

def run_experiment(config_files, num_runs, output_dir, warmup_duration, wrk_duration, wrk_rps):
    """Runs the experiment for each config file."""
    # Ensure output dir exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # For each config, for each run
    configs = []
    if config_files == ["--all"]:
        configs = glob.glob(f"configs/*.yml")
    else:
        configs = config_files

    # Path to agent script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_agent_path = os.path.join(script_dir, AGENT_SCRIPT)
    if not os.path.exists(local_agent_path):
        print(f"Error: Agent script not found at {local_agent_path}")
        return

    for config in configs:
        config_name = os.path.basename(config).replace(".yml", "")
        
        for i in range(1, num_runs + 1):
            # Determine version
            version = get_next_version(output_dir, config_name)
            run_id = f"{config_name}-run{version}"
            output_file = os.path.join(output_dir, f"{run_id}.txt")
            print(f"Starting run {run_id} -> {output_file}")
            
            # 1. Distribute and Start Agents
            active_nodes = []
            for node in WORKER_NODES:
                print(f"[{node}] Deploying agent...")
                
                # SCP agent
                scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no", local_agent_path, f"{SSH_USER}@{node}:{REMOTE_AGENT_PATH}"]
                subprocess.run(scp_cmd, check=True)
                
                # Start agent
                cmd = f"nohup sudo python3 {REMOTE_AGENT_PATH} --output {REMOTE_METRICS_PATH} > /dev/null 2>&1 & echo $!"
                ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}", cmd]
                
                try:
                    proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    stdout, _ = proc.communicate()
                    remote_pid = stdout.decode().strip()
                    if remote_pid:
                        active_nodes.append((node, remote_pid))
                        print(f"[{node}] Agent started (PID {remote_pid})")
                except Exception as e:
                    print(f"[{node}] Failed to start agent: {e}")

            # Set ENV vars
            env = os.environ.copy()
            env["WARMUP_DURATION"] = warmup_duration
            env["WRK_DURATION"] = wrk_duration
            env["WRK_RPS"] = str(wrk_rps)
            
            # 2. Run Experiment
            api_script_dir = "../socialNetwork/kubernetes"
            abs_config_path = os.path.abspath(config)
            cmd = ["./test-configs.sh", abs_config_path, "-n", "1"]
            print(f"Running experiment: {' '.join(cmd)}, run {i} of {num_runs}")
            try:
                subprocess.run(cmd, cwd=api_script_dir, env=env, check=True)
            except subprocess.CalledProcessError as e:
                print(f"Experiment failed: {e}")

            # 3. Stop Agents
            for node, pid in active_nodes:
                kill_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}", f"sudo kill {pid}"]
                subprocess.run(kill_cmd)
                print(f"[{node}] Agent stopped")

            # 4. Aggregate Data
            print(f"Aggregating data to {output_file}...")
            with open(output_file, 'w') as outfile:
                header_written = False
                
                for node, pid in active_nodes:
                    local_temp = f"/tmp/metrics-{node}.csv"
                    
                    # SCP Results back
                    scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}:{REMOTE_METRICS_PATH}", local_temp]
                    try:
                        subprocess.run(scp_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        # Append content
                        with open(local_temp, 'r') as infile:
                            # Read header and write if it doesn't exist
                            header = infile.readline()
                            if not header:
                                continue # Empty file
                            if not header_written:
                                outfile.write(header)
                                header_written = True
                            
                            # Write remaining lines
                            for line in infile:
                                outfile.write(line)
                        
                        # Cleanup local temp
                        os.remove(local_temp)
                        
                    except subprocess.CalledProcessError:
                        print(f"[{node}] Failed to retrieve metrics")
                    except Exception as e:
                        print(f"[{node}] Error processing metrics: {e}")

                # Cleanup remote files on all nodes
                for node in WORKER_NODES:
                     cleanup_cmd = f"sudo rm -f {REMOTE_AGENT_PATH} {REMOTE_METRICS_PATH}"
                     ssh_clean = ["ssh", "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{node}", cleanup_cmd]
                     subprocess.run(ssh_clean, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            print(f"Run {run_id} complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="*", help="Config files to run or --all")
    parser.add_argument("--output", default="data", help="Output directory for text files")
    parser.add_argument("--num-runs", type=int, default=1)
    parser.add_argument("--warmup", default="180s")
    parser.add_argument("--duration", default="600s")
    parser.add_argument("--rps", default=500)
    
    # Parse arguments
    args = parser.parse_args()
    if not args.configs:
        print("Please provide --configs <files> or --configs --all")
        sys.exit(1)

    # Start the SSH agent and run experiment
    setup_ssh_agent() 
    run_experiment(args.configs, args.num_runs, args.output, \
        args.warmup, args.duration, args.rps)