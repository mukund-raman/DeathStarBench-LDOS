#!/bin/bash

# Given a directory of results, visualize the results.
# Usage: visualize-results.sh <results-directory>
# Example: visualize-results.sh results/rand-search
# Example: visualize-results.sh results/bayes-search

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <results-directory>"
    exit 1
fi

RESULTS_DIR=$1
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

pip3 install matplotlib numpy

echo "Visualizing results in $RESULTS_DIR..."
python3 "$DIR/visualize_results.py" "$RESULTS_DIR"
echo "Visual stored in $RESULTS_DIR-dist-graph.png"