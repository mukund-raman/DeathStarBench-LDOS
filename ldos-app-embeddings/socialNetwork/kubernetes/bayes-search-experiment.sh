#!/bin/bash

# TODO: Implement Bayesian optimization for placement vector selection.
# Notes:
# 1. The dimension of each placement vector is 27 > 20 (typically recommended).
# 2. The values for each dimension are in the range [0, 4], so the
#    feasible set/config space is a hyper-rectangle, allowing the
#    use of standard Bayesian optimization.
# 3. The domain is discrete, but we can still use standard Bayesian
#    optimization by using a discrete kernel.
# 4. The problem is derivative-free and we assume objective is continuous.
# 5. The objective function maps a 27-dim placement vector to a scalar value
#    representing the average median end-to-end latency across all actions.
# 6. The acquisition function is expected improvement.
# 7. No inherent pattern/structure in objective, so it is black-box.