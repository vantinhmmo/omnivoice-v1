#!/usr/bin/env python3
"""FastAPI backend for OmniVoice React web UI.

This backend intentionally separates two paths:

- Short interactive TTS: handled in-process with one lazily-loaded model.
- Long text jobs: submitted as background subprocesses that run
  ``python -m omnivoice.cli.long_text`` and write resumable manifests.

Run:
    python -m uvicorn server.api:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from omnivoice import OmniVoice, OmniVoiceGenerationConfig
from omnivoice.utils.common import str2bool

ROOT_DIR = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT_DIR / "jobs"
SHORT_DIR = ROOT_DIR / "outputs" / "short"
DEFAULT_MODEL = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
DEFAULT_DEVICE = os.environ.get("OMNIVOICE_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
DEFAULT_DTYPE = os.environ.get("OMNIVOICE_DTYPE", "auto")

JOBS_DIR.mkdir(parents=True, exist_ok=True)
SHORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OmniVoice API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("OMNIVOICE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_model: Optional[OmniVoice] = None
_sampling_rate: Optional[int] = None
logger = logging.getLogger("omnivoice.api")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_dtype(dtype_name: str, device: str) -> torch.dtype:
    if dtype_name == "auto":
        return torch.float16 if str(device).startswith("cuda") else torch.float32
    if dtype_name == "float16":
        return torch.float16
    if dtype_name == "bfloat16":
        return torch.bfloat16
    if dtype_name == "float32":
        return torch.float32
    raise ValueError(f"Unsupported dtype: {dtype_name}")


def get_model() -> OmniVoice:
    global _model, _sampling_rate
    if _model is None:
        dtype = resolve_dtype(DEFAULT_DTYPE, DEFAULT_DEVICE)
        _model = OmniVoice.from_pretrained(
            DEFAULT_MODEL,
            device_map=DEFAULT_DEVICE,
            dtype=dtype,
            load_asr=False,
        )
        _model.eval()
        _sampling_rate = _model.sampling_rate
    return _model


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


async def save_upload(upload: Optional[UploadFile], path: Path) -> Optional[Path]:
    if upload is None or not upload.filename:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return path


def job_dir(job_id: str) -> Path:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
    return JOBS_DIR / safe


def job_meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "work" / "manifest.json"


def normalize_user_id(user_id: Optional[str]) -> str:
    raw = str(user_id or "").strip().lower()
    safe = "".join(c for c in raw if c.isalnum() or c in "-_.:@")
    return safe[:120] or "anonymous"


def ensure_job_access(meta: Dict[str, Any], user_id: Optional[str]) -> None:
    if user_id is None:
        return
    requested_user = normalize_user_id(user_id)
    owner_user = normalize_user_id(meta.get("user_id"))
    if requested_user != owner_user:
        raise HTTPException(status_code=404, detail="Job not found")


def read_job(job_id: str) -> Dict[str, Any]:
    path = job_meta_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    meta = read_json(path)
    mpath = manifest_path(job_id)
    if mpath.exists():
        try:
            meta["manifest"] = read_json(mpath)
        except Exception as exc:  # noqa: BLE001
            meta["manifest_error"] = str(exc)
    output = Path(meta.get("output", ""))
    meta["output_exists"] = bool(output.exists())
    meta["log_exists"] = bool((job_dir(job_id) / "worker.log").exists())
    return meta


def is_pid_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def refresh_job_status(meta: Dict[str, Any]) -> Dict[str, Any]:
    status = meta.get("status")
    pid = meta.get("pid")
    if status == "running" and not is_pid_running(pid):
        output = Path(meta.get("output", ""))
        if output.exists():
            meta["status"] = "done"
        else:
            meta["status"] = "stopped"
        meta["updated_at"] = utc_now()
        write_json(job_meta_path(meta["id"]), meta)
    return meta


ACTIVE_LONG_JOB_STATUSES = {"queued", "pending", "running"}
TERMINAL_JOB_STATUSES = {"done", "cancelled", "stopped", "failed", "merged"}

try:
    _max_jobs_env = int(os.environ.get("OMNIVOICE_MAX_CONCURRENT_LONG_JOBS", "1"))
except ValueError:
    _max_jobs_env = 1
MAX_CONCURRENT_LONG_JOBS = max(1, _max_jobs_env)

JOB_SUBMIT_LOCK = JOBS_DIR / ".create_job.lock"
SINGLE_ACTIVE_GUARD = JOBS_DIR / ".single_active_guard"
JOB_SUBMIT_ASYNC_LOCK = asyncio.Lock()
JOB_QUEUE_DISPATCH_LOCK = threading.Lock()


def find_active_long_job() -> Optional[Dict[str, Any]]:
    """Strict single-job guard with stale-running cleanup."""
    terminal_statuses = {"done", "cancelled", "stopped", "failed"}
    for path in sorted(JOBS_DIR.glob("*/job.json"), reverse=True):
        try:
            meta = read_json(path)
            status = str(meta.get("status", "")).lower()

            # Nếu job được ghi running nhưng PID không còn sống thì coi là stopped.
            if status == "running" and not is_pid_running(meta.get("pid")):
                meta["status"] = "stopped"
                meta["updated_at"] = utc_now()
                write_json(path, meta)
                status = "stopped"

            if status not in terminal_statuses:
                return read_job(meta["id"])
        except Exception:  # noqa: BLE001
            continue
    return None


def _raise_active_job(active: Optional[Dict[str, Any]] = None) -> None:
    active = active or find_active_long_job()
    detail: Dict[str, Any] = {
        "message": "Đang có job hoạt động. Vui lòng chờ job xong hoặc bấm Dừng job trước khi tạo job mới.",
    }
    if active:
        detail.update({"job_id": active.get("id"), "status": active.get("status")})
    raise HTTPException(status_code=409, detail=detail)


def acquire_single_active_guard() -> None:
    """Acquire cross-process guard so only one long job can be created/run.

    Includes stale-guard recovery to avoid permanent lock after crash/restart.
    """
    try:
        fd = os.open(str(SINGLE_ACTIVE_GUARD), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        stale = False
        try:
            raw = read_json(SINGLE_ACTIVE_GUARD)
            job_id = str(raw.get("job_id") or "").strip()
            created_at = raw.get("created_at")

            if job_id:
                jpath = job_meta_path(job_id)
                if not jpath.exists():
                    stale = True
                else:
                    meta = read_json(jpath)
                    status = str(meta.get("status", "")).lower()
                    if status == "running" and not is_pid_running(meta.get("pid")):
                        meta["status"] = "stopped"
                        meta["updated_at"] = utc_now()
                        write_json(jpath, meta)
                        status = "stopped"
                    stale = status in {"done", "cancelled", "stopped", "failed"}
            elif isinstance(created_at, str):
                dt = datetime.fromisoformat(created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
                stale = age_seconds > 120
        except Exception:  # noqa: BLE001
            stale = False

        if stale:
            try:
                SINGLE_ACTIVE_GUARD.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                fd = os.open(str(SINGLE_ACTIVE_GUARD), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Đang có job hoạt động. Vui lòng chờ job xong hoặc bấm Dừng job trước khi tạo job mới.",
                    },
                )
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Đang có job hoạt động. Vui lòng chờ job xong hoặc bấm Dừng job trước khi tạo job mới.",
                },
            )

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"created_at": utc_now()}, f)



def acquire_job_submit_lock() -> Path:
    active = find_active_long_job()
    if active:
        _raise_active_job(active)

    try:
        fd = os.open(str(JOB_SUBMIT_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Lock tồn tại: có thể là request tạo job đang diễn ra hoặc lock cũ.
        lock_is_stale = False
        try:
            raw = json.loads(JOB_SUBMIT_LOCK.read_text(encoding="utf-8"))
            created_at = raw.get("created_at")
            if isinstance(created_at, str):
                dt = datetime.fromisoformat(created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
                lock_is_stale = age_seconds > 120
        except Exception:  # noqa: BLE001
            # Không đọc được lock file => coi như lock còn hiệu lực ngắn hạn.
            lock_is_stale = False

        active = find_active_long_job()
        if active:
            _raise_active_job(active)

        if not lock_is_stale:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Hệ thống đang xử lý yêu cầu tạo job trước đó. Vui lòng chờ vài giây rồi thử lại.",
                },
            )

        # Chỉ dọn lock nếu xác định là lock cũ.
        try:
            JOB_SUBMIT_LOCK.unlink()
        except OSError:
            pass

        try:
            fd = os.open(str(JOB_SUBMIT_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Hệ thống đang xử lý yêu cầu tạo job trước đó. Vui lòng chờ vài giây rồi thử lại.",
                },
            )

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"created_at": utc_now()}, f)

    # Re-check sau khi đã giữ lock để chặn race:
    # request B có thể đã qua pre-check trước khi request A ghi job.json.
    active_after_lock = find_active_long_job()
    if active_after_lock:
        try:
            JOB_SUBMIT_LOCK.unlink()
        except OSError:
            pass
        _raise_active_job(active_after_lock)

    return JOB_SUBMIT_LOCK


def release_job_submit_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _sort_jobs_by_time_desc(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    jobs.sort(
        key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""),
        reverse=True,
    )
    return jobs


def _collect_jobs(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    requested_user = normalize_user_id(user_id) if user_id is not None else None
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            meta = refresh_job_status(read_json(path))
            job = read_job(meta["id"])
            if requested_user is not None and normalize_user_id(job.get("user_id")) != requested_user:
                continue
            jobs.append(job)
        except Exception:  # noqa: BLE001
            continue
    return _sort_jobs_by_time_desc(jobs)


def dispatch_next_queued_job() -> Optional[Dict[str, Any]]:
    """Dispatch queued jobs up to MAX_CONCURRENT_LONG_JOBS."""
    with JOB_QUEUE_DISPATCH_LOCK:
        candidates: List[Dict[str, Any]] = []
        running_count = 0

        for path in JOBS_DIR.glob("*/job.json"):
            try:
                meta = refresh_job_status(read_json(path))
                status = str(meta.get("status", "")).lower()
                if status == "running":
                    running_count += 1
                elif status in {"queued", "pending"}:
                    candidates.append(meta)
            except Exception:  # noqa: BLE001
                continue

        slots = max(0, MAX_CONCURRENT_LONG_JOBS - running_count)
        if slots <= 0 or not candidates:
            return None

        candidates.sort(key=lambda m: str(m.get("created_at") or m.get("updated_at") or ""))
        dispatched: List[Dict[str, Any]] = []

        for meta in candidates[:slots]:
            job_id = str(meta.get("id") or "").strip()
            if not job_id:
                continue

            cmd = meta.get("command")
            if not isinstance(cmd, list) or not cmd:
                meta["status"] = "failed"
                meta["updated_at"] = utc_now()
                meta["error"] = "Missing command for queued job"
                write_json(job_meta_path(job_id), meta)
                dispatched.append(read_job(job_id))
                continue

            log_path = Path(meta.get("log") or (job_dir(job_id) / "worker.log"))
            log_path.parent.mkdir(parents=True, exist_ok=True)

            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT_DIR),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )

            meta["status"] = "running"
            meta["pid"] = process.pid
            meta["updated_at"] = utc_now()
            write_json(job_meta_path(job_id), meta)
            logger.info(
                "[queue] dispatched job: id=%s pid=%s running=%s/%s",
                job_id,
                process.pid,
                running_count + len(dispatched) + 1,
                MAX_CONCURRENT_LONG_JOBS,
            )
            dispatched.append(read_job(job_id))

        return dispatched[0] if dispatched else None


def collect_server_queue_stats() -> Dict[str, int]:
    running = 0
    queued = 0
    total = 0

    for path in JOBS_DIR.glob("*/job.json"):
        try:
            meta = refresh_job_status(read_json(path))
            status = str(meta.get("status", "")).lower()
            total += 1
            if status == "running":
                running += 1
            elif status in {"queued", "pending"}:
                queued += 1
        except Exception:  # noqa: BLE001
            continue

    return {
        "running_long_jobs": running,
        "queued_long_jobs": queued,
        "total_long_jobs": total,
        "active_long_jobs": running + queued,
    }


@app.get("/api/health")
def health() -> Dict[str, Any]:
    queue_stats = collect_server_queue_stats()
    return {
        "ok": True,
        "model": DEFAULT_MODEL,
        "device": DEFAULT_DEVICE,
        "dtype": DEFAULT_DTYPE,
        "cuda": torch.cuda.is_available(),
        "model_loaded": _model is not None,
        "anti_spam_version": "guard-atomic-v1",
        "max_concurrent_long_jobs": MAX_CONCURRENT_LONG_JOBS,
        **queue_stats,
    }


@app.get("/api/config")
def config() -> Dict[str, Any]:
    return {
        "defaultModel": DEFAULT_MODEL,
        "defaultDevice": DEFAULT_DEVICE,
        "shortOutputDir": str(SHORT_DIR),
        "jobsDir": str(JOBS_DIR),
    }


@app.post("/api/tts/short")
async def short_tts(
    text: str = Form(...),
    language: Optional[str] = Form(None),
    mode: str = Form("auto"),
    ref_audio: Optional[UploadFile] = File(None),
    ref_text: Optional[str] = Form(None),
    instruct: Optional[str] = Form(None),
    num_step: int = Form(32),
    guidance_scale: float = Form(2.0),
    speed: float = Form(1.0),
    duration: Optional[float] = Form(None),
    denoise: bool = Form(True),
    preprocess_prompt: bool = Form(True),
    postprocess_output: bool = Form(True),
) -> FileResponse:
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text is required")

    model = get_model()
    request_id = uuid.uuid4().hex
    request_dir = SHORT_DIR / request_id
    request_dir.mkdir(parents=True, exist_ok=True)

    ref_path = await save_upload(ref_audio, request_dir / "reference.wav")
    gen_config = OmniVoiceGenerationConfig(
        num_step=num_step,
        guidance_scale=guidance_scale,
        denoise=denoise,
        preprocess_prompt=preprocess_prompt,
        postprocess_output=postprocess_output,
    )

    kwargs: Dict[str, Any] = {
        "text": text.strip(),
        "language": language if language and language != "Auto" else None,
        "generation_config": gen_config,
    }
    if speed and speed != 1.0:
        kwargs["speed"] = speed
    if duration and duration > 0:
        kwargs["duration"] = duration

    if mode == "clone":
        if ref_path is None:
            raise HTTPException(status_code=400, detail="Reference audio is required for clone mode")
        kwargs["voice_clone_prompt"] = model.create_voice_clone_prompt(
            ref_audio=str(ref_path),
            ref_text=ref_text or None,
            preprocess_prompt=preprocess_prompt,
        )
    elif mode == "design" and instruct:
        kwargs["instruct"] = instruct.strip()

    with torch.inference_mode():
        audio = model.generate(**kwargs)[0]

    output_path = request_dir / "output.wav"
    sf.write(str(output_path), audio, model.sampling_rate)
    return FileResponse(str(output_path), media_type="audio/wav", filename="omnivoice.wav")


@app.post("/api/jobs/long")
async def create_long_job(
    script_text: Optional[str] = Form(None),
    script_file: Optional[UploadFile] = File(None),
    ref_audio: Optional[UploadFile] = File(None),
    ref_text: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    model: str = Form(DEFAULT_MODEL),
    output_format: str = Form("wav"),
    num_step: int = Form(32),
    max_chars: int = Form(1200),
    min_chars: int = Form(120),
    guidance_scale: float = Form(2.0),
    speed: float = Form(1.0),
    smart_pause: bool = Form(True),
    pause_scale: float = Form(1.0),
    comma_pause: float = Form(0.18),
    sentence_pause: float = Form(0.45),
    paragraph_pause: float = Form(0.75),
    postprocess_output: bool = Form(True),
) -> Dict[str, Any]:
    if not script_text and (script_file is None or not script_file.filename):
        raise HTTPException(status_code=400, detail="script_text or script_file is required")

    async with JOB_SUBMIT_ASYNC_LOCK:
        job_id = uuid.uuid4().hex[:12]

        jdir = job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)

        script_path = jdir / "script.txt"
        if script_file and script_file.filename:
            await save_upload(script_file, script_path)
        else:
            script_path.write_text(script_text or "", encoding="utf-8")

        ref_path = await save_upload(ref_audio, jdir / "reference.wav")
        output_ext = "mp3" if output_format.lower() == "mp3" else "wav"
        output_path = jdir / f"final.{output_ext}"
        work_dir = jdir / "work"
        log_path = jdir / "worker.log"

        cmd: List[str] = [
            sys.executable,
            "-m",
            "omnivoice.cli.long_text",
            "--model",
            model,
            "--input",
            str(script_path),
            "--output",
            str(output_path),
            "--work_dir",
            str(work_dir),
            "--num_step",
            str(num_step),
            "--max_chars",
            str(max_chars),
            "--min_chars",
            str(min_chars),
            "--guidance_scale",
            str(guidance_scale),
            "--speed",
            str(speed),
            "--smart_pause",
            str(smart_pause).lower(),
            "--pause_scale",
            str(pause_scale),
            "--comma_pause",
            str(comma_pause),
            "--sentence_pause",
            str(sentence_pause),
            "--paragraph_pause",
            str(paragraph_pause),
            "--postprocess_output",
            str(postprocess_output).lower(),
            "--device",
            DEFAULT_DEVICE,
            "--dtype",
            DEFAULT_DTYPE,
            "--resume",
            "true",
        ]
        if language:
            cmd += ["--language", language]
        if ref_path is not None:
            cmd += ["--ref_audio", str(ref_path)]
        if ref_text:
            cmd += ["--ref_text", ref_text]

        meta = {
            "id": job_id,
            "user_id": normalize_user_id(user_id),
            "status": "queued",
            "pid": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "script": str(script_path),
            "reference": str(ref_path) if ref_path else None,
            "output": str(output_path),
            "work_dir": str(work_dir),
            "log": str(log_path),
            "command": cmd,
        }
        write_json(job_meta_path(job_id), meta)

    # Trigger dispatcher: nếu rảnh sẽ tự chuyển queued -> running.
    dispatch_next_queued_job()
    return read_job(job_id)


@app.get("/api/jobs")
def list_jobs(user_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    dispatch_next_queued_job()
    return {"jobs": _collect_jobs(user_id=user_id)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    job = refresh_job_status(read_job(job_id))
    ensure_job_access(job, user_id)
    dispatch_next_queued_job()
    updated = read_job(str(job.get("id") or job_id))
    ensure_job_access(updated, user_id)
    return updated


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    logger.info("[cancel_job] request received: job_id=%s", job_id)

    meta = read_job(job_id)
    ensure_job_access(meta, user_id)
    pid = meta.get("pid")
    was_running = bool(pid and is_pid_running(pid))
    logger.info(
        "[cancel_job] before kill: job_id=%s status=%s pid=%s is_running=%s",
        job_id,
        meta.get("status"),
        pid,
        was_running,
    )

    if was_running:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            logger.info(
                "[cancel_job] taskkill result: job_id=%s pid=%s returncode=%s stdout=%s stderr=%s",
                job_id,
                pid,
                result.returncode,
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
        else:
            os.killpg(pid, signal.SIGTERM)
            logger.info("[cancel_job] sent SIGTERM to process group: job_id=%s pid=%s", job_id, pid)

    is_running_after = bool(pid and is_pid_running(pid))
    logger.info(
        "[cancel_job] after kill check: job_id=%s pid=%s is_running=%s",
        job_id,
        pid,
        is_running_after,
    )

    meta["status"] = "cancelled"
    meta["updated_at"] = utc_now()
    write_json(job_meta_path(job_id), meta)

    # Đồng bộ manifest để UI không giữ trạng thái chunk "running" sau khi cancel.
    mpath = manifest_path(job_id)
    if mpath.exists():
        try:
            manifest = read_json(mpath)
            chunks = manifest.get("chunks") or []
            active_chunk_statuses = {"running", "queued", "pending"}
            for chunk in chunks:
                st = str(chunk.get("status", "")).lower()
                if st in active_chunk_statuses:
                    chunk["status"] = "cancelled"
                    chunk["updated_at"] = utc_now()
                    if chunk.get("error") in (None, ""):
                        chunk["error"] = "Cancelled by user"

            summary = manifest.get("summary") or {}
            summary["final_status"] = "cancelled"
            summary["completed_chunks"] = sum(1 for c in chunks if str(c.get("status", "")).lower() in {"done", "merged"})
            summary["failed_chunks"] = sum(1 for c in chunks if str(c.get("status", "")).lower() in {"failed", "cancelled", "stopped"})
            manifest["summary"] = summary
            manifest["updated_at"] = utc_now()
            write_json(mpath, manifest)
            logger.info("[cancel_job] manifest normalized to cancelled: job_id=%s", job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cancel_job] manifest normalize failed: job_id=%s error=%s", job_id, exc)

    dispatch_next_queued_job()

    updated = read_job(job_id)
    logger.info(
        "[cancel_job] response: job_id=%s status=%s output_exists=%s log_exists=%s",
        job_id,
        updated.get("status"),
        updated.get("output_exists"),
        updated.get("log_exists"),
    )
    return updated


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str, user_id: Optional[str] = Query(None)) -> FileResponse:
    meta = read_job(job_id)
    ensure_job_access(meta, user_id)
    output = Path(meta.get("output", ""))
    if not output.exists():
        raise HTTPException(status_code=404, detail="Output file is not ready")
    media = "audio/mpeg" if output.suffix.lower() == ".mp3" else "audio/wav"
    return FileResponse(str(output), media_type=media, filename=output.name)


@app.get("/api/jobs/{job_id}/log")
def download_log(job_id: str, user_id: Optional[str] = Query(None)) -> FileResponse:
    meta = read_job(job_id)
    ensure_job_access(meta, user_id)
    log_path = Path(meta.get("log", ""))
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(str(log_path), media_type="text/plain", filename="worker.log")
