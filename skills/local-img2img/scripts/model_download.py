"""Shared helpers for atomically downloading and validating local models.

Provides:
- ModelDownloadTemplate / download_required_model (backward-compatible)
- ModelInfo / load_skill_info / load_model_infos / ensure_models (new API)
- download progress tracking (get_download_progress / format_download_progress)
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence


class ModelValidation(NamedTuple):
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class ModelDownloadTemplate:
    models_root: Path
    required_files: tuple[str, ...]
    snapshot_target_kwarg: str = "local_dir"
    snapshot_kwargs: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models_root", Path(self.models_root))
        object.__setattr__(self, "required_files", tuple(self.required_files))


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    dir_name: str
    required_files: tuple[str, ...]
    revision: str | None = None
    # Optional allowlist of glob patterns passed to snapshot_download so only the
    # needed files are fetched. Repos that ship the same weights in several
    # frameworks (PyTorch + TF + Flax + Rust + ONNX + CoreML, e.g. BERT /
    # Opus-MT) would otherwise download gigabytes of unused duplicates. ``None``
    # downloads the full snapshot (the default for most models).
    allow_patterns: tuple[str, ...] | None = None


REVISION_MARKER = ".photo-magic-revision"


def validate_model_dir(local_dir: Path, required_files: Sequence[str]) -> ModelValidation:
    """Return ok only when required files exist, are non-empty, and every
    OpenVINO IR ``*.xml`` has a non-empty companion ``*.bin``.

    Checking only ``model_index.json`` / ``vae_bn_stats.npz`` is not enough:
    an interrupted download can leave those small files while the multi-GB
    weight ``.bin`` files are still missing. Loading then fails with
    ``Empty weights data in bin file``, which callers often misread as a
    Python dependency install failure.
    """
    if not local_dir.is_dir():
        return ModelValidation(ok=False, reason="directory missing")

    missing_files: list[str] = []
    empty_files: list[str] = []
    for relative_path in required_files:
        path = local_dir / relative_path
        if not path.is_file():
            missing_files.append(relative_path)
        elif path.stat().st_size <= 0:
            empty_files.append(relative_path)

    missing_bins: list[str] = []
    for xml_path in local_dir.rglob("openvino*.xml"):
        bin_path = xml_path.with_suffix(".bin")
        rel = str(bin_path.relative_to(local_dir)).replace("\\", "/")
        if not bin_path.is_file():
            missing_bins.append(rel)
        elif bin_path.stat().st_size < 1024:
            empty_files.append(rel)

    problems: list[str] = []
    if missing_files:
        problems.append(f"missing files: {', '.join(missing_files)}")
    if empty_files:
        problems.append(f"empty files: {', '.join(empty_files)}")
    if missing_bins:
        problems.append(f"missing weight bins: {', '.join(missing_bins)}")
    if problems:
        return ModelValidation(ok=False, reason="; ".join(problems))
    return ModelValidation(ok=True)


def validate_installed_model(local_dir: Path, model: ModelInfo) -> ModelValidation:
    """Validate payload completeness and the pinned model revision."""
    validation = validate_model_dir(local_dir, model.required_files)
    if not validation.ok:
        return validation
    if model.revision:
        marker = local_dir / REVISION_MARKER
        try:
            installed_revision = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return ModelValidation(ok=False, reason=f"cannot read revision marker: {exc}")
        if installed_revision != model.revision:
            return ModelValidation(
                ok=False,
                reason=(
                    f"revision mismatch: expected {model.revision}, "
                    f"found {installed_revision or '<empty>'}"
                ),
            )
    return ModelValidation(ok=True)


def _assert_under_models_root(path: Path, models_root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = models_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError(
            f"refusing to touch path outside models root: {resolved}"
        ) from exc
    return resolved


def _remove_tree_safely(path: Path, models_root: Path) -> None:
    resolved = _assert_under_models_root(path, models_root)
    if resolved.exists():
        shutil.rmtree(resolved)


def _backup_invalid_model_dir(local_dir: Path, models_root: Path) -> Path:
    _assert_under_models_root(local_dir, models_root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{stamp}-{os.getpid()}"
    backup_dir = local_dir.with_name(f"{local_dir.name}.invalid-{suffix}")
    while backup_dir.exists():
        suffix = f"{suffix}-1"
        backup_dir = local_dir.with_name(f"{local_dir.name}.invalid-{suffix}")
    _assert_under_models_root(backup_dir, models_root)
    os.replace(local_dir, backup_dir)
    # Broken weights can be downloaded again. Retain only the newest backup so
    # repeated recovery attempts cannot fill the user's disk with multi-GB
    # invalid model trees.
    for old_backup in models_root.glob(f"{local_dir.name}.invalid-*"):
        if old_backup != backup_dir:
            _remove_tree_safely(old_backup, models_root)
    return backup_dir


def invalidate_model_dir(
    local_dir: Path,
    models_root: Path,
    logger: Callable[[str], None] | None = None,
) -> Path | None:
    """Move a broken model directory aside so the next ensure_models re-downloads."""
    if not local_dir.exists():
        return None
    backup_dir = _backup_invalid_model_dir(local_dir, models_root)
    _emit(logger, f"invalidated model dir {local_dir} -> {backup_dir}")
    return backup_dir


def _emit(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is not None:
        logger(message)


# ---------------------------------------------------------------------------
# Download progress tracking
#
# ``snapshot_download`` offers no progress callback, so progress is sampled by
# a monitor thread that periodically measures the on-disk size of the
# ``.partial`` directory and compares it against the total size reported by the
# ModelScope hub. The snapshot lives in module-level state so a long-lived
# server can return it from its ``status`` RPC while the init thread downloads.
# ---------------------------------------------------------------------------

# Sampling is a cheap directory walk, so it runs twice a second to keep the
# percentage/speed readout fresh for the client polling the server. Sampling is
# deliberately decoupled from *display*: a multi-GB download must not flood the
# user with progress lines, but the lines we do show have to carry a current
# speed/ETA rather than a 5-minute average — so fast samples feed a slow
# display (see PROGRESS_DISPLAY_INTERVAL / should_display_progress).
PROGRESS_SAMPLE_INTERVAL = 2.0

# How often a *user-visible* download progress line may be emitted. The first
# line is shown as soon as a download starts, then at most one line per
# interval, so a long download reports in every 5 minutes instead of streaming
# hundreds of lines into the conversation.
PROGRESS_DISPLAY_INTERVAL = 300.0

_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict | None = None


def should_display_progress(
    state: dict,
    now: float | None = None,
    interval: float = PROGRESS_DISPLAY_INTERVAL,
) -> bool:
    """Rate-limit user-visible progress lines to one per ``interval`` seconds.

    ``state`` is any caller-owned dict used to remember when a line was last
    shown (kept under the ``"displayed_at"`` key). The first call always returns
    ``True`` so the start of a download is never a silent wait; later calls
    return ``True`` only once ``interval`` seconds have elapsed.
    """
    now = time.time() if now is None else now
    last = state.get("displayed_at")
    if last is None or (now - last) >= interval:
        state["displayed_at"] = now
        return True
    return False


def get_download_progress() -> dict | None:
    """Return a snapshot of the in-flight download progress (or None)."""
    with _PROGRESS_LOCK:
        return dict(_PROGRESS) if _PROGRESS is not None else None


def clear_download_progress() -> None:
    with _PROGRESS_LOCK:
        globals()["_PROGRESS"] = None


def set_download_progress(progress: dict | None) -> None:
    """Publish a progress snapshot (used by skills with a bespoke downloader)."""
    with _PROGRESS_LOCK:
        globals()["_PROGRESS"] = dict(progress) if progress is not None else None


_set_download_progress = set_download_progress


def format_bytes(num_bytes: float | None) -> str:
    if num_bytes is None or num_bytes < 0:
        return "未知"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "未知"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{secs:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分"


def format_download_progress(progress: Mapping | None, bar_width: int = 20) -> str:
    """Render a progress snapshot as a single human-readable Chinese line."""
    if not progress:
        return "模型下载准备中..."

    model_id = progress.get("model_id") or "模型"
    index = progress.get("model_index")
    total_models = progress.get("model_total")
    percent = progress.get("percent")
    downloaded = progress.get("downloaded_bytes")
    total = progress.get("total_bytes")
    speed = progress.get("speed_bps")
    eta = progress.get("eta_s")

    if isinstance(percent, (int, float)):
        filled = int(round(bar_width * max(0.0, min(100.0, float(percent))) / 100.0))
        bar = "█" * filled + "░" * (bar_width - filled)
        head = f"[{bar}] {float(percent):5.1f}%"
    else:
        head = "[" + "░" * bar_width + "]  --.-%"

    parts = [f"模型下载中 {head}"]
    if total:
        parts.append(f"{format_bytes(downloaded)}/{format_bytes(total)}")
    elif downloaded:
        parts.append(f"已下载 {format_bytes(downloaded)}")
    if speed:
        parts.append(f"{format_bytes(speed)}/s")
    if eta is not None:
        parts.append(f"剩余约 {format_duration(eta)}")
    if index and total_models and total_models > 1:
        parts.append(f"第 {index}/{total_models} 个模型")
    parts.append(str(model_id))
    return " | ".join(parts)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    # File vanished mid-walk (temp file renamed) — ignore.
                    continue
    except OSError:
        return total
    return total


def remote_model_size(model_id: str, allow_patterns: Sequence[str] | None = None) -> int | None:
    """Best-effort total download size (bytes) for a ModelScope model.

    Returns ``None`` when the hub cannot be queried; callers must treat the
    total as unknown rather than failing the download.
    """
    try:
        from modelscope.hub.api import HubApi

        files = HubApi().get_model_files(model_id, recursive=True)
    except Exception:  # noqa: BLE001 — progress must never break a download
        return None

    total = 0
    for entry in files or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("Type", "")).lower() == "tree":
            continue
        rel_path = entry.get("Path") or entry.get("path") or ""
        if allow_patterns and not any(
            fnmatch.fnmatch(rel_path, pattern) for pattern in allow_patterns
        ):
            continue
        try:
            total += int(entry.get("Size") or entry.get("size") or 0)
        except (TypeError, ValueError):
            continue
    return total or None


class _ProgressMonitor:
    """Samples the size of a ``.partial`` directory on a background thread."""

    def __init__(
        self,
        model_id: str,
        partial_dir: Path,
        total_bytes: int | None,
        model_index: int = 1,
        model_total: int = 1,
        callback: Callable[[dict], None] | None = None,
        interval: float = PROGRESS_SAMPLE_INTERVAL,
    ) -> None:
        self._model_id = model_id
        self._partial_dir = Path(partial_dir)
        self._total_bytes = total_bytes
        self._model_index = model_index
        self._model_total = model_total
        self._callback = callback
        self._interval = max(0.1, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.time()
        self._last_sample: tuple[float, int] | None = None
        self._speed_bps: float | None = None

    def __enter__(self) -> "_ProgressMonitor":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        self._publish(self._sample_bytes())
        self._thread = threading.Thread(
            target=self._run, name="model-download-progress", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2)
            self._thread = None

    def _sample_bytes(self) -> int:
        return _dir_size(self._partial_dir)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._publish(self._sample_bytes())

    def _publish(self, downloaded: int) -> None:
        now = time.time()
        if self._last_sample is not None:
            elapsed = now - self._last_sample[0]
            delta = downloaded - self._last_sample[1]
            if elapsed > 0 and delta >= 0:
                instant = delta / elapsed
                # Exponential moving average keeps the readout steady while
                # still reacting to a stalled or accelerating transfer.
                self._speed_bps = (
                    instant if self._speed_bps is None else 0.7 * self._speed_bps + 0.3 * instant
                )
        self._last_sample = (now, downloaded)

        percent: float | None = None
        eta: float | None = None
        if self._total_bytes:
            percent = min(99.9, 100.0 * downloaded / float(self._total_bytes))
            if self._speed_bps and self._speed_bps > 0:
                eta = max(0.0, (self._total_bytes - downloaded) / self._speed_bps)

        progress = {
            "state": "downloading",
            "model_id": self._model_id,
            "model_index": self._model_index,
            "model_total": self._model_total,
            "downloaded_bytes": downloaded,
            "total_bytes": self._total_bytes,
            "percent": percent,
            "speed_bps": self._speed_bps,
            "eta_s": eta,
            "elapsed_s": now - self._started_at,
            "updated_at": now,
        }
        _set_download_progress(progress)
        if self._callback is not None:
            try:
                self._callback(dict(progress))
            except Exception:  # noqa: BLE001 — a bad reporter must not stop a download
                pass


def download_required_model(
    model_id: str,
    local_dir: Path,
    snapshot_download,
    template: ModelDownloadTemplate,
    logger: Callable[[str], None] | None = None,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    progress_callback: Callable[[dict], None] | None = None,
    model_index: int = 1,
    model_total: int = 1,
) -> None:
    partial_dir = local_dir.with_name(f"{local_dir.name}.partial")
    #_remove_tree_safely(partial_dir, template.models_root)

    _emit(logger, f"downloading model {model_id} -> {partial_dir}")
    snapshot_kwargs = dict(template.snapshot_kwargs or {})
    snapshot_kwargs[template.snapshot_target_kwarg] = str(partial_dir)

    total_bytes = remote_model_size(model_id, snapshot_kwargs.get("allow_patterns"))
    _emit(logger, f"expected download size for {model_id}: {format_bytes(total_bytes)}")
    monitor = _ProgressMonitor(
        model_id=model_id,
        partial_dir=partial_dir,
        total_bytes=total_bytes,
        model_index=model_index,
        model_total=model_total,
        callback=progress_callback,
    )

    # A single snapshot_download can fail mid-transfer on a flaky network — most
    # painfully on a large weight file (e.g. the 440MB bert-base-uncased
    # safetensors), where modelscope raises FileDownloadError after exhausting
    # its own per-file retries. Retry the whole call a few times with backoff: a
    # fresh attempt reuses modelscope's on-disk cache (already-fetched files are
    # skipped / resumed), so this completes the download rather than restarting
    # 440MB each time. The partial dir is deliberately NOT wiped between attempts
    # so that resume can take effect. The last exception propagates if every
    # attempt fails, so a genuinely unreachable model still fails init loudly.
    attempts = max(1, max_attempts)
    with monitor:
        for attempt in range(1, attempts + 1):
            try:
                snapshot_download(model_id, **snapshot_kwargs)
                revision = snapshot_kwargs.get("revision")
                if revision:
                    (partial_dir / ".photo-magic-revision").write_text(
                        str(revision) + "\n", encoding="utf-8"
                    )
                break
            except Exception as exc:  # noqa: BLE001 — surface AND retry any download error
                if attempt >= attempts:
                    _emit(logger, f"download of {model_id} failed after {attempt} attempt(s): {exc}")
                    raise
                backoff = 2.0 * attempt  # 2s, 4s, ... — modest, generous enough for a blip
                _emit(
                    logger,
                    f"download of {model_id} failed (attempt {attempt}/{attempts}): {exc}; "
                    f"retrying in {backoff:.0f}s",
                )
                sleep(backoff)

    partial_validation = validate_model_dir(partial_dir, template.required_files)
    if not partial_validation.ok:
        raise RuntimeError(
            f"downloaded model {model_id} failed validation: {partial_validation.reason}"
        )

    current_validation = validate_model_dir(local_dir, template.required_files)
    if local_dir.exists() and not current_validation.ok:
        backup_dir = _backup_invalid_model_dir(local_dir, template.models_root)
        _emit(logger, f"backed up invalid model dir {local_dir} -> {backup_dir}")
    elif current_validation.ok:
        _remove_tree_safely(partial_dir, template.models_root)
        return

    try:
        os.replace(partial_dir, local_dir)
    except OSError as exc:
        raise RuntimeError(f"failed to install downloaded model {model_id}: {exc}") from exc

    _emit(logger, f"installed downloaded model {model_id} -> {local_dir}")


def load_skill_info(info_json_path: Path) -> dict:
    """Load the full info.json as a dict."""
    return json.loads(info_json_path.read_text(encoding="utf-8"))


def load_model_infos(info_json_path: Path) -> list[ModelInfo]:
    """Parse the 'models' array from info.json into ModelInfo objects."""
    data = load_skill_info(info_json_path)
    models_raw = data.get("models", [])
    return [
        ModelInfo(
            model_id=m["model_id"],
            dir_name=m["dir_name"],
            required_files=tuple(m["required_files"]),
            revision=m.get("revision"),
            allow_patterns=tuple(m["allow_patterns"]) if m.get("allow_patterns") else None,
        )
        for m in models_raw
    ]


def _promote_partial_model(local_dir: Path, models_root: Path, logger=None) -> bool:
    """If ``local_dir.partial`` already validates, rename it to ``local_dir``.

    Interrupted first-time downloads often leave a complete ``.partial`` tree
    that was never promoted (for example after a client timeout). Without this
    step every later run treats the model as missing and re-enters download.
    """
    partial_dir = local_dir.with_name(f"{local_dir.name}.partial")
    if not partial_dir.is_dir():
        return False
    # Caller must already know required_files; this helper only renames.
    if local_dir.exists():
        return False
    try:
        _assert_under_models_root(partial_dir, models_root)
        _assert_under_models_root(local_dir, models_root)
        os.replace(partial_dir, local_dir)
        _emit(logger, f"promoted completed partial model {partial_dir.name} -> {local_dir}")
        return True
    except OSError as exc:
        _emit(logger, f"failed to promote partial model {partial_dir}: {exc}")
        # Fallback: copy then remove partial so a locked rename cannot loop forever.
        try:
            if local_dir.exists():
                return False
            shutil.copytree(partial_dir, local_dir)
            shutil.rmtree(partial_dir, ignore_errors=True)
            _emit(logger, f"copied completed partial model {partial_dir.name} -> {local_dir}")
            return True
        except OSError as copy_exc:
            _emit(logger, f"fallback copy of partial model failed: {copy_exc}")
            return False


def _migrate_legacy_model(
    model: ModelInfo,
    local_dir: Path,
    models_root: Path,
    legacy_models_roots: Sequence[Path],
    logger: Callable[[str], None] | None = None,
) -> bool:
    """Safely copy an existing legacy model into the plugin data directory.

    The source is intentionally trusted and preserved: discovering the legacy
    directory is enough to reuse it. The copy is staged, marked with the pinned
    revision, then atomically renamed. An operational migration failure is
    raised rather than being mistaken for a reason to download.
    """
    destination = local_dir.resolve()
    attempt_marker = models_root / f".{model.dir_name}.legacy-migrated"
    _assert_under_models_root(attempt_marker, models_root)
    if attempt_marker.exists():
        _emit(logger, "legacy model was already migrated once; use normal recovery flow")
        return False
    for legacy_root in legacy_models_roots:
        source = Path(legacy_root).expanduser() / model.dir_name
        try:
            source_resolved = source.resolve()
        except OSError:
            continue
        if source_resolved == destination:
            continue

        if not source.is_dir():
            continue

        models_root.mkdir(parents=True, exist_ok=True)
        staging = local_dir.with_name(f"{local_dir.name}.migration-partial")
        _assert_under_models_root(staging, models_root)
        if staging.exists():
            _remove_tree_safely(staging, models_root)
        _emit(logger, f"migrating existing legacy model {source} -> {local_dir}")
        replaced_dir: Path | None = None
        try:
            shutil.copytree(source, staging, copy_function=shutil.copy2)
            if model.revision:
                (staging / REVISION_MARKER).write_text(
                    model.revision + "\n", encoding="utf-8"
                )
            if local_dir.exists():
                replaced_dir = _backup_invalid_model_dir(local_dir, models_root)
                _emit(logger, f"preserved incomplete plugin model -> {replaced_dir}")
            attempt_marker.write_text(str(source) + "\n", encoding="utf-8")
            os.replace(staging, local_dir)
        except Exception as exc:
            attempt_marker.unlink(missing_ok=True)
            if staging.exists():
                _remove_tree_safely(staging, models_root)
            if replaced_dir is not None and replaced_dir.exists() and not local_dir.exists():
                os.replace(replaced_dir, local_dir)
            raise RuntimeError(
                f"failed to migrate legacy model from {source}: {exc}"
            ) from exc
        _emit(logger, f"migrated legacy model successfully -> {local_dir}")
        return True
    return False


def ensure_models(
    models: list[ModelInfo],
    models_root: Path,
    logger: Callable[[str], None] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    legacy_models_roots: Sequence[Path] = (),
) -> None:
    """Validate and download all models that aren't already present.

    While a download is running, progress is published to the module-level
    snapshot (see :func:`get_download_progress`) and, when supplied, pushed to
    ``progress_callback``. The snapshot is cleared once everything is on disk.

    A finished ``.partial`` directory with all required files is promoted once
    and never re-downloaded. Incomplete partials are resumed in place.
    """
    models_root = Path(models_root)
    legacy_models_roots = tuple(Path(path) for path in legacy_models_roots)

    migrated: set[str] = set()
    for m in models:
        local_dir = models_root / m.dir_name
        if validate_installed_model(local_dir, m).ok:
            continue
        if _migrate_legacy_model(
            m, local_dir, models_root, legacy_models_roots, logger=logger
        ):
            migrated.add(m.dir_name)
            continue
        partial_dir = local_dir.with_name(f"{local_dir.name}.partial")
        if validate_installed_model(partial_dir, m).ok:
            if _promote_partial_model(local_dir, models_root, logger=logger):
                continue

    missing = [
        m for m in models
        if m.dir_name not in migrated
        and not validate_installed_model(models_root / m.dir_name, m).ok
    ]
    if not missing:
        if migrated:
            _emit(logger, "legacy model migration complete; skip download")
        else:
            _emit(logger, "all required models already present; skip download")
        return

    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "modelscope is required to download models. "
            "Please re-run the expert's standard Bash entry."
        ) from exc

    models_root.mkdir(parents=True, exist_ok=True)

    total_models = len(missing)
    for index, m in enumerate(missing, start=1):
        local_dir = models_root / m.dir_name
        # Re-check: another process may have finished while we imported modelscope.
        if validate_installed_model(local_dir, m).ok:
            continue
        partial_dir = local_dir.with_name(f"{local_dir.name}.partial")
        if validate_installed_model(partial_dir, m).ok and _promote_partial_model(
            local_dir, models_root, logger=logger
        ):
            continue

        validation = validate_installed_model(local_dir, m)
        if validation.reason and validation.reason != "directory missing":
            _emit(logger, f"model {m.model_id} is incomplete: {validation.reason}; resuming download")
        elif partial_dir.is_dir():
            _emit(logger, f"resuming incomplete partial download for {m.model_id}")

        template = ModelDownloadTemplate(
            models_root=models_root,
            required_files=m.required_files,
            snapshot_kwargs={
                **({"allow_patterns": list(m.allow_patterns)} if m.allow_patterns else {}),
                **({"revision": m.revision} if m.revision else {}),
            },
        )
        try:
            download_required_model(
                model_id=m.model_id,
                local_dir=local_dir,
                snapshot_download=snapshot_download,
                template=template,
                logger=logger,
                progress_callback=progress_callback,
                model_index=index,
                model_total=total_models,
            )
        except Exception:
            clear_download_progress()
            raise

    clear_download_progress()

    failed = [
        f"{m.model_id} ({validate_installed_model(models_root / m.dir_name, m).reason})"
        for m in models
        if m.dir_name not in migrated
        and not validate_installed_model(models_root / m.dir_name, m).ok
    ]
    if failed:
        raise RuntimeError(f"model download did not complete: {', '.join(failed)}")


__all__ = [
    "ModelDownloadTemplate",
    "clear_download_progress",
    "format_bytes",
    "format_download_progress",
    "format_duration",
    "get_download_progress",
    "remote_model_size",
    "set_download_progress",
    "should_display_progress",
    "PROGRESS_DISPLAY_INTERVAL",
    "PROGRESS_SAMPLE_INTERVAL",
    "ModelInfo",
    "ModelValidation",
    "download_required_model",
    "ensure_models",
    "invalidate_model_dir",
    "load_model_infos",
    "load_skill_info",
    "validate_installed_model",
    "validate_model_dir",
]
