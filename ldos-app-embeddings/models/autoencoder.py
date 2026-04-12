import torch
import torch.nn as nn

class AdditiveAutoencoder(nn.Module):
    """
    Additive Latent Autoencoder (Draft 2).
    Learns service embeddings from 60s windows of metrics. Ensures embeddings are additive 
    so predicting total load on a node is just summing embeddings.
    """
    def __init__(self, input_size=13, hidden_size=64, num_layers=2, embedding_dim=32):
        super(AdditiveAutoencoder, self).__init__()
        
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.embedding_dim = embedding_dim
        
        # Encoder
        self.lstm = nn.LSTM(input_size=input_size, 
                            hidden_size=hidden_size, 
                            num_layers=num_layers, 
                            batch_first=True)
                            
        # Map final hidden state to embedding dimension
        self.encoder_linear = nn.Linear(hidden_size, embedding_dim)
        
        # Ensure embeddings are positive so they add up correctly
        self.relu = nn.ReLU() 
        
        # Decoder
        # IMPORTANT: No bias allowed. This ensures vector math works for summing embeddings: W(A + B) = W(A) + W(B).
        self.decoder = nn.Linear(in_features=embedding_dim, out_features=input_size, bias=False)

    # Forward Operations
    def encode(self, x):
        """
        Input x shape: (Batch, Sequence_Length, F)
        Returns latent embedding E_i shape: (Batch, d)
        """
        out, (hn, cn) = self.lstm(x)
        
        # Extract the state from the top LSTM layer
        final_hidden = hn[-1, :, :] # (Batch, Hidden)
        
        # Project and force non-negativity
        e_i = self.relu(self.encoder_linear(final_hidden))
        return e_i

    def decode(self, e_i):
        """
        Input e_i shape: (Batch, d)
        Returns reconstructed/predicted aggregate features shape: (Batch, F)
        """
        return self.decoder(e_i)

    def forward(self, x):
        """
        Standard forward pass. 
        Returns both the reconstructed aggregate variables and the latent embeddings.
        """
        e_i = self.encode(x)
        x_pred = self.decode(e_i)
        return x_pred, e_i

    def get_max_node_tensor(self, device='cpu'):
        """
        Calculates the maximum allowed sum of embeddings for a single node. 
        We do this by passing a synthetic 60s window of 100% hardware utilization through the encoder.
        """
        # Shape: (Batch=1, Seq_len=60, F=input_size)
        synthetic_max_input = torch.ones(1, 60, self.input_size, dtype=torch.float32).to(device)
        
        # Inference mode
        self.eval() 
        with torch.no_grad():
            E_N_max = self.encode(synthetic_max_input).squeeze(0) # Shape: (d,)
            
        return E_N_max * 0.15
