#!/usr/bin/env bash
set -uo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVOCATION_DIR="$(pwd -P)"
UV="$SKILL_ROOT/bin/uv.exe"
REQ="$SKILL_ROOT/requirements.lock"
CHECKSUMS="$SKILL_ROOT/bin/checksums.sha256"
OVERALL_SECONDS=3600
INSTALL_SECONDS=1800
MIN_ENV_FREE_GB=4
MIN_MODEL_FREE_GB=20
START_SECONDS=$SECONDS
ACTIVE=0
LOCK_HELD=0
CONTINUE_ONLY=0
CANCEL_ONLY=0
REQUEST_FILE_MODE=0
REQUEST_FILE=""
REQUEST_ID=""

fail() { echo "ERROR: $*" >&2; exit 1; }
command -v bash >/dev/null 2>&1 || fail 'Bash is required.'
command -v cygpath >/dev/null 2>&1 || fail 'WorkBuddy requires Git Bash; cygpath is unavailable.'
command -v timeout >/dev/null 2>&1 || fail 'WorkBuddy requires Git Bash coreutils; timeout is unavailable.'
command -v sha256sum >/dev/null 2>&1 || fail 'WorkBuddy requires Git Bash coreutils; sha256sum is unavailable.'
to_win() { cygpath -w "$1"; }
to_unix() { cygpath -u "$1" 2>/dev/null || printf '%s\n' "$1"; }

if [ -n "${LOCAL_IMG2IMG_DATA_DIR:-}" ]; then
  DATA_DIR="$(to_unix "$LOCAL_IMG2IMG_DATA_DIR")"
else
  DATA_DIR="$(to_unix "${USERPROFILE:-$HOME}/.openvino/photo-magic")"
  echo 'LOCAL_IMG2IMG_DATA_DIR was not supplied; using the stable local runtime directory.'
fi
mkdir -p "$DATA_DIR" || fail "cannot create plugin data directory: $DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd -P)" || fail "cannot resolve plugin data directory: $DATA_DIR"
export LOCAL_IMG2IMG_DATA_DIR="$(to_win "$DATA_DIR")"
export LOCAL_IMG2IMG_DEADLINE_EPOCH=$(( $(date +%s) + OVERALL_SECONDS ))
export UV_CACHE_DIR="$(to_win "$DATA_DIR/uv-cache")"

if [ -n "${LOCAL_IMG2IMG_OUTPUT_DIR:-}" ]; then OUTPUT_DIR="$(to_unix "$LOCAL_IMG2IMG_OUTPUT_DIR")"; else OUTPUT_DIR="$INVOCATION_DIR/outputs"; fi
case "$OUTPUT_DIR" in /*) ;; *) fail "workspace output directory must be absolute: $OUTPUT_DIR" ;; esac
mkdir -p "$OUTPUT_DIR" || fail "cannot create workspace output directory: $OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)" || fail "cannot resolve workspace output directory: $OUTPUT_DIR"
OUTPUT_DIR_WIN="$(to_win "$OUTPUT_DIR")"

VENV_DIR="$DATA_DIR/venv/img2img"
VPY="$VENV_DIR/Scripts/python.exe"
SHA_FILE="$VENV_DIR/requirements.sha"
LOCK_DIR="$DATA_DIR/locks/img2img-env.lock"
CLIENT_PY_WIN="$(to_win "$SKILL_ROOT/scripts/client.py")"
SHARED_MODEL_DIR="$(to_unix "${USERPROFILE:-$HOME}/.openvino/models/FLUX.2-klein-4B-int4-ov")"
LEGACY_MODEL_DIR="$(to_unix "${USERPROFILE:-$HOME}/.openvino/photo-magic/models/FLUX.2-klein-4B-int4-ov")"
if [ -n "${LOCAL_IMG2IMG_MODEL_DIR:-}" ]; then
  MODEL_DIR="$(to_unix "$LOCAL_IMG2IMG_MODEL_DIR")"
elif [ -d "$SHARED_MODEL_DIR" ]; then
  MODEL_DIR="$SHARED_MODEL_DIR"
elif [ -d "$LEGACY_MODEL_DIR" ]; then
  MODEL_DIR="$LEGACY_MODEL_DIR"
else
  MODEL_DIR="$SHARED_MODEL_DIR"
fi
MODEL_ROOT="$(dirname "$MODEL_DIR")"
mkdir -p "$MODEL_ROOT" || fail "cannot create shared model directory: $MODEL_ROOT"
export LOCAL_IMG2IMG_MODEL_DIR="$(to_win "$MODEL_DIR")"

new_uuid() {
  local hex
  hex="$(printf '%s-%s-%s-%s' "$RANDOM" "$RANDOM" "$RANDOM" "$(date +%s%N)" | sha256sum | awk '{print $1}')"
  printf '%s-%s-4%s-8%s-%s\n' "${hex:0:8}" "${hex:8:4}" "${hex:13:3}" "${hex:17:3}" "${hex:20:12}"
}

cleanup() {
  if [ "$ACTIVE" -eq 1 ] && [ -n "$REQUEST_ID" ] && [ -f "$VPY" ]; then timeout --foreground 20 "$VPY" -u "$CLIENT_PY_WIN" --cancel --request-id "$REQUEST_ID" >/dev/null 2>&1 || true; fi
  if [ "$LOCK_HELD" -eq 1 ]; then rm -f "$LOCK_DIR/owner.pid" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

MODE="${1:-}"
case "$MODE" in
  --request-file)
    REQUEST_FILE_MODE=1
    REQUEST_FILE="$(to_unix "${2:-}")"
    [ -n "$REQUEST_FILE" ] || fail '--request-file requires a JSON file path.'
    [ -f "$REQUEST_FILE" ] || fail "request file not found: $REQUEST_FILE"
    REQUEST_FILE="$(cd "$(dirname "$REQUEST_FILE")" && pwd -P)/$(basename "$REQUEST_FILE")" || fail "cannot resolve request file: $REQUEST_FILE"
    REQUEST_ID="${3:-$(new_uuid)}"
    ;;
  --continue) CONTINUE_ONLY=1; REQUEST_ID="${2:-}" ;;
  --cancel) CANCEL_ONLY=1; REQUEST_ID="${2:-}"; [ -n "$REQUEST_ID" ] || fail '--cancel requires a request ID.' ;;
  --dev-direct)
    [ "${LOCAL_IMG2IMG_DEV_MODE:-0}" = "1" ] || fail '--dev-direct is disabled outside explicit local development.'
    IMAGE="$(to_unix "${2:-}")"
    PROMPT="${3:-}"
    [ -f "$IMAGE" ] || fail "image path not found: ${2:-}"
    [ -n "$PROMPT" ] || fail 'prompt is required.'
    REQUEST_ID="${4:-$(new_uuid)}"
    ;;
  '') fail 'usage: run.sh --request-file <request.json> | --continue [request-id] | --cancel <request-id>' ;;
  *) fail "unsupported launcher mode: $MODE" ;;
esac

echo "Request ID: ${REQUEST_ID:-pending-selection}"
echo "Output directory: $OUTPUT_DIR_WIN"

if [ "$CANCEL_ONLY" -eq 1 ]; then
  [ -f "$VPY" ] || { echo '任务已取消；推理环境未运行。'; exit 0; }
  "$VPY" -u "$CLIENT_PY_WIN" --cancel --request-id "$REQUEST_ID"
  ACTIVE=0
  exit $?
fi

[ -f "$UV" ] || fail 'bundled bin/uv.exe is missing.'
[ -f "$CHECKSUMS" ] || fail 'binary checksum manifest is missing.'
(cd "$SKILL_ROOT" && sha256sum --check --status 'bin/checksums.sha256') || fail 'bundled runtime checksum verification failed.'

first_run_disk_check_path() {
    local target="$1" label="$2" required_gb="$3"
    local available_kb required_kb
    available_kb="$(df -Pk "$target" | awk 'NR==2 {print $4}')"
    required_kb=$((required_gb * 1024 * 1024))
    [ -n "$available_kb" ] || fail 'cannot determine free disk space.'
    [ "$available_kb" -ge "$required_kb" ] || fail "first run requires at least ${required_gb} GB free in $label."
}

first_run_disk_check() {
  if [ ! -f "$VPY" ]; then first_run_disk_check_path "$DATA_DIR" 'the runtime data directory' "$MIN_ENV_FREE_GB"; fi
  if [ ! -d "$MODEL_DIR" ]; then first_run_disk_check_path "$MODEL_ROOT" 'the selected model directory' "$MIN_MODEL_FREE_GB"; fi
}

acquire_install_lock() {
  local waited=0 owner
  mkdir -p "$(dirname "$LOCK_DIR")" || return 1
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if [ -f "$LOCK_DIR/owner.pid" ]; then
      owner="$(tr -d '[:space:]' < "$LOCK_DIR/owner.pid" 2>/dev/null || true)"
      if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then rm -f "$LOCK_DIR/owner.pid" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true; continue; fi
    fi
    [ "$waited" -lt "$INSTALL_SECONDS" ] || { echo 'ERROR: environment installation lock timed out.' >&2; return 124; }
    sleep 1
    waited=$((waited + 1))
  done
  printf '%s' "$$" > "$LOCK_DIR/owner.pid"
  LOCK_HELD=1
}

release_install_lock() {
  if [ "$LOCK_HELD" -eq 1 ]; then rm -f "$LOCK_DIR/owner.pid" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true; LOCK_HELD=0; fi
}

install_environment() {
  first_run_disk_check
  acquire_install_lock || return $?
  if [ ! -f "$VPY" ]; then timeout --foreground "$INSTALL_SECONDS" "$UV" venv --clear --seed --python 3.11 "$(to_win "$VENV_DIR")" || { release_install_lock; return 1; }; fi
  local current_hash old_hash
  current_hash="$(sha256sum "$REQ" | awk '{print $1}')"
  old_hash="$(tr -d '[:space:]' < "$SHA_FILE" 2>/dev/null || true)"
  if [ "$current_hash" != "$old_hash" ] || ! timeout --foreground 30 "$VPY" -c 'import colorama, PIL, openvino, modelscope, psutil' >/dev/null 2>&1; then
    timeout --foreground "$INSTALL_SECONDS" "$UV" pip install --python "$(to_win "$VPY")" -r "$(to_win "$REQ")" --require-hashes --index-url https://pypi.tuna.tsinghua.edu.cn/simple || \
      timeout --foreground "$INSTALL_SECONDS" "$UV" pip install --python "$(to_win "$VPY")" -r "$(to_win "$REQ")" --require-hashes || { release_install_lock; return 1; }
    printf '%s' "$current_hash" > "$SHA_FILE"
  fi
  timeout --foreground 30 "$VPY" -u "$(to_win "$SKILL_ROOT/scripts/get_gpu_mem.py")" >/dev/null || { echo 'ERROR: this expert requires a Windows Intel AIPC with a readable Intel GPU memory adapter.' >&2; release_install_lock; return 1; }
  release_install_lock
}

if [ "$CONTINUE_ONLY" -eq 0 ]; then install_environment || exit $?; fi
[ -f "$VPY" ] || fail "Python venv not found at $VPY"

round=0
repair_attempted=0
use_continue=$CONTINUE_ONLY
exit_code=1
while [ "$round" -lt 8 ] && [ $((SECONDS - START_SECONDS)) -lt "$OVERALL_SECONDS" ]; do
  round=$((round + 1))
  remaining=$((OVERALL_SECONDS - (SECONDS - START_SECONDS)))
  ACTIVE=1
  if [ "$use_continue" -eq 1 ]; then
    args=("$VPY" -u "$CLIENT_PY_WIN" --continue --output-dir "$OUTPUT_DIR_WIN")
    [ -n "$REQUEST_ID" ] && args+=(--request-id "$REQUEST_ID")
  elif [ "$REQUEST_FILE_MODE" -eq 1 ]; then
    args=("$VPY" -u "$CLIENT_PY_WIN" --request-file "$(to_win "$REQUEST_FILE")" --consume-request-file --request-id "$REQUEST_ID" --output-dir "$OUTPUT_DIR_WIN")
  else
    args=("$VPY" -u "$CLIENT_PY_WIN" --image-path "$(to_win "$IMAGE")" -i "$PROMPT" --request-id "$REQUEST_ID" --output-dir "$OUTPUT_DIR_WIN")
  fi
  timeout --foreground "$remaining" "${args[@]}"
  exit_code=$?
  REQUEST_FILE_MODE=0
  if [ "$exit_code" -eq 124 ] && [ -n "$REQUEST_ID" ]; then timeout --foreground 20 "$VPY" -u "$CLIENT_PY_WIN" --cancel --request-id "$REQUEST_ID" >/dev/null 2>&1 || true; fi
  ACTIVE=0
  if [ "$exit_code" -eq 4 ] && [ "$repair_attempted" -eq 0 ]; then repair_attempted=1; install_environment || { exit_code=$?; break; }; continue; fi
  [ "$exit_code" -eq 3 ] || break
  use_continue=1
  echo 'Model is still downloading; continuing the same request automatically...'
  sleep 3
done

if [ "$exit_code" -eq 3 ] || [ $((SECONDS - START_SECONDS)) -ge "$OVERALL_SECONDS" ]; then
  echo 'ERROR: bounded continuation/overall timeout reached; pending request was preserved.' >&2
  exit_code=124
fi
exit "$exit_code"
