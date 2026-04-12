import os
import subprocess
import time

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
            
            subprocess.run(['ssh-add', '/users/mkraman/.ssh/id_rsa'], check=True)
            print("Key successfully added!")
        else:
            print("SSH Identity already available via existing agent environment.")
    except Exception as e:
        print(f"Error connecting to agent: {e}")

if __name__ == "__main__":
    setup_ssh_agent()

    node = "clnode199.clemson.cloudlab.us"
    base_url = "http://localhost:32000"
    script = "/users/mkraman/DeathStarBench-LDOS/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"
    
    def run_wrk():
        cmd = f"/users/mkraman/DeathStarBench-LDOS/wrk2/wrk -D exp -t 2 -c 100 -d 20 -L -s {script} {base_url}/wrk2-api/mixed-workload -R 500"
        print("Running WRK: ", cmd)
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
        
    print("\n[Baseline Latency]")
    try:
        base_latency = run_wrk()
        print(f"Baseline: {base_latency:.2f} ms")
    except Exception as e:
        print(f"Failed to measure baseline latency: {e}")

    print(f"\n[Injecting 500ms Network Delay Anomaly on {node}]")
    cmd = (
        f"ssh -o StrictHostKeyChecking=no mkraman@{node} "
        "\"sudo tc qdisc del dev docker0 root netem 2>/dev/null; "
        "sudo tc qdisc add dev docker0 root netem delay 500ms; "
        "sudo tc qdisc del dev cni0 root netem 2>/dev/null; "
        "sudo tc qdisc add dev cni0 root netem delay 500ms\""
    )
    subprocess.run(cmd, shell=True, text=True)
    time.sleep(5)
    
    try:
        print("\n[Anomalous Latency]")
        anom_latency = run_wrk()
        print(f"Anomaly: {anom_latency:.2f} ms")
    except Exception as e:
        print(f"Failed to measure anomalous latency: {e}")
        anom_latency = 0.0
        
    print("\n[Cleaning Up]")
    clean_cmd = (
        f"ssh -o StrictHostKeyChecking=no mkraman@{node} "
        "\"sudo tc qdisc del dev docker0 root netem 2>/dev/null; "
        "sudo tc qdisc del dev cni0 root netem 2>/dev/null\""
    )
    subprocess.run(clean_cmd, shell=True, text=True)
    
    print("\n=== LATENCY COMPARISON ===")
    print(f"Baseline: {base_latency:.2f} ms")
    print(f"Anomaly:  {anom_latency:.2f} ms")
    if base_latency > 0:
        multiplier = anom_latency / base_latency
        print(f"Spike Multiplier: {multiplier:.2f}x")
