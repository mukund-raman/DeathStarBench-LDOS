import torch
import numpy as np
import logging
from torch.utils.data import Dataset

logger = logging.getLogger("Dataset")

class AdditiveEmbeddingDataset(Dataset):
    """
    Creates the training dataset.
    Maps 60-second windows of metrics to the 'True Demand' (the P99 of the first 2 healthy minutes).
    """
    def __init__(self, data_list, window_size=60, stride=30, baseline_cutoff_sec=120):
        """
        data_list: list of dicts. Each dict contains:
            'M_tensor': numpy array of shape (M, T, F) representing preprocessed scaled metrics
            'placement': numpy array of size M, mapping service index i to node index n
        """
        self.samples = []
        
        if not data_list:
            logger.warning("No data provided to Dataset initialization.")
            return
            
        for exp_id, data in enumerate(data_list):
            m_tensor = data['M_tensor'] # Shape: (M, T, F)
            placement = data['placement'] # Size M
            
            # Ensure tensor is large enough for a baseline
            if m_tensor.shape[1] <= baseline_cutoff_sec:
               logger.warning(f"Experiment {exp_id} shorter than {baseline_cutoff_sec}s baseline cutoff. Skipping.")
               continue
               
            # Extract Baseline Range (Minutes 0-2)
            baseline_tensor = m_tensor[:, :baseline_cutoff_sec, :]
            
            # Calculate True Demand label: 
            # 99th percentile (P99) of each feature's rate during the healthy baseline
            # Shape: (M, F)
            # We use P99 because it captures sustained load while ignoring quick spikes.
            x_true_baseline_demand = np.percentile(baseline_tensor, 99, axis=1)
            
            # Create Sliding Windows for the entire trace
            # We train on the whole trace (baseline and stressed periods)
            # This teaches the model to estimate the true baseline demand even when the system is stressed.
            total_T = m_tensor.shape[1]
            for start_idx in range(0, total_T - window_size + 1, stride):
                window = m_tensor[:, start_idx : start_idx + window_size, :]
                
                self.samples.append({
                    'x': torch.tensor(window, dtype=torch.float32), # (M, 60, F)
                    'y': torch.tensor(x_true_baseline_demand, dtype=torch.float32), # (M, F)
                    'placement': torch.tensor(placement, dtype=torch.long) # (M,)
                })
        
        logger.info(f"Initialized Dataset with {len(self.samples)} samples across {len(data_list)} experiment configurations.")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]

# Helper Methods
def get_node_ground_truth(y_batch, placement_batch, num_nodes=5):
    """
    Calculates the total true demand for each node by summing the demands of services placed on it.
    
    y_batch: (Batch_Size, M, F) - The true individual demands X_i^{agg_{true}}
    placement_batch: (Batch_Size, M) - The node assignments for each service
    
    Returns: node_sums (Batch_Size, num_nodes, F) representing true node demands
    """
    batch_size, M, F = y_batch.shape
    node_sums = torch.zeros(batch_size, num_nodes, F, device=y_batch.device)
    
    # Sum the demands of all services assigned to each node
    for b in range(batch_size):
        for i in range(M):
            n = placement_batch[b, i]
            node_sums[b, n] += y_batch[b, i]
            
    return node_sums
