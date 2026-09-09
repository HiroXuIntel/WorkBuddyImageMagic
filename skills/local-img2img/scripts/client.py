"""Short-lived CLI client for the local-img2img server.

Talks to ``server.py`` over the Windows named pipe ``\\\\.\\pipe\\img2img``.
Starts a detached server in the background if one is not already running.

Mirrors ``skills/local-computer-use/src/scripts/client.py`` so the
``--continue`` / pending-request protocol behaves identically: when the
first run of the skill needs to download model weights, the
request is persisted and the caller re-invokes with ``--continue`` until
the model is ready.
"""

from __future__ import annotations

import argparse
import contextlib
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from multiprocessing.connection import Client
from pathlib import Path

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:
    print("** The local-img2img environment is incomplete. Re-run the expert's standard Bash entry. **")
    # Dedicated environment error so launchers can repair once and retry.
    sys.exit(4)

PIPE_ADDRESS = r"\\.\pipe\photo-magic-img2img-v1"
AUTHKEY = b"photo-magic-img2img-v1"
SKILL_NAME = "photo-magic-local-img2img-v1"
DOG_PIPE_ADDRESS = r"\\.\pipe\photo-magic-server-dog-v1"
DOG_AUTHKEY = b"photo-magic-server-dog-v1"
EXPECTED_DOG_PROTOCOL_VERSION = 3
DOG_BOOT_TIMEOUT = 30.0
DOG_BOOT_POLL_INTERVAL = 0.3
SERVER_BOOT_TIMEOUT = 60.0
SERVER_BOOT_POLL_INTERVAL = 0.3
DEFAULT_SHUTDOWN_TIMEOUT = 10.0
OPENVINO_ROOT = Path(
    os.environ.get(
        "LOCAL_IMG2IMG_DATA_DIR",
        str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".openvino" / "photo-magic"),
    )
).expanduser().resolve()
PENDING_REQUEST_DIR = OPENVINO_ROOT / "pending" / "img2img"
LEGACY_PENDING_REQUEST_PATH = OPENVINO_ROOT / "img2img-pending-request.json"
TEMP_ROOT = OPENVINO_ROOT / "temp"
TEMP_DOG = TEMP_ROOT / "server-dog.py"
TEMP_GET_GPU_MEM = TEMP_ROOT / "get_gpu_mem.py"
TEMP_DIR = TEMP_ROOT / "img2img"
TEMP_SERVER = TEMP_DIR / "server.py"
TEMP_MODEL_DOWNLOAD = TEMP_DIR / "model_download.py"
TEMP_INFO_JSON = TEMP_DIR / "info.json"
TEMP_BIN = TEMP_DIR / "bin"
LEGACY_TEMP_SERVER_DOG = TEMP_DIR / "server-dog.py"
STATUS_POLL_INTERVAL = 2.0
# While a model download is running the status is polled more often so the
# progress line refreshes several times per second of wall clock time.
PROGRESS_POLL_INTERVAL = 1.0
ERROR_RETRY_MAX = 3
ERROR_RETRY_GAP = 5.0
ABNORMAL_IMAGE_RETRY_MAX = 3
SERVER_BUSY_RETRY_GAP = 2.0
DEFAULT_SERVER_BUSY_TIMEOUT = 900.0
DEFAULT_IPC_TIMEOUT = 10.0
DEFAULT_SERVER_START_TIMEOUT = 90.0
DEFAULT_GENERATION_TIMEOUT = 900.0
GPU_UNAVAILABLE_RETRY_GAP = 10.0
RUNTIME_LOCK_TIMEOUT = 60.0
CLAW_MAP = {
    # Map unique substrings of skill root paths to their corresponding Claw executables, if any.
    ".workbuddy": "WorkBuddy.exe",
    ".openclaw": "openclaw.mjs",
    "Marvis": "Marvis.exe",
    ".trae-cn": "TRAE SOLO CN.exe",
    "Coze": "Coze.exe",
    ".qwenworkcn":"QwenWorkCN.exe",
}
SUPPORTED_SCENES = {
    "background-swap",
    "mark-removal",
    "id-photo-background",
    "clutter-removal",
    "product-scene",
    "natural-portrait",
    "old-photo-restoration",
}
MAX_REQUEST_FILE_BYTES = 64 * 1024


def _configure_stream_encoding(stream) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


_configure_stream_encoding(sys.stdout)
_configure_stream_encoding(sys.stderr)
colorama_init()


def _normalize_log_path(log_path: str | None) -> Path | None:
    if not log_path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = str(OPENVINO_ROOT / "log" / f"img2img-client-py-{timestamp}.log")

    path = Path(log_path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _log_message(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            stream.write(f"[{timestamp}] [client pid={os.getpid()}] {message}\n")
    except OSError:
        pass


def _cleanup_old_logs() -> None:
    try:
        days = max(1, int(_read_info_json().get("log_retention_days", 7)))
        cutoff = time.time() - days * 86400
        log_dir = OPENVINO_ROOT / "log"
        for pattern in ("img2img-*.log", "install-env-*.log"):
            for path in log_dir.glob(pattern):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        pending_cutoff = time.time() - 86400
        for path in PENDING_REQUEST_DIR.glob("*.json"):
            if path.is_file() and path.stat().st_mtime < pending_cutoff:
                path.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        pass


def _safe_request_id(value: str | None) -> str:
    if value:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError):
            raise ValueError("request_id must be a UUID") from None
    return str(uuid.uuid4())


def _pending_request_path(request_id: str) -> Path:
    return PENDING_REQUEST_DIR / f"{_safe_request_id(request_id)}.json"


def _save_pending_request(
    request_id: str,
    reference_image_path: str,
    prompt: str,
    output_dir: str | None,
    log_path: Path | None,
    scene: str | None = None,
) -> None:
    try:
        pending_path = _pending_request_path(request_id)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_id": request_id,
            "reference_image_path": reference_image_path,
            "prompt": prompt,
            "output_dir": output_dir,
            "scene": scene,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log_path": str(log_path) if log_path is not None else None,
            "progress_signature": _PROGRESS_STATE.get("progress_signature"),
            "progress_changed_at": _PROGRESS_STATE.get("progress_changed_at", time.time()),
        }
        temp_path = pending_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(pending_path)
        _log_message(log_path, f"saved pending request request_id={request_id}")
    except OSError as exc:
        _log_message(log_path, f"failed to save pending request: {exc}")


def _load_request_file(
    path_text: str,
    consume: bool,
    allowed_root: str | None = None,
) -> tuple[str, str, str]:
    raw_path = Path(path_text).expanduser()
    if not raw_path.is_absolute():
        raise ValueError("request file must be an existing absolute path")
    path = raw_path.resolve()
    if not path.is_file():
        raise ValueError("request file must be an existing absolute path")
    if consume:
        if not allowed_root:
            raise ValueError("consuming a request file requires an explicit workspace output directory")
        try:
            path.relative_to(Path(allowed_root).expanduser().resolve())
        except ValueError:
            raise ValueError("request file must stay inside the current workspace") from None
    if path.stat().st_size > MAX_REQUEST_FILE_BYTES:
        raise ValueError(f"request file exceeds {MAX_REQUEST_FILE_BYTES} bytes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid request JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("request JSON must be an object")
    image_path = data.get("image_path")
    prompt = data.get("prompt")
    scene = data.get("scene")
    if not isinstance(image_path, str) or not image_path.strip():
        raise ValueError("request image_path must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("request prompt must be a non-empty string")
    if scene not in SUPPORTED_SCENES:
        raise ValueError(f"request scene must be one of: {', '.join(sorted(SUPPORTED_SCENES))}")
    if consume:
        try:
            path.unlink()
        except OSError as exc:
            raise ValueError(f"request was read but could not be removed: {exc}") from None
    return image_path, prompt, scene


def _load_pending_request(request_id: str | None) -> dict | None:
    if request_id:
        candidates = [_pending_request_path(request_id)]
    else:
        try:
            candidates = sorted(PENDING_REQUEST_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        if len(candidates) > 1:
            raise RuntimeError("存在多个待处理请求，请使用 --request-id 指定要继续的任务")
        if not candidates:
            candidates = [LEGACY_PENDING_REQUEST_PATH] if LEGACY_PENDING_REQUEST_PATH.is_file() else []
        if not candidates:
            return None
    try:
        text = candidates[0].read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "reference_image_path" not in data or "prompt" not in data:
        return None
    if not data.get("request_id"):
        data["request_id"] = str(uuid.uuid4())
        try:
            migrated_path = _pending_request_path(data["request_id"])
            migrated_path.parent.mkdir(parents=True, exist_ok=True)
            migrated_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if candidates[0] == LEGACY_PENDING_REQUEST_PATH:
                LEGACY_PENDING_REQUEST_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    return data


def _delete_pending_request(request_id: str, log_path: Path | None = None) -> None:
    try:
        _pending_request_path(request_id).unlink()
        _log_message(log_path, f"deleted pending request request_id={request_id}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log_message(log_path, f"failed to delete pending request: {exc}")


def _try_connect():
    try:
        return Client(PIPE_ADDRESS, authkey=AUTHKEY)
    except (FileNotFoundError, OSError, EOFError):
        return None


def _hidden_startupinfo():
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def _pythonw_executable() -> str:
    python_exe = Path(sys.executable)
    pythonw_exe = python_exe.with_name("pythonw.exe")
    return str(pythonw_exe) if pythonw_exe.exists() else str(python_exe)


def _detect_claw_name() -> str | None:
    skill_root = str(Path(__file__).resolve().parent.parent).casefold()
    for key, exe in CLAW_MAP.items():
        if key.casefold() in skill_root:
            return exe
    return None


def _needs_sync(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return not filecmp.cmp(str(src), str(dst), shallow=False)


def _dircmp_differs(cmp: filecmp.dircmp) -> bool:
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return True
    return any(_dircmp_differs(sub) for sub in cmp.subdirs.values())


def _dir_needs_sync(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return _dircmp_differs(filecmp.dircmp(str(src), str(dst), ignore=["__pycache__"]))


def _copy_runtime_tree(src: Path, dst: Path, log_path: Path | None) -> None:
    last_err: Exception | None = None
    for _ in range(5):
        try:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            last_err = None
            break
        except (PermissionError, OSError) as exc:
            last_err = exc
            time.sleep(0.3)
    if last_err is not None:
        raise RuntimeError(f"failed to copy bin tree to {dst} (still in use): {last_err}")
    _log_message(log_path, f"refreshed OpenVINO runtime tree under {dst}")


def _terminate_legacy_dog(log_path: Path | None) -> None:
    """Kill any in-flight legacy per-skill server-dog from a previous release."""
    try:
        import psutil
    except ImportError:
        return
    target = str(LEGACY_TEMP_SERVER_DOG).casefold()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(target in str(part).casefold() for part in cmdline):
                proc.terminate()
                _log_message(log_path, f"terminated legacy dog pid={proc.info.get('pid')}")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue


def _try_dog_connect():
    try:
        return Client(DOG_PIPE_ADDRESS, authkey=DOG_AUTHKEY)
    except (FileNotFoundError, OSError, EOFError):
        return None


def _spawn_dog(log_path: Path | None) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = {
        k: v for k, v in os.environ.items()
        if not (k.startswith("WORKBUDDY_") or k.startswith("CODEBUDDY_") or k == "INTEL_SKILL_DOG_NO_EVICTION")
    }
    subprocess.Popen(
        [_pythonw_executable(), str(TEMP_DOG)],
        creationflags=creationflags,
        startupinfo=_hidden_startupinfo(),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(OPENVINO_ROOT),
        env=env,
    )
    _log_message(log_path, f"spawned shared server-dog: {TEMP_DOG}")


def _ensure_dog(log_path: Path | None):
    conn = _try_dog_connect()
    if conn is not None:
        try:
            conn.send({"op": "status"})
            status = _recv_with_timeout(
                conn,
                _config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT),
                "server-dog status",
            )
        except (TimeoutError, EOFError, OSError):
            status = None
        finally:
            conn.close()

        if isinstance(status, dict) and status.get("protocol_version") == EXPECTED_DOG_PROTOCOL_VERSION:
            current = _try_dog_connect()
            if current is not None:
                return current

        other_servers = []
        if isinstance(status, dict):
            other_servers = [
                item for item in status.get("servers", [])
                if isinstance(item, dict) and item.get("skill_name") != SKILL_NAME
            ]
        if other_servers:
            raise RuntimeError(
                "incompatible private server-dog has unexpected foreign registrations; "
                "refusing to reuse or evict them"
            )

        _log_message(log_path, "restarting outdated server-dog before launching img2img server")
        shutdown_conn = _try_dog_connect()
        if shutdown_conn is not None:
            try:
                shutdown_conn.send({"op": "shutdown"})
                _recv_with_timeout(
                    shutdown_conn,
                    _config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT),
                    "server-dog shutdown",
                )
            except (TimeoutError, EOFError, OSError):
                pass
            finally:
                shutdown_conn.close()
        shutdown_deadline = time.time() + DOG_BOOT_TIMEOUT
        while time.time() < shutdown_deadline:
            probe = _try_dog_connect()
            if probe is None:
                break
            probe.close()
            time.sleep(DOG_BOOT_POLL_INTERVAL)
        else:
            raise RuntimeError("outdated server-dog did not stop")

    _spawn_dog(log_path)
    deadline = time.time() + DOG_BOOT_TIMEOUT
    while time.time() < deadline:
        conn = _try_dog_connect()
        if conn is not None:
            return conn
        time.sleep(DOG_BOOT_POLL_INTERVAL)
    raise RuntimeError("server-dog did not start")


def _read_info_json() -> dict:
    skill_root = Path(__file__).resolve().parent.parent
    info_json = skill_root / "info.json"
    return json.loads(info_json.read_text(encoding="utf-8"))


def _config_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(_read_info_json().get(name, default)))
    except (TypeError, ValueError, OSError, json.JSONDecodeError):
        return default


def _recv_with_timeout(conn, timeout: float, operation: str):
    if not conn.poll(max(0.1, timeout)):
        raise TimeoutError(f"{operation} timed out after {timeout:.0f}s")
    return conn.recv()


@contextlib.contextmanager
def _runtime_sync_lock(timeout: float = RUNTIME_LOCK_TIMEOUT):
    lock_path = TEMP_ROOT / "img2img-runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    locked = False
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            while time.monotonic() < deadline:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.2)
        else:
            locked = True
        if not locked:
            raise TimeoutError("timed out waiting for runtime update lock")
        yield
    finally:
        if locked and os.name == "nt":
            try:
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        stream.close()


def _download_wait_deadline() -> float:
    raw_timeout = _read_info_json().get("download_wait_timeout_minutes", 9)
    try:
        timeout_minutes = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_minutes = 9.0
    local_deadline = float("inf") if timeout_minutes < 0 else time.time() + timeout_minutes * 60.0
    try:
        task_deadline = float(os.environ.get("LOCAL_IMG2IMG_DEADLINE_EPOCH", "inf"))
    except ValueError:
        task_deadline = float("inf")
    return min(local_deadline, task_deadline)


def _remaining_task_timeout(default: float) -> float:
    try:
        deadline = float(os.environ.get("LOCAL_IMG2IMG_DEADLINE_EPOCH", "inf"))
    except ValueError:
        return default
    return max(0.1, min(default, deadline - time.time()))


def _request_server_start(dog_conn, log_path: Path | None) -> None:
    env = _read_info_json()
    venv_name = env.get("venv_name", "img2img")
    mem_need_gb = float(env.get("mem_need_gb", 8.0))
    server_alive_timeout = env.get("server_alive_timeout", 300)
    venv_python = str(OPENVINO_ROOT / "venv" / venv_name / "Scripts" / "pythonw.exe")
    extra_env = {
        "PYTHONPATH": f"{TEMP_BIN};{os.environ.get('PYTHONPATH', '')}",
        "PATH": f"{TEMP_BIN}\\openvino_genai;{os.environ.get('PATH', '')}",
        "OPENVINO_TELEMETRY_OPT_OUT": "1",
        "LOCAL_IMG2IMG_DATA_DIR": str(OPENVINO_ROOT),
        "LOCAL_IMG2IMG_MODEL_DIR": os.environ.get("LOCAL_IMG2IMG_MODEL_DIR", ""),
    }
    payload = {
        "op": "start_server",
        "skill_name": SKILL_NAME,
        "server_path": str(TEMP_SERVER),
        "venv_python": venv_python,
        "pipe_address": PIPE_ADDRESS,
        "authkey": AUTHKEY.decode("latin-1"),
        "mem_need_gb": mem_need_gb,
        "server_alive_timeout": server_alive_timeout,
        "server_unreachable_max_checks": int(env.get("server_unreachable_max_checks", 3)),
        "claw_name": _detect_claw_name(),
        "extra_env": extra_env,
    }
    _log_message(log_path, f"start_server -> dog (mem_need_gb={mem_need_gb})")
    reply: dict | None = None
    try:
        dog_conn.send(payload)
        # Starting a fresh Python/OpenVINO process is a cold-start operation,
        # not an ordinary IPC round trip. Keep status/keepalive calls at the
        # short IPC timeout, but allow imports and pipe creation to finish here.
        reply = _recv_with_timeout(
            dog_conn,
            _config_float("server_start_timeout_seconds", DEFAULT_SERVER_START_TIMEOUT),
            "server-dog start",
        )
    finally:
        try:
            dog_conn.close()
        except Exception:
            pass
    if not isinstance(reply, dict) or not reply.get("ok"):
        err = (reply or {}).get("error", "<unknown>")
        if err == "not_enough_memory":
            print("系统资源不足, 无法启动该技能", file=sys.stderr)
        elif err == "not_enough_memory_busy":
            print("系统资源不足，且现有推理服务正在处理任务；为避免误杀进程，本次不启动。", file=sys.stderr)
        raise RuntimeError(f"start_server failed: {err}")
    _log_message(log_path, f"dog spawned server pid={reply.get('pid')}")


def _send_keepalive(dog_conn, log_path: Path | None) -> None:
    try:
        dog_conn.send({"op": "keepalive", "skill_name": SKILL_NAME})
        try:
            _recv_with_timeout(dog_conn, _config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT), "server-dog keepalive")
        except Exception:
            pass
    except Exception as exc:
        _log_message(log_path, f"keepalive failed: {exc}")
    finally:
        try:
            dog_conn.close()
        except Exception:
            pass


def _sync_runtime_scripts(log_path: Path | None) -> None:
    src_server = Path(__file__).with_name("server.py")
    src_model_download = Path(__file__).with_name("model_download.py")
    src_info_json = Path(__file__).resolve().parent.parent / "info.json"
    src_dog = Path(__file__).with_name("server-dog.py")
    src_get_gpu_mem = Path(__file__).with_name("get_gpu_mem.py")
    src_runtime = Path(__file__).resolve().parent.parent / "bin" / "openvino_genai"
    temp_runtime = TEMP_BIN / "openvino_genai"
    runtime_scripts = [
        (src_server, TEMP_SERVER),
        (src_model_download, TEMP_MODEL_DOWNLOAD),
        (src_info_json, TEMP_INFO_JSON),
        (src_dog, TEMP_DOG),
        (src_get_gpu_mem, TEMP_GET_GPU_MEM),
    ]
    with _runtime_sync_lock():
        runtime_outdated = _dir_needs_sync(src_runtime, temp_runtime)
        if not runtime_outdated and not any(_needs_sync(src, dst) for src, dst in runtime_scripts):
            return

        # Never replace shared runtime files while an existing server may be
        # generating. Ask for a bounded graceful shutdown; a busy server makes
        # the update fail fast and the caller can retry later.
        existing = _try_connect()
        if existing is not None:
            status = _send(existing, {"op": "status"}, log_path)
            if status.get("state") == "generating":
                raise RuntimeError("runtime update deferred: image generation is active")
            _log_message(log_path, "shutting down idle server before runtime refresh")
            _cmd_shutdown(DEFAULT_SHUTDOWN_TIMEOUT, log_path)
            deadline = time.time() + DEFAULT_SHUTDOWN_TIMEOUT
            while time.time() < deadline and _try_connect() is not None:
                time.sleep(0.2)
            if _try_connect() is not None:
                raise RuntimeError("runtime update aborted: server did not stop")
        _terminate_legacy_dog(log_path)

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        for src, dst in runtime_scripts:
            last_err: Exception | None = None
            for _ in range(5):
                try:
                    shutil.copy2(src, dst)
                    last_err = None
                    break
                except PermissionError as exc:
                    last_err = exc
                    time.sleep(0.3)
            if last_err is not None:
                raise RuntimeError(f"failed to update {dst} (still in use): {last_err}")
        if runtime_outdated:
            TEMP_BIN.mkdir(parents=True, exist_ok=True)
            _copy_runtime_tree(src_runtime, temp_runtime, log_path)
        _log_message(log_path, f"refreshed runtime scripts under {TEMP_ROOT}")


def _ensure_server(log_path: Path | None = None, task_deadline: float = float("inf")):
    _sync_runtime_scripts(log_path)
    conn = _try_connect()
    if conn is not None:
        _log_message(log_path, "connected to existing server")
        ka = _try_dog_connect()
        if ka is not None:
            _send_keepalive(ka, log_path)
        return conn
    _log_message(log_path, "server not running; asking dog to start it")
    dog = _ensure_dog(log_path)
    _request_server_start(dog, log_path)
    deadline = min(time.time() + SERVER_BOOT_TIMEOUT, task_deadline)
    while time.time() < deadline:
        conn = _try_connect()
        if conn is not None:
            _log_message(log_path, "background server became ready")
            return conn
        time.sleep(SERVER_BOOT_POLL_INTERVAL)
    raise RuntimeError(f"img2img server did not come up within {SERVER_BOOT_TIMEOUT:.0f}s")


# Download progress is surfaced to the caller (and therefore to the agent
# talking to the user): the first line as soon as the server reports it, then a
# refresh every 5 minutes, so a multi-GB first run is neither a silent wait nor
# a flood of lines. _PROGRESS_STATE also carries the throttle's last-shown time.
_PROGRESS_STATE: dict = {}


def _format_download_progress(progress) -> str:
    try:
        from model_download import format_download_progress
    except Exception:  # noqa: BLE001 — progress display must never break the client
        return "模型下载中..."
    return format_download_progress(progress)


def _should_display_progress(state) -> bool:
    try:
        from model_download import should_display_progress
    except Exception:  # noqa: BLE001 — throttling must never break the client
        return True
    return should_display_progress(state)


def _report_download_progress(reply, log_path: Path | None = None, force: bool = False) -> None:
    progress = reply.get("progress") if isinstance(reply, dict) else None
    if progress is not None:
        signature = json.dumps(
            {key: progress.get(key) for key in ("model_id", "model_index", "downloaded_bytes", "total_bytes")},
            sort_keys=True,
            default=str,
        )
        if signature != _PROGRESS_STATE.get("progress_signature"):
            _PROGRESS_STATE["progress_signature"] = signature
            _PROGRESS_STATE["progress_changed_at"] = time.time()
        _PROGRESS_STATE["progress"] = progress
    elif not force:
        return
    line = _format_download_progress(progress or _PROGRESS_STATE.get("progress"))
    if not force and line == _PROGRESS_STATE.get("line"):
        return
    # First line shows immediately, then at most one line per 5 minutes, so a
    # multi-GB download reports in periodically instead of flooding the caller.
    if not force and not _should_display_progress(_PROGRESS_STATE):
        return
    _PROGRESS_STATE["line"] = line
    print(line, flush=True)
    _log_message(log_path, line)


def _wait_for_running(log_path: Path | None, deadline: float) -> tuple[str, str]:
    """Poll server status until it reaches 'running', 'error', or the deadline."""
    last_progress_signature = _PROGRESS_STATE.get("progress_signature")
    saved_progress_at = float(_PROGRESS_STATE.get("progress_changed_at", time.time()))
    last_progress_at = time.monotonic() - max(0.0, time.time() - saved_progress_at)
    no_progress_timeout = _config_float("no_progress_timeout_minutes", 15.0) * 60.0
    while True:
        now = time.time()
        if now >= deadline:
            _log_message(log_path, "wait_for_running: timed out")
            return ("timeout", "")
        conn = _try_connect()
        if conn is None:
            _log_message(log_path, "wait_for_running: no server; ensuring")
            try:
                _ensure_server(log_path, deadline)
            except RuntimeError as exc:
                return ("error", str(exc))
            time.sleep(STATUS_POLL_INTERVAL)
            continue
        try:
            reply = _send(conn, {"op": "status"}, log_path)
        except (TimeoutError, EOFError, OSError) as exc:
            return ("error", f"server status unavailable: {exc}")
        state = reply.get("state") if isinstance(reply, dict) else None
        _log_message(log_path, f"wait_for_running: state={state}")
        _report_download_progress(reply, log_path)
        current_progress = reply.get("progress") or {}
        progress_signature = json.dumps(
            {key: current_progress.get(key) for key in ("model_id", "model_index", "downloaded_bytes", "total_bytes")},
            sort_keys=True,
            default=str,
        )
        if progress_signature != last_progress_signature:
            last_progress_signature = progress_signature
            last_progress_at = time.monotonic()
        elif state in {"starting", "downloading", "loading"} and time.monotonic() - last_progress_at >= no_progress_timeout:
            return ("error", f"model initialization made no observable progress for {no_progress_timeout / 60:.0f} minutes")
        if state == "running":
            return ("running", "")
        if state == "error":
            return ("error", reply.get("error", "<unknown init error>"))
        remaining = deadline - time.time()
        if remaining <= 0:
            return ("timeout", "")
        interval = (
            PROGRESS_POLL_INTERVAL
            if _PROGRESS_STATE.get("progress") is not None
            else STATUS_POLL_INTERVAL
        )
        time.sleep(min(interval, remaining))


def _send(conn, msg: dict, log_path: Path | None = None, timeout: float | None = None) -> dict:
    payload = dict(msg)
    _log_message(log_path, f"sending op={payload.get('op')!r}")
    try:
        conn.send(payload)
        effective_timeout = timeout or _config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT)
        reply = _recv_with_timeout(conn, effective_timeout, str(payload.get("op") or "request"))
    finally:
        conn.close()
    if isinstance(reply, dict):
        _log_message(log_path, f"received reply for op={payload.get('op')!r}: ok={reply.get('ok')}")
    else:
        _log_message(log_path, f"received non-dict reply for op={payload.get('op')!r}")
    return reply


def _stop_server_via_dog(log_path: Path | None = None) -> bool:
    dog = _try_dog_connect()
    if dog is None:
        return False
    try:
        dog.send({"op": "stop_server", "skill_name": SKILL_NAME})
        reply = _recv_with_timeout(dog, _config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT), "stop_server")
        return isinstance(reply, dict) and bool(reply.get("ok"))
    except (TimeoutError, EOFError, OSError) as exc:
        _log_message(log_path, f"failed to stop server: {exc}")
        return False
    finally:
        dog.close()


def _format_timing(t: dict) -> str:
    parts = []
    if "load_s" in t:
        parts.append(f"加载: {t.get('load_s', 0.0):.3f}秒")
    if "infer_s" in t:
        parts.append(f"推理: {t.get('infer_s', 0.0):.3f}秒")
    if "save_s" in t:
        parts.append(f"保存: {t.get('save_s', 0.0):.3f}秒")
    total = t.get("total_s", t.get("total", 0.0))
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"耗时: {float(total):.3f} 秒{detail}"


def _print_request_reply(reply: dict) -> int:
    if not reply.get("ok", False):
        print(Fore.RED + "❌ 服务器处理失败:" + Style.RESET_ALL)
        print(reply.get("error", "<unknown error>"))
        return 1

    if not reply.get("success", False):
        print(Fore.RED + "❌ 图片生成失败:" + Style.RESET_ALL)
        print(reply.get("error", reply.get("error_code", "<unknown error>")))
        return 1

    image_path = reply.get("image_path")
    if image_path:
        print(Style.BRIGHT + Fore.GREEN + "✅ 图片已修改: " + Style.RESET_ALL + str(image_path))
    reference_image_path = reply.get("reference_image_path")
    if reference_image_path:
        print(f"  原图:   {reference_image_path}")
    prompt = reply.get("prompt")
    if prompt:
        print(f"  提示词: {prompt}")
    seed = reply.get("seed")
    if seed is not None:
        print(f"  种子:   {seed}")
    w = reply.get("width")
    h = reply.get("height")
    steps = reply.get("steps")
    if w and h and steps:
        print(f"  参数:   {w}x{h}, steps={steps}")
    device = reply.get("device")
    if device:
        print(f"  设备:   {device}")

    print(_format_timing(reply.get("timing", {})))
    return 0


def _cmd_status(log_path: Path | None = None) -> int:
    _log_message(log_path, "checking server status")
    conn = _try_connect()
    if conn is None:
        _log_message(log_path, "status check: server not running")
        print("server not running")
        return 0
    try:
        reply = _send(conn, {"op": "status"}, log_path)
    except (TimeoutError, EOFError, OSError) as exc:
        print(Fore.RED + f"status failed: {exc}" + Style.RESET_ALL)
        return 2
    if not reply.get("ok", False):
        _log_message(log_path, f"status failed: {reply.get('error', '<unknown>')}")
        print(Fore.RED + "status failed:" + Style.RESET_ALL, reply.get("error", "<unknown>"))
        return 1
    _log_message(log_path, f"status ok: pid={reply.get('pid')}")
    print(
        f"state:   {reply.get('state')}\n"
        f"pid:     {reply.get('pid')}\n"
        f"uptime:  {reply.get('uptime_s', 0.0):.1f}s\n"
        f"model:   {reply.get('loaded_model_id') or 'not_loaded'}\n"
        f"device:  {reply.get('loaded_device') or 'none'}"
    )
    return 0


def _cmd_shutdown(timeout: float, log_path: Path | None = None) -> int:
    _log_message(log_path, f"requesting server shutdown with timeout={timeout:.1f}s")
    conn = _try_connect()
    if conn is None:
        _log_message(log_path, "shutdown request skipped: server not running")
        print("server not running")
        return 0
    try:
        reply = _send(conn, {"op": "shutdown", "timeout": timeout}, log_path)
    except (TimeoutError, EOFError, OSError) as exc:
        print(Fore.RED + f"shutdown failed: {exc}" + Style.RESET_ALL)
        return 2
    if not reply.get("ok", False):
        _log_message(log_path, f"shutdown failed: {reply.get('error', '<unknown>')}")
        print(Fore.RED + "shutdown failed:" + Style.RESET_ALL, reply.get("error", "<unknown>"))
        return 1
    _log_message(log_path, "shutdown request accepted by server")
    print(f"server shutting down (grace period: {timeout:.1f}s)")
    return 0


def _is_abnormal_image(reply: dict, log_path: Path | None) -> bool:
    """Return True when a reported output is missing or is not a valid image."""
    if not isinstance(reply, dict):
        return False
    if not reply.get("ok", False) or not reply.get("success", False):
        return False
    image_path = reply.get("image_path")
    if not image_path:
        return False
    try:
        from PIL import Image  # type: ignore

        output_path = Path(image_path)
        if not output_path.is_file():
            _log_message(log_path, f"generated image is missing: {output_path}")
            return True
        with Image.open(output_path) as image:
            width, height = image.size
            image.verify()
        if width <= 0 or height <= 0:
            _log_message(log_path, f"generated image has invalid dimensions: {width}x{height}")
            return True
        return False
    except Exception as exc:  # noqa: BLE001 - any decode failure means a bad output
        _log_message(log_path, f"generated image validation failed: {image_path}: {exc}")
        return True


def _delete_image(image_path: str | None, log_path: Path | None) -> None:
    if not image_path:
        return
    try:
        Path(image_path).unlink()
        _log_message(log_path, f"deleted abnormal image {image_path}")
    except OSError as exc:
        _log_message(log_path, f"failed to delete abnormal image {image_path}: {exc}")


def _humanize_init_error(detail: str) -> str:
    text = detail or ""
    lowered = text.lower()
    if (
        "m_weights" in text
        or "bin file cannot be found" in lowered
        or "empty weights" in lowered
        or "missing weight bins" in lowered
        or "missing files:" in lowered
    ):
        return (
            "模型权重不完整（缺少或为空的 .bin），不是 Python 依赖安装失败。"
            "下次运行会自动重新下载模型。\n"
            f"{text}"
        )
    if "install-env" in lowered or "no module named" in lowered:
        return (
            "运行环境依赖缺失。请重新执行专家的标准 Bash 入口，它会自动修复一次环境。\n"
            f"{text}"
        )
    if "no opencl gpu device available" in lowered:
        return (
            "Intel OpenCL GPU 当前不可用，模型和图片本身没有问题。"
            "已完成一次有界重启仍未恢复；请关闭占用 GPU 的程序，或重启系统并检查 Intel 显卡/OpenCL 驱动后再试。"
            "本专家不会静默切换到 CPU 或在线服务。\n"
            f"{text}"
        )
    return text


def _is_gpu_unavailable_error(detail: str) -> bool:
    return "no opencl gpu device available" in (detail or "").lower()


def _cmd_request(
    reference_image_path: str,
    prompt: str,
    request_id: str,
    output_dir: str | None = None,
    log_path: Path | None = None,
    scene: str | None = None,
) -> int:
    reference_image = Path(reference_image_path).expanduser()
    if not reference_image.exists() or not reference_image.is_file():
        print(Fore.RED + f"reference image does not exist: {reference_image}" + Style.RESET_ALL)
        return 1

    _log_message(log_path, f"processing request_id={request_id} image_name={reference_image.name!r} prompt_sha256={hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}")
    print("正在启动推理服务（server-dog / img2img server）...", flush=True)

    deadline = _download_wait_deadline()
    try:
        _ensure_server(log_path, deadline)
    except RuntimeError as exc:
        _log_message(log_path, f"failed to ensure server: {exc}")
        print(Fore.RED + _humanize_init_error(str(exc)) + Style.RESET_ALL)
        return 2

    print("等待模型就绪（下载或加载到 GPU）...", flush=True)
    outcome, detail = _wait_for_running(log_path, deadline)
    for attempt in range(2, ERROR_RETRY_MAX + 1):
        if outcome != "error":
            break
        # One delayed restart may recover a transient GPU reset. Repeating a
        # deterministic OpenCL-device failure only creates a slow error loop.
        gpu_unavailable = _is_gpu_unavailable_error(detail)
        if gpu_unavailable and attempt > 2:
            break
        _log_message(
            log_path,
            f"init error on attempt {attempt - 1}/{ERROR_RETRY_MAX}; restarting server: {detail}",
        )
        print(Fore.YELLOW + f"server init failed (attempt {attempt - 1}/{ERROR_RETRY_MAX}), restarting..." + Style.RESET_ALL)
        _cmd_shutdown(DEFAULT_SHUTDOWN_TIMEOUT, log_path)
        pipe_deadline = min(time.time() + 5.0, deadline)
        while time.time() < pipe_deadline and _try_connect() is not None:
            time.sleep(0.2)
        if time.time() >= deadline:
            outcome = "timeout"
            break
        retry_gap = GPU_UNAVAILABLE_RETRY_GAP if gpu_unavailable else ERROR_RETRY_GAP
        time.sleep(min(retry_gap, max(0.0, deadline - time.time())))
        if time.time() >= deadline:
            outcome = "timeout"
            break
        try:
            _ensure_server(log_path, deadline)
        except RuntimeError as exc:
            outcome, detail = "error", str(exc)
            continue
        outcome, detail = _wait_for_running(log_path, deadline)

    if outcome == "timeout":
        _save_pending_request(request_id, str(reference_image), prompt, output_dir, log_path, scene)
        if _PROGRESS_STATE.get("progress") is not None:
            _report_download_progress({}, log_path, force=True)
        print(Fore.YELLOW + f"模型正在下载，请保持当前 Bash 任务运行；手动恢复时使用 `bash scripts/run.sh --continue {request_id}`" + Style.RESET_ALL)
        print(
            Fore.YELLOW
            + "（标准 Bash 入口会自动续传，无需单独调用 client.py）"
            + Style.RESET_ALL
        )
        _log_message(log_path, "exiting with code 3: download still in progress")
        return 3

    if outcome == "error":
        _log_message(log_path, f"server reports init error after retries: {detail}")
        print(Fore.RED + "❌ 服务器初始化失败:" + Style.RESET_ALL)
        print(_humanize_init_error(detail))
        _delete_pending_request(request_id, log_path)
        lowered_detail = detail.lower()
        if any(marker in lowered_detail for marker in (
            "no module named", "importerror", "dll load failed", "winerror 1114",
            "bad magic number", "invalid distribution", "entry point",
        )):
            return 4
        return 1

    # outcome == "running"
    print("模型已就绪，开始生成...", flush=True)
    try:
        reply: dict = {}
        for attempt in range(1, ABNORMAL_IMAGE_RETRY_MAX + 1):
            busy_attempt = 0
            busy_deadline = min(
                deadline,
                time.time() + _config_float("server_busy_timeout_seconds", DEFAULT_SERVER_BUSY_TIMEOUT),
            )
            while True:
                conn = _try_connect()
                if conn is None:
                    _log_message(log_path, "connection to server lost immediately after ready state")
                    print(Fore.RED + "lost connection to server" + Style.RESET_ALL)
                    return 2
                _log_message(
                    log_path,
                    f"generate request_id={request_id} image_name={reference_image.name!r} "
                    f"(attempt {attempt}/{ABNORMAL_IMAGE_RETRY_MAX}, busy_wait={busy_attempt})",
                )
                try:
                    reply = _send(
                        conn,
                        {
                            "op": "generate",
                            "reference_image_path": str(reference_image),
                            "prompt": prompt,
                            "scene": scene,
                            "request_id": request_id,
                            "client_pid": os.getpid(),
                            "output_dir": output_dir,
                        },
                        log_path,
                        timeout=_remaining_task_timeout(_config_float("generation_timeout_seconds", DEFAULT_GENERATION_TIMEOUT)),
                    )
                except (TimeoutError, EOFError, OSError) as exc:
                    _log_message(log_path, f"generation transport failed: {exc}; stopping server")
                    _stop_server_via_dog(log_path)
                    print(Fore.RED + f"❌ 推理服务超时或失联: {exc}" + Style.RESET_ALL)
                    return 2
                if reply.get("error_code") != "SERVER_BUSY":
                    break
                busy_attempt += 1
                if time.time() >= busy_deadline:
                    print(Fore.RED + "❌ 推理服务持续繁忙，已达到有界等待上限" + Style.RESET_ALL)
                    return 2
                if busy_attempt == 1:
                    print("推理服务正在处理另一张图片，等待当前任务完成...", flush=True)
                time.sleep(min(SERVER_BUSY_RETRY_GAP, max(0.1, busy_deadline - time.time())))

            if not _is_abnormal_image(reply, log_path):
                break

            _delete_image(reply.get("image_path"), log_path)
            if attempt >= ABNORMAL_IMAGE_RETRY_MAX:
                _log_message(log_path, "abnormal image persisted after max retries")
                print(Fore.RED + "❌ 本地图生图服务异常" + Style.RESET_ALL)
                return 1

            # A malformed file does not prove that the loaded model is broken.
            # Retry on the resident pipeline first to avoid an expensive reload.
            _log_message(log_path, "retrying generation on the resident pipeline")

        rc = _print_request_reply(reply)
        _log_message(log_path, f"generate exit_code={rc}")
    finally:
        _delete_pending_request(request_id, log_path)
    return rc


def _cmd_continue(request_id: str | None, output_dir: str | None, log_path: Path | None = None) -> int:
    try:
        pending = _load_pending_request(request_id)
    except RuntimeError as exc:
        print(Fore.RED + str(exc) + Style.RESET_ALL)
        return 1
    if pending is None:
        _log_message(log_path, "--continue invoked but no pending request file found")
        print(Fore.RED + "无待处理请求，请先使用专家的标准 Bash 入口发起请求" + Style.RESET_ALL)
        return 1
    saved_image = pending.get("reference_image_path", "")
    saved_prompt = pending.get("prompt", "")
    saved_output_dir = output_dir or pending.get("output_dir")
    saved_log = _normalize_log_path(pending.get("log_path")) or log_path
    saved_request_id = _safe_request_id(pending.get("request_id"))
    if pending.get("progress_signature"):
        _PROGRESS_STATE["progress_signature"] = pending["progress_signature"]
        _PROGRESS_STATE["progress_changed_at"] = pending.get("progress_changed_at", time.time())
    _log_message(saved_log, f"--continue resuming request_id={saved_request_id}")
    return _cmd_request(
        saved_image,
        saved_prompt,
        saved_request_id,
        saved_output_dir,
        saved_log,
        pending.get("scene"),
    )


def _cmd_cancel(request_id: str, log_path: Path | None = None) -> int:
    _delete_pending_request(request_id, log_path)
    conn = _try_connect()
    if conn is None:
        print("任务已取消；推理服务未运行。")
        return 0
    try:
        reply = _send(
            conn,
            {"op": "cancel", "request_id": request_id},
            log_path,
            timeout=_config_float("ipc_timeout_seconds", DEFAULT_IPC_TIMEOUT),
        )
    except (TimeoutError, EOFError, OSError) as exc:
        _log_message(log_path, f"request-scoped cancel failed: {exc}")
        print("任务取消请求未确认；共享推理服务保持运行。")
        return 1
    if not isinstance(reply, dict) or not reply.get("ok"):
        _log_message(log_path, f"request-scoped cancel rejected: {reply}")
        print("任务取消请求被拒绝；共享推理服务保持运行。")
        return 1
    if reply.get("cancel_pending"):
        print("任务已取消；当前推理完成后会丢弃该任务输出，共享服务保持运行。")
    else:
        print("任务已取消；该任务未在推理，共享服务保持运行。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="local-img2img CLI client")
    parser.add_argument(
        "-i", "--input",
        type=str, default=None,
        help="prompt describing how to edit the image",
    )
    parser.add_argument(
        "--image-path",
        type=str,
        default=None,
        help="source image path to edit",
    )
    parser.add_argument("--request-file", type=str, default=None, help="structured JSON request file")
    parser.add_argument(
        "--consume-request-file",
        action="store_true",
        help="remove the request file immediately after successful validation",
    )
    parser.add_argument(
        "--log",
        type=str,
        default=None,
        help="append debug logs to this file",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server-status", action="store_true", help="query server status")
    group.add_argument("--server-shutdown", action="store_true", help="request server shutdown")
    group.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="resume the last pending request (used after a download timeout exit)",
    )
    group.add_argument("--cancel", action="store_true", help="cancel one request without stopping the shared server")
    parser.add_argument("--request-id", type=str, default=None, help="UUID used to isolate pending and concurrent requests")
    parser.add_argument("--output-dir", type=str, default=None, help="absolute directory for the generated image")
    parser.add_argument(
        "--server-shutdown-timeout",
        type=float, default=DEFAULT_SHUTDOWN_TIMEOUT,
        help=f"grace period for in-flight work on shutdown (default: {DEFAULT_SHUTDOWN_TIMEOUT:.0f}s)",
    )
    args = parser.parse_args()
    log_path = _normalize_log_path(args.log)
    _cleanup_old_logs()
    if args.request_id:
        try:
            args.request_id = _safe_request_id(args.request_id)
        except ValueError as exc:
            print(Fore.RED + str(exc) + Style.RESET_ALL)
            return 1

    if log_path is not None:
        _log_message(log_path, f"client started with argv={sys.argv[1:]}")

    try:
        if args.server_status:
            exit_code = _cmd_status(log_path)
        elif args.server_shutdown:
            exit_code = _cmd_shutdown(args.server_shutdown_timeout, log_path)
        elif args.cancel:
            if not args.request_id:
                print(Fore.RED + "--cancel requires --request-id" + Style.RESET_ALL)
                return 1
            exit_code = _cmd_cancel(_safe_request_id(args.request_id), log_path)
        elif args.cont:
            exit_code = _cmd_continue(args.request_id, args.output_dir, log_path)
        else:
            scene = None
            if args.request_file is not None:
                args.image_path, args.input, scene = _load_request_file(
                    args.request_file,
                    args.consume_request_file,
                    str(Path(args.output_dir).expanduser().parent) if args.output_dir else None,
                )
            elif args.image_path is None or args.input is None:
                print(Fore.RED + "usage: client.py --request-file <request.json> | --image-path \"<image>\" -i \"<prompt>\" | --continue" + Style.RESET_ALL)
                return 1
            exit_code = _cmd_request(
                args.image_path,
                args.input,
                _safe_request_id(args.request_id),
                args.output_dir,
                log_path,
                scene,
            )
    except ValueError as exc:
        print(Fore.RED + str(exc) + Style.RESET_ALL)
        return 1

    _log_message(log_path, f"client exiting with code {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
