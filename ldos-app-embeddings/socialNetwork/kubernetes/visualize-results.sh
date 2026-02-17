#!/bin/bash

# Given a directory of results, visualize the results.
# Usage: visualize-results.sh <results-directory> [options]
# Example: visualize-results.sh results/rand-search
# Example: visualize-results.sh results/bayes-search --aggregate-folders --run 2
#
# Options forwarded to visualize_results.py:
#   --aggregate-folders  Aggregate results from results-config-* folders
#   --run N              Specific run number to process (1, 2, ...)
#   --config CONFIG_ID   Specific configuration ID to display
#   --differences V1 V2  Calculate stats on latency differences between two versions
#   --exclude-outliers   Exclude configs with avg P99 latency > threshold (500ms) (for --differences)

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <results-directory> [options]"
    exit 1
fi

RESULTS_DIR=$1
shift # Shift arguments so $@ contains the rest of the options

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Install dependencies
pip3 install matplotlib numpy > /dev/null

echo "Visualizing results in $RESULTS_DIR..."
python3 "$DIR/visualize_results.py" "$RESULTS_DIR" "$@"