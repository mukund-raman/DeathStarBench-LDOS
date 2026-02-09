import argparse
import time
import subprocess
import os
import threading
import signal
import sys
import glob

# Constants
CRI_INTERVAL = 30  # Refresh PIDs every 30s
COLLECTION_INTERVAL = 1.0  # Collect metrics every 1s

class CRICollector:
    def __init__(self, output_file):
        self.output_file = output_file
        self.container_pid_map = {}
        self.prev_metrics = {}
        self.running = True
        self.lock = threading.Lock()

    def refresh_pids(self):
        """Rebuilds the cache of ContainerID -> HostPID using crictl"""
        try:
            # Get all running containers
            cmd = "sudo crictl ps -q --state Running"
            try:
                container_ids = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
            except subprocess.CalledProcessError:
                return

            new_map = {}
            for cid in container_ids:
                if not cid: continue
                
                try:
                    # Inspect to find PID
                    inspect_cmd = f"sudo crictl inspect --output go-template --template '{{{{.info.pid}}}}' {cid}"
                    pid = subprocess.check_output(inspect_cmd, shell=True).decode().strip()
                    
                    # Also get metadata (name) for tagging
                    meta_cmd = f"sudo crictl inspect --output go-template --template '{{{{.status.metadata.name}}}}' {cid}"
                    name = subprocess.check_output(meta_cmd, shell=True).decode().strip()
                    
                    if pid and name:
                        new_map[cid] = {'pid': pid, 'name': name}
                except subprocess.CalledProcessError:
                    continue
            
            with self.lock:
                self.container_pid_map = new_map
            print(f"DEBUG: Refreshed PIDs, found {len(new_map)} containers")
        except Exception as e:
            print(f"Error refreshing PIDs: {e}")

    def read_net_metrics(self, pid):
        """Parses /proc/<pid>/net/dev for eth0 stats"""
        try:
            path = f"/proc/{pid}/net/dev"
            with open(path, 'r') as f:
                lines = f.readlines()
            for line in lines:
                if "eth0" in line:
                    parts = line.split()
                    rx = int(parts[1])
                    tx = int(parts[9])
                    return rx, tx
        except (IOError, IndexError, ValueError):
            return 0, 0
        return 0, 0

    def read_cpu_metrics(self, pid):
        """Parses /proc/<pid>/stat for utime, stime"""
        try:
            path = f"/proc/{pid}/stat"
            with open(path, 'r') as f:
                parts = f.read().split()
                utime = int(parts[13])
                stime = int(parts[14])
                return utime, stime
        except (IOError, IndexError, ValueError):
            return 0, 0

    def find_cgroup_memory_path(self, cid):
        """Finds the cgroup memory path for a container ID"""
        try:
            # Find the cgroup scope file for the container
            find_cmd = f"find /sys/fs/cgroup -name '*{cid}*.scope' -print -quit"
            path_scope = subprocess.check_output(find_cmd, shell=True).decode().strip()
            if not path_scope:
                return None, None
            
            # Check if it is cgroup v1 or v2
            # v2: /sys/fs/cgroup/kubepods.slice/.../foo.scope
            # v1: /sys/fs/cgroup/memory/kubepods.slice/.../foo.scope
            if "memory" in path_scope:
                return os.path.join(path_scope, "memory.stat"), "v1"
            else:
                # Check if memory.stat exists in that scope directory
                mem_stat = os.path.join(path_scope, "memory.stat")
                if os.path.exists(mem_stat):
                    return mem_stat, "v2"
                
                # Double check for v1 memory controller if not in v2 path
                find_v1_cmd = f"find /sys/fs/cgroup/memory -name '*{cid}*.scope' -print -quit"
                try:
                    path_v1 = subprocess.check_output(find_v1_cmd, shell=True).decode().strip()
                    if path_v1:
                         return os.path.join(path_v1, "memory.stat"), "v1"
                except:
                    pass
                return None, None
        except subprocess.CalledProcessError:
            return None, None

    def read_mem_metrics(self, cid):
        """Reads rss, cache, pgfault from memory.stat"""
        path, version = self.find_cgroup_memory_path(cid)
        if not path:
            return 0, 0, 0
            
        rss, cache, pgfault = 0, 0, 0
        try:
            # Open memory.stat file and parse it
            with open(path, 'r') as f:
                lines = f.readlines()
            data = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    data[parts[0]] = int(parts[1])
            
            if version == "v2":
                # cgroup v2 memory.stat fields: rss~=anon, file, pgfault...
                rss = data.get('anon', 0)
                cache = data.get('file', 0)
            else:
                rss = data.get('rss', 0)
                cache = data.get('cache', 0)
            pgfault = data.get('pgfault', 0)
        except (IOError, ValueError):
            pass
        return rss, cache, pgfault

    def collect(self):
        """Collects metrics for all containers"""
        timestamp = time.time()
        batch = []
        with self.lock:
            current_map = self.container_pid_map.copy()

        # Retrieve the relevant info per container and append to batch
        for cid, info in current_map.items():
            pid = info['pid']
            name = info['name']
            rx, tx = self.read_net_metrics(pid)
            utime, stime = self.read_cpu_metrics(pid)
            rss, cache, pgfault = self.read_mem_metrics(cid)
            batch.append(f"{timestamp},{name},{utime},{stime},{rss},{cache},{pgfault},{rx},{tx}")
            
        return batch

    def run_agent(self):
        """Runs the collection agent"""
        # Initial refresh
        self.refresh_pids()
        
        # Start refresh thread
        def refresh_loop():
            while self.running:
                time.sleep(CRI_INTERVAL)
                if self.running:
                    self.refresh_pids()
        
        refresh_thread = threading.Thread(target=refresh_loop)
        refresh_thread.daemon = True
        refresh_thread.start()
        
        # Start the collection thread
        with open(self.output_file, 'w') as f:
            f.write("timestamp,service,utime,stime,rss,cache,pgfault,rx,tx\n")
            
            while self.running:
                loop_start = time.time()
                
                # Collect metrics
                data = self.collect()
                if data:
                    f.write("\n".join(data) + "\n")
                    f.flush()
                
                # Sleep remainder of the time
                elapsed = time.time() - loop_start
                sleep_time = max(0, COLLECTION_INTERVAL - elapsed)
                time.sleep(sleep_time)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Output file path", required=True)
    args = parser.parse_args()
    
    # Create collector and run agent, handle cleanup on SIGTERM/SIGINT
    collector = CRICollector(args.output)
    def handler(signum, frame):
        collector.running = False
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    collector.run_agent()