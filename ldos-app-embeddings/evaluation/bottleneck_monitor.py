import torch
import torch.nn.functional as F
import logging
import datetime
import json
import os
import time
import argparse
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models.autoencoder import AdditiveAutoencoder
from data_collection.preprocess import TimeSeriesPreprocessor
import glob

logger = logging.getLogger("BottleneckMonitor")

class BottleneckMonitor:
    """
    Implements Phase 6: Bottleneck Identification via Cosine Distance limits on EMA steady-states.
    Pinpoints when services diverge from their healthy historical norm.
    """
    def __init__(self, service_names, alpha=0.1, threshold=0.2, timestamp=None):
        self.service_names = service_names
        self.alpha = alpha
        self.threshold = threshold
        
        self.timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.eval_results = []
        
        self.E_bar = {} # Tracking dictionary for the Exponential Moving Average (EMA) of service embeddings
        self.healthy_minutes_count = 0
        self.baseline_established = False
        
        # Buffer to store the first 10 healthy minutes to establish a baseline
        self.baseline_history = {s: [] for s in service_names}

    # Evaluation Log Saving
    def save_results(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eval_dir = os.path.join(base_dir, "results", "evaluations")
        os.makedirs(eval_dir, exist_ok=True)
        filename = os.path.join(eval_dir, f"bottleneck_{self.timestamp}.json")
        with open(filename, 'w') as f:
            json.dump(self.eval_results, f, indent=4)
        logger.info(f"Bottleneck Monitor evaluations saved to {filename}")

    # Core Logic
    def process_minute(self, E_dict, is_healthy=True):
        """
        Evaluates the service embeddings. Should be called every 60s.
        """
        # Wait 10 minutes to establish the initial baseline
        if not self.baseline_established:
            if is_healthy:
                for i, s in enumerate(self.service_names):
                    self.baseline_history[s].append(E_dict[i])
                self.healthy_minutes_count += 1
                
                if self.healthy_minutes_count >= 10:
                    # Calculate the initial arithmetic average
                    for s in self.service_names:
                        stack = torch.stack(self.baseline_history[s])
                        self.E_bar[s] = torch.mean(stack, dim=0) 
                    self.baseline_established = True
                    logger.info("BottleneckMonitor: 10-minute baseline successfully established.")
            else:
                logger.warning("BottleneckMonitor: Skipping unhealthy minute during baseline.")
            return

        # Check the current embedding against the EMA baseline
        cycle_time = time.time()
        
        for i, s in enumerate(self.service_names):
            e_t = E_dict[i]
            e_bar_prev = self.E_bar[s]
            
            # Calculate the cosine distance
            cos_sim = F.cosine_similarity(e_t.unsqueeze(0), e_bar_prev.unsqueeze(0)).item()
            d_i = 1.0 - cos_sim
            
            # Update the EMA:
            # \bar{E}_i(t) = \alpha E_i(t) + (1-\alpha)\bar{E}_i(t-1)
            self.E_bar[s] = (self.alpha * e_t) + ((1.0 - self.alpha) * e_bar_prev)
            
            if d_i > self.threshold:
                logger.error(f"BOTTLENECK ALERT! Service '{s}' is deviating from its historical norm. (Cosine Distance {d_i:.3f} > threshold {self.threshold})")
                self.eval_results.append({
                    "cycle_time": cycle_time,
                    "service": s,
                    "cosine_distance": float(d_i),
                    "threshold_breached": True
                })
                self.save_results()

# Standalone Testing Executions
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Bottleneck Monitor logic sequence")
    parser.add_argument("--model-path", type=str, required=True, help="Path to trained Autoencoder .pth file")
    args = parser.parse_args()
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Initializing preprocessor and loading historical datasets...")
    
    preprocessor = TimeSeriesPreprocessor()
    
    # We must import main's trace function carefully
    try:
        from main import parse_offline_data_directories
        data_list, services = parse_offline_data_directories(preprocessor)
        
        # Load autoencoder
        F_input = data_list[0]['M_tensor'].shape[-1]
        model = AdditiveAutoencoder(input_size=F_input).to(device)
        model.load_state_dict(torch.load(args.model_path, map_location=device))
        model.eval()
        
        monitor = BottleneckMonitor(services)
        
        logger.info(f"Processing {len(data_list)} historical trace minutes through the bottleneck detector...")
        for count, data in enumerate(data_list):
            T_offline = min(data['M_tensor'].shape[1], 60)
            offline_padded = torch.zeros((len(services), 60, data['M_tensor'].shape[2]))
            offline_padded[:, :T_offline, :] = torch.tensor(data['M_tensor'])[:, :T_offline, :]
            
            x_test = offline_padded.float().to(device).unsqueeze(0)
            with torch.no_grad():
                e_i = model.encode(x_test).squeeze(0)
                
            E_dict_list = [e_i[i] for i in range(len(services))]
            monitor.process_minute(E_dict_list, is_healthy=(count < 10))
            
        logger.info("Standalone Bottleneck Monitor historical detection sweep completed.")
    except Exception as e:
        logger.error(f"Failed to run standalone monitor detection: {e}")
