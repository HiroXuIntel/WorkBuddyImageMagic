"""Persistent local-img2img server.

Listens on the Windows named pipe ``\\\\.\\pipe\\img2img`` via
``multiprocessing.connection.Listener`` and dispatches image-to-image
requests from ``client.py``. Keeps an OpenVINO FLUX.2 klein pipeline
resident across invocations so each generate avoids cold-start cost.

State machine (same shape as ``skills/local-computer-use/src/scripts/server.py``):

    starting -> downloading -> loading -> running
                                        |-> error

Supported ops::

    status   -> {ok, state, pid, uptime_s, loaded_model_id, loaded_device}
    shutdown -> {ok, state: "shutting_down"}
    generate -> {ok, success, image_path, reference_image_path, prompt, seed, steps,
                 width, height, device, timing: {...}}
"""

from __future__ import annotations

import argparse
import gc
import os
import random
import sys
import threading
import time
import traceback
import uuid
from multiprocessing.connection import Client as PipeClient, Listener
from pathlib import Path
from typing import Any, Optional

_DLL_DIRECTORY_HANDLES: list[Any] = []
_BOOTSTRAP_HERE = Path(__file__).resolve().parent
for _bin_root in (_BOOTSTRAP_HERE / "bin", _BOOTSTRAP_HERE.parent / "bin"):
    _genai_package = _bin_root / "openvino_genai"
    if not _genai_package.is_dir():
        continue
    _bin_text = str(_bin_root)
    if _bin_text not in sys.path:
        sys.path.insert(0, _bin_text)
    os.environ["PATH"] = f"{_genai_package}{os.pathsep}{os.environ.get('PATH', '')}"
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        try:
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(_genai_package)))
        except OSError:
            pass
    break

from model_download import (
    ensure_models,
    get_download_progress,
    invalidate_model_dir,
    load_skill_info,
    load_model_infos,
    validate_installed_model,
)

PIPE_ADDRESS = r"\\.\pipe\photo-magic-img2img-v1"
AUTHKEY = b"photo-magic-img2img-v1"
DOG_PIPE_ADDRESS = r"\\.\pipe\photo-magic-server-dog-v1"
DOG_AUTHKEY = b"photo-magic-server-dog-v1"
SKILL_NAME = "photo-magic-local-img2img-v1"
DEFAULT_SHUTDOWN_TIMEOUT = 10.0
STATE_STARTING = "starting"
STATE_DOWNLOADING = "downloading"
STATE_LOADING = "loading"
STATE_RUNNING = "running"
STATE_GENERATING = "generating"
STATE_ERROR = "error"

OPENVINO_ROOT = Path(
    os.environ.get(
        "LOCAL_IMG2IMG_DATA_DIR",
        str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".openvino" / "photo-magic"),
    )
).expanduser().resolve()
USER_OPENVINO_ROOT = (
    Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".openvino"
).expanduser().resolve()
_HERE = Path(__file__).resolve().parent
INFO_JSON = _HERE / "info.json" if (_HERE / "info.json").exists() else _HERE.parent / "info.json"
MODEL_INFOS = load_model_infos(INFO_JSON)
MODEL_INFO = MODEL_INFOS[0]
MODEL_ID = MODEL_INFO.model_id
_configured_model_dir = os.environ.get("LOCAL_IMG2IMG_MODEL_DIR", "").strip()
_model_candidates = (
    *((Path(_configured_model_dir).expanduser().resolve(),) if _configured_model_dir else ()),
    USER_OPENVINO_ROOT / "models" / MODEL_INFO.dir_name,
    USER_OPENVINO_ROOT / "photo-magic" / "models" / MODEL_INFO.dir_name,
)
# Reuse the first existing model in place. If none exists, download to the
# shared ~/.openvino/models location. No model copy or migration is performed.
MODEL_DIR = next(
    (candidate for candidate in _model_candidates if candidate.is_dir()),
    USER_OPENVINO_ROOT / "models" / MODEL_INFO.dir_name,
)
MODELS_ROOT = MODEL_DIR.parent
REQUIRED_MODEL_FILES = MODEL_INFO.required_files
LEGACY_MODELS_ROOTS: tuple[Path, ...] = ()
SKILL_CONFIG = load_skill_info(INFO_JSON)
MAX_INFERENCE_SIDE = max(256, int(SKILL_CONFIG.get("max_inference_side", 1024)))
MAX_INPUT_PIXELS = max(1_000_000, int(SKILL_CONFIG.get("max_input_pixels", 80_000_000)))
MAX_INFERENCE_STEPS = max(1, int(SKILL_CONFIG.get("max_inference_steps", 20)))
MAX_PROMPT_CHARS = max(100, int(SKILL_CONFIG.get("max_prompt_chars", 4000)))
SUPPORTED_SCENES = frozenset(SKILL_CONFIG.get("supported_scenarios", ()))
NO_PROGRESS_TIMEOUT = max(60.0, float(SKILL_CONFIG.get("no_progress_timeout_minutes", 15)) * 60.0)

DEFAULT_STEPS = 4
DEFAULT_GUIDANCE = 1.0
_INTEL_VENDOR_ID = "0x8086"

SERVER_VERSION = "0.3.2"

os.environ.setdefault("OPENVINO_TELEMETRY_OPT_OUT", "1")

for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

LOG_LOCK = threading.Lock()

def _normalize_log_path(log_path: str | None) -> Path | None:
    if not log_path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = str(OPENVINO_ROOT / "log" / f"img2img-server-py-{timestamp}.log")

    path = Path(log_path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _log_message(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_LOCK:
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"[{timestamp}] [server pid={os.getpid()}] {message}\n")
    except OSError:
        pass


def _report_download_message(log_path: Path | None, message: str) -> None:
    print(f"[server] {message}", flush=True)
    _log_message(log_path, message)


def _notify_dog_activity(log_path: Path | None) -> None:
    """Best-effort activity refresh without delaying image generation."""
    def _send() -> None:
        try:
            conn = PipeClient(DOG_PIPE_ADDRESS, authkey=DOG_AUTHKEY)
            try:
                conn.send({"op": "keepalive", "skill_name": SKILL_NAME})
                if conn.poll(1.0):
                    conn.recv()
            finally:
                conn.close()
        except (FileNotFoundError, OSError, EOFError) as exc:
            _log_message(log_path, f"server-dog activity refresh unavailable: {exc}")

    threading.Thread(target=_send, daemon=True, name="dog-activity").start()


def _ensure_required_model(log_path: Path | None = None) -> None:
    """Reuse the stable model directory, downloading only when it is absent."""
    # Product policy: discovering the existing directory is sufficient for
    # reuse. Do not copy it into plugin data and do not turn validation drift
    # into an eager re-download. A genuinely broken payload will fail during
    # pipeline loading and the existing recovery path will invalidate it.
    if MODEL_DIR.is_dir():
        _log_message(log_path, f"reusing existing model directory: {MODEL_DIR}")
        return
    validation = validate_installed_model(MODEL_DIR, MODEL_INFO)
    if validation.ok:
        return

    def _logger(message: str):
        _report_download_message(log_path, message)

    ensure_models(
        MODEL_INFOS,
        MODELS_ROOT,
        logger=_logger,
        legacy_models_roots=LEGACY_MODELS_ROOTS,
    )

# ---------------------------------------------------------------------------
# OpenVINO GenAI pipeline helpers (FLUX.2 klein via openvino_genai)
# ---------------------------------------------------------------------------
def _load_pipeline(model_dir: Path, device: str) -> Any:
    """Build an ``openvino_genai.Image2ImagePipeline`` on the given device.

    ``openvino_genai`` is resolved from the ``bin`` directory the client copies
    into ``TEMP_DIR`` (exposed via ``PYTHONPATH`` / ``PATH`` when the server is
    spawned by server-dog).
    """
    try:
        import openvino_genai as ov_genai  # type: ignore
    except Exception as exc:
        raise ImportError(
            "openvino_genai is not importable. Ensure the skill 'bin' directory "
            "is on PYTHONPATH/PATH (set by the client when starting the server). "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc
    return ov_genai.Image2ImagePipeline(str(model_dir), device)


def _resolve_device() -> str:
    return "GPU"


def _tensor_to_image(image_tensor: Any) -> Optional[Any]:
    """Convert an ``openvino_genai`` image tensor into a PIL image."""
    import numpy as np  # type: ignore
    from PIL import Image  # type: ignore

    arr = np.array(image_tensor.data, copy=True)
    get_shape = getattr(image_tensor, "get_shape", None)
    if callable(get_shape):
        arr = arr.reshape(get_shape())
    if arr.ndim == 4:
        arr = arr[0]
    return Image.fromarray(arr.astype(np.uint8))


def _load_reference_tensor(reference_image_path: str) -> tuple[Any, int, int, int, int]:
    """Load an image at a bounded inference size while preserving its aspect ratio."""
    import numpy as np  # type: ignore
    import openvino as ov  # type: ignore
    from PIL import Image, ImageOps  # type: ignore

    with Image.open(reference_image_path) as image:
        raw_width, raw_height = image.size
        if raw_width <= 0 or raw_height <= 0 or raw_width * raw_height > MAX_INPUT_PIXELS:
            raise ValueError(
                f"image dimensions {raw_width}x{raw_height} exceed the {MAX_INPUT_PIXELS:,}-pixel safety limit"
            )
        image = ImageOps.exif_transpose(image)
        rgb = image.convert("RGB")
        width, height = rgb.size
        scale = min(1.0, MAX_INFERENCE_SIDE / float(max(width, height)))
        scaled_width = max(16, int(round(width * scale)))
        scaled_height = max(16, int(round(height * scale)))
        new_width = max(16, int(round(scaled_width / 16.0)) * 16)
        new_height = max(16, int(round(scaled_height / 16.0)) * 16)
        if width != new_width or height != new_height:
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else 1
            rgb = rgb.resize((new_width, new_height), resample)
        ref_arr = np.array(rgb)[None]
    return ov.Tensor(ref_arr), width, height, new_width, new_height


def _safe_seed() -> int:
    return random.SystemRandom().randint(0, 2**31 - 1)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class Server:
    def __init__(self, log_path: Path | None = None) -> None:
        self.start_time = time.time()
        self.shutdown_event = threading.Event()
        self.shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT
        self.listener: Listener | None = None
        self.runtime_lock = threading.Lock()
        self.generation_lock = threading.Lock()
        self.pipeline: Any = None
        self.loaded_model_id: str | None = None
        self.loaded_device: str | None = None
        self.log_path = log_path
        self.state: str = STATE_STARTING
        self.init_error: str = ""
        self.init_thread: threading.Thread | None = None
        self.active_request_id: str | None = None
        self.active_request_started_at: float | None = None
        self.cancelled_request_ids: set[str] = set()
        self.active_client_pid: int | None = None
        self.active_client_create_time: float | None = None

    def log(self, message: str) -> None:
        _log_message(self.log_path, message)

    def _watch_active_client(self, request_id: str) -> None:
        try:
            import psutil  # type: ignore
        except ImportError:
            return
        disappeared_at: float | None = None
        while not self.shutdown_event.wait(1.0):
            with self.runtime_lock:
                if self.active_request_id != request_id:
                    return
                if request_id in self.cancelled_request_ids:
                    self.log(f"client cancellation acknowledged for request_id={request_id}; preserving shared server")
                    return
                pid = self.active_client_pid
                expected_create_time = self.active_client_create_time
            try:
                if pid is None:
                    return
                proc = psutil.Process(pid)
                if expected_create_time is not None and abs(proc.create_time() - expected_create_time) <= 0.01:
                    disappeared_at = None
                    continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except psutil.AccessDenied:
                continue
            # TaskStop starts a separate client that sends a request-scoped
            # cancel. Give that message time to arrive before treating the
            # disappearance as an uncontrolled crash.
            if disappeared_at is None:
                disappeared_at = time.monotonic()
                self.log(f"active client disappeared for request_id={request_id}; awaiting scoped cancel")
                continue
            if time.monotonic() - disappeared_at < 3.0:
                continue
            with self.runtime_lock:
                if request_id in self.cancelled_request_ids:
                    self.log(f"late client cancellation acknowledged for request_id={request_id}; preserving shared server")
                    return
            self.log(f"active client disappeared for request_id={request_id}; terminating server to release inference resources")
            os._exit(125)

    def _init_runtime_worker(self) -> None:
        try:
            with self.runtime_lock:
                self.state = STATE_DOWNLOADING
            self.log("init thread: downloading required model")
            _ensure_required_model(self.log_path)

            with self.runtime_lock:
                self.state = STATE_LOADING
            self.log("init thread: loading OpenVINO FLUX.2 klein image-to-image pipeline")

            device = _resolve_device()
            self.log(f"init thread: resolved device={device}")

            pipeline = _load_pipeline(MODEL_DIR, device)

            with self.runtime_lock:
                self.pipeline = pipeline
                self.loaded_model_id = MODEL_ID
                self.loaded_device = device
                self.state = STATE_RUNNING
            self.log(f"init thread: pipeline ready on device={device}")
        except Exception as exc:
            error_text = traceback.format_exc()
            detail = str(exc)
            if (
                "m_weights" in detail
                or "bin file cannot be found" in detail.lower()
                or "empty weights" in detail.lower()
            ):
                try:
                    invalidate_model_dir(MODEL_DIR, MODELS_ROOT, logger=self.log)
                    self.log(
                        "init thread: incomplete model weights detected; "
                        "invalidated model dir so the next start re-downloads"
                    )
                except Exception:
                    self.log(
                        "init thread: failed to invalidate incomplete model:\n"
                        + traceback.format_exc()
                    )
            with self.runtime_lock:
                self.init_error = error_text
                self.state = STATE_ERROR
            self.log(f"init thread failed:\n{error_text}")

    def _start_init_thread(self) -> None:
        if self.init_thread is not None and self.init_thread.is_alive():
            return
        self.init_thread = threading.Thread(
            target=self._init_runtime_worker,
            name="runtime-init",
            daemon=True,
        )
        self.init_thread.start()
        self.log("init thread started")

    def _do_generate(
        self,
        reference_image_path: str,
        prompt: str,
        steps: int,
        seed: int,
        output_dir: str | None = None,
    ) -> dict:
        t0 = time.time()
        ref_path = Path(reference_image_path)
        requested_output_dir = Path(output_dir).expanduser() if output_dir else ref_path.parent
        if not requested_output_dir.is_absolute():
            return {
                "ok": True,
                "success": False,
                "error_code": "SAVE_FAILED",
                "error": "output_dir must be an absolute path",
                "reference_image_path": reference_image_path,
                "prompt": prompt,
                "seed": seed,
                "timing": {"load_s": 0.0, "infer_s": 0.0, "save_s": 0.0, "total_s": time.time() - t0},
            }
        try:
            requested_output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "ok": True,
                "success": False,
                "error_code": "SAVE_FAILED",
                "error": f"cannot create output directory: {type(exc).__name__}: {exc}",
                "reference_image_path": reference_image_path,
                "prompt": prompt,
                "seed": seed,
                "timing": {"load_s": 0.0, "infer_s": 0.0, "save_s": 0.0, "total_s": time.time() - t0},
            }
        
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{ref_path.stem}_edited_{timestamp}_{uuid.uuid4().hex[:8]}.png"
        output_path = requested_output_dir / filename

        load_start = time.time()
        try:
            ref_tensor, width, height, new_width, new_height = _load_reference_tensor(reference_image_path)
        except Exception as exc:
            return {
                "ok": True,
                "success": False,
                "error_code": "BAD_IMAGE",
                "error": f"{type(exc).__name__}: {exc}",
                "reference_image_path": reference_image_path,
                "prompt": prompt,
                "seed": seed,
                "timing": {"load_s": time.time() - load_start, "infer_s": 0.0, "save_s": 0.0, "total_s": time.time() - t0},
            }
        load_s = time.time() - load_start

        self.log(f"starting generation: original_size=({width}x{height}), resized_size=({new_width}x{new_height}), steps={steps}, seed={seed}") 
        infer_start = time.time()
        try:
            result = self.pipeline.generate(
                prompt,
                ref_tensor,
                width=int(new_width),
                height=int(new_height),
                num_inference_steps=int(steps),
                guidance_scale=float(DEFAULT_GUIDANCE),
                rng_seed=int(seed),
            )
        except Exception as exc:
            return {
                "ok": True,
                "success": False,
                "error_code": "GENERATION_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "reference_image_path": reference_image_path,
                "prompt": prompt,
                "seed": seed,
                "timing": {"load_s": load_s, "infer_s": time.time() - infer_start, "save_s": 0.0, "total_s": time.time() - t0},
            }
        infer_s = time.time() - infer_start

        image = _tensor_to_image(result)
        if image is None:
            return {
                "ok": True,
                "success": False,
                "error_code": "GENERATION_FAILED",
                "error": "pipeline returned no images",
                "reference_image_path": reference_image_path,
                "prompt": prompt,
                "seed": seed,
                "timing": {"load_s": load_s, "infer_s": infer_s, "save_s": 0.0, "total_s": time.time() - t0},
            }

        # Restore size of the generated image to original width and height!
        if image.width != width or image.height != height:
            try:
                from PIL import Image  # type: ignore
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:
                    resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else 1
                image = image.resize((width, height), resample)
            except Exception as exc:
                return {
                    "ok": True,
                    "success": False,
                    "error_code": "RESIZE_FAILED",
                    "error": f"failed to restore image size: {type(exc).__name__}: {exc}",
                    "reference_image_path": reference_image_path,
                    "prompt": prompt,
                    "seed": seed,
                    "timing": {"load_s": load_s, "infer_s": infer_s, "save_s": 0.0, "total_s": time.time() - t0},
                }

        save_start = time.time()
        try:
            image.save(output_path, format="PNG")
        except Exception as exc:
            if output_dir:
                return {
                    "ok": True,
                    "success": False,
                    "error_code": "SAVE_FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "reference_image_path": reference_image_path,
                    "prompt": prompt,
                    "seed": seed,
                    "timing": {"load_s": load_s, "infer_s": infer_s, "save_s": time.time() - save_start, "total_s": time.time() - t0},
                }
            fallback_dir = OPENVINO_ROOT / "output" / "img2img"
            try:
                fallback_dir.mkdir(parents=True, exist_ok=True)
                output_path = fallback_dir / filename
                image.save(output_path, format="PNG")
            except Exception as fallback_exc:
                return {
                    "ok": True,
                    "success": False,
                    "error_code": "SAVE_FAILED",
                    "error": f"primary={type(exc).__name__}: {exc}; fallback={type(fallback_exc).__name__}: {fallback_exc}",
                    "reference_image_path": reference_image_path,
                    "prompt": prompt,
                    "seed": seed,
                    "timing": {"load_s": load_s, "infer_s": infer_s, "save_s": time.time() - save_start, "total_s": time.time() - t0},
                }
        save_s = time.time() - save_start

        return {
            "ok": True,
            "success": True,
            "image_path": str(output_path),
            "reference_image_path": reference_image_path,
            "prompt": prompt,
            "seed": int(seed),
            "steps": int(steps),
            "width": int(width),
            "height": int(height),
            "device": self.loaded_device,
            "model_id": self.loaded_model_id,
            "timing": {"load_s": load_s, "infer_s": infer_s, "save_s": save_s, "total_s": time.time() - t0},
        }

    def dispatch(self, msg: dict) -> dict:
        op = msg.get("op")
        self.log(f"dispatching op={op!r}")
        if op == "generate":
            _notify_dog_activity(self.log_path)
            scene = msg.get("scene")
            if scene is not None and scene not in SUPPORTED_SCENES:
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_SCENE",
                    "error": "scene is not supported by this expert",
                }
            prompt = msg.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_PROMPT",
                    "error": "prompt must be a non-empty string",
                }
            if len(prompt) > MAX_PROMPT_CHARS:
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_PROMPT",
                    "error": f"prompt exceeds the {MAX_PROMPT_CHARS}-character limit",
                }
            reference_image_path = msg.get("reference_image_path")
            if not isinstance(reference_image_path, str) or not reference_image_path.strip():
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_IMAGE",
                    "error": "reference_image_path must be a non-empty string",
                }
            reference_image = Path(reference_image_path).expanduser()
            if not reference_image.exists() or not reference_image.is_file():
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_IMAGE",
                    "error": f"reference image does not exist: {reference_image}",
                }
            request_id = msg.get("request_id")
            try:
                request_id = str(uuid.UUID(str(request_id))) if request_id else str(uuid.uuid4())
            except (ValueError, AttributeError):
                return {"ok": False, "success": False, "error_code": "BAD_REQUEST_ID", "error": "request_id must be a UUID"}
            with self.runtime_lock:
                state = self.state
                init_error = self.init_error
                pipeline = self.pipeline
            if state not in (STATE_RUNNING, STATE_GENERATING) or pipeline is None:
                self.log(f"generate rejected: state={state}")
                reply = {
                    "ok": False,
                    "success": False,
                    "state": state,
                    "prompt": prompt,
                    "error": f"runtime not ready: {state}",
                }
                if state == STATE_ERROR and init_error:
                    reply["error"] = init_error
                return reply
            steps = msg.get("steps", DEFAULT_STEPS)
            if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_INFERENCE_STEPS:
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "BAD_STEPS",
                    "error": f"steps must be an integer between 1 and {MAX_INFERENCE_STEPS}",
                }
            seed = msg.get("seed")
            if not isinstance(seed, int) or isinstance(seed, bool):
                seed = _safe_seed()
            output_dir = msg.get("output_dir")
            if output_dir is not None and (not isinstance(output_dir, str) or not output_dir.strip()):
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "SAVE_FAILED",
                    "error": "output_dir must be a non-empty absolute path",
                }
            if not self.generation_lock.acquire(blocking=False):
                return {
                    "ok": False,
                    "success": False,
                    "error_code": "SERVER_BUSY",
                    "error": "another image generation is already running",
                    "state": STATE_GENERATING,
                }
            try:
                with self.runtime_lock:
                    self.state = STATE_GENERATING
                    self.active_request_id = request_id
                    self.active_request_started_at = time.time()
                    client_pid = msg.get("client_pid")
                    self.active_client_pid = client_pid if isinstance(client_pid, int) and client_pid > 0 else None
                    self.active_client_create_time = None
                    if self.active_client_pid is not None:
                        try:
                            import psutil  # type: ignore
                            self.active_client_create_time = psutil.Process(self.active_client_pid).create_time()
                        except Exception:
                            self.active_client_pid = None
                threading.Thread(target=self._watch_active_client, args=(request_id,), daemon=True, name="client-watch").start()
                reply = self._do_generate(str(reference_image), prompt, steps, seed, output_dir)
                reply["scene"] = scene
                with self.runtime_lock:
                    cancelled = request_id in self.cancelled_request_ids
                    self.cancelled_request_ids.discard(request_id)
                if cancelled:
                    image_path = reply.get("image_path")
                    if image_path:
                        try:
                            Path(image_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                    return {"ok": False, "success": False, "error_code": "CANCELLED", "error": "request was cancelled"}
                reply["state"] = STATE_RUNNING
                self.log(
                    f"generate completed: success={reply.get('success')} "
                    f"request_id={request_id} output_name={Path(reply.get('image_path', '')).name!r}"
                )
                return reply
            except Exception:
                error_text = traceback.format_exc()
                self.log(f"generate failed:\n{error_text}")
                return {
                    "ok": False,
                    "success": False,
                    "state": STATE_RUNNING,
                    "prompt": prompt,
                    "error": error_text,
                }
            finally:
                with self.runtime_lock:
                    self.state = STATE_RUNNING
                    self.active_request_id = None
                    self.active_request_started_at = None
                    self.active_client_pid = None
                    self.active_client_create_time = None
                self.generation_lock.release()
        if op == "status":
            with self.runtime_lock:
                state = self.state
                init_error = self.init_error
                loaded_model_id = self.loaded_model_id
                loaded_device = self.loaded_device
                active_request_id = self.active_request_id
                active_request_started_at = self.active_request_started_at
            self.log(f"status requested: state={state}")
            reply = {
                "ok": True,
                "state": state,
                "pid": os.getpid(),
                "uptime_s": time.time() - self.start_time,
                "loaded_model_id": loaded_model_id,
                "loaded_device": loaded_device,
                "server_version": SERVER_VERSION,
                "active_request_id": active_request_id,
                "active_request_elapsed_s": (
                    time.time() - active_request_started_at if active_request_started_at is not None else None
                ),
            }
            progress = get_download_progress()
            if progress is not None:
                reply["progress"] = progress
            if state == STATE_ERROR and init_error:
                reply["error"] = init_error
            return reply
        if op == "cancel":
            request_id = str(msg.get("request_id") or "")
            with self.runtime_lock:
                if request_id and request_id == self.active_request_id:
                    self.cancelled_request_ids.add(request_id)
                    return {"ok": True, "state": STATE_GENERATING, "cancel_pending": True}
            return {"ok": True, "state": self.state, "cancel_pending": False}
        if op == "shutdown":
            timeout = msg.get("timeout", DEFAULT_SHUTDOWN_TIMEOUT)
            try:
                self.shutdown_timeout = float(timeout)
            except (TypeError, ValueError):
                self.shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT
            self.log(f"shutdown requested with timeout={self.shutdown_timeout:.1f}s")
            # Release the pipeline so GPU memory / DLL handles drop before exit.
            try:
                with self.runtime_lock:
                    self.pipeline = None
                    self.loaded_model_id = None
                    self.loaded_device = None
                gc.collect()
            except Exception:
                pass
            self.shutdown_event.set()
            # Closing a Windows named-pipe listener from another thread does
            # not reliably unblock accept(). A self-connection wakes the loop
            # so normal shutdown does not end in os._exit(1).
            try:
                wake = PipeClient(PIPE_ADDRESS, authkey=AUTHKEY)
                wake.send({"op": "wake"})
                wake.close()
            except (FileNotFoundError, OSError, EOFError) as exc:
                self.log(f"accept-loop wake failed: {exc}")
            return {"ok": True, "state": "shutting_down"}
        if op == "wake" and self.shutdown_event.is_set():
            return {"ok": True, "state": "shutting_down"}
        self.log(f"unknown operation received: {op!r}")
        return {"ok": False, "error": f"unknown op: {op!r}"}

    def _handle_connection(self, conn) -> None:
        try:
            if not conn.poll(10.0):
                self.log("client connected but sent no request within 10s")
                return
            msg = conn.recv()
            self.log(f"client request: op={msg.get('op') if isinstance(msg, dict) else None}")
            reply = self.dispatch(msg if isinstance(msg, dict) else {})
            conn.send(reply)
            self.log(f"server reply: ok={reply.get('ok')} success={reply.get('success')}")
        except (EOFError, OSError):
            self.log("client disconnected before request completion")
        except Exception:
            error_text = traceback.format_exc()
            try:
                conn.send({"ok": False, "error": error_text})
            except Exception:
                pass
            self.log(f"connection handler error:\n{error_text}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve_forever(self) -> None:
        assert self.listener is not None
        while not self.shutdown_event.is_set():
            try:
                conn = self.listener.accept()
                self.log("accepted client connection")
            except OSError:
                if self.shutdown_event.is_set():
                    self.log("listener stopped after shutdown was requested")
                    return
                raise

            threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                daemon=True,
                name="client-connection",
            ).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="persistent local-img2img server")
    parser.add_argument(
        "--log",
        type=str,
        default=None,
        help="append debug logs to this file",
    )
    args = parser.parse_args()

    server = Server(log_path=_normalize_log_path(args.log))
    server.log(f"server starting with argv={sys.argv[1:]}")
    try:
        server.listener = Listener(PIPE_ADDRESS, family="AF_PIPE", authkey=AUTHKEY)
    except OSError as e:
        server.log(f"bind failed: {e}")
        print(f"[server] bind failed: {e}", flush=True)
        return 2

    server.log(f"listener ready on {PIPE_ADDRESS}")
    server.log("kicking off background runtime init")
    server._start_init_thread()
    print(
        f"local-img2img server listening on {PIPE_ADDRESS} (pid={os.getpid()}, "
        f"version={SERVER_VERSION})",
        flush=True,
    )

    worker = threading.Thread(target=server.serve_forever, name="accept-loop", daemon=True)
    worker.start()

    last_init_progress: tuple[Any, ...] | None = None
    last_init_progress_at = time.monotonic()
    try:
        while not server.shutdown_event.wait(timeout=0.5):
            if not worker.is_alive():
                server.log("accept-loop thread exited unexpectedly")
                return 1
            with server.runtime_lock:
                current_state = server.state
            if current_state == STATE_DOWNLOADING:
                progress = get_download_progress() or {}
                signature = (
                    progress.get("model_id"), progress.get("model_index"),
                    progress.get("downloaded_bytes"), progress.get("total_bytes"),
                )
                if signature != last_init_progress:
                    last_init_progress = signature
                    last_init_progress_at = time.monotonic()
                elif time.monotonic() - last_init_progress_at >= NO_PROGRESS_TIMEOUT:
                    server.log(f"model download made no progress for {NO_PROGRESS_TIMEOUT:.0f}s; exiting")
                    return 3
            elif current_state in {STATE_STARTING, STATE_LOADING}:
                if time.monotonic() - last_init_progress_at >= NO_PROGRESS_TIMEOUT:
                    server.log(f"runtime initialization remained in {current_state} for {NO_PROGRESS_TIMEOUT:.0f}s; exiting")
                    return 3
            else:
                last_init_progress_at = time.monotonic()
    finally:
        try:
            if server.listener is not None:
                server.listener.close()
        except Exception:
            pass

    worker.join(timeout=server.shutdown_timeout)
    if worker.is_alive():
        server.log(
            f"worker did not finish within {server.shutdown_timeout:.1f}s; forcing exit"
        )
        print(
            f"[server] worker did not finish within {server.shutdown_timeout:.1f}s; forcing exit",
            flush=True,
        )
        os._exit(1)
    server.log("server exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
