#!/usr/bin/env bash

set -euo pipefail

# Kubernetes-based social network benchmark, mirroring the Docker Swarm experiment.

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

WORKER_NODES=(
  "c220g5-111219.wisc.cloudlab.us"  # node1
  "c220g5-111226.wisc.cloudlab.us"  # node2
  "c220g5-111205.wisc.cloudlab.us"  # node3
  "c220g5-111228.wisc.cloudlab.us"  # node4
)

WRK_THREADS=4
WRK_CONNS=64
WRK_DURATION="30s"
WRK_RPS=1000
RUNS_PER_WORKLOAD=3

INIT_GRAPH="socfb-Reed98"

VERBOSE=false

OUTPUT_JSON="$(dirname "$0")/k8s-default-snet-results.json"

MAX_RUN_RETRIES=4
RETRY_BACKOFF_SEC=5

log() { echo "[k8s-snet-experiment] $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }; }

ensure_local_prereqs() {
  need_cmd kubectl
  need_cmd python3
}

wait_for_frontend_ready() {
  log "Waiting for frontend service to be Ready (Kubernetes)"

  for _ in $(seq 1 60); do
    if kubectl get pods -l "service=nginx-web-server" -o jsonpath='{.items[*].status.containerStatuses[*].ready}' 2>/dev/null | grep -q true; then
      log "Frontend pod is Ready"
      return 0
    fi
    sleep 5
  done

  log "Frontend never became Ready."
  kubectl get pods -o wide || true
  kubectl logs -l "service=nginx-web-server" --tail=100 || true
  return 1
}

init_social_graph() {
  log "Initializing social graph (${INIT_GRAPH})"
  (
    cd "$(cd "$(dirname "$0")/../../.." && pwd)/socialNetwork" && \
    python3 -m pip install -q aiohttp asyncio || true && \
    python3 scripts/init_social_graph.py --graph="${INIT_GRAPH}" --limit=64 || true
  )
}

build_wrk2() {
  log "Building wrk2 locally"
  ( cd "$(cd "$(dirname "$0")/../../.." && pwd)/wrk2" && make -j || make )
}

run_wrk2_and_parse() {
  local url="$1" script_path="$2" label="$3"
  local results_json="[]"

  log "Warming up ${label} endpoint"
  (
    cd "$(dirname "$0")/runs" && \
    tmpdir=$(mktemp -d "warmup-${label}-XXXX") && \
    (
      cd "$tmpdir" && \
      "$(cd "$(dirname "$0")/../../.." && pwd)/wrk2/wrk" \
        -D exp -t ${WRK_THREADS} -c ${WRK_CONNS} -d 10s -L \
        -s "${script_path}" "${url}" -R $(( WRK_RPS>100 ? 100 : WRK_RPS )) \
        > /dev/null 2>&1 || true
    ) && rm -rf "$tmpdir"
  )

  for ((i=1; i<=RUNS_PER_WORKLOAD; i++)); do
    log "Running ${label} (run $i/${RUNS_PER_WORKLOAD})"

    local RUN_BASE_DIR rundir
    RUN_BASE_DIR="$(dirname "$0")/runs"
    mkdir -p "$RUN_BASE_DIR"
    rundir=$(mktemp -d "$RUN_BASE_DIR/e2e-${label}-${i}-XXXX")

    local attempt=0 out ok=false
    while (( attempt <= MAX_RUN_RETRIES )); do
      ( cd "$rundir" && "$(cd "$(dirname "$0")/../../.." && pwd)/wrk2/wrk" \
        -D exp -t ${WRK_THREADS} -c ${WRK_CONNS} -d ${WRK_DURATION} -L -P \
        -s "${script_path}" "${url}" -R ${WRK_RPS} | tee output.txt ) >/dev/null
      out=$(cat "$rundir/output.txt")

      local bad_line
      bad_line=$(printf "%s\n" "$out" | awk -F: '/Non-2xx or 3xx responses/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')

      shopt -s nullglob
      local thread_files=("$rundir"/[0-9]*.txt)
      local thread_count=${#thread_files[@]}
      local have_numbers
      have_numbers=$(awk 'BEGIN{n=0} /^[0-9]+$/ {n=1} END{print n}' "${thread_files[@]:-}" 2>/dev/null || echo 0)
      shopt -u nullglob

      if [[ -z "$bad_line" || "$bad_line" == "0" ]]; then
        if (( thread_count >= WRK_THREADS )) && [[ "$have_numbers" == "1" ]]; then
          ok=true; break
        fi
      fi

      attempt=$((attempt+1))
      log "Run unhealthy (Non-2xx=${bad_line:-none}, threads=${thread_count}, numbers=${have_numbers}). Retrying in ${RETRY_BACKOFF_SEC}s..."
      sleep "${RETRY_BACKOFF_SEC}"
    done
    if [[ "$ok" != true ]]; then
      log "Run $i for ${label} remained unhealthy after retries; keeping latest output for visibility."
    fi

    local p50 p90 p99 p999 rps ts
    p50=$(printf "%s\n" "$out" | awk '/^\s*50\.000%/ {print $2}')
    p90=$(printf "%s\n" "$out" | awk '/^\s*90\.000%/ {print $2}')
    p99=$(printf "%s\n" "$out" | awk '/^\s*99\.000%/ {print $2}')
    p999=$(printf "%s\n" "$out" | awk '/^\s*99\.900%/ {print $2}')
    rps=$(printf "%s\n" "$out" | awk -F: '/Requests\/sec/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    p50=${p50:-"na"}; p90=${p90:-"na"}; p99=${p99:-"na"}; p999=${p999:-"na"}; rps=${rps:-"na"}

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

    if [ "$results_json" = "[]" ]; then
      results_json="[${run_json}]"
    else
      results_json="${results_json%]} , ${run_json}]"
    fi
  done
  echo "$results_json"
}

build_placements_json() {
  local assign_tmp map_tmp
  assign_tmp=$(mktemp)
  map_tmp=$(mktemp)

  # Collect node|service for running Pods in the social network namespace (default namespace assumed)
  kubectl get pods -o jsonpath='{range .items[*]}{.spec.nodeName}{"|"}{.metadata.labels.service}{"\n"}{end}' \
    | awk 'NF==2 && $2!="": {print}' > "$assign_tmp" || true

  # Build friendly name map: node0 = control-plane, node1.. workers sorted
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels."node-role\.kubernetes\.io/control-plane"}{"\n"}{end}' \
    | awk '$2!="" {print $1}' | head -n1 | awk '{print $1"|node0"}' > "$map_tmp"

  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels."node-role\.kubernetes\.io/control-plane"}{"\n"}{end}' \
    | awk '$2=="" {print $1}' | sort \
    | nl -v 1 -w 1 -s ' ' \
    | awk '{printf "%s|node%d\n", $2, $1}' >> "$map_tmp"

  local json first=true
  json="{"
  while IFS='|' read -r host friendly; do
    local arr
    arr=$(awk -F'|' -v n="$host" '$1==n {print $2}' "$assign_tmp" | sort -u \
      | awk 'BEGIN{printf "["} {if(NR>1) printf ","; printf "\"%s\"", $0} END{printf "]"}')
    if [[ -z "$arr" || "$arr" = "[]" ]]; then
      arr="[]"
    fi
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

main() {
  ensure_local_prereqs
  if [ "${VERBOSE}" = "true" ]; then set -x; fi

  local RUNS_ROOT
  RUNS_ROOT="$(dirname "$0")/runs"
  mkdir -p "$RUNS_ROOT"

  wait_for_frontend_ready
  init_social_graph
  build_wrk2

  local BASE_URL="http://localhost:8080"
  local SCRIPT_BASE
  SCRIPT_BASE="$(cd \"$(dirname \"$0\")/../../..\" && pwd)/wrk2/scripts/social-network"

  local compose_json home_json user_json mixed_json
  compose_json=$(run_wrk2_and_parse \"${BASE_URL}/wrk2-api/post/compose\" \"${SCRIPT_BASE}/compose-post.lua\" \"compose-post\")
  home_json=$(run_wrk2_and_parse    \"${BASE_URL}/wrk2-api/home-timeline/read\" \"${SCRIPT_BASE}/read-home-timeline.lua\" \"read-home-timelines\")
  user_json=$(run_wrk2_and_parse    \"${BASE_URL}/wrk2-api/user-timeline/read\" \"${SCRIPT_BASE}/read-user-timeline.lua\" \"read-user-timelines\")
  mixed_json=$(run_wrk2_and_parse   \"${BASE_URL}/wrk2-api/mixed-workload\" \"${SCRIPT_BASE}/mixed-workload.lua\" \"mixed-workload\")

  local placements_json
  placements_json=$(build_placements_json)
  write_results_json "$placements_json" "$compose_json" "$home_json" "$user_json" "$mixed_json"
  python3 -m json.tool "${OUTPUT_JSON}" > "${OUTPUT_JSON}.tmp" && mv "${OUTPUT_JSON}.tmp" "${OUTPUT_JSON}"

  log "Done. Inspect Kubernetes resources with: kubectl get pods,svc"
}

main "$@"

