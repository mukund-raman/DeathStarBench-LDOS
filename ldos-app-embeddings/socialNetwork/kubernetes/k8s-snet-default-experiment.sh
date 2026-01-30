#!/usr/bin/env bash

set -euo pipefail # Exit early on errors

# =========================
# Editable constants
# =========================

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

# Path variables
ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
SOCIAL_DIR="${ROOT_DIR}/socialNetwork"
MAIN_WRK2_DIR="${ROOT_DIR}/wrk2"
SNET_WRK2_DIR="${SOCIAL_DIR}/wrk2"

# Remote worker nodes (control node is the current local machine)
WORKER_NODES=(
  "clnode218.clemson.cloudlab.us"  # node1
  "clnode198.clemson.cloudlab.us"  # node2
  "clnode216.clemson.cloudlab.us"  # node3
  "clnode199.clemson.cloudlab.us"  # node4
  "clnode215.clemson.cloudlab.us"  # node5
)

# Workload parameters (tuned for stability on CloudLab cluster)
WRK_THREADS=4
WRK_CONNS=64
WRK_DURATION="30s"
WRK_RPS=100
RUNS_PER_WORKLOAD=3

# Warm-up parameters (lower RPS, longer duration to stabilize services)
WARMUP_DURATION="30s"
WARMUP_RPS=200

INIT_GRAPH="socfb-Reed98" # Graph to initialize
CLEAN_RUN_DIRS_ON_START=true # set true to remove existing run directories
VERBOSE=false # set true to enable bash -x and verbose SSH
OUTPUT_JSON="$(dirname "$0")/results/k8s-default-snet-results.json"

# Retries/backoff for unhealthy runs
MAX_RUN_RETRIES=4
RETRY_BACKOFF_SEC=5

# Get the node IP for NodePort access (NodePorts bind to node IPs)
NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' | head -n 1)"
if [ -z "$NODE_IP" ]; then NODE_IP="127.0.0.1"; fi

# =========================
# Internal helpers
# =========================

# Log every bash command run for debugging purposes
log() { echo "[k8s-snet-experiment] $*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2; exit 1;
  };
}

# Ensure that prereqs are met for installation
ensure_local_prereqs() {
  need_cmd kubectl
  need_cmd python3
  need_cmd curl
}

# Wait for the frontend of socialnet to be ready (~1 min timeout)
wait_for_frontend_ready() {
  log "Waiting for frontend service to be Ready (Kubernetes)"

  for _ in $(seq 1 60); do
    if kubectl get pods -l "service=nginx-thrift" -o jsonpath='{.items[*].status.containerStatuses[*].ready}' 2>/dev/null | grep -q true; then
      log "Frontend pod is Ready; checking HTTP endpoint on NodePort 32000"
      
      # Verify that the HTTP endpoint is responding (similar to Swarm script).
      local code="000"
      for _ in $(seq 1 60); do
        # Use a high random ID to avoid conflict with initial social graph users (ids 0+)
        local probe_id=$((RANDOM + 100000))
        code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 \
          -X POST "http://${NODE_IP}:32000/wrk2-api/user/register" \
          -d "first_name=probe&last_name=probe&username=probe_$RANDOM&password=x&user_id=$probe_id" || echo "000")
        if [[ "$code" == "200" ]]; then
          log "Registration endpoint ready (HTTP 200)"
          return 0
        fi
        sleep 3
      done
      log "Frontend pod is Ready but HTTP endpoint did not return 200 within timeout (last code: ${code}). Proceeding anyway."
      return 0
    fi
    sleep 5
  done

  log "Frontend never became Ready."
  kubectl get pods -o wide
  kubectl logs -l "service=nginx-thrift" --tail=100
  # Do not hard-fail here: allow the rest of the script to try, so that
  # temporary readiness issues do not permanently block experiments.
  return 0
}

# Initialize social graph on control node
init_social_graph() {
  log "Initializing social graph (${INIT_GRAPH})"

  # Activate venv if it exists
  if [ -f "${ROOT_DIR}/../.venv/bin/activate" ]; then
      source "${ROOT_DIR}/../.venv/bin/activate"
  elif [ -f "${ROOT_DIR}/.venv/bin/activate" ]; then
      source "${ROOT_DIR}/.venv/bin/activate"
  fi

  # Clean existing user data to prevent conflicts with previous runs
  log "Cleaning MongoDB user database..."
  local mongo_pod
  mongo_pod=$(kubectl get pod -l service=user-mongodb -o jsonpath="{.items[0].metadata.name}" 2>/dev/null || echo "")
  if [ -n "$mongo_pod" ]; then
      kubectl exec "$mongo_pod" -- mongo user --eval "db.dropDatabase()" || true
  fi

  # Initialize social graph
  (
    cd "${SOCIAL_DIR}" && \
    python3 -m pip install -q aiohttp asyncio && \
    python3 "${SOCIAL_DIR}/scripts/init_social_graph.py" --graph="${INIT_GRAPH}" --limit=16 --ip="${NODE_IP}" --port=32000
  )
}

# Build wrk2 on control node
build_wrk2() {
  log "Building wrk2 locally"
  ( cd "${MAIN_WRK2_DIR}" && make -j || make )
}

# Run wrk2 with given script and parse output into JSON
run_wrk2_and_parse() {
  local url="$1" script_path="$2" label="$3"
  local results_json="[]"

  # Lightweight warm-up to stabilize services and verify health.
  # Uses lower RPS, shorter duration, and ignores output.
  log "Warming up ${label} endpoint"
  (
    cd "$(dirname "$0")/runs" && \
    tmpdir=$(mktemp -d "warmup-${label}-XXXX") && \
    (
      cd "$tmpdir" && \
      "${MAIN_WRK2_DIR}/wrk" \
        -D exp -t ${WRK_THREADS} -c ${WRK_CONNS} -d "${WARMUP_DURATION}" -L \
        -s "${script_path}" "${url}" -R ${WARMUP_RPS} \
        > warmup-output.txt 2>&1
    ) && rm -rf "$tmpdir"
  )

  for ((i=1; i<=RUNS_PER_WORKLOAD; i++)); do
    log "Running ${label} (run $i/${RUNS_PER_WORKLOAD})"

    # Create isolated run dir for -P output files (one per thread)
    local RUN_BASE_DIR rundir
    RUN_BASE_DIR="$(dirname "$0")/runs"
    mkdir -p "$RUN_BASE_DIR"
    rundir=$(mktemp -d "$RUN_BASE_DIR/e2e-${label}-${i}-XXXX")

    # Execute wrk2 with retries if run is unhealthy (non-2xx code)
    local attempt=0 out ok=false
    while (( attempt <= MAX_RUN_RETRIES )); do
      # Run workload generation script
      ( cd "$rundir" && "${MAIN_WRK2_DIR}/wrk" \
        -D exp -t ${WRK_THREADS} -c ${WRK_CONNS} -d ${WRK_DURATION} -L -P \
        -s "${script_path}" "${url}" -R ${WRK_RPS} | tee output.txt ) >/dev/null
      out=$(cat "$rundir/output.txt")

      # 1) Check for Non-2xx line
      local bad_line
      bad_line=$(printf "%s\n" "$out" | awk -F: '/Non-2xx or 3xx responses/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')

      # 2) Ensure all per-thread files exist and contain numbers
      shopt -s nullglob
      local thread_files=("$rundir"/[0-9]*.txt)
      local thread_count=${#thread_files[@]}
      local have_numbers
      have_numbers=$(awk 'BEGIN{n=0} /^[0-9]+$/ {n=1} END{print n}' "${thread_files[@]:-}" 2>/dev/null || echo 0)
      shopt -u nullglob

      # 3) Check p50 latency
      local p50_str p50_val p50_unit p50_ms="0" latency_ok=true
      p50_str=$(printf "%s\n" "$out" | awk '/^\s*50\.000%/ {print $2}')

      # Parse p50 string (e.g. 12.34ms, 1.50s, 500us)
      if [[ "$p50_str" =~ ([0-9.]+)(ms|s|us) ]]; then
          p50_val="${BASH_REMATCH[1]}"
          p50_unit="${BASH_REMATCH[2]}"
      else
          p50_val="$p50_str"
          p50_unit="ms"
      fi

      case "$p50_unit" in
          "ms") p50_ms="$p50_val" ;;
          "s")  p50_ms=$(awk -v v="$p50_val" 'BEGIN {print v * 1000}') ;;
          "us") p50_ms=$(awk -v v="$p50_val" 'BEGIN {print v / 1000}') ;;
          *)    p50_ms="$p50_val" ;;
      esac

      # Check if > 100ms
      if (( $(awk -v v="$p50_ms" 'BEGIN {print (v > 100) ? 1 : 0}') )); then
          latency_ok=false
      fi

      # 4) If everything is okay, then exit out; this run was healthy
      if [[ -z "$bad_line" || "$bad_line" == "0" ]]; then
        if (( thread_count >= WRK_THREADS )) && [[ "$have_numbers" == "1" ]]; then
          if [[ "$latency_ok" == "true" ]]; then
            ok=true; break
          else
             log "High p50 latency: ${p50_str} (${p50_ms}ms > 100ms)"
          fi
        fi
      fi

      # 5) Otherwise, move on to next attempt
      attempt=$((attempt+1))
      log "Run unhealthy (Non-2xx=${bad_line:-none}, threads=${thread_count}, numbers=${have_numbers}, p50=${p50_str}). Retrying in ${RETRY_BACKOFF_SEC}s..."
      sleep "${RETRY_BACKOFF_SEC}"
    done
    if [[ "$ok" != true ]]; then
      log "Run $i for ${label} remained unhealthy after retries; keeping latest output for visibility."
    fi

    # Parse percentiles from wrk2 output
    local p50 p90 p99 p999 rps ts
    p50=$(printf "%s\n" "$out" | awk '/^\s*50\.000%/ {print $2}')
    p90=$(printf "%s\n" "$out" | awk '/^\s*90\.000%/ {print $2}')
    p99=$(printf "%s\n" "$out" | awk '/^\s*99\.000%/ {print $2}')
    p999=$(printf "%s\n" "$out" | awk '/^\s*99\.900%/ {print $2}')
    rps=$(printf "%s\n" "$out" | awk -F: '/Requests\/sec/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Fallback if parsing failed
    p50=${p50:-"na"}; p90=${p90:-"na"}; p99=${p99:-"na"}; p999=${p999:-"na"}; rps=${rps:-"na"}

    # Collect per-request E2E latencies from all thread files (0.txt,1.txt,...)
    local e2e_median e2e_array
    shopt -s nullglob
    local thread_files=("$rundir"/[0-9]*.txt)
    if ((${#thread_files[@]})); then
      e2e_array=$(awk 'BEGIN{printf"["} /^[0-9]+$/ {if(n++) printf","; printf "%s",$1} END{printf"]"}' "${thread_files[@]}")
      e2e_median=$(awk '/^[0-9]+$/ {print $1}' "${thread_files[@]}" \
        | sort -n \
        | awk '{ a[NR]=$1 } END { \
            if (NR==0) { print "0"; exit } \
            if (NR%2==1) { printf "%.3f", a[(NR+1)/2]; } \
            else { printf "%.3f", (a[NR/2]+a[NR/2+1])/2; } \
          }')
    else
      e2e_array="[]"
      e2e_median=0
    fi
    shopt -u nullglob

    # Consolidate results as JSON entry
    local run_json
    run_json=$(cat <<-JSON
      {
          "timestamp": "${ts}",
          "threads": ${WRK_THREADS},
          "conns": ${WRK_CONNS},
          "duration": "${WRK_DURATION}",
          "rps_target": ${WRK_RPS},
          "rps_observed": "${rps}",
          "p50": "${p50}",
          "p90": "${p90}",
          "p99": "${p99}",
          "p999": "${p999}",
          "e2e_median": "${e2e_median}",
          "e2e_vals": ${e2e_array}
      }
JSON
    )

    # Append to results array
    if [ "$results_json" = "[]" ]; then
      results_json="[${run_json}]"
    else
      results_json="${results_json%]} , ${run_json}]"
    fi
  done
  echo "$results_json" # Return combined results JSON
}

# Build placements JSON: map node hostname -> [service, ...]
build_placements_json() {
  kubectl get pods -o json \
  | jq '
      # Extract {nodeIdx, podNamePrefix} and group by node index
      .items
      | map({
          nodeIdx: (.spec.nodeName | capture("node(?<n>[0-9]+)") | .n | tonumber),
          pod: (.metadata.name | sub("-[a-z0-9]+-[a-z0-9]+$"; ""))
        })
      | group_by(.nodeIdx)

      # Convert into {"nodeX": [pods...]}
      | map({
          ("node" + (.[0].nodeIdx|tostring)):
            (map(.pod) | sort | unique)
        })
      | add

      # Ensure node1..node5 always exist
      | . as $podsByNode
      | reduce range(1;6) as $i (
          {};
          . + {
            ("node" + ($i|tostring)):
              ($podsByNode["node\($i)"] // [])
          }
        )
    '
}

# Write node placements to file
write_placements_json() {
  local placements_json="$1"
  cat > "$OUTPUT_JSON" <<-EOF
    {
      "placements": $placements_json
    }
EOF
  log "Wrote placements to $OUTPUT_JSON"
}

# Write combined results to file, appending to existing JSON
write_results_json() {
  local compose_json="$1" home_json="$2" user_json="$3" mixed_json="$4"
  
  # Read current content of file if it exists
  local current_content="{}"
  if [ -f "$OUTPUT_JSON" ]; then
    current_content=$(cat "$OUTPUT_JSON")
  fi

  # Create JSON object with new data
  local new_data=$(cat <<-JSON
    {
      "compose-post": ${compose_json},
      "read-home-timelines": ${home_json},
      "read-user-timelines": ${user_json},
      "mixed-workload": ${mixed_json}
    }
JSON
  )

  # Append new data to file
  echo "$current_content" "$new_data" | jq -s '.[0] * .[1]' > "$OUTPUT_JSON"
  log "Wrote results to $OUTPUT_JSON"
}

# =========================
# Main Function
# =========================

main() {
  # Ensure that prereqs are met
  ensure_local_prereqs
  if [ "${VERBOSE}" = "true" ]; then set -x; fi

  # Clean previous run artifacts if specified
  local RUNS_ROOT
  RUNS_ROOT="$(dirname "$0")/runs"
  if [ "${CLEAN_RUN_DIRS_ON_START}" = "true" ] && [ -d "$RUNS_ROOT" ]; then
    log "Cleaning previous run directories under $RUNS_ROOT"
    rm -rf "$RUNS_ROOT"
  fi
  mkdir -p "$RUNS_ROOT"

  # Update output JSON location if provided as argument
  if [ -n "${1:-}" ]; then
    OUTPUT_JSON="${1}"
  fi

  # Build placements JSON and write to file
  local placements_json
  placements_json=$(build_placements_json)
  write_placements_json "$placements_json"

  # Start the SSH agent
  # eval "$(ssh-agent -s)"
  # ssh-add "$SSH_KEY"

  # Wait for services to be ready, init social graph, build wrk2 scripts
  wait_for_frontend_ready
  init_social_graph
  build_wrk2

  # Define variables for connections/running workload generation scripts
  # nginx-thrift Service exposes port 8080 on NodePort 32000
  log "Using node IP ${NODE_IP} for NodePort access"
  local BASE_URL="http://${NODE_IP}:32000"
  local SCRIPT_BASE
  SCRIPT_BASE="${SNET_WRK2_DIR}/scripts/social-network"

  # Run workloads and gather results
  local compose_json home_json user_json mixed_json
  # compose_json=$(run_wrk2_and_parse "${BASE_URL}/wrk2-api/post/compose" \
  #                "${SCRIPT_BASE}/compose-post.lua" "compose-post")
  # home_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/home-timeline/read" \
  #             "${SCRIPT_BASE}/read-home-timeline.lua" "read-home-timelines")
  # user_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/user-timeline/read" \
  #             "${SCRIPT_BASE}/read-user-timeline.lua" "read-user-timelines")
  mixed_json=$(run_wrk2_and_parse   "${BASE_URL}/wrk2-api/mixed-workload" \
               "${SCRIPT_BASE}/mixed-workload.lua" "mixed-workload")

  # Save combined JSON locally with placements first
  # write_results_json "$compose_json" "$home_json" "$user_json" "$mixed_json"
  write_results_json "{}" "{}" "{}" "$mixed_json"
  python3 -m json.tool "${OUTPUT_JSON}" > "${OUTPUT_JSON}.tmp" && mv "${OUTPUT_JSON}.tmp" "${OUTPUT_JSON}"
  log "Done. Inspect Kubernetes resources with: kubectl get pods,svc"
}

main "$@"
