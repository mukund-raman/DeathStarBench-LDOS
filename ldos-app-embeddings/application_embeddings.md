## ML Model Learning Problem (Draft 2\)

### The Learning Problem

The core learning problem is to accurately profile and represent the time-dependent resource usage behavior of individual microservices using limited offline tracing data. By learning these "signatures" (application embeddings), the system can intelligently place microservices across a Kubernetes cluster to minimize resource contention. The goal is to minimize end-to-end (E2E) tail latencies (like the P99 latency) and prevent Service Level Agreement (SLA) violations.

### Phase 1: Data Collection, Feature Engineering & Anomaly Injection

To map intrinsic resource demand independently of a specific placement, we must force the microservices to reveal their maximum required capacities under various stress conditions.

**1\. Raw Metrics & Feature Engineering:**  
We collect a comprehensive suite of raw system and application-level metrics.

* **Rate Conversions (First Derivative):** Cumulative OS counters are converted to instantaneous rates (per second) to represent immediate load: [![][image1]](https://www.codecogs.com/eqnedit.php?latex=%5CDelta%5Ctext%7Butime%7D%2C%20%5CDelta%5Ctext%7Bstime%7D#0) (CPU), [![][image2]](https://www.codecogs.com/eqnedit.php?latex=%5CDelta%5Ctext%7Brx%7D%2C%20%5CDelta%5Ctext%7Btx%7D#0) (Network), [![][image3]](https://www.codecogs.com/eqnedit.php?latex=%5CDelta%5Ctext%7Bpgfault%7D#0) (Memory), [![][image4]](http://www.texrendr.com/?eqn=%5CDelta%5Ctext%7Bread_%7Bbytes%7D%7D%2C%20%5CDelta%5Ctext%7Bwrite_%7Bbytes%7D%7D#0) (Disk I/O).  
* **State Metrics:** `rss`, `cache` (Memory).  
* **Demand/Queue Metrics:** `jobs_waiting_count`, `unique_jobs_waiting`, `thread_queue_length`, `active_connections`.

**2\. Normalization Strategy:**  
All metrics are min-max scaled to [![][image5]](https://www.codecogs.com/eqnedit.php?latex=%5B0%2C%201%5D#0). We obtain the maximum physical limits directly from the physical host node (e.g., total CPU cores via `nproc`, total RAM via `/proc/meminfo`, max NIC bandwidth via `ethtool`).

* **Why node limits:** Scaling by the physical node limit ensures that an aggregated feature value of [![][image6]](https://www.codecogs.com/eqnedit.php?latex=1.0#0) universally means "100% Physical Node Saturation." This is mathematically required so the autoencoder's linear decoder can map the latent sum back to a physical capacity limit.

**3\. The 15-Minute Anomaly Injection Timeline:**  
For each of the \~30 Maximin configurations, run a constant 500 Requests Per Second (RPS) workload. We sweep the constraint space using this 15-minute timeline per configuration:

* **Minute 0-2 (Warmup & Baseline):** No stressors. The application warms up. This captures the "Unthrottled Baseline."  
* **Minute 2-4 (CPU Stress):** `stress-ng --cpu 4 --cpu-load 85`. Simulates heavy compute neighbors.  
* **Minute 4-6 (Memory Stress):** `stress-ng --vm 2 --vm-bytes 80%`. Simulates memory leaks or cache thrashing.  
* **Minute 6-8 (Network Delay):** `tc qdisc add dev eth0 root netem delay 50ms`. Simulates switch saturation or cross-node RPC latency.  
* **Minute 8-10 (Disk I/O Stress):** `sysbench fileio --file-test-mode=rndrw run`. Simulates logging or database contention.  
* **Minute 10-12 (Multi-Stress: CPU \+ Network):** `stress-ng --cpu 4` AND `tc qdisc delay 50ms`. Tests coupled queueing delays due to stress on shared resources.  
* **Minute 12-14 (Multi-Stress: Memory \+ Disk):** `stress-ng --vm 2` AND `sysbench fileio`. Tests extreme page-faulting and swap space thrashing.  
* **Minute 14-15 (Cooldown):** Remove all stressors. Observe how it recovers.

#### Sources & Research Justifications

* **Idea Source:** The `FIRM` paper methodology for SLO-oriented microservices and prior discussions on decoupling dependencies through performance anomaly injection.  
* **Supporting Research:** *FIRM: An Intelligent Fine-grained Resource Management Framework for SLO-Oriented Microservices* (Qiu et al., OSDI 2020).  
* **Justification:** This paper proves that injecting fine-grained, localized anomalies (CPU/Memory/Network) successfully mimics real-world resource contention, allowing ML models to accurately localize and learn SLO violations without requiring exhaustive combinatorial testing of the entire placement space.

---

### Phase 2: Training the Additive Autoencoder

**1\. Defining "True Demand" and Target Labels:**  
The fundamental assumption is that a microservice's resource usage during the unthrottled baseline (Minutes 0-2) represents its *true demand* for the current workload.

* **The Target Label ([![][image7]](https://www.codecogs.com/eqnedit.php?latex=X_i%5E%7Bagg_%7Btrue%7D%7D#0)):** Regardless of what resource usage pattern the microservice is currently exhibiting (e.g., flattened CPU due to stress at Minute 3), the target label for the autoencoder is the P99 value of the [![][image8]](https://www.codecogs.com/eqnedit.php?latex=F#0) features across the first 2 minutes (the baseline) of that specific run.  
* **The Result:** By pairing a throttled 60-second input trace ([![][image9]](https://www.codecogs.com/eqnedit.php?latex=X_%7Bi%2C%20%5Ctext%7Bstressed%7D%7D#0)) with its unthrottled baseline aggregate ([![][image10]](https://www.codecogs.com/eqnedit.php?latex=X_%7Bi%2C%20%5Ctext%7Bbaseline%7D%7D#0)), the model learns: *"When I see flattened usage but spiking queues, I must output the high-magnitude embedding representing the resources the app actually NEEDS (demand), not what it is currently getting."*  
* **True Node Demand ([![][image11]](https://www.codecogs.com/eqnedit.php?latex=X_n%5E%7Btotal_%7Btrue%7D%7D#0)):** The true node demand is simply the sum of the true demands of all microservices physically placed on that node: [![][image12]](https://www.codecogs.com/eqnedit.php?latex=%5Csum_%7Bi%20%5Cin%20n%7D%20X_i%5E%7Bagg_%7Btrue%7D%7D#0).

**2\. Handling Data of Shape `(M, Batch, 60, F)`:**  
The dataset is aggregated such that each sample contains the 60-second traces for all [![][image13]](https://www.codecogs.com/eqnedit.php?latex=M#0) microservices (e.g., [![][image14]](https://www.codecogs.com/eqnedit.php?latex=M%3D27#0)). During a forward pass, the model processes the batch of sequences for each microservice [![][image15]](https://www.codecogs.com/eqnedit.php?latex=i#0), generates all [![][image16]](https://www.codecogs.com/eqnedit.php?latex=M#0) embeddings, and then applies the placement mapping [![][image17]](https://www.codecogs.com/eqnedit.php?latex=P#0) to calculate the node-level sums for the additive loss.

**3\. Model Architecture Specifications**

* **Encoder ([![][image18]](https://www.codecogs.com/eqnedit.php?latex=f_%7B%5Ctheta%7D#0)):**  
  * *Input:* Sequence of shape `(Batch, 60, F)` where [![][image19]](https://www.codecogs.com/eqnedit.php?latex=F#0) is the number of features.  
  * *Layers:* Variable-layer LSTM: `input_size=F`, `hidden_size=H`.  
  * *Projection:* A fully connected Linear layer mapping the final LSTM hidden state from size [![][image20]](https://www.codecogs.com/eqnedit.php?latex=H#0) to embedding dimension [![][image21]](https://www.codecogs.com/eqnedit.php?latex=d#0).  
    * *Activation:* `ReLU()`. Ensures [![][image22]](https://www.codecogs.com/eqnedit.php?latex=E_i%20%5Cin%20%5Cmathbb%7BR%7D_%7B%5Cge%200%7D%5Ed#0) for physical addition of resources.  
  * Output shape: `(Batch, d)`.  
* **Decoder ([![][image23]](https://www.codecogs.com/eqnedit.php?latex=g_%7B%5Cphi%7D#0)):**  
  * *Layer:* A single `nn.Linear(in_features=d, out_features=F, bias=False)`, with weight matrix [![][image24]](https://www.codecogs.com/eqnedit.php?latex=W#0).  
  * *Constraint:* No bias term allowed. This guarantees [![][image25]](https://www.codecogs.com/eqnedit.php?latex=W\(E_A%20%2B%20E_B\)%20%3D%20W\(E_A\)%20%2B%20W\(E_B\)#0), maintaining strict mathematical addition.  
  * Output shape: `(Batch, F)`.

**4\. Training with the Mega Loss Function & Training Loop:**  
We optimize both individual reconstruction and node-level addition simultaneously:

[![][image26]](https://www.codecogs.com/eqnedit.php?latex=%7B%5Cmathcal%7BL%7D%7D_%7Bmega%7D%20%3D%20%5Calpha%20%5Csum_%7Bi%3D1%7D%5EM%20%7C%7C%20X_%7Bi%7D%5E%7Bagg_%7Btrue%7D%7D%20-%20W%20%5Ccdot%20E_i%20%7C%7C_2%5E2%20%2B%20%5Cbeta%20%5Csum_%7Bn%3D1%7D%5E%7BNodes%7D%20%5Cleft%7C%5Cleft%7C%20X_%7Bn%7D%5E%7Btotal_%7Btrue%7D%7D%20-%20W%20%5Ccdot%20%5Cleft\(%20%5Csum_%7Bi%20%5Cin%20n%7D%20E_i%20%5Cright\)%20%5Cright%7C%5Cright%7C_2%5E2#0)

**Execution Loop (Training Pseudocode):**  
INITIALIZE Encoder f\_theta, Decoder W  
SET Optimizer (e.g., Adam)  
FOR EACH epoch:  
    FOR EACH batch IN dataloader (shape: M, Batch\_Size, 60, F):  
        ZERO GRADIENTS  
        INITIALIZE E\_dict \= {}  
        INITIALIZE L\_ind \= 0

        // 1\. Generate Embeddings & Individual Loss  
        FOR i FROM 1 TO M:  
            X\_i \= batch\[i\]   
            E\_i \= f\_theta(X\_i)  // Shape: (Batch\_Size, d)  
            E\_dict\[i\] \= E\_i  
            X\_agg\_pred \= W \* E\_i  
            L\_ind \+= MSE(X\_agg\_pred, X\_i\_true\_baseline\_demand)

        // 2\. Node Addition Loss  
        INITIALIZE L\_add \= 0  
        FOR EACH node n IN cluster:  
            E\_sum\_n \= SUM(E\_dict\[i\] for all i placed on node n)  
            X\_node\_pred \= W \* E\_sum\_n  
            X\_node\_true \= SUM(X\_i\_true\_baseline\_demand for all i placed on node n)  
            L\_add \+= MSE(X\_node\_pred, X\_node\_true)

        // 3\. Mega Loss Optimization  
        L\_mega \= (alpha \* L\_ind) \+ (beta \* L\_add)  
        L\_mega.BACKWARD()  
        OPTIMIZER.STEP()

**5\. Synthetic Max Node Tensor:**  
To determine the bounding constraint [![][image27]](https://www.codecogs.com/eqnedit.php?latex=E_%7BN_x%7D#0), we create a synthetic matrix of shape `(60, F)` where every element is exactly `1.0` (representing 100% capacity of all normalized hardware limits for 60 seconds). We pass this through the trained encoder: [![][image28]](https://www.codecogs.com/eqnedit.php?latex=E_%7BN_%7Bmax%7D%7D%20%3D%20f_%7B%5Ctheta%7D\(%5Cmathbf%7B1%7D_%7B60%20%5Ctimes%20F%7D\)#0).

**6\. Hyperparameter Search Space Array Example (Optuna/Ray Tune format):**  
search\_space \= {  
    \# Encoder Architecture  
    "lstm\_hidden\_size": tune.grid\_search(\[32, 64, 128\]),  
    "lstm\_num\_layers": tune.grid\_search(\[1, 2, 3\]),  
    "embedding\_dim\_d": tune.grid\_search(\[16, 32, 64\]), \# Size of E\_i

    \# Optimizer Params  
    "learning\_rate": tune.grid\_search(\[1e-4, 1e-3, 1e-2\]),  
    "weight\_decay": tune.grid\_search(\[1e-5, 1e-4, 1e-3\]),  
    "batch\_size": tune.grid\_search(\[16, 32, 64\]),

    \# Mega Loss Weights  
    "alpha": tune.grid\_search(\[0.1, …, 1.0\]), \# Individual reconstruction weight  
    "beta": tune.grid\_search(\[0.5, …, 2.0\])   \# Node addition weight  
}

#### Sources & Research Justifications (Phase 2\)

* **Idea Source:** The discussion in the meeting about the mathematical constraint of [![][image29]](https://www.codecogs.com/eqnedit.php?latex=E_A%20%2B%20E_B%20%5Cle%20E_%7BNodeLimit%7D#0).  
* **Supporting Research:** *Latent Linear Adjustment Autoencoder v1.0: a novel method for estimating and emulating dynamic precipitation at high resolution* (Heinze-Deml et al., 2021).  
* **Justification:** This recent paper proves that an autoencoder equipped with a linear component/decoder successfully captures and projects physical, additive environmental variables onto a latent space. It demonstrates that combining a non-linear encoder for feature extraction with a linear decoder penalty guarantees that the latent space maintains strict linear additivity.

---

### Phase 3: Online Execution A (Gradient Routing & Swapping)

This execution loop dynamically shifts workload using active/inactive replicas and a calculated latency gradient.

**1\. Mathematical Formulation:**

* **State:** Let [![][image30]](https://www.codecogs.com/eqnedit.php?latex=%7B%5Cvec%7BV%7D%7D_n%20%3D%20%5Cmax\(0%2C%20%5Csum_%7B%7Bi%20%5Cin%20n%7D%7D%20E_i%20-%20E_%7BN_%7Bmax%7D%7D\)#0) be the violation vector for node [![][image31]](https://www.codecogs.com/eqnedit.php?latex=n#0). If [![][image32]](https://www.codecogs.com/eqnedit.php?latex=%5Cvec%7BV%7D_n%20%3D%20%5Cvec%7B0%7D#0), the node is healthy.  
* **Evacuation vs. Swap:** If Node A is overloaded ([![][image33]](https://www.codecogs.com/eqnedit.php?latex=%7C%7C%5Cvec%7BV%7D_A%7C%7C_1%20%3E%200#0)), we evaluate all candidate Nodes [![][image34]](https://www.codecogs.com/eqnedit.php?latex=B%20%5Cneq%20A#0).  
  * If Node B is healthy ([![][image35]](https://www.codecogs.com/eqnedit.php?latex=%5Cvec%7BV%7D_B%20%3D%20%5Cvec%7B0%7D#0)), we test an *evacuation* (moving microservice [![][image36]](https://www.codecogs.com/eqnedit.php?latex=m_A#0) from A to B).  
  * If Node B is also overloaded ([![][image37]](https://www.codecogs.com/eqnedit.php?latex=%5Cvec%7BV%7D_B%20%3E%200#0)), we test a *swap*. We want to find a service [![][image38]](https://www.codecogs.com/eqnedit.php?latex=m_B%20%5Cin%20B#0) that, when swapped with [![][image39]](https://www.codecogs.com/eqnedit.php?latex=m_A#0), minimizes the predicted joint violation.  
* **Mathematical Joint Violation Minimization:** For a candidate swap [![][image40]](https://www.codecogs.com/eqnedit.php?latex=\(m_A%20%5Cleftrightarrow%20m_B\)#0):  
  1. Predicted Sum on A: [![][image41]](https://www.codecogs.com/eqnedit.php?latex=E_%7B%7BA%2C%20test%7D%7D%20%3D%20E_%7Bsum_A%7D%20-%20E_%7Bm_A%7D%20%2B%20E_%7Bm_B%7D#0)  
  2. Predicted Sum on B: [![][image42]](https://www.codecogs.com/eqnedit.php?latex=E_%7B%7BB%2C%20test%7D%7D%20%3D%20E_%7Bsum_B%7D%20-%20E_%7Bm_B%7D%20%2B%20E_%7Bm_A%7D#0)  
  3. Predicted Violation A: [![][image43]](https://www.codecogs.com/eqnedit.php?latex=%5Cvec%7BV%7D_%7B%7BA%2C%20test%7D%7D%20%3D%20%5Cmax\(0%2C%20E_%7B%7BA%2C%20test%7D%7D%20-%20E_%7BN_%7Bmax%7D%7D\)#0)  
  4. Predicted Violation B: [![][image44]](https://www.codecogs.com/eqnedit.php?latex=%5Cvec%7BV%7D_%7B%7BB%2C%20test%7D%7D%20%3D%20%5Cmax\(0%2C%20E_%7B%7BB%2C%20test%7D%7D%20-%20E_%7BN_%7Bmax%7D%7D\)#0)  
  5. Joint Cost: [![][image45]](https://www.codecogs.com/eqnedit.php?latex=Cost%20%3D%20%7C%7C%5Cvec%7BV%7D_%7B%7BA%2C%20test%7D%7D%7C%7C_1%20%2B%20%7C%7C%5Cvec%7BV%7D_%7B%7BB%2C%20test%7D%7D%7C%7C_1#0). We select the [![][image46]](https://www.codecogs.com/eqnedit.php?latex=m_B%20%5Cin%20B#0) that yields the absolute lowest [![][image47]](https://www.codecogs.com/eqnedit.php?latex=Cost#0). If this [![][image48]](https://www.codecogs.com/eqnedit.php?latex=Cost%20%3C%20%7C%7C%5Cvec%7BV%7D_A%7C%7C_1%20%2B%20%7C%7C%5Cvec%7BV%7D_B%7C%7C_1#0), the swap is mathematically valid to test.

**2\. Execution Loop (Logical Flow):**  
LOOP EVERY 60s:  
    // 1\. Inference (Encoder Only)  
    FOR EACH service i:  
        COMPUTE E\_i \= Encoder(X\_i\_last\_60s)

    // 2\. Identify Overloads  
    FOR EACH node n:  
        COMPUTE E\_sum\_n \= SUM(E\_i for all i on n)  
        COMPUTE V\_n \= MAX(0, E\_sum\_n \- E\_N\_max)  
        IF ||V\_n||\_1 \> 0: MARK n as OVERLOADED

    IF OVERLOADED nodes exist:  
        SELECT worst Node A (highest ||V\_A||\_1)  
        SORT services on A descending by ||E\_i||\_1  // L1 Norm \= Resource Heaviness

        FOR EACH service m\_A in sorted list:  
            MEASURE LAT\_curr  
            SET best\_gradient \= 0, target\_node \= NULL, swap\_target \= NULL  
            // Evaluate ALL candidate nodes  
            FOR EACH candidate Node B \!= A:  
                IF B is HEALTHY:  
                    // Test Evacuation  
                    ROUTE 1% m\_A traffic to replica on B  
                    WAIT 60s; MEASURE LAT\_test  
                    COMPUTE grad \= (LAT\_test \- LAT\_curr) / 0.01  
                    IF grad \< best\_gradient: best\_gradient \= grad; target\_node \= B  
                    REVERT 1% traffic  
                  
                ELSE IF B is OVERLOADED:  
                    // Test Swap (Try to balance complementary overloads)  
                    SET lowest\_joint\_cost \= ||V\_A||\_1 \+ ||V\_B||\_1  
                    SET best\_m\_B \= NULL

                    // Find mathematically optimal m\_B  
                    FOR EACH service m\_B on B:  
                        COMPUTE V\_A\_test \= MAX(0, E\_sum\_A \- E\_m\_A \+ E\_m\_B \- E\_N\_max)  
                        COMPUTE V\_B\_test \= MAX(0, E\_sum\_B \- E\_m\_B \+ E\_m\_A \- E\_N\_max)  
                        COMPUTE Cost \= ||V\_A\_test||\_1 \+ ||V\_B\_test||\_1

                        IF Cost \< lowest\_joint\_cost:  
                            lowest\_joint\_cost \= Cost  
                            best\_m\_B \= m\_B

                    IF best\_m\_B \!= NULL:  
                        ROUTE 1% m\_A to B AND 1% best\_m\_B to A  
                        WAIT 60s; MEASURE LAT\_test  
                        COMPUTE grad \= (LAT\_test \- LAT\_curr) / 0.01  
                        IF grad \< best\_gradient:   
                            best\_gradient \= grad; target\_node \= B; swap\_target \= best\_m\_B  
                        REVERT 1% traffic

            // Execute best mitigation  
            IF best\_gradient \< 0:  
                IF swap\_target is NULL:  
                    ROUTE 100% m\_A to target\_node  
                ELSE:  
                    ROUTE 100% m\_A to target\_node AND 100% swap\_target to A  
                UPDATE Placements  
                BREAK  // Wait for next 60s cycle to stabilize

#### Sources & Research Justifications (Phase 3\)

* **Idea Source:** The discussion in the meeting about the 1% traffic shift to approximate a latency gradient.  
* **Supporting Research:** *Guided-SPSA: Simultaneous Perturbation Stochastic Approximation Assisted by the Parameter Shift Rule* (Anand et al., IEEE, 2024).  
* **Justification:** This recent research modernizes gradient estimation in black-box systems. It proves that combining localized parameter-shift rules (analogous to routing exactly 1% of traffic to a replica to sample an effect) with stochastic approximation converges to optimal system states significantly faster and with more stability than exhaustive search. This mathematically justifies that small, localized empirical traffic perturbations are a robust, state-of-the-art method for estimating gradients in noisy, non-differentiable environments like E2E cloud latency.

---

### Phase 4: Online Execution B (Guided Bayesian Optimization)

Another option is a Constrained Bayesian Optimization (cBO) approach that mathematically predicts E2E latency.

**1\. Mathematical Formulation:**

* **Pre-computed Constants:** We use the encoder to compute all 27 embeddings [![][image49]](https://www.codecogs.com/eqnedit.php?latex=%7BE_1%20...%20E_%7B27%7D%7D#0) from the current 60s time window. *We do not run the encoder again during candidate evaluation.*  
* **Search Space:** [![][image50]](https://www.codecogs.com/eqnedit.php?latex=P_%7Bcand%7D%20%5Cin%20%7B1..5%7D%5E%7B27%7D#0), mapping services to one of 5 nodes.  
* **Fast Constraint ([![][image51]](https://www.codecogs.com/eqnedit.php?latex=c\(P_%7Bcand%7D\)#0)):** For any candidate placement [![][image52]](https://www.codecogs.com/eqnedit.php?latex=P_%7Bcand%7D#0), we calculate the predicted node sums by mathematically adding the *pre-computed* [![][image53]](https://www.codecogs.com/eqnedit.php?latex=E_i#0) vectors based on where [![][image54]](https://www.codecogs.com/eqnedit.php?latex=P_%7Bcand%7D#0) places them: [![][image55]](https://www.codecogs.com/eqnedit.php?latex=c\(P_%7Bcand%7D\)%20%3D%20%5Cmax_%7Bn%7D%20%7C%7C%5Csum_%7Bi%20%5Cin%20n%7D%20E_i%20-%20E_%7BN_%7Bmax%7D%7D%7C%7C%20%5Cle%200#0).  
* **Surrogate Model (GP):** The Gaussian Process predicts latency based on the node-level aggregated demands. [![][image56]](https://www.codecogs.com/eqnedit.php?latex=X_%7BBO%7D%20%3D%20%5BE_%7Bsum%7D%5E1%20%5Coplus%20E_%7Bsum%7D%5E2%20%5Coplus%20...%20%5Coplus%20E_%7Bsum%7D%5E5%5D#0), a vector of size [![][image57]](https://www.codecogs.com/eqnedit.php?latex=5d#0), where [![][image58]](https://www.codecogs.com/eqnedit.php?latex=%5Coplus#0) denotes vector concatenation. [![][image59]](https://www.codecogs.com/eqnedit.php?latex=f_%7BGP%7D\(X_%7BBO%7D\)%20%5Capprox%20%5Ctext%7BLatency%7D#0).  
* **GP Training:** Offline, the GP is trained on the baseline configurations.  
* Input: [![][image60]](https://www.codecogs.com/eqnedit.php?latex=X_%7BBO%7D#0) (the concatenated node sums of embeddings from that run).  
* Output/Target: The measured E2E P99 latency of that run.

**2\. Execution Loop (Logical Flow):**  
INITIALIZE Gaussian Process (GP) trained on baseline E2E latencies  
LOOP EVERY 60s:  
    // 1\. Compute Base Embeddings  
    FOR EACH service i: E\_i \= Encoder(X\_i\_last\_60s)

    // 2\. Check Constraints  
    IF ANY Node n exceeds E\_N\_max:  
        GENERATE 10,000 candidate placements (P\_cand)  
        VALID\_PLACEMENTS \= \[\]

        // 3\. Mathematical Filtering (No ML inference here, just vector addition)  
        FOR EACH P\_cand:  
            FOR EACH node n:  
                E\_sum\_n\_cand \= SUM(E\_i for i placed on n in P\_cand)

            IF ALL E\_sum\_n\_cand \<= E\_N\_max:  
                APPEND P\_cand to VALID\_PLACEMENTS

        // 4\. GP Evaluation  
        SET best\_P \= NULL; lowest\_lat \= INFINITY  
        FOR EACH P\_cand IN VALID\_PLACEMENTS:  
            CONSTRUCT X\_BO \= CONCATENATE(E\_sum\_1\_cand, ..., E\_sum\_5\_cand)  
            PREDICT predicted\_lat \= GP(X\_BO)  
            IF predicted\_lat \< lowest\_lat:  
                lowest\_lat \= predicted\_lat  
                best\_P \= P\_cand

        // 5\. Execution  
        EXECUTE best\_P via Kubernetes

#### Sources & Research Justifications (Phase 4\)

* **Idea Source:** The discussion in the meeting about a guided BO that learns the placement-to-latency landscape using the embeddings.  
* **Supporting Research:** *CherryPick: Adaptively Unearthing the Best Cloud Configurations for Big Data Analytics* (Alipourfard et al., NSDI 2017).  
* **Justification:** This paper demonstrates that Bayesian Optimization fundamentally outperforms rule-based or greedy bin-packing heuristics in cloud resource allocation. By building a surrogate model, BO can accurately map complex, non-linear performance landscapes and find optimal configurations with a minimal number of physical evaluations.

---

### Phase 5: Robust Evaluation with Performance Anomaly Injection

We evaluate the system by forcing a failure and timing the automated recovery.

**Execution Loop (Logical Flow):**  
DEFINE ANOMALIES \= \[  
    ("CPU", "stress-ng \--cpu 4", Node 1),  
    ("Memory", "stress-ng \--vm 2", Node 2),  
    ("Network", "tc qdisc delay 50ms", Node 3\)  
\]

FOR EACH anomaly IN ANOMALIES:  
    RUN 500 RPS workload  
    WAIT 2 mins; RECORD average baseline P99 latency as L\_base  
    INJECT anomaly ON anomaly.target\_node  
    RECORD T\_inject \= NOW()

    LOOP CONTINUOUSLY:  
        POLL current P99 latency  
        IF (Phase 3 or 4 executes mitigation) AND (current P99 \<= 1.10 \* L\_base):  
            RECORD T\_recover \= NOW()  
            LOG "PASS: Recovered ${anomaly.type} in ${T\_recover \- T\_inject} sec."  
            BREAK LOOP

        IF (NOW() \- T\_inject) \> 300 seconds:  
            LOG "FAIL: Mitigation timeout for ${anomaly.type}."  
            BREAK LOOP

    REMOVE anomaly; WAIT 2 mins for stabilization

#### Sources & Research Justifications (Phase 5\)

* **Idea Source:** Standard deployment validation workflows combined with the anomaly specifications in `FIRM`.  
* **Supporting Research:** *Lineage-driven Fault Injection* (Alvaro et al., SIGMOD 2015).  
* **Efficacy:** This paper proves that targeted, scripted fault injection discovers edge-case system failures faster and provides more reliable robustness guarantees in distributed systems than randomized stress testing, validating the Recovery Time Objective (RTO) evaluation loop defined above.

---

### Phase 6: Bottleneck Identification (Secondary Use Case)

Another use case is in determining if a microservice's current embedding drastically changes compared to its historical steady-state, which would indicate that the service is bottlenecking the application.

**1\. Mathematical Formulation**:

* **Initialization:** Let [![][image61]](https://www.codecogs.com/eqnedit.php?latex=%5Cbar%7BE%7D_i\(0\)#0) be the simple average of the first N valid embeddings [![][image62]](https://www.codecogs.com/eqnedit.php?latex=E_i#0) generated during a known healthy baseline period (e.g., the first 10 minutes of execution with no SLA violations).  
* **Exponential Moving Average (EMA):** [![][image63]](https://www.codecogs.com/eqnedit.php?latex=%5Cbar%7BE%7D_i\(t\)%20%3D%20%5Calpha%20E_i\(t\)%20%2B%20\(1-%5Calpha\)%5Cbar%7BE%7D_i\(t-1\)#0) for [![][image64]](https://www.codecogs.com/eqnedit.php?latex=t%20%5Cge%201#0), where [![][image65]](https://www.codecogs.com/eqnedit.php?latex=%5Calpha%20%5Capprox%200.1#0). Keeps track of the steady state for an embedding to compare against.  
* **Cosine Distance:** [![][image66]](https://www.codecogs.com/eqnedit.php?latex=D_i%20%3D%201%20-%20%5Cfrac%7BE_i\(t\)%20%5Ccdot%20%5Cbar%7BE%7D_i\(t-1\)%7D%7B%7C%7CE_i\(t\)%7C%7C_2%20%5Ccdot%20%7C%7C%5Cbar%7BE%7D_i\(t-1\)%7C%7C_2%7D#0). This ranges from 0 to 1, with 0 meaning they are exactly similar and 1 meaning they have no similarity (orthogonal).

**2\. Execution Loop (Logical Flow):**  
// Initialize baseline using first 10 healthy minutes  
FOR EACH service i:  
    E\_bar\_i \= AVERAGE(E\_i from minute 1 to 10\)  
SET threshold \= 0.2  
SET alpha \= 0.1

LOOP EVERY 60s: // Runs in parallel to phase 3/4  
    FOR EACH service i:  
        COMPUTE E\_i(t) \= Encoder(X\_i\_last\_60s)

        // Compute distance against historical state  
        COMPUTE Cosine Distance D\_i between E\_i(t) and E\_bar\_i  
        IF D\_i \> threshold:  
            TRIGGER ALERT: "Service i is exhibiting an anomaly causing a bottleneck."  
            // Perform any action as needed

        // Update history  
        E\_bar\_i \= (alpha \* E\_i(t)) \+ ((1 \- alpha) \* E\_bar\_i)

#### Sources & Research Justifications (Phase 6\)

* **Idea Source:** Thoughts from Divyanshu on identifying how 'different' the embeddings for a given service are from 'usual' can help pinpoint bottlenecks.  
* **Supporting Research:** *Seer: Leveraging Big Data to Navigate the Complexity of Cloud Debugging* (Gan et al., ASPLOS 2019).  
* **Efficacy:** This paper proves that using spatial/temporal distance metrics on deep learning representations of microservice traces can accurately predict and localize the root causes of internal performance bottlenecks and QoS violations before they propagate to user-facing SLO failures.

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGkAAAANBAMAAABYyV1FAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMs3vuxCJRHZm3VSrIpmuo/zQAAAAHUlEQVR4XmP8z0A6+MiELkIUGNWFDEZ1IQPydAEAQ+sCCtZd2Z8AAAAASUVORK5CYII=>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADwAAAANBAMAAAAH9BBJAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMs3vuxCJRHZm3VSrIpmuo/zQAAAAF0lEQVR4XmP8z4APMKELoIJRaayAMmkApocBGZMpvjUAAAAASUVORK5CYII=>

[image3]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAANBAMAAADlKAswAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMs3vuxCJRHZm3VSrIpmuo/zQAAAAGElEQVR4XmP8z4AHfGRCF0EFo9JYAWXSAAXKAgrGJV23AAAAAElFTkSuQmCC>

[image4]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAKUAAAAQBAMAAACFAqIvAAAAMFBMVEX////V1dXKysq/v7+1tbWpqamenp5ubm5hYWFUVFRGRkY4ODgoKCgYGBgAAAAAAACVaI6XAAAAAXRSTlMAQObYZgAAAYtJREFUeF6tlMFOg0AQhn8oWg+YeNZLezXG7NGkieHqwXfg6MEDiS/ACxhJGuOVR2ji3fAIHIxeSXyCBg4FKXWGhXZZS2Mav4RlZ/iZmZ1dMFbYl3yoeyRzk4ZS9/ax7FgvHUuBY77rzj6miWp5qqFCMXOR6N4evJnmCDRbQjGHIGkCgbjAAjGQPQhdVpNvSquQ0pjdcz/CjUJicjKSjlJU4hBHuACeHmNdVvMpbzkceo3T2hbwBVfVMMZqOdB9KpGzntI+V9z+wFOcNaGrWrTvU17JGqedUCNATXGorDDjhVKP8E2RA4/708HtmkZq9560zOaxtKJrLg+iDSX9vcxNPvMyZNnuY9I8tF95tOCYBS5RNCHfGnlDRAuh+kuccwB/BhGax/JRQi/P4FU4TXGGEWmpIbeURZAyOyQFX8wEz7JbUV3GhFMcRFbwQdlz/waxa+z6Nter/QPJqJ2NB/7G/Ys73dFLcHXCt5wOF8Z193vYfvK3EC48bkOBoS8K4e1c+37U/6X/IFfmP12Zb9/s9nBKAAAAAElFTkSuQmCC>

[image5]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABsAAAAQBAMAAAAVPRmlAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAq1TviSLdMnbNRLtmEJl6m9XOAAAAFUlEQVR4XmP8z4AEPjIh8xgYhh8XANLSAhAwdYSEAAAAAElFTkSuQmCC>

[image6]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABEAAAALBAMAAABrDns0AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAdu9mmRC7RM1UMiKJq90p5NcAAAAAFElEQVR4XmP8zwABH5mgDAYG2rEA0NACBo6EUuYAAAAASUVORK5CYII=>

[image7]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAQBAMAAACigOGCAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAF0lEQVR4XmP8z4AdMKELwMCoBAagogQAqBABH3bG7ngAAAAASUVORK5CYII=>

[image8]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAsAAAALBAMAAABbgmoVAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZkS7MhCJze/dInb7CH9wAAAAFElEQVR4XmP8zwAEH5lAJAMDhRQAkicCBm41e8MAAAAASUVORK5CYII=>

[image9]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADgAAAAPBAMAAABD1xE4AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAF0lEQVR4XmP8z4AbMKELIINRSYaBkQQAy40BHVkI2eoAAAAASUVORK5CYII=>

[image10]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADgAAAAPBAMAAABD1xE4AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAF0lEQVR4XmP8z4AbMKELIINRSYaBkQQAy40BHVkI2eoAAAAASUVORK5CYII=>

[image11]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADcAAAARBAMAAACLACleAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAGElEQVR4XmP8z4ATfGRCF0EGo5IMQ00SAMKsAhIXGkYnAAAAAElFTkSuQmCC>

[image12]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEkAAAAhBAMAAABn+pvYAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIs0yiauZEO9URGZ2u92QCztbAAAAI0lEQVR4XmP8z0AYfGRCF8EKRlXBwKgqGBhVBQOjqmCA/qoAAO4CMi4+fuEAAAAASUVORK5CYII=>

[image13]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA8AAAALBAMAAABSacpvAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARM27Imbvmat2EDLdiVRWT+/bAAAAFElEQVR4XmP8zwAGH5kgNAMD1RkAu+0CBiZoofUAAAAASUVORK5CYII=>

[image14]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADMAAAALBAMAAAAgpqjZAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARM27Imbvmat2EDLdiVRWT+/bAAAAFUlEQVR4XmP8z4ADfGRCF0GAESgFADQBAgbQMNZvAAAAAElFTkSuQmCC>

[image15]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAALBAMAAACqiTGYAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3NZkSrVBDvuzKZdiJR1kIBAAAAD0lEQVR4XmP8z8DEgA8BACFsARUFeiXrAAAAAElFTkSuQmCC>

[image16]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA8AAAALBAMAAABSacpvAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARM27Imbvmat2EDLdiVRWT+/bAAAAFElEQVR4XmP8zwAGH5kgNAMD1RkAu+0CBiZoofUAAAAASUVORK5CYII=>

[image17]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAsAAAALBAMAAABbgmoVAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3vzburmXZEVBBmMiJm6649AAAAFElEQVR4XmP8zwAEH5lAJAMDhRQAkicCBm41e8MAAAAASUVORK5CYII=>

[image18]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAwAAAANBAMAAABvB5JxAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARInN71QQdpkiZrur3TKR0mKdAAAAEUlEQVR4XmP8zwACTGCSuhQAXqABGU9CYrwAAAAASUVORK5CYII=>

[image19]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAsAAAALBAMAAABbgmoVAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZkS7MhCJze/dInb7CH9wAAAAFElEQVR4XmP8zwAEH5lAJAMDhRQAkicCBm41e8MAAAAASUVORK5CYII=>

[image20]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAwAAAALBAMAAAC5XnFsAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAu+/dzasQIolURJkyZnZtUmbLAAAAEUlEQVR4XmP8zwACTGCSUgoAT1ABFRIG5iYAAAAASUVORK5CYII=>

[image21]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAcAAAALBAMAAABBvoqbAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAECIyVHaJq7vN70SZ3WY3t8peAAAAEklEQVR4XmP8z8DwkYkBCEgiAGhhAgZfdY9rAAAAAElFTkSuQmCC>

[image22]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAATBAMAAADc9GjbAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAGUlEQVR4XmP8z4AHfGRCF0EFo9JYwUiVBgCGMwIWCgbo0QAAAABJRU5ErkJggg==>

[image23]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAwAAAALBAMAAAC5XnFsAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAImarzd2JRO8yuxCZdlRn2aP5AAAAEUlEQVR4XmP8zwACTGCSUgoAT1ABFRIG5iYAAAAASUVORK5CYII=>

[image24]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAALBAMAAACEzBAKAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMt3vzburZhCJIplEVHbghmtJAAAAEUlEQVR4XmP8zwABTFCa+gwAZkIBFRa1sv4AAAAASUVORK5CYII=>

[image25]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAOAAAAAQBAMAAAD9kW7LAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMt3vzburZhCJIplEVHbghmtJAAAAJklEQVR4XmP8z0BfwIQuQGswaiHVwaiFVAejFlIdjFpIdTD8LQQAgGoBH1U+uBgAAAAASUVORK5CYII=>

[image26]: <data:image/png;base64,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>

[image27]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAANBAMAAABBQrPjAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAEklEQVR4XmP8z4AATEjswcYBALCWARn3DEZ1AAAAAElFTkSuQmCC>

[image28]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHsAAAAQBAMAAADE2h3VAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAHUlEQVR4XmP8z0AB+MiELkIaGNVONhjVTjYYydoBqPwCEMFFyCgAAAAASUVORK5CYII=>

[image29]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJMAAAAMBAMAAACU+5KwAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAHklEQVR4XmP8z0Al8JEJXYR8MGoU8WDUKOLB4DQKALclAgj0+E1PAAAAAElFTkSuQmCC>

[image30]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMgAAAAkBAMAAAA+8GhUAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAANUlEQVR4Xu3NIQIAEADAQPz/z3RtgXQXVzb3eG/d4QWTxCQxSUwSk8QkMUlMEpPEJDFJvkwOM0EBR36MV4gAAAAASUVORK5CYII=>

[image31]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAHBAMAAADHdxFtAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIpnN71Qydqu73RCJRGbyacg8AAAAEUlEQVR4XmP8z8DAwMSAjwAAIvYBDazScAcAAAAASUVORK5CYII=>

[image32]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAARBAMAAABUTlNBAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAAF0lEQVR4XmP8z4ANMKELQMCoMCagijAAoAcBIVwRQAIAAAAASUVORK5CYII=>

[image33]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEMAAAATBAMAAAA5aq23AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAELvvZjKJIqvNRJlU3Xb9qPr1AAAAHElEQVR4XmP8z0AAfGRCF8EEo0qwg1El2AERSgAbrgIWXTkOqQAAAABJRU5ErkJggg==>

[image34]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACsAAAAPCAMAAABKvsbSAAADAFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALI7fhAAAAEnRSTlMAZt3vu6uZdjIQzUSJIlTR+ckMoWBsAAAAiUlEQVR4Xp1SywqAMAyrIug8CPb/v3EHFUT04NZpH2MqmMuWJm3DGMATsEzpzLQMGOSa2WbNWauH5MUjstZoXRQF1ElzJ+JG7TVRWmhy72mBTFfe1b+lbfhIdY86g0078O2aoUcVlmAlb4a7iM6OTWxkL8IsopMr3EsWU/uFz/cT2AQK8h8Yj94TMn0SXa1LY+MAAAAASUVORK5CYII=>

[image35]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAC8AAAARBAMAAAC/eehCAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAAGElEQVR4XmP8z4AVfGRCF4GBUQkMQA8JAD3oAhJlptfEAAAAAElFTkSuQmCC>

[image36]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQAAAAIBAMAAAALs8LeAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARJm73e/NVDJ2qxCJZiLOuiuHAAAAEUlEQVR4XmP8zwADTHAWFZgAWcEBDw36L/YAAAAASUVORK5CYII=>

[image37]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAARBAMAAABUTlNBAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAAF0lEQVR4XmP8z4ANMKELQMCoMCagijAAoAcBIVwRQAIAAAAASUVORK5CYII=>

[image38]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADQAAAAMBAMAAADff4MYAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARJm73e/NVDJ2qxCJZiLOuiuHAAAAFUlEQVR4XmP8z4ALMKELIMCoFDIAAFJAARcdQ4qvAAAAAElFTkSuQmCC>

[image39]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQAAAAIBAMAAAALs8LeAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARJm73e/NVDJ2qxCJZiLOuiuHAAAAEUlEQVR4XmP8zwADTHAWFZgAWcEBDw36L/YAAAAASUVORK5CYII=>

[image40]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAE8AAAAQBAMAAAClwj+XAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIrvvMmZ2mc3dEESriVRKaaqQAAAAGklEQVR4XmP8z0AU+MiELoILjCrEC0YV4gUA/CUCEEwcs50AAAAASUVORK5CYII=>

[image41]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAANAAAAAQBAMAAACVYuzzAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAJUlEQVR4XmP8z0AfwIQuQCswahHZYNQissGoRWSDUYvIBsPPIgD4WwEfx2fXxgAAAABJRU5ErkJggg==>

[image42]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAANEAAAAQBAMAAAB6oIfNAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAJklEQVR4XmP8z0Af8JEJXYRmYNQmSsCoTZSAUZsoAaM2UQKGo00A43wCECdcoNMAAAAASUVORK5CYII=>

[image43]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAOAAAAAUBAMAAABmACzdAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAAKklEQVR4Xu3NIQIAAATAQPz/z3R5pF1cWXb8qh2uOcQ5xDnEOcQ5xDnEDYQYASePlwXvAAAAAElFTkSuQmCC>

[image44]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAOEAAAAUBAMAAACJwkfjAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIkSZ7zK7VIkQ3c1mq3YXrLxZAAAALElEQVR4XmP8z0Bf8JEJXYTmYNRGWoBRG2kBRm2kBRi1kRZg1EZagFEbaQEAkicCGCSrdfYAAAAASUVORK5CYII=>

[image45]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMwAAAAUBAMAAAAzYc+DAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlR2q7vN3e+JEESZMmZ6kRRHAAAAK0lEQVR4XmP8z0APwIQuQBswag0ZYNQaMsCoNWSAUWvIAKPWkAFGrSEDAACsaQEntg6RIQAAAABJRU5ErkJggg==>

[image46]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADgAAAANBAMAAAAOH7AzAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARJm73e/NVDJ2qxCJZiLOuiuHAAAAFklEQVR4XmP8z4AbMKELIINRSQZKJAGLNQEZqhDuqwAAAABJRU5ErkJggg==>

[image47]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAMBAMAAADxOqKKAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlR2q7vN3e+JEESZMmZ6kRRHAAAAEklEQVR4XmP8z4AKmND4Q0gAANSRARfVNXU2AAAAAElFTkSuQmCC>

[image48]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAKAAAAAUBAMAAAD4uit9AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlR2q7vN3e+JEESZMmZ6kRRHAAAAJUlEQVR4XmP8z0BdwIQuQCkYNZByMGog5WDUQMrBqIGUg8FvIADR6wEnywdfOgAAAABJRU5ErkJggg==>

[image49]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADQAAAAMBAMAAADff4MYAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAFUlEQVR4XmP8z4ALMKELIMCoFDIAAFJAARcdQ4qvAAAAAElFTkSuQmCC>

[image50]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFcAAAAQBAMAAACRu/6LAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3vzburmXZEVBBmMiJm6649AAAAHUlEQVR4XmP8z0A0+MiELoIPjCpGBqOKkQHtFAMAeLACEMgWE8MAAAAASUVORK5CYII=>

[image51]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADEAAAAQBAMAAABNQoq8AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlSJu93vzZlEdhCrMma5B41NAAAAF0lEQVR4XmP8z4AdfGRCF4GDURn6yQAAKUUCEAPkF3gAAAAASUVORK5CYII=>

[image52]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAMBAMAAADxOqKKAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3vzburmXZEVBBmMiJm6649AAAAEklEQVR4XmP8z4AKmND4Q0gAANSRARfVNXU2AAAAAElFTkSuQmCC>

[image53]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA0AAAAMBAMAAABLmSrqAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAFElEQVR4XmP8zwACH5nAFAMDtWkAt0oCCKZhFucAAAAASUVORK5CYII=>

[image54]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAMBAMAAADxOqKKAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3vzburmXZEVBBmMiJm6649AAAAEklEQVR4XmP8z4AKmND4Q0gAANSRARfVNXU2AAAAAElFTkSuQmCC>

[image55]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAPwAAAAhBAMAAAAPJdulAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlSJu93vzZlEdhCrMma5B41NAAAANElEQVR4Xu3NoQEAIAzAMOD/n4fnAGIaWdM9Szpv+Ks91B5qD7WH2kPtofZQe6g91B7C+wt7TgFBjwR56QAAAABJRU5ErkJggg==>

[image56]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAOQAAAAQBAMAAAD0es6xAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAJklEQVR4XmP8z0BvwIQuQHswaiWNwKiVNAKjVtIIjFpJIzAyrAQAomoBH4UkeDQAAAAASUVORK5CYII=>

[image57]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA8AAAALBAMAAABSacpvAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAiasy791mELtUmc1EInbPtqVlAAAAFElEQVR4XmP8zwAGH5kgNAMD1RkAu+0CBiZoofUAAAAASUVORK5CYII=>

[image58]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAsAAAALBAMAAABbgmoVAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAMlSJq83d7xCZdiJmu0SiI45yAAAAFElEQVR4XmP8zwAEH5lAJAMDhRQAkicCBm41e8MAAAAASUVORK5CYII=>

[image59]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAI0AAAAQBAMAAADZiOELAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARInN71QQdpkiZrur3TKR0mKdAAAAIklEQVR4XmP8z0AN8JEJXYRMMGoOfjBqDn4wag5+MNjMAQDBIgIQ14TfewAAAABJRU5ErkJggg==>

[image60]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAB0AAAAMBAMAAABsN6sCAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAEM3v3burMomZRCJ2VGaYzhOJAAAAFUlEQVR4XmP8z4AMPjKhcBkYBjsfAG6xAggUORl2AAAAAElFTkSuQmCC>

[image61]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAQBAMAAACFLmBqAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARFTd75mrZrt2EInNIjJ0PhryAAAAEklEQVR4XmP8z4AKmND4I0wAACAQAR8WuYnrAAAAAElFTkSuQmCC>

[image62]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA0AAAAMBAMAAABLmSrqAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAVJmrZrt2RBCJze/dIjJISVHlAAAAFElEQVR4XmP8zwACH5nAFAMDtWkAt0oCCKZhFucAAAAASUVORK5CYII=>

[image63]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAN8AAAAQBAMAAABkabd+AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMARFTd75mrZrt2EInNIjJ0PhryAAAAJ0lEQVR4XmP8z0BX8JEJXYTWYNRCqoNRC6kORi2kOhi1kOpg+FsIAL1kAhDyw/2jAAAAAElFTkSuQmCC>

[image64]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACEAAAAMBAMAAAAe+Mm0AAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIpnvzURmVN0QiXYyu6v7l9iSAAAAFUlEQVR4XmP8z4AKPjKhCTAwDEURAJyHAggi2TiTAAAAAElFTkSuQmCC>

[image65]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAALBAMAAADLkRPaAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAIlSZu93vzWZEiRCrMnbw+v1pAAAAFElEQVR4XmP8z4AdMKELwMCwlwAAHeEBFZuj2O8AAAAASUVORK5CYII=>

[image66]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAANkAAAAnBAMAAABwCPAsAAAAMFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAv3aB7AAAAD3RSTlMAid3vzatmMkS7dhBUmSL1zwcfAAAAO0lEQVR4Xu3NoQHAIADAMOD/Z3fB8OiCSmRN5z/e+dZZrnKruFXcKm4Vt4pbxa3iVnGruFXcKm6Vt7cNsJICPqqraUgAAAAASUVORK5CYII=>