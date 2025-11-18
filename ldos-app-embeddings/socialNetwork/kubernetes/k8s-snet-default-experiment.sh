#!/usr/bin/env bash

set -euo pipefail # Exit early on errors

# Kubernetes-based social network benchmark

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
  "c220g5-111219.wisc.cloudlab.us"  # node1
  "c220g5-111226.wisc.cloudlab.us"  # node2
  "c220g5-111205.wisc.cloudlab.us"  # node3
  "c220g5-111228.wisc.cloudlab.us"  # node4
)

# Workload parameters
WRK_THREADS=4
WRK_CONNS=64
WRK_DURATION="30s"
WRK_RPS=1000
RUNS_PER_WORKLOAD=3

# Graph to initialize (see README) — optional but recommended
INIT_GRAPH="socfb-Reed98"

# If true, remove existing run directories at startup
CLEAN_RUN_DIRS_ON_START=true

VERBOSE=false # set true to enable bash -x and verbose SSH

# Local output JSON file (written in this directory)
OUTPUT_JSON="$(dirname "$0")/k8s-default-snet-results.json"

# Retries/backoff for unhealthy runs
MAX_RUN_RETRIES=4
RETRY_BACKOFF_SEC=5

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
}

# Wait for the frontend of socialnet to be ready (~1 min timeout)
wait_for_frontend_ready() {
  log "Waiting for frontend service to be Ready (Kubernetes)"

  for _ in $(seq 1 12); do
    if kubectl get pods -l "service=nginx-thrift" -o jsonpath='{.items[*].status.containerStatuses[*].ready}' 2>/dev/null | grep -q true; then
      log "Frontend pod is Ready; checking HTTP endpoint on NodePort 32000"
      
      # Verify that the HTTP endpoint is responding (similar to Swarm script)
      for _ in $(seq 1 20); do
        code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 \
          -X POST http://127.0.0.1:32000/wrk2-api/user/register \
          -d "first_name=probe&last_name=probe&username=probe_$RANDOM&password=x&user_id=0" || true)
        if [[ "$code" == "200" ]]; then
          log "Registration endpoint ready (HTTP 200)"
          return 0
        fi
        sleep 3
      done
      log "Frontend pod is Ready but HTTP endpoint did not return 200 within timeout"
      return 1
    fi
    sleep 5
  done

  log "Frontend never became Ready."
  kubectl get pods -o wide || true
  kubectl logs -l "service=nginx-thrift" --tail=100 || true
  return 1
}

# Initialize social graph on control node
init_social_graph() {
  log "Initializing social graph (${INIT_GRAPH})"
  (
    cd "${SOCIAL_DIR}" && \
    python3 -m pip install -q aiohttp asyncio || true && \
    python3 scripts/init_social_graph.py --graph="${INIT_GRAPH}" --limit=64 || true
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
        -D exp -t ${WRK_THREADS} -c ${WRK_CONNS} -d 10s -L \
        -s "${script_path}" "${url}" -R $(( WRK_RPS>100 ? 100 : WRK_RPS )) \
        > /dev/null 2>&1 || true
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

      # 3) If everything is okay, then exit out; this run was healthy
      if [[ -z "$bad_line" || "$bad_line" == "0" ]]; then
        if (( thread_count >= WRK_THREADS )) && [[ "$have_numbers" == "1" ]]; then
          ok=true; break
        fi
      fi

      # 4) Otherwise, move on to next attempt
      attempt=$((attempt+1))
      log "Run unhealthy (Non-2xx=${bad_line:-none}, threads=${thread_count}, numbers=${have_numbers}). Retrying in ${RETRY_BACKOFF_SEC}s..."
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
  local assign_tmp map_tmp
  assign_tmp=$(mktemp)
  map_tmp=$(mktemp)

  # Collect node|service for running Pods in the social network namespace
  # (default namespace assumed)
  kubectl get pods -o jsonpath='{range .items[*]}{.spec.nodeName}{"|"}{.metadata.labels.service}{"\n"}{end}' \
    | awk 'NF==2 && $2!="": {print}' > "$assign_tmp" || true

  # Build friendly name map: node0 = control-plane, node1.. workers sorted
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels."node-role\.kubernetes\.io/control-plane"}{"\n"}{end}' \
    | awk '$2!="" {print $1}' | head -n1 | awk '{print $1"|node0"}' > "$map_tmp"
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels."node-role\.kubernetes\.io/control-plane"}{"\n"}{end}' \
    | awk '$2=="" {print $1}' | sort \
    | nl -v 1 -w 1 -s ' ' \
    | awk '{printf "%s|node%d\n", $2, $1}' >> "$map_tmp"

  # Build JSON grouped by friendly node name, include empty arrays for
  # nodes with no tasks
  local json first=true
  json="{"
  while IFS='|' read -r host friendly; do
    # Services for this host
    local arr
    arr=$(awk -F'|' -v n="$host" '$1==n {print $2}' "$assign_tmp" | sort -u \
      | awk 'BEGIN{printf "["} {if(NR>1) printf ","; printf "\"%s\"", $0} END{printf "]"}')
    if [[ -z "$arr" || "$arr" = "[]" ]]; then
      arr="[]"
    fi
    
    # If no services found, produce an empty array
    if [ "$first" = true ]; then
      json+=$(printf '"%s": %s' "$friendly" "$arr")
      first=false
    else
      json+=$(printf ', "%s": %s' "$friendly" "$arr")
    fi
  done < "$map_tmp"
  json+="}"
  rm -f "$assign_tmp" "$map_tmp"
  printf '%s' "$json"
}

# Write combined results JSON to file (placements first)
write_results_json() {
  local placements_json="$1" compose_json="$2" home_json="$3" user_json="$4" mixed_json="$5"
  cat > "$OUTPUT_JSON" <<-EOF
    {
      "placements": ${placements_json},
      "compose-post": ${compose_json},
      "read-home-timelines": ${home_json},
      "read-user-timelines": ${user_json},
      "mixed-workload": ${mixed_json}
    }
EOF
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

  # Start the SSH agent
  eval "$(ssh-agent -s)"
  ssh-add "$SSH_KEY"

  # Wait for services to be ready, init social graph, and build wrk2 scripts
  wait_for_frontend_ready
  init_social_graph
  build_wrk2

  # Define variables for connections/running workload generation scripts
  # nginx-thrift Service exposes port 8080 on NodePort 32000
  local BASE_URL="http://localhost:32000"
  local SCRIPT_BASE
  SCRIPT_BASE="${SNET_WRK2_DIR}/scripts/social-network"

  # Run workloads and gather results
  local compose_json home_json user_json mixed_json
  compose_json=$(run_wrk2_and_parse "${BASE_URL}/wrk2-api/post/compose" \
                 "${SCRIPT_BASE}/compose-post.lua" "compose-post")
  home_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/home-timeline/read" \
              "${SCRIPT_BASE}/read-home-timeline.lua" "read-home-timelines")
  user_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/user-timeline/read" \
              "${SCRIPT_BASE}/read-user-timeline.lua" "read-user-timelines")
  mixed_json=$(run_wrk2_and_parse   "${BASE_URL}/wrk2-api/mixed-workload" \
               "${SCRIPT_BASE}/mixed-workload.lua" "mixed-workload")

  # Save combined JSON locally with placements first
  local placements_json
  placements_json=$(build_placements_json)
  write_results_json "$placements_json" "$compose_json" "$home_json" "$user_json" "$mixed_json"
  python3 -m json.tool "${OUTPUT_JSON}" > "${OUTPUT_JSON}.tmp" && mv "${OUTPUT_JSON}.tmp" "${OUTPUT_JSON}"

  log "Done. Inspect Kubernetes resources with: kubectl get pods,svc"
}

main "$@"
