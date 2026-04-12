import torch
import torch.nn as nn
import torch.optim as optim
import logging
import os
import datetime
from torch.utils.data import DataLoader
from models.autoencoder import AdditiveAutoencoder
from training.dataset import AdditiveEmbeddingDataset, get_node_ground_truth
import optuna
import numpy as np
import argparse
import sys

logger = logging.getLogger("Trainer")

def mega_loss_fn(x_ind_pred, x_ind_true, x_node_pred, x_node_true, alpha, beta):
    """
    Computes the Additive Autoencoder Mega Loss.
    Optimizes both individual reconstruction and node-level addition constraint.
    L_mega = (alpha * L_ind) + (beta * L_add)
    Use L1Loss (MAE) instead of MSE to avoid vanishing gradients on extremely small scaled features (e.g. 0.0001).
    """
    criterion = nn.L1Loss()
    L_ind = criterion(x_ind_pred, x_ind_true)
    L_add = criterion(x_node_pred, x_node_true)
    return (alpha * L_ind) + (beta * L_add), L_ind, L_add

# Core Training Logic
def train_epoch(model, dataloader, optimizer, alpha, beta, num_nodes=5, device='cpu'):
    model.train()
    total_mega = 0.0
    total_ind = 0.0
    total_add = 0.0
    
    for batch in dataloader:
        x = batch['x'].to(device) # (Batch, M, 60, F)
        y = batch['y'].to(device) # (Batch, M, F)
        placement = batch['placement'].to(device) # (Batch, M)
        
        batch_size, M, seq_len, F = x.shape
        optimizer.zero_grad()
        
        # 1. Generate Embeddings & Individual Loss
        # Flatten for LSTM sequence batch processing: (Batch*M, 60, F)
        x_flat = x.view(batch_size * M, seq_len, F)
        
        # Forward pass returning target reconstructions and latent embeddings
        x_pred_flat, e_i_flat = model(x_flat)
        
        # Unflatten back to distinct microservices
        x_pred = x_pred_flat.view(batch_size, M, F)
        e_i = e_i_flat.view(batch_size, M, model.embedding_dim)
        
        # 2. Node Addition Loss
        # Calculate the true demand for each node by summing the baseline P99 of services currently on it.
        x_node_true = get_node_ground_truth(y, placement, num_nodes=num_nodes)
        
        # Compute the predicted node demands (by summing service embeddings, then decoding the sum)
        e_node_sum = torch.zeros(batch_size, num_nodes, model.embedding_dim, device=device)
        for b in range(batch_size):
            for i in range(M):
                n = placement[b, i]
                e_node_sum[b, n] += e_i[b, i]
                
        # Decode the summed latent embeddings back to physical features
        x_node_pred = model.decode(e_node_sum) # (Batch, num_nodes, F)
        
        # 3. Mega Loss Optimization
        loss, l_ind, l_add = mega_loss_fn(x_pred, y, x_node_pred, x_node_true, alpha, beta)
        
        loss.backward()
        optimizer.step()
        
        total_mega += loss.item()
        total_ind += l_ind.item()
        total_add += l_add.item()
        
    n = len(dataloader)
    return total_mega/n, total_ind/n, total_add/n

def run_training_pipeline(dataset, epochs=50, batch_size=32, lr=1e-3, 
                          alpha=1.0, beta=1.0, hidden_size=64, num_layers=2, 
                          embedding_dim=32, device='cpu', timestamp=None):
    
    if timestamp is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ckpt_dir = os.path.join(base_dir, "results", "checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)
    save_path = os.path.join(ckpt_dir, f"model_{timestamp}.pth")
                          
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    
    # We pull F (input_size) dynamically from dataset
    first_sample_x = dataset[0]['x']
    F = first_sample_x.shape[-1]
    
    model = AdditiveAutoencoder(input_size=F, hidden_size=hidden_size, 
                                num_layers=num_layers, embedding_dim=embedding_dim).to(device)
                                
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4) 
    
    logger.info(f"Starting Offline Training for Additive Autoencoder. Alpha={alpha}, Beta={beta}")
    
    best_loss = float('inf')
    for epoch in range(epochs):
        mega_loss, ind_loss, add_loss = train_epoch(model, dataloader, optimizer, alpha, beta, device=device)
        
        if (epoch+1) % 5 == 0 or epoch == 0:
            logger.info(f"Epoch [{epoch+1}/{epochs}] - Loss: {mega_loss:.4f} (Ind: {ind_loss:.4f}, Add: {add_loss:.4f})")
            
        if mega_loss < best_loss:
            best_loss = mega_loss
            # Save the optimal state
            torch.save(model.state_dict(), save_path)
            
    logger.info(f"Training Complete. Best Mega Loss: {best_loss:.4f}. Model saved to {save_path}")
    
    # Load and return the best model version
    model.load_state_dict(torch.load(save_path))
    model.eval()
    return model

def optimize_hyperparameters(dataset, n_trials=20, device='cpu'):
    # Hyperparameter Tuning
    """
    Runs hyperparameter tuning using Optuna to find the best configuration.
    """
    if not optuna:
        logger.error("Optuna is not installed. Cannot run Optuna/RayTune hyperparameter search. Please pip install optuna.")
        return None

    def objective(trial):
        # Hyperparameter Search Space
        lstm_hidden_size = trial.suggest_categorical("lstm_hidden_size", [32, 64, 128])
        lstm_num_layers = trial.suggest_categorical("lstm_num_layers", [1, 2, 3])
        embedding_dim_d = trial.suggest_categorical("embedding_dim_d", [16, 32, 64])
        
        learning_rate = trial.suggest_categorical("learning_rate", [1e-4, 1e-3, 1e-2])
        weight_decay = trial.suggest_categorical("weight_decay", [1e-5, 1e-4, 1e-3])
        batch_size = trial.suggest_categorical("batch_size", [16, 32, 64])
        
        # Mega Loss Weights
        alpha = trial.suggest_float("alpha", 0.1, 1.0, step=0.1)
        beta = trial.suggest_float("beta", 0.5, 2.0, step=0.1)
        
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
        F = dataset[0]['x'].shape[-1]
        
        model = AdditiveAutoencoder(input_size=F, hidden_size=lstm_hidden_size, 
                                    num_layers=lstm_num_layers, embedding_dim=embedding_dim_d).to(device)
                                    
        optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
        
        # Fast trial evaluation (10 epochs)
        final_loss = 0.0
        for _ in range(10): 
            mega_loss, _, _ = train_epoch(model, dataloader, optimizer, alpha, beta, device=device)
            final_loss = mega_loss
            
        return final_loss

    logger.info("Starting Bayesian Hyperparameter Search for Autoencoder...")
    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials)
    
    logger.info("Optuna Hyperparameter Search Complete.")
    logger.info(f"Best Trial Value (Mega Loss): {study.best_value}")
    logger.info(f"Best Params: {study.best_params}")
    return study.best_params

# Standalone Testing Executions
if __name__ == "__main__":    
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data_collection.preprocess import TimeSeriesPreprocessor
    from main import parse_offline_data_directories
    
    parser = argparse.ArgumentParser(description="Standalone Model Training Execution")
    parser.add_argument("--epochs", type=int, default=2, help="Number of training epochs")
    parser.add_argument("--run-optuna", action="store_true", help="Run Bayesan Hyperparameter Optimization")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    try:
        preprocessor = TimeSeriesPreprocessor()
        data_list, _ = parse_offline_data_directories(preprocessor)
        dataset = AdditiveEmbeddingDataset(data_list)
        
        if args.run_optuna:
            optimize_hyperparameters(dataset, n_trials=5, device=device)
        else:
            run_training_pipeline(dataset, epochs=args.epochs, device=device)
            
        logger.info("Standalone Model Training Execution completed.")
    except Exception as e:
        logger.error(f"Failed standalone training execution: {e}")
