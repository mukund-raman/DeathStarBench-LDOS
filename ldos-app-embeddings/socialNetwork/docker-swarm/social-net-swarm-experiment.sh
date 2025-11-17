#!/usr/bin/env bash

set -euo pipefail # Exit early on errors

# =========================
# Editable constants
# =========================

# SSH key and user for worker nodes
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="mkraman"

# Remote worker nodes (manager is the current local machine)
WORKER_NODES=(
  "c220g5-111219.wisc.cloudlab.us"  # node1
  "c220g5-111226.wisc.cloudlab.us"  # node2
  "c220g5-111205.wisc.cloudlab.us"  # node3
  "c220g5-111228.wisc.cloudlab.us"  # node4
)

# Path on remote manager where this project will be copied
REMOTE_APP_DIR="~/socialNetwork"

# Docker stack/compose service name
STACK_NAME="socialnet"

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

# Debug/verbosity options
VERBOSE=false              # set true to enable bash -x and verbose SSH
SSH_TIMEOUT_SEC=600        # kill remote command after 10 min

# Local output JSON file (written in this directory)
OUTPUT_JSON="$(dirname "$0")/social-net-swarm-results.json"

# Retries/backoff for unhealthy runs
MAX_RUN_RETRIES=4
RETRY_BACKOFF_SEC=5

# =========================
# Internal helpers
# =========================

log() { echo "[social-net-experiment] $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }; }

ensure_local_prereqs() {
  need_cmd sshpass
  need_cmd ssh
  need_cmd scp
  need_cmd rsync || true
}

_ssh_opts() {
  local extra=""
  if [ "${VERBOSE}" = "true" ]; then extra="-vvv"; fi
  
  # No TTY, force publickey, non-interactive (fail instead of hanging)
  printf '%s' "-T -i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes \
    -o PreferredAuthentications=publickey \
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=120 ${extra}"
}

ssh_cmd() {
  local host="$1"; shift
  log "SSH -> ${host}: $*"
  timeout -k 5 "${SSH_TIMEOUT_SEC}" \
    ssh $(_ssh_opts) "${SSH_USER}@${host}" "$@"
}

# Stream remote output to local terminal with host prefix
ssh_stream() {
  local host="$1"; shift
  log "SSH(stream) -> ${host}: $*"
  timeout -k 5 "${SSH_TIMEOUT_SEC}" \
    ssh $(_ssh_opts) "${SSH_USER}@${host}" "$@" 2>&1 | sed -u "s/^/[${host}] /"
}

scp_to() {
  local src="$1" dst_host="$2" dst_path="$3"
  scp -i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -r "$src" "${SSH_USER}@${dst_host}:$dst_path"
}

rsync_to() {
  local src="$1" dst_host="$2" dst_path="$3"
  if command -v rsync >/dev/null 2>&1; then
    local ssh_cmd_str="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=120"
    rsync -az --delete -e "${ssh_cmd_str}" "$src" "${SSH_USER}@${dst_host}:$dst_path"
  else
    scp_to "$src" "$dst_host" "$dst_path"
  fi
}

# Install Docker on remote host (fully automated via ssh + heredoc)
remote_install_docker() {
  local host="$1"
  log "Installing Docker on $host"

  # Send a shell script over SSH and execute it, streaming output
  { timeout -k 5 "${SSH_TIMEOUT_SEC}" \
      ssh $(_ssh_opts) "${SSH_USER}@${host}" "bash -s" 2>&1 <<'REMOTE_SCRIPT'
set -ex

# Function to run a command as sudo user
run_sudo() {
  if sudo -n true 2>/dev/null; then
    sudo "$@"
  elif [ -n "${SUDO_PASS:-}" ]; then
    echo "$SUDO_PASS" | sudo -S -k "$@"
  else
    echo "ERROR: sudo requires a password and SUDO_PASS is not set on remote host." >&2
    exit 1
  fi
}

# Install Docker
if ! command -v docker >/dev/null 2>&1; then
  run_sudo apt-get update -y
  run_sudo apt-get install -y ca-certificates curl gnupg lsb-release
  run_sudo install -m 0755 -d /etc/apt/keyrings || true
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | run_sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) stable" | run_sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  run_sudo apt-get update -y
  run_sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  run_sudo usermod -aG docker "$USER" || true
  run_sudo systemctl enable --now docker
else
  echo "Docker already installed"
fi
REMOTE_SCRIPT
  } | sed -u "s/^/[${host}] /"
}

# Initialize docker swarm on local manager and return join command for workers
ensure_swarm_manager_ready() {
  # Are we already a manager?
  if sudo docker info --format '{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}' \
     2>/dev/null | grep -q '^active true$'; then
    log "Swarm manager already active."
    return 0
  fi

  # Otherwise (re)initialize the swarm
  local mgr_ip
  mgr_ip=$(hostname -I | awk '{print $1}')
  log "Initializing swarm on ${mgr_ip}"
  sudo docker swarm init --advertise-addr "${mgr_ip}" || true
}

# Get the join command for the workers (remote nodes)
get_worker_join_cmd() {
  sudo docker swarm join-token -q worker | awk -v ip="$(hostname -I | \
       awk '{print $1}')" '{print "docker swarm join --token "$0" "ip":2377"}'
}

# Pre-pull all images on every remote node
prepull_images_on_host() {
  local host="$1"
  local -a images=(
    cassandra:3.9
    mongo:4.4.6
    memcached:latest
    redis:latest
    yg397/openresty-thrift:xenial
    yg397/media-frontend:xenial
    jaegertracing/jaeger-agent:latest
    jaegertracing/jaeger-collector:latest
    jaegertracing/jaeger-query:latest
    jaegertracing/jaeger-cassandra-schema:latest
    deathstarbench/social-network-microservices:latest
  )
  ssh_stream "$host" "bash -lc 'set -e; for i in ${images[@]}; do echo pulling \$i; sudo docker pull \$i; done'"
}

# Join worker to swarm using given join command
swarm_join_worker() {
  local host="$1" join_cmd="$2"
  log "${host} joining swarm"

  # 
  { timeout -k 5 "${SSH_TIMEOUT_SEC}" \
      ssh $(_ssh_opts) "${SSH_USER}@${host}" "JOIN_CMD=\"${join_cmd}\" bash -s" 2>&1 <<'REMOTE_SCRIPT'
set -ex
run_sudo() {
  if sudo -n true 2>/dev/null; then
    sudo "$@"
  elif [ -n "${SUDO_PASS:-}" ]; then
    echo "$SUDO_PASS" | sudo -S -k "$@"
  else
    echo "ERROR: sudo requires a password and SUDO_PASS is not set on remote host." >&2
    exit 1
  fi
}
run_sudo docker swarm leave
run_sudo $JOIN_CMD || true
REMOTE_SCRIPT
  } | sed -u "s/^/[${host}] /"
}

deploy_stack() {
  log "Deploying docker stack on local manager"
  ( cd "$(dirname "$0")" && sudo docker stack deploy --compose-file docker-compose-swarm.yml "${STACK_NAME}" --prune )
}

# Pin nginx to the local manager and publish 8080 in host mode on that node
pin_frontend_to_manager() {
  log "Pinning ${STACK_NAME}_nginx-web-server to this manager and binding :8080 in host mode"

  local mgr_name
  mgr_name="$(sudo docker node ls --filter role=manager --format '{{.Hostname}}' | head -n1)"

  # Label the manager so we can constrain placement
  sudo docker node update --label-add socialnet.frontend=true "$mgr_name"

  # If the service exists, force its placement + publish settings.
  # (publish-rm by port number is supported; if your engine wants key syntax, use --publish-rm published=8080,target=8080)
  sudo docker service update \
    --constraint-add 'node.labels.socialnet.frontend==true' \
    --publish-rm 8080 \
    --publish-add mode=host,published=8080,target=8080 \
    "${STACK_NAME}_nginx-web-server" || true
}

# Return 0 as soon as port 8080 is accepting connections
wait_for_frontend_tcp() {
  log "Waiting for frontend (nginx) to accept TCP on http://127.0.0.1:8080"
  local deadline=$((SECONDS + 60))  # 1 minute
  while (( SECONDS < deadline )); do
    # Fast TCP check
    if bash -lc 'exec 3<>/dev/tcp/127.0.0.1/8080' 2>/dev/null; then
      # Log the HTTP status just for info - don’t fail on non-2xx
      local code
      code="$(curl -s -o /dev/null -m 3 -w "%{http_code}" http://127.0.0.1:8080)"
      log "Frontend port is open (HTTP ${code})"

      # Extra readiness: confirm user-service through nginx can register a user
      log "Checking user-service registration endpoint readiness"
      (
        deadline=$((SECONDS + 180))
        while (( SECONDS < deadline )); do
          code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 \
            -X POST http://127.0.0.1:8080/wrk2-api/user/register \
            -d "first_name=probe&last_name=probe&username=probe_$RANDOM&password=x&user_id=0" || true)
          if [[ "$code" == "200" ]]; then
            log "Registration endpoint ready (HTTP 200)"; break
          fi
          sleep 3
        done
      )
      return 0
    fi
    sleep 3
  done

  # Display all services' logs if frontend failed
  echo "Frontend never became ready." >&2
  echo "---- service ls ----" >&2
  sudo docker service ls >&2 || true
  echo "---- nginx tasks ----" >&2
  sudo docker service ps "${STACK_NAME}_nginx-web-server" --no-trunc >&2 || true
  echo "---- nginx logs (tail) ----" >&2
  sudo docker service logs "${STACK_NAME}_nginx-web-server" --raw --tail 200 >&2 || true
  echo "---- port binds (inspect) ----" >&2
  sudo docker service inspect "${STACK_NAME}_nginx-web-server" --format '{{json .Endpoint.Ports}}' | jq . >&2 || true
  return 1
}

# Initialize social graph on manager
init_social_graph() {
  log "Initializing social graph (${INIT_GRAPH}) on local manager"
  ( cd "$(dirname "$0")" && python3 -m pip install -q aiohttp asyncio || true && python3 scripts/init_social_graph.py --graph="${INIT_GRAPH}" --limit=64 || true )
}

# Build wrk2 on local manager
build_wrk2_on_manager() {
  log "Building wrk2 on local manager"
  ( cd "$(cd "$(dirname "$0")/.." && pwd)/wrk2" && make -j || make )
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
      "$(cd "$(dirname "$0")/../../.." && pwd)/wrk2/wrk" \
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
      ( cd "$rundir" && "$(cd "$(dirname "$0")/../../.." && pwd)/wrk2/wrk" \
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
    local e2e_median
    shopt -s nullglob
    local thread_files=("$rundir"/[0-9]*.txt)
    if ((${#thread_files[@]})); then
      e2e_array=$(awk 'BEGIN{printf"["} /^[0-9]+$/ {if(n++) printf","; printf "%s",$1} END{printf"]"}' "${thread_files[@]}")
      # Median of all numeric lines across thread files
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

  # Collect node|service for running tasks in the stack
  while IFS= read -r svc; do
    # Strip stack prefix (e.g., socialnet_nginx-web-server -> nginx-web-server)
    local base
    base=${svc#${STACK_NAME}_}
    sudo docker service ps "$svc" --format '{{.Node}} {{.CurrentState}}' \
      | awk -v s="$base" '$0 ~ /Running/ {print $1"|"s}' >> "$assign_tmp"
  done < <(sudo docker stack services "${STACK_NAME}" --format '{{.Name}}')

  # Build friendly name map: node0 = manager, node1.. = workers (sorted)
  # Format: "<hostname>|nodeN"
  sudo docker node ls --format '{{.Hostname}} {{if .ManagerStatus}}manager{{else}}worker{{end}}' \
    | sort | uniq \
    | awk '$2=="manager" {print $1}' \
    | head -n1 \
    | awk '{print $0"|node0"}' >> "$map_tmp"
  sudo docker node ls --format '{{.Hostname}} {{if .ManagerStatus}}manager{{else}}worker{{end}}' \
    | sort | uniq \
    | awk '$2=="worker" {print $1}' | sort \
    | nl -v 1 -w 1 -s ' ' \
    | awk '{printf "%s|node%d\n", $2, $1}' >> "$map_tmp"

  # Build JSON grouped by friendly node name, include empty arrays for nodes with no tasks
  local json first=true
  json="{"
  while IFS='|' read -r host friendly; do
    # Services for this host
    local arr
    arr=$(awk -F'|' -v n="$host" '$1==n {print $2}' "$assign_tmp" | sort -u \
      | awk 'BEGIN{printf "["} {if(NR>1) printf ","; printf "\"%s\"", $0} END{printf "]"}')
    # If no services found, produce an empty array
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

# Write combined results JSON to file (placements first)
write_results_json() {
  local placements_json="$1" compose_json="$2" home_json="$3" user_json="$4"
  cat > "$OUTPUT_JSON" <<-EOF
    {
      "placements": ${placements_json},
      "compose-post": ${compose_json},
      "read-home-timelines": ${home_json},
      "read-user-timelines": ${user_json}
    }
EOF
  log "Wrote results to $OUTPUT_JSON"
}

# =========================
# Main Function
# =========================

main() {
  # Ensure that prereqs are met and create variables
  ensure_local_prereqs
  if [ "${VERBOSE}" = "true" ]; then set -x; fi
  local MANAGER_HOST="127.0.0.1"
  local WORKER_HOSTS=("${WORKER_NODES[@]}")

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

  # 1) Prepare all nodes with Docker
  for n in "${WORKER_NODES[@]}"; do
    remote_install_docker "$n"
  done

  # 2) Ensure local manager is a manager
  ensure_swarm_manager_ready
  join_cmd="$(get_worker_join_cmd)"

  # 3) Join workers
  for w in "${WORKER_HOSTS[@]}"; do
    swarm_join_worker "$w" "$join_cmd"
  done

  # 4) Pre-pull images on manager and workers
  prepull_images_on_host "127.0.0.1"
  for w in "${WORKER_NODES[@]}"; do
    prepull_images_on_host "$w"
  done

  # 5) Deploy stack and wait
  deploy_stack "$MANAGER_HOST"
  pin_frontend_to_manager
  wait_for_frontend_tcp

  # 6) Initialize social graph and build wrk2
  init_social_graph "$MANAGER_HOST"
  build_wrk2_on_manager "$MANAGER_HOST"

  # 7) Run workloads and gather results
  local BASE_URL="http://localhost:8080"
  local compose_json home_json user_json
  local SCRIPT_DIR="$(pwd)/wrk2/scripts/social-network"
  compose_json=$(run_wrk2_and_parse "${BASE_URL}/wrk2-api/post/compose" "${SCRIPT_DIR}/compose-post.lua" "compose-post")
  home_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/home-timeline/read" "${SCRIPT_DIR}/read-home-timeline.lua" "read-home-timelines")
  user_json=$(run_wrk2_and_parse    "${BASE_URL}/wrk2-api/user-timeline/read" "${SCRIPT_DIR}/read-user-timeline.lua" "read-user-timelines")

  # 8) Save combined JSON locally with placements first
  local placements_json
  placements_json=$(build_placements_json)
  write_results_json "$placements_json" "$compose_json" "$home_json" "$user_json"
  python -m json.tool ${OUTPUT_JSON} > tmp.json && mv tmp.json ${OUTPUT_JSON}

  log "Done. Inspect services with: sudo docker stack services ${STACK_NAME}"
}

main "$@"
