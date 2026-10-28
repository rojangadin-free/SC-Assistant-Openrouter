#!/usr/bin/env bash
# One-host rollout. No action without --apply; only named project containers change.
set -Eeuo pipefail
if [[ "${1:-}" != "--apply" ]]; then
    printf '%s\n' 'Dry run: --apply IMAGE ENV_FILE retires stopped older versions, checks the measured image allowance, and keeps the live version until its replacement is healthy.'
    exit 0
fi
image="${2:?Supply an immutable image tag}"
image_repository="${image%@*}"
[[ "${image_repository##*/}" == *:* ]] && image_repository="${image_repository%:*}"
env_file="$(realpath "${3:?Supply an environment file}")"
target="${GUNICORN_WORKERS:-3}"
keep_rollback="${SC_KEEP_ROLLBACK:-false}"
[[ "$keep_rollback" == true || "$keep_rollback" == false ]] || { echo 'SC_KEEP_ROLLBACK must be true or false'; exit 1; }
[[ "$target" =~ ^[1-8]$ ]] || { echo 'GUNICORN_WORKERS must be 1-8'; exit 1; }
state="${SC_DEPLOY_DIR:-$HOME/.sc-assistant-deploy}"
mkdir -p "$state/proxy"
chmod 700 "$state"
chmod 600 "$env_file"
exec 9>"$state/deploy.lock"
flock -n 9 || { echo 'Another deployment is active'; exit 1; }
network=sc-assistant-network
proxy=sc-assistant-proxy
indexer=sc-assistant-indexer
legacy=rag-app-container
old=""
if [[ -f "$state/proxy/nginx.conf" ]]; then
    old="$(sed -n 's/^# sc-active://p' "$state/proxy/nginx.conf")"
fi
case "$old" in ''|sc-assistant-web-blue|sc-assistant-web-green) ;; *) echo 'Unknown active backend; refusing cleanup'; exit 1;; esac
candidate=sc-assistant-web-blue
[[ "$old" == "$candidate" ]] && candidate=sc-assistant-web-green
runtime="$state/$candidate"
mkdir -p "$runtime"
root="$(cd "$(dirname "$0")/.." && pwd)"
switched=0
legacy_stopped=0
index_stopped=0
candidate_started=0
index_renamed=0
proxy_image="${SC_PROXY_IMAGE:-nginx:stable-alpine}"

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == true ]]; }
owned() { [[ "$(docker inspect -f '{{index .Config.Labels "sc.project"}}' "$1" 2>/dev/null || true)" == sc-assistant ]]; }
stop_candidate() { if (( candidate_started )); then docker stop -t 150 "$candidate" >/dev/null 2>&1; fi; }
write_proxy() {
    if ! python3 "$root/tools/proxy_config.py" "$1" > "$state/proxy/next.conf"; then
        rm -f "$state/proxy/next.conf"
        return 1
    fi
    mv "$state/proxy/next.conf" "$state/proxy/nginx.conf"
}
ready() {
    local name="$1"
    for _ in $(seq 1 60); do
        running "$name" || return 1
        if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=5).read()" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}
worker_count() {
    docker exec "$candidate" python -c "import os; p='/run/sc-assistant'; children=open('/proc/1/task/1/children').read().split(); print(sum(os.path.exists(p+'/ready-'+pid) for pid in children))"
}
proxy_ready() {
    for _ in $(seq 1 30); do
        if python3 -c "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=5); sys.exit(r.headers.get('X-SC-Backend') != sys.argv[1])" "$candidate" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}
restore() {
    local code=$?
    trap - ERR
    set +e
    echo 'Deployment failed; restoring the previous service where available.'
    if [[ -n "$old" ]]; then
        if ! running "$old"; then
            stop_candidate
            docker start "$old" >/dev/null && ready "$old"
        fi
        write_proxy "$old"
        docker exec "$proxy" nginx -t -c /etc/nginx/sc/nginx.conf >/dev/null 2>&1
        docker exec "$proxy" nginx -s reload -c /etc/nginx/sc/nginx.conf >/dev/null 2>&1
        stop_candidate
    elif (( legacy_stopped )); then
        docker stop -t 15 "$proxy" >/dev/null 2>&1
        docker start "$legacy" >/dev/null 2>&1
        stop_candidate
    elif (( ! switched )); then
        stop_candidate
    fi
    if (( index_renamed )) && owned "$backup"; then
        if owned "$indexer"; then
            docker stop -t 90 "$indexer" >/dev/null 2>&1
            docker rm "$indexer" >/dev/null 2>&1
        fi
        docker rename "$backup" "$indexer" >/dev/null 2>&1
    fi
    if (( index_stopped )); then docker start "$indexer" >/dev/null 2>&1; fi
    exit "$code"
}
for name in "$proxy" "$indexer" "$candidate"; do
    if docker inspect "$name" >/dev/null 2>&1 && ! owned "$name"; then
        echo "Container name $name is not project-owned; refusing rollout."
        exit 1
    fi
done
if [[ -n "$old" ]] && ! owned "$old"; then echo 'Active backend is not project-owned'; exit 1; fi
if running "$candidate"; then echo 'The inactive backend is still running; refusing to remove it'; exit 1; fi
# Pull/preflight failures must not enter rollback: no service has changed yet.
# Retire only older STOPPED versions. The live backend and indexer still protect
# the current image, which remains available for rollback throughout this run.
storage_args=(--image "$image" --apply)
if [[ "$keep_rollback" == false && -n "$old" ]] && running "$old"; then
    storage_args+=(--retire-stopped --active-container "$old")
fi
python3 "$root/tools/deploy_storage.py" "${storage_args[@]}"
docker pull "$image"
docker pull "$proxy_image"
docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
if [[ -z "$old" ]] && running "$legacy"; then
    legacy_image="$(docker inspect -f '{{.Config.Image}}' "$legacy")"
    legacy_repository="${legacy_image%@*}"
    [[ "${legacy_repository##*/}" == *:* ]] && legacy_repository="${legacy_repository%:*}"
    [[ "$legacy_repository" == "$image_repository" ]] || { echo 'Legacy name belongs to another image; refusing to stop it'; exit 1; }
    docker exec "$legacy" python -c "import config; from rag import jobs; import sys; active=[j for j in jobs._store().load().get('jobs',{}).values() if j.get('status')=='running' and jobs._age_seconds(j.get('updated_at',''))<jobs.STALE_AFTER_SECONDS]; sys.exit(bool(active))" || { echo 'Wait for legacy uploads to finish before migration'; exit 1; }
fi
trap restore ERR
if running "$indexer"; then
    owned "$indexer" || { echo 'Indexer name is not project-owned'; exit 1; }
    docker stop -t 90 "$indexer" >/dev/null
    index_stopped=1
fi
available="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
[[ "$available" -ge "${SC_DEPLOY_MIN_FREE_MB:-1800}" ]] || { echo 'Insufficient free memory for one candidate worker'; false; }
if docker inspect "$candidate" >/dev/null 2>&1; then
    owned "$candidate" || { echo 'Candidate name is not project-owned'; exit 1; }
    docker rm "$candidate" >/dev/null
fi
rm -f "$runtime"/ready-*
printf '1\n' > "$runtime/workers"
docker run -d --name "$candidate" --label sc.project=sc-assistant --restart unless-stopped \
    --network "$network" --memory "${SC_WEB_MEMORY:-5g}" --env-file "$env_file" \
    --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
    -e GUNICORN_WORKERS="$target" -e GUNICORN_WORKERS_FILE=/run/sc-assistant/workers \
    -v "$runtime:/run/sc-assistant" "$image" >/dev/null
candidate_started=1
ready "$candidate"
if [[ -z "$old" ]] && running "$legacy"; then
    docker stop -t 150 "$legacy" >/dev/null
    legacy_stopped=1
fi
write_proxy "$candidate"
if running "$proxy"; then
    owned "$proxy" || { echo 'Proxy name is not project-owned'; false; }
    docker exec "$proxy" nginx -t -c /etc/nginx/sc/nginx.conf
    docker exec "$proxy" nginx -s reload -c /etc/nginx/sc/nginx.conf
else
    if docker inspect "$proxy" >/dev/null 2>&1; then owned "$proxy" && docker rm "$proxy" >/dev/null; fi
    docker run -d --name "$proxy" --label sc.project=sc-assistant --restart unless-stopped \
        --network "$network" -p 8080:8080 -v "$state/proxy:/etc/nginx/sc:ro" \
        --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
        "$proxy_image" nginx -c /etc/nginx/sc/nginx.conf -g 'daemon off;' >/dev/null
fi
switched=1
proxy_ready
if [[ -n "$old" ]] && running "$old"; then docker stop -t 150 "$old" >/dev/null; fi
printf '%s\n' "$target" > "$runtime/workers"
for expected in $(seq 2 "$target"); do
    docker kill --signal=TTIN "$candidate" >/dev/null
    for _ in $(seq 1 60); do
        [[ "$(worker_count)" -ge "$expected" ]] && break
        sleep 2
    done
    [[ "$(worker_count)" -ge "$expected" ]]
done
ready "$candidate"
if docker inspect "$indexer" >/dev/null 2>&1; then
    backup=sc-assistant-indexer-backup
    if docker inspect "$backup" >/dev/null 2>&1; then
        owned "$backup" || { echo 'Indexer backup is not project-owned'; false; }
        docker rm "$backup" >/dev/null
    fi
    docker rename "$indexer" "$backup"
    index_renamed=1
fi
docker run -d --name "$indexer" --label sc.project=sc-assistant --restart unless-stopped \
    --network "$network" --memory "${SC_INDEX_MEMORY:-2g}" --cpus "${SC_INDEX_CPUS:-1}" \
    --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
    --env-file "$env_file" "$image" python tools/index_worker.py >/dev/null
sleep 1
running "$indexer"
trap - ERR
echo "Deployed $candidate with $target web workers; durable index worker active."
if [[ "$keep_rollback" == false ]]; then
    if ! python3 "$root/tools/deploy_storage.py" --image "$image" --apply \
            --retire-stopped --active-container "$candidate" --cleanup-only; then
        echo 'Deployment is healthy; stopped-version cleanup was incomplete and can be retried.'
    fi
fi
