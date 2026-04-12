import torch
import numpy as np
import time
import subprocess
import logging
import datetime
import json
import os
import argparse
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern

class ConstrainedBayesianOptimizer:
    """
    Executes Loop B (Guided Bayesian Optimization Phase 4).
    Filters valid placement configs using node limit constraints, then
    predicts P99 latency via the Gaussian Process surrogate model.
    """
    def __init__(self, service_names, E_N_max, num_nodes=5, pin_script_path="socialNetwork/kubernetes/pin-microservices.sh", timestamp=None):
        self.service_names = service_names
        self.num_services = len(service_names)
        self.E_N_max = E_N_max # Tensor shape: (d,) ceiling bound
        self.embedding_dim = self.E_N_max.shape[0]
        self.num_nodes = num_nodes
        self.pin_script_path = pin_script_path
        
        self.timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.eval_results = []
        
        # Surrogate model to predict P99 latency from node sum embeddings
        self.gp = GaussianProcessRegressor(kernel=Matern(nu=2.5), alpha=1e-4, normalize_y=True)
        self.is_fitted = False
        
        self.X_train = []
        self.y_train = []
        self.current_placements = {s: 0 for s in service_names} 
        
    def save_results(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eval_dir = os.path.join(base_dir, "results", "evaluations")
        os.makedirs(eval_dir, exist_ok=True)
        filename = os.path.join(eval_dir, f"cbo_{self.timestamp}.json")
        with open(filename, 'w') as f:
            json.dump(self.eval_results, f, indent=4)
        logger.info(f"CBO Optimizer evaluations saved to {filename}")
        
    # Core Logic
    def fit_offline(self, offline_datasets):
        """
        Trains the GP exclusively offline.
        offline_datasets format definition:
        List of dicts: {'E_dict': [...], 'placement': [node indexing], 'p99_latency': float}
        """
        logger.info("Training offline GP surrogate model...")
        for data in offline_datasets:
            E_dict = data['E_dict']
            placement = data['placement'] 
            
            x_bo = self._construct_x_bo(E_dict, placement)
            self.X_train.append(x_bo)
            self.y_train.append(data['p99_latency'])
            
        if len(self.X_train) > 0:
            self.gp.fit(np.array(self.X_train), np.array(self.y_train))
            self.is_fitted = True
            logger.info(f"GP model fitted on {len(self.X_train)} historical trace vectors.")
        else:
            logger.error("No historical offline data for GP.")

    def _construct_x_bo(self, E_dict, placement):
        """
        Creates the input vector for GP prediction by concatenating node sums.
        """
        node_sums = [torch.zeros(self.embedding_dim) for _ in range(self.num_nodes)]
        for i_idx, n in enumerate(placement):
            node_sums[n] += E_dict[i_idx]
            
        x_bo = torch.cat(node_sums).numpy() # Shape: (num_nodes * embedding_dim,) resolving as (320,)
        return x_bo

    def _check_constraint(self, E_dict, placement):
        """
        Checks if the placement is valid by ensuring no node exceeds the E_N_max constraint.
        """
        node_sums = [torch.zeros(self.embedding_dim) for _ in range(self.num_nodes)]
        for i_idx, n in enumerate(placement):
            node_sums[n] += E_dict[i_idx]
            
        for n_sum in node_sums:
            diff = n_sum - self.E_N_max
            v_n = torch.clamp(diff, min=0.0)
            if torch.norm(v_n, p=1).item() > 0:
                return False
        return True

    def _execute_full_placement(self, placement_array):
        """
        Applies the placement to Kubernetes clusters using a pinning script.
        """
        for i, s in enumerate(self.service_names):
            self.current_placements[s] = placement_array[i]
            
        args = [str(x) for x in placement_array]
        cmd = f"bash {self.pin_script_path} {' '.join(args)}"
        try:
            logger.info(f"Deploying chosen CBO layout: {cmd[:60]}...")
            subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL)
            logger.info("Deployment executed successfully.")
        except Exception as e:
            logger.error(f"Error applying Kubernetes cluster placements: {e}")

    def loop(self, E_dict, current_p99=999.0):
        """
        CBO Loop: Every 60 seconds evaluates current placement against E_N_max.
        Tests 10,000 configurations minimizing GP prediction latency.
        """
        current_placement_array = [self.current_placements[s] for s in self.service_names]
        
        # 1. Validation Tracking
        if self._check_constraint(E_dict, current_placement_array):
            logger.info("CBO System Normal - All nodes are within limits.")
            return
            
        logger.warning(f"CBO Limits Exceeded! A node is currently over capacity. Current physical P99 = {current_p99:.2f}ms")
        
        # 2. Fast 10,000 Configuration Generation
        logger.info("Generating 10,000 random placement permutation candidates...")
        candidates = np.random.randint(0, self.num_nodes, size=(10000, self.num_services))
        
        # 3. Fast Pre-Filter
        valid_placements = []
        for cand in candidates:
            if self._check_constraint(E_dict, cand):
                valid_placements.append(cand)   
        logger.info(f"Constraints valid for {len(valid_placements)} / 10000 candidates.")
        if not valid_placements:
            logger.error("No valid placements possible! Demands exceed physical cluster capacity.")
            return
        
        if not self.is_fitted: # Make sure GP is fitted before looping
            logger.warning("GP Surrogate is untrained. Falling back to simple bounds checking selection.")
            best_P = valid_placements[0]
            lowest_lat = -1.0
        else:
            # 4. GP Predictions
            logger.info("Running GP surrogate to predict latencies and find the best placement...")
            X_queries = []
            for cand in valid_placements:
                X_queries.append(self._construct_x_bo(E_dict, cand))
            X_queries = np.array(X_queries)
            
            predicted_lats = self.gp.predict(X_queries)
            best_idx = np.argmin(predicted_lats)
            lowest_lat = predicted_lats[best_idx]
            best_P = valid_placements[best_idx]
            logger.info(f"Found best placement with predicted P99 = {lowest_lat:.2f}ms.")
            
        # 5. Pipeline Placements Execution
        self.eval_results.append({
            "cycle_time": time.time(),
            "status": "violation_detected",
            "candidates_evaluated": len(candidates),
            "valid_placements_found": len(valid_placements),
            "predicted_lowest_lat": lowest_lat,
            "new_placement_chosen": best_P.tolist() if hasattr(best_P, 'tolist') else list(best_P)
        })
        self.save_results()
        self._execute_full_placement(best_P)

# Standalone Testing Executions
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone CBO Optimizer Execution")
    parser.add_argument("--services", type=str, nargs='+', required=True, help="List of service names")
    parser.add_argument("--en-max", type=float, default=5.0, help="Node limit capacity constraint")
    parser.add_argument("--dim", type=int, default=64, help="Embedding dimension size")
    args = parser.parse_args()
    
    E_N_max = torch.ones(args.dim) * args.en_max
    cbo = ConstrainedBayesianOptimizer(args.services, E_N_max)
    
    # Creates random synthetic embeddings if running as standalone for testing
    mock_E_dict = [torch.rand(args.dim) * 2 for _ in range(len(args.services))] # Multiplied by 2 to force violation
    cbo.loop(mock_E_dict)
    cbo.save_results()
    logger.info("Standalone CBO Optimizer execution completed.")
