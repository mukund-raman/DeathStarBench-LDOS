import pandas as pd
import numpy as np
import subprocess
import logging
import json
import os

logger = logging.getLogger("Preprocessor")

class TimeSeriesPreprocessor:
    def __init__(self, ssh_user="mkraman", target_node="clnode218.clemson.cloudlab.us"):
        self.ssh_user = ssh_user
        self.target_node = target_node
        self.limits = self._fetch_physical_limits(target_node)
        
        # Features that represent a running total, which need to be converted to rates of change
        self.cumulative_features = ['utime', 'stime', 'rx', 'tx', 'pgfault', 'read_bytes', 'write_bytes']
        
        # Features that represent point-in-time state or queues (no rate conversion)
        self.state_features = ['rss', 'cache', 'jobs_waiting_count', 'unique_jobs_waiting', 
                               'thread_queue_length', 'active_connections']
                               
        self.all_features = self.cumulative_features + self.state_features
        self._warned_missing_features = set()
        # F = 13 features total (7 cumulative rates + 6 state/queue metrics)

    # Core Helper Methods
    def _fetch_physical_limits(self, node):
        """Gets maximum hardware limits from a node to scale metrics properly."""
        limits = {}
        
        # Physical Cores
        cmd_cpu = f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes {self.ssh_user}@{node} 'nproc'"
        try:
            res = subprocess.check_output(cmd_cpu, shell=True, text=True, stderr=subprocess.STDOUT)
            limits['cpu_cores'] = int(res.strip())
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch Physical Cores from {node} using command `{cmd_cpu}`. Output: {e.output.strip() if e.output else 'None'}. Ensure SSH access is valid.")
        except Exception as e:
            raise RuntimeError(f"Unexpected error fetching Physical Cores from {node}: {e}")

        # Physical RAM
        cmd_mem = f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes {self.ssh_user}@{node} 'cat /proc/meminfo | grep MemTotal | awk \"{{print \\$2}}\"'"
        try:
            res = subprocess.check_output(cmd_mem, shell=True, text=True, stderr=subprocess.STDOUT)
            limits['mem_bytes'] = int(res.strip()) * 1024 # KB to Bytes
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch Physical RAM from {node} using command `{cmd_mem}`. Output: {e.output.strip() if e.output else 'None'}.")
        except Exception as e:
            raise RuntimeError(f"Unexpected error fetching Physical RAM from {node}: {e}")

        # Network limits (dynamically finds highest speed interface)
        cmd_net = f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes {self.ssh_user}@{node} 'cat /sys/class/net/*/speed 2>/dev/null | sort -nr | head -n 1'"
        try:
            res = subprocess.check_output(cmd_net, shell=True, text=True, stderr=subprocess.STDOUT)
            limits['net_bytes_sec'] = int(res.strip()) * 125000.0
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch Network Link Speed from {node} using command `{cmd_net}`. Output: {e.output.strip() if e.output else 'None'}. Note: interface might not be eth0!")
        except Exception as e:
            raise RuntimeError(f"Unexpected error fetching Network Limits from {node}: {e}")

        # Active connections max (fs.file-max)
        cmd_filemax = f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes {self.ssh_user}@{node} 'cat /proc/sys/fs/file-max'"
        try:
            res = subprocess.check_output(cmd_filemax, shell=True, text=True, stderr=subprocess.STDOUT)
            limits['active_connections_max'] = float(res.strip())
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to fetch OS File Max from {node} using command `{cmd_filemax}`. Output: {e.output.strip() if e.output else 'None'}.")
        except Exception as e:
            raise RuntimeError(f"Unexpected error fetching OS File Max from {node}: {e}")
            
        # Disk IO Benchmarking and Caching
        cache_file = os.path.expanduser('~/.cache/disk_limit.txt')
        cached_disk_limit = None
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    cached_disk_limit = float(f.read().strip())
            except Exception:
                pass
        
        if cached_disk_limit is not None:
            limits['disk_bytes_sec'] = cached_disk_limit
            logger.info(f"Loaded cached disk I/O limit for {node}: {cached_disk_limit/1024/1024:.1f} MB/s")
        else:
            # Run `dd` to measure sequential write max MB/s
            logger.info(f"No cached disk benchmark for {node}. Running immediate dd synthetic test...")
            cmd_disk = f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes {self.ssh_user}@{node} 'dd if=/dev/zero of=/tmp/test_limit.img bs=1M count=50 oflag=direct 2>&1 | grep copied | awk \"{{print \\$(NF-1), \\$NF}}\"'"
            try:
                res = subprocess.check_output(cmd_disk, shell=True, text=True, stderr=subprocess.STDOUT).strip()
                if not res:
                    raise RuntimeError(f"Disk benchmark `dd` returned empty parsed output.")
                val, unit = res.split()
                val = float(val)
                if 'GB' in unit.upper(): val *= 1024
                elif 'KB' in unit.upper(): val /= 1024
                
                limits['disk_bytes_sec'] = val * 1024 * 1024
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Failed to execute Disk Benchmark on {node} using command `{cmd_disk}`. Output: {e.output.strip() if e.output else 'None'}.")
            except Exception as e:
                raise RuntimeError(f"Unexpected error parsing Disk Benchmark result from {node}: {e}. Raw result: '{res}'")
            
            # Save to central cache file mapping
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, 'w') as f:
                f.write(str(limits['disk_bytes_sec']))
        
        logger.info(f"Fetched actual maximum bounds from {node}: {limits['cpu_cores']} Cores, {limits['mem_bytes']/1024/1024/1024:.1f} GB RAM, {limits['net_bytes_sec']/1000000:.1f} MB/s Net")
        return limits

    def _get_max_limit_for_feature(self, feature):
        """Maps a feature to its physical physical hardware limit."""
        # Assume cgroup cpuacct utime/stime rates are mapped in Ubuntu jiffies (CLK_TCK = 100)
        MAX_CPU_RATE = self.limits['cpu_cores'] * 100.0
        
        if feature in ['utime', 'stime']:
            return MAX_CPU_RATE
        elif feature in ['rx', 'tx']:
            return self.limits['net_bytes_sec']
        elif feature in ['read_bytes', 'write_bytes']:
            return self.limits['disk_bytes_sec']
        elif feature in ['rss', 'cache']:
            return self.limits['mem_bytes']
        elif feature == 'active_connections':
            return self.limits['active_connections_max']
        return 1.0

    def compute_global_bounds(self, dataset_records):
        """Pass 1: Computes the absolute minimums and maximums across the entire dataset."""
        self.global_min = {}
        self.global_max = {}
        
        logger.info("Pass 1: Computing absolute structural (x - min) / (max - min) global boundaries...")
        
        all_dfs = []
        for record in dataset_records:
            df = record['df'].copy()
            grouped = df.sort_values(['service', 'timestamp']).groupby('service')
            df['dt'] = grouped['timestamp'].diff().fillna(1.0)
            df.loc[df['dt'] == 0, 'dt'] = 1.0
            
            for col in self.cumulative_features:
                if col in df.columns:
                    prev = grouped[col].shift(1)
                    v_now = df[col]
                    diffs = (v_now - prev).fillna(0.0)
                    
                    # Rectify counter resets: when diffs < 0, a reset happened, use v_now directly
                    diffs[diffs < 0] = v_now[diffs < 0]
                    df[f"{col}_rate"] = diffs / df['dt']
            all_dfs.append(df)
            
        master_df = pd.concat(all_dfs, ignore_index=True)
        empirical_features = ['pgfault', 'jobs_waiting_count', 'unique_jobs_waiting', 'thread_queue_length']
        
        for col in self.all_features:
            is_cum = col in self.cumulative_features
            target_col = f"{col}_rate" if is_cum else col
            
            if target_col in master_df.columns:
                self.global_min[col] = master_df[target_col].min()
                if col in empirical_features:
                    self.global_max[col] = master_df[target_col].max()
                else:
                    self.global_max[col] = self._get_max_limit_for_feature(col)
                    
                if pd.isna(self.global_min[col]): self.global_min[col] = 0.0
                if pd.isna(self.global_max[col]) or self.global_max[col] <= self.global_min[col]: 
                    self.global_max[col] = self.global_min[col] + 1.0
                    
    # Main Preprocessing Logic
    def preprocess(self, df, services=None):
        """
        Input: DataFrame with columns: timestamp, service, and raw metric columns.
        Outputs: 
        - 3D numpy array of shape (Services, Timestamps, Features) representing all microservices over time.
        - List of scaled feature names.
        - List of ordered service names.
        """
        if df.empty:
            logger.error("Empty dataframe provided to preprocessor.")
            return None, None, []
            
        df = df.copy()
        if 'timestamp' not in df.columns or 'service' not in df.columns:
            raise ValueError("Dataframe must contain 'timestamp' and 'service' columns")
        df = df.sort_values(by=['service', 'timestamp'])
        
        # Validate all columns and prune feature sets as needed
        present_cumulative = []
        present_state = []
        
        for col in df.columns.drop(['timestamp', 'service'], errors='ignore'):
            if col.startswith('Unnamed'): continue
            if col not in self.all_features:
                raise ValueError(f"CRITICAL ERROR: Found unexpected feature '{col}' in dataframe, which is not natively supported!")
        
        for col in self.cumulative_features:
            if col not in df.columns:
                if col not in self._warned_missing_features:
                    logger.warning(f"Feature '{col}' is missing from the dataset. Proceeding without it.")
                    self._warned_missing_features.add(col)
            else:
                present_cumulative.append(col)
                
        for col in self.state_features:
            if col not in df.columns:
                if col not in self._warned_missing_features:
                    logger.warning(f"Feature '{col}' is missing from the dataset. Proceeding without it.")
                    self._warned_missing_features.add(col)
            else:
                present_state.append(col)

        if not present_cumulative and not present_state:
            raise ValueError("No valid features found in dataframe mapping!")
                
        # 1. First-Derivative Rate Conversions
        grouped = df.groupby('service')
        
        # Time delta (dt)
        df['dt'] = grouped['timestamp'].diff().fillna(1.0)
        df.loc[df['dt'] == 0, 'dt'] = 1.0
        
        for col in present_cumulative:
            rate_col_name = f"{col}_rate"
            prev = grouped[col].shift(1)
            v_now = df[col]
            diffs = (v_now - prev).fillna(0.0)
            diffs[diffs < 0] = v_now[diffs < 0] # Rectify counter resets
            df[rate_col_name] = diffs / df['dt']
            
        # 2. Hybrid Min-Max Scaling (Physical limits & Empirical maxima)
        scaled_features = []
        
        for col in present_cumulative:
            rate_col = f"{col}_rate"
            min_val = getattr(self, 'global_min', {}).get(col, 0.0)
            max_val = getattr(self, 'global_max', {}).get(col, self._get_max_limit_for_feature(col))
            if max_val <= min_val: max_val = min_val + 1.0
                
            df[f"{col}_scaled"] = (df[rate_col] - min_val) / (max_val - min_val)
            df[f"{col}_scaled"] = df[f"{col}_scaled"].clip(0.0, 1.0) # 1.0 means 100% Saturation
            scaled_features.append(f"{col}_scaled")
            
        for col in present_state:
            min_val = getattr(self, 'global_min', {}).get(col, 0.0)
            max_val = getattr(self, 'global_max', {}).get(col, self._get_max_limit_for_feature(col))
            if max_val <= min_val: max_val = min_val + 1.0
                
            df[f"{col}_scaled"] = (df[col] - min_val) / (max_val - min_val)
            df[f"{col}_scaled"] = df[f"{col}_scaled"].clip(0.0, 1.0)
            scaled_features.append(f"{col}_scaled")
            
        # Drop any malformed/blank rows which produce KeyError('nan')
        df = df.dropna(subset=['timestamp', 'service'])

        # 3. Format as Tensor-compatible 3D array (M, T, F)
        if services is None: services = sorted(df['service'].unique())
        timestamps = sorted(df['timestamp'].unique())
        M_tensor = np.zeros((len(services), len(timestamps), len(scaled_features)), dtype=np.float32)
        
        service_to_idx = {s: i for i, s in enumerate(services)}
        time_to_idx = {t: i for i, t in enumerate(timestamps)}
        
        # Only populate rows from services we are tracking (handles subset omission seamlessly)
        valid_df = df[df['service'].isin(services)].copy()
        s_indices = valid_df['service'].map(service_to_idx).values
        t_indices = valid_df['timestamp'].map(time_to_idx).values
        
        M_tensor[s_indices, t_indices, :] = valid_df[scaled_features].values
        return M_tensor, scaled_features, services

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Preprocessor Execution")
    parser.add_argument("--dir", type=str, required=True, help="Path to the directory containing the CSV files")
    args = parser.parse_args()
    
    # Read all CSV files in the directory and preprocess them
    all_files = [os.path.join(args.dir, f) for f in os.listdir(args.dir) if f.endswith('.csv')]
    df = pd.concat([pd.read_csv(f) for f in all_files])
    preprocessor = TimeSeriesPreprocessor()
    M_tensor, scaled_features, services = preprocessor.preprocess(df)
    
    print("M_tensor shape:", M_tensor.shape)
    print("Scaled features:", scaled_features)
    print("Services:", services)