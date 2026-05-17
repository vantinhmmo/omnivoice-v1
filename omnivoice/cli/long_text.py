#!/usr/bin/env python3
"""Long-form text-to-speech renderer for OmniVoice.

This CLI is designed for long jobs such as audiobook/video narration where the
final output can be 2-3 hours long.  It does not try to synthesize the whole
script in one request.  Instead it:

1. Reads a long text file.
2. Splits it into sentence/paragraph-aware chunks.
3. Creates a reusable voice-clone prompt once, if reference audio is provided.
4. Generates one chunk at a time.
5. Saves each chunk WAV immediately.
6. Writes a manifest so interrupted jobs can resume.
7. Merges chunks into a final WAV file.

Example:
    python -m omnivoice.cli.long_text \
        --model k2-fsa/OmniVoice \
        --input script.txt \
        --output out/final.wav \
        --work_dir out/job_001 \
        --ref_audio voice.wav \
        --ref_text "Reference transcript." \
        --language vi \
        --num_step 32
"""

import argparse
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import threading

import numpy as np
import soundfile as sf
import torch

from omnivoice.models.omnivoice import OmniVoice, OmniVoiceGenerationConfig
from omnivoice.utils.common import fix_random_seed, str2bool
from omnivoice.utils.text import add_punctuation, chunk_text_punctuation

logger = logging.getLogger(__name__)


@dataclass
class TextChunkPlan:
    text: str
    paragraph_end: bool = False


@dataclass
class ChunkRecord:
    idx: int
    text: str
    wav: str
    status: str = "pending"
    chars: int = 0
    pause_after: float = 0.0
    pause_reason: str = "none"
    paragraph_end: bool = False
    duration: Optional[float] = None
    elapsed: Optional[float] = None
    error: Optional[str] = None
    updated_at: Optional[str] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_best_device() -> str:
    """Auto-detect the best available device: CUDA > MPS > CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def positive_int(value: str) -> int:
    parsed = int(str(value).strip())
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def env_positive_int(name: str, default: int) -> int:
    try:
        return positive_int(os.environ.get(name, str(default)))
    except Exception:
        return default


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


def normalize_script_text(text: str) -> str:
    """Normalize whitespace without destroying paragraph boundaries."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\x0b\x0c]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_oversized_chunk(text: str, max_chars: int) -> List[str]:
    """Hard-split a too-long chunk at whitespace when punctuation splitting fails."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = max(window.rfind(" "), window.rfind("\n"))
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _last_meaningful_char(text: str) -> str:
    """Return the last punctuation/content char, ignoring closing quotes/brackets."""
    stripped = text.strip()
    closing_marks = set("\"'”’)]}）】》」』>、 ")
    while stripped and stripped[-1] in closing_marks:
        stripped = stripped[:-1].rstrip()
    return stripped[-1] if stripped else ""


def infer_pause_after(text: str, paragraph_end: bool, args: argparse.Namespace) -> tuple[float, str]:
    """Infer a natural pause after a chunk from its final punctuation."""
    if not args.smart_pause:
        return max(0.0, float(args.silence_between_chunks)), "fixed"

    tail = text.strip()
    last_char = _last_meaningful_char(tail)
    if tail.endswith("...") or tail.endswith("…") or tail.endswith("……"):
        pause, reason = args.ellipsis_pause, "ellipsis"
    elif last_char in {",", "，", "、"}:
        pause, reason = args.comma_pause, "comma"
    elif last_char in {";", ":", "；", "："}:
        pause, reason = args.semicolon_pause, "semicolon"
    elif last_char in {"?", "!", "？", "！"}:
        pause, reason = args.question_pause, "question_exclamation"
    elif last_char in {".", "。"}:
        pause, reason = args.sentence_pause, "sentence"
    else:
        pause, reason = args.default_pause, "default"

    if paragraph_end:
        pause = max(float(pause), float(args.paragraph_pause))
        reason = f"paragraph_{reason}"

    return max(0.0, float(pause) * float(args.pause_scale)), reason


def split_long_text(
    text: str,
    max_chars: int = 1200,
    min_chars: int = 120,
) -> List[TextChunkPlan]:
    """Split a long script into model-friendly chunks.

    The function first preserves paragraph boundaries, then uses the project's
    punctuation-aware splitter, and finally hard-splits oversized chunks as a
    safety net.  Each returned item records whether it ends a paragraph so the
    final merge can insert a more natural pause.
    """
    text = normalize_script_text(text)
    if not text:
        raise ValueError("Input text is empty.")

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    plans: List[TextChunkPlan] = []

    for paragraph in paragraphs:
        paragraph_chunks: List[str] = []
        for chunk in chunk_text_punctuation(
            paragraph,
            chunk_len=max_chars,
            min_chunk_len=min_chars,
        ):
            paragraph_chunks.extend(split_oversized_chunk(chunk, max_chars=max_chars))

        for i, chunk in enumerate(paragraph_chunks):
            chunk = add_punctuation(chunk.strip())
            if not chunk:
                continue
            paragraph_end = i == len(paragraph_chunks) - 1
            if (
                plans
                and len(chunk) < min_chars
                and not plans[-1].paragraph_end
                and len(plans[-1].text) + 1 + len(chunk) <= max_chars
            ):
                plans[-1].text = f"{plans[-1].text} {chunk}"
                plans[-1].paragraph_end = paragraph_end
            else:
                plans.append(TextChunkPlan(text=chunk, paragraph_end=paragraph_end))

    return plans


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def read_text_file(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def make_manifest(
    args: argparse.Namespace,
    chunks: List[TextChunkPlan],
    chunks_dir: Path,
) -> Dict[str, Any]:
    records = []
    for idx, chunk in enumerate(chunks):
        wav_path = chunks_dir / f"chunk_{idx:06d}.wav"
        pause_after, pause_reason = infer_pause_after(
            chunk.text,
            paragraph_end=chunk.paragraph_end,
            args=args,
        )
        records.append(
            asdict(
                ChunkRecord(
                    idx=idx,
                    text=chunk.text,
                    wav=str(wav_path),
                    chars=len(chunk.text),
                    pause_after=pause_after,
                    pause_reason=pause_reason,
                    paragraph_end=chunk.paragraph_end,
                )
            )
        )

    return {
        "version": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "input": str(Path(args.input).resolve()),
        "output": str(Path(args.output).resolve()),
        "work_dir": str(Path(args.work_dir).resolve()),
        "model": args.model,
        "language": args.language,
        "generation": {
            "num_step": args.num_step,
            "guidance_scale": args.guidance_scale,
            "t_shift": args.t_shift,
            "denoise": args.denoise,
            "postprocess_output": args.postprocess_output,
            "audio_chunk_duration": args.audio_chunk_duration,
            "audio_chunk_threshold": args.audio_chunk_threshold,
            "speed": args.speed,
            "chunk_workers": args.chunk_workers,
        },
        "split": {
            "max_chars": args.max_chars,
            "min_chars": args.min_chars,
        },
        "pause": {
            "smart_pause": args.smart_pause,
            "pause_scale": args.pause_scale,
            "comma_pause": args.comma_pause,
            "semicolon_pause": args.semicolon_pause,
            "sentence_pause": args.sentence_pause,
            "question_pause": args.question_pause,
            "ellipsis_pause": args.ellipsis_pause,
            "paragraph_pause": args.paragraph_pause,
            "default_pause": args.default_pause,
            "fallback_silence_between_chunks": args.silence_between_chunks,
        },
        "chunks": records,
        "summary": {
            "total_chunks": len(records),
            "completed_chunks": 0,
            "failed_chunks": 0,
            "total_audio_duration": 0.0,
            "total_elapsed": 0.0,
            "final_status": "pending",
        },
    }


def ensure_pause_fields(manifest: Dict[str, Any], args: argparse.Namespace) -> bool:
    """Backfill pause metadata for manifests created by older versions."""
    changed = False
    chunks = manifest.get("chunks", [])
    for record in chunks:
        if "paragraph_end" not in record:
            record["paragraph_end"] = False
            changed = True
        if "pause_after" not in record or "pause_reason" not in record:
            pause_after, pause_reason = infer_pause_after(
                record.get("text", ""),
                paragraph_end=bool(record.get("paragraph_end", False)),
                args=args,
            )
            record["pause_after"] = pause_after
            record["pause_reason"] = pause_reason
            changed = True

    pause_cfg = manifest.setdefault("pause", {})
    for key, value in {
        "smart_pause": args.smart_pause,
        "pause_scale": args.pause_scale,
        "comma_pause": args.comma_pause,
        "semicolon_pause": args.semicolon_pause,
        "sentence_pause": args.sentence_pause,
        "question_pause": args.question_pause,
        "ellipsis_pause": args.ellipsis_pause,
        "paragraph_pause": args.paragraph_pause,
        "default_pause": args.default_pause,
        "fallback_silence_between_chunks": args.silence_between_chunks,
    }.items():
        if key not in pause_cfg:
            pause_cfg[key] = value
            changed = True
    return changed


def load_or_create_manifest(args: argparse.Namespace) -> Dict[str, Any]:
    work_dir = Path(args.work_dir)
    chunks_dir = work_dir / "chunks"
    manifest_path = work_dir / "manifest.json"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    if args.resume and manifest_path.exists() and not args.rebuild_manifest:
        logger.info("Loading existing manifest: %s", manifest_path)
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest_changed = ensure_pause_fields(manifest, args)
        if args.force:
            for record in manifest["chunks"]:
                record["status"] = "pending"
                record["duration"] = None
                record["elapsed"] = None
                record["error"] = None
                record["updated_at"] = utc_now()
            update_manifest_summary(manifest)
            atomic_write_json(manifest_path, manifest)
        elif manifest_changed:
            update_manifest_summary(manifest)
            atomic_write_json(manifest_path, manifest)
        return manifest

    text = read_text_file(Path(args.input))
    chunks = split_long_text(text, max_chars=args.max_chars, min_chars=args.min_chars)
    if not chunks:
        raise ValueError("No text chunks were created from input.")

    logger.info("Created %d text chunks", len(chunks))
    manifest = make_manifest(args, chunks, chunks_dir)
    atomic_write_json(manifest_path, manifest)
    return manifest


def update_manifest_summary(manifest: Dict[str, Any]) -> None:
    chunks = manifest["chunks"]
    completed = [c for c in chunks if c.get("status") == "done"]
    failed = [c for c in chunks if c.get("status") == "failed"]
    total_duration = sum(float(c.get("duration") or 0.0) for c in completed)
    total_elapsed = sum(float(c.get("elapsed") or 0.0) for c in completed)

    if len(completed) == len(chunks):
        final_status = "chunks_done"
    elif failed:
        final_status = "has_failed_chunks"
    else:
        final_status = "running"

    manifest["updated_at"] = utc_now()
    manifest["summary"] = {
        "total_chunks": len(chunks),
        "completed_chunks": len(completed),
        "failed_chunks": len(failed),
        "total_audio_duration": total_duration,
        "total_elapsed": total_elapsed,
        "average_rtf": total_elapsed / total_duration if total_duration > 0 else None,
        "final_status": final_status,
    }


def should_skip_chunk(record: Dict[str, Any], force: bool) -> bool:
    if force:
        return False
    wav_path = Path(record["wav"])
    return record.get("status") == "done" and wav_path.exists() and wav_path.stat().st_size > 0


def clear_device_cache() -> None:
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def save_audio_atomic(path: Path, audio: np.ndarray, sampling_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp.wav")
    sf.write(str(tmp_path), audio, sampling_rate)
    os.replace(tmp_path, path)


def build_generation_config(args: argparse.Namespace) -> OmniVoiceGenerationConfig:
    return OmniVoiceGenerationConfig(
        num_step=args.num_step,
        guidance_scale=args.guidance_scale,
        t_shift=args.t_shift,
        denoise=args.denoise,
        postprocess_output=args.postprocess_output,
        preprocess_prompt=args.preprocess_prompt,
        audio_chunk_duration=args.audio_chunk_duration,
        audio_chunk_threshold=args.audio_chunk_threshold,
        layer_penalty_factor=args.layer_penalty_factor,
        position_temperature=args.position_temperature,
        class_temperature=args.class_temperature,
    )


def render_chunks(
    model: OmniVoice,
    args: argparse.Namespace,
    manifest: Dict[str, Any],
    manifest_path: Path,
) -> Dict[str, Any]:
    gen_config = build_generation_config(args)
    chunk_workers = max(1, int(getattr(args, "chunk_workers", 1) or 1))
    manifest_lock = threading.Lock()

    voice_clone_prompt = None
    if args.ref_audio:
        logger.info("Creating reusable voice clone prompt from %s", args.ref_audio)
        voice_clone_prompt = model.create_voice_clone_prompt(
            ref_audio=args.ref_audio,
            ref_text=args.ref_text,
            preprocess_prompt=args.preprocess_prompt,
        )
        logger.info("Voice clone prompt is ready.")

    def write_manifest_locked() -> None:
        update_manifest_summary(manifest)
        atomic_write_json(manifest_path, manifest)

    def render_one_record(record: Dict[str, Any], total: int) -> None:
        idx = int(record["idx"])
        wav_path = Path(record["wav"])

        if should_skip_chunk(record, args.force):
            logger.info("[%d/%d] Skip existing chunk: %s", idx + 1, total, wav_path)
            return

        logger.info(
            "[%d/%d] Rendering %d chars -> %s",
            idx + 1,
            total,
            len(record["text"]),
            wav_path,
        )

        start = time.time()
        with manifest_lock:
            record["status"] = "running"
            record["error"] = None
            record["updated_at"] = utc_now()
            write_manifest_locked()

        try:
            generation_kwargs: Dict[str, Any] = {
                "text": record["text"],
                "language": args.language,
                "generation_config": gen_config,
            }
            if voice_clone_prompt is not None:
                generation_kwargs["voice_clone_prompt"] = voice_clone_prompt
            elif args.instruct:
                generation_kwargs["instruct"] = args.instruct

            if args.speed is not None and args.speed > 0 and args.speed != 1.0:
                generation_kwargs["speed"] = args.speed

            with torch.inference_mode():
                audio = model.generate(**generation_kwargs)[0]

            elapsed = time.time() - start
            duration = float(audio.shape[-1]) / float(model.sampling_rate)
            save_audio_atomic(wav_path, audio, model.sampling_rate)

            with manifest_lock:
                record["status"] = "done"
                record["duration"] = duration
                record["elapsed"] = elapsed
                record["error"] = None
                record["updated_at"] = utc_now()
                write_manifest_locked()
            logger.info(
                "[%d/%d] Done: duration=%.2fs elapsed=%.2fs rtf=%.3f",
                idx + 1,
                total,
                duration,
                elapsed,
                elapsed / duration if duration > 0 else float("inf"),
            )
        except Exception as e:
            with manifest_lock:
                record["status"] = "failed"
                record["elapsed"] = time.time() - start
                record["error"] = f"{type(e).__name__}: {e}"
                record["updated_at"] = utc_now()
                write_manifest_locked()
            logger.exception("[%d/%d] Failed", idx + 1, total)
            if not args.continue_on_error:
                raise
        finally:
            clear_device_cache()

    total = len(manifest["chunks"])
    pending_records = [
        record
        for record in manifest["chunks"]
        if not should_skip_chunk(record, args.force)
    ]

    if not pending_records:
        logger.info("All chunks are already done. Nothing to render.")
        with manifest_lock:
            write_manifest_locked()
        return manifest

    if chunk_workers == 1:
        logger.info("Rendering chunks sequentially: workers=1")
        for record in manifest["chunks"]:
            render_one_record(record, total)
        return manifest

    logger.warning(
        "Rendering chunks in parallel with %d threads in one process. "
        "This is experimental and may increase VRAM usage or expose model thread-safety issues.",
        chunk_workers,
    )
    with ThreadPoolExecutor(max_workers=chunk_workers) as executor:
        future_to_record = {
            executor.submit(render_one_record, record, total): record
            for record in pending_records
        }
        for future in as_completed(future_to_record):
            try:
                future.result()
            except Exception:
                if not args.continue_on_error:
                    for pending in future_to_record:
                        pending.cancel()
                    raise

    with manifest_lock:
        write_manifest_locked()
    return manifest


def iter_done_chunk_records(manifest: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for record in sorted(manifest["chunks"], key=lambda x: int(x["idx"])):
        if record.get("status") != "done":
            raise RuntimeError(
                f"Chunk {record.get('idx')} is not done; status={record.get('status')}"
            )
        wav_path = Path(record["wav"])
        if not wav_path.exists():
            raise FileNotFoundError(f"Missing chunk wav: {wav_path}")
        yield record


def merge_wav_python(
    chunk_records: List[Dict[str, Any]],
    output_path: Path,
    subtype: str = "PCM_16",
) -> float:
    if not chunk_records:
        raise ValueError("No chunks to merge.")

    first_path = Path(chunk_records[0]["wav"])
    first_info = sf.info(str(first_path))
    samplerate = first_info.samplerate
    channels = first_info.channels
    total_inserted_pause = 0.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    with sf.SoundFile(
        str(tmp_path),
        mode="w",
        samplerate=samplerate,
        channels=channels,
        subtype=subtype,
        format="WAV",
    ) as out_f:
        for i, record in enumerate(chunk_records):
            path = Path(record["wav"])
            info = sf.info(str(path))
            if info.samplerate != samplerate:
                raise ValueError(
                    f"Sample rate mismatch: {path} has {info.samplerate}, expected {samplerate}"
                )
            with sf.SoundFile(str(path), mode="r") as in_f:
                while True:
                    block = in_f.read(frames=65536, dtype="float32", always_2d=True)
                    if block.size == 0:
                        break
                    out_f.write(block)
            if i != len(chunk_records) - 1:
                pause = max(0.0, float(record.get("pause_after") or 0.0))
                silence_frames = int(pause * samplerate)
                if silence_frames > 0:
                    silence = np.zeros((silence_frames, channels), dtype=np.float32)
                    out_f.write(silence)
                    total_inserted_pause += silence_frames / samplerate

    os.replace(tmp_path, output_path)
    return total_inserted_pause


def convert_with_ffmpeg(input_wav: Path, output_path: Path, audio_bitrate: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found; cannot convert final audio format.")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_wav),
        "-b:a",
        audio_bitrate,
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def merge_final_audio(args: argparse.Namespace, manifest: Dict[str, Any]) -> None:
    output_path = Path(args.output)
    chunk_records = list(iter_done_chunk_records(manifest))

    logger.info("Merging %d chunks -> %s", len(chunk_records), output_path)

    if output_path.suffix.lower() == ".wav":
        inserted_pause = merge_wav_python(
            chunk_records,
            output_path,
            subtype=args.wav_subtype,
        )
    else:
        tmp_wav = output_path.with_suffix(".merge_tmp.wav")
        inserted_pause = merge_wav_python(
            chunk_records,
            tmp_wav,
            subtype=args.wav_subtype,
        )
        try:
            convert_with_ffmpeg(tmp_wav, output_path, args.audio_bitrate)
        finally:
            if tmp_wav.exists():
                tmp_wav.unlink()

    manifest["summary"]["final_status"] = "merged"
    manifest["summary"]["final_output"] = str(output_path.resolve())
    manifest["summary"]["final_size_bytes"] = output_path.stat().st_size
    manifest["summary"]["inserted_pause_duration"] = inserted_pause
    manifest["updated_at"] = utc_now()


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Long-form OmniVoice renderer with chunking, resume, and merge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--model", default="k2-fsa/OmniVoice")
    parser.add_argument("--input", required=True, help="Path to long UTF-8 text file.")
    parser.add_argument("--output", required=True, help="Final output path (.wav or .mp3).")
    parser.add_argument(
        "--work_dir",
        required=True,
        help="Working directory for chunks and manifest.json.",
    )

    parser.add_argument("--language", default=None, help="Language name or code, e.g. vi/en.")
    parser.add_argument("--ref_audio", default=None, help="Short reference audio for cloning.")
    parser.add_argument("--ref_text", default=None, help="Transcript of reference audio.")
    parser.add_argument("--instruct", default=None, help="Voice design instruction if no ref_audio.")

    parser.add_argument("--max_chars", type=int, default=1200)
    parser.add_argument("--min_chars", type=int, default=120)

    parser.add_argument("--num_step", type=int, default=32)
    parser.add_argument("--guidance_scale", type=float, default=2.0)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--t_shift", type=float, default=0.1)
    parser.add_argument("--denoise", type=str2bool, default=True)
    parser.add_argument("--preprocess_prompt", type=str2bool, default=True)
    parser.add_argument("--postprocess_output", type=str2bool, default=True)
    parser.add_argument("--audio_chunk_duration", type=float, default=15.0)
    parser.add_argument("--audio_chunk_threshold", type=float, default=30.0)
    parser.add_argument("--layer_penalty_factor", type=float, default=5.0)
    parser.add_argument("--position_temperature", type=float, default=5.0)
    parser.add_argument("--class_temperature", type=float, default=0.0)

    parser.add_argument("--device", default=None, help="cuda, cuda:0, cpu, mps, ...")
    parser.add_argument(
        "--dtype",
        type=lambda value: value.strip().lower(),
        choices=["auto", "float16", "bfloat16", "float32"],
        default="auto",
    )
    parser.add_argument("--load_asr", type=str2bool, default=False)
    parser.add_argument("--seed", type=int, default=42)

    parser.add_argument("--resume", type=str2bool, default=True)
    parser.add_argument("--force", type=str2bool, default=False)
    parser.add_argument("--rebuild_manifest", type=str2bool, default=False)
    parser.add_argument("--continue_on_error", type=str2bool, default=False)
    parser.add_argument("--merge", type=str2bool, default=True)
    parser.add_argument(
        "--smart_pause",
        type=str2bool,
        default=True,
        help="Insert different pauses after chunks based on ending punctuation.",
    )
    parser.add_argument(
        "--pause_scale",
        type=float,
        default=1.0,
        help="Multiply all smart pause durations by this factor.",
    )
    parser.add_argument(
        "--comma_pause",
        type=float,
        default=0.18,
        help="Pause after comma-like punctuation, in seconds.",
    )
    parser.add_argument(
        "--semicolon_pause",
        type=float,
        default=0.28,
        help="Pause after semicolon/colon punctuation, in seconds.",
    )
    parser.add_argument(
        "--sentence_pause",
        type=float,
        default=0.45,
        help="Pause after sentence-ending period punctuation, in seconds.",
    )
    parser.add_argument(
        "--question_pause",
        type=float,
        default=0.52,
        help="Pause after question/exclamation punctuation, in seconds.",
    )
    parser.add_argument(
        "--ellipsis_pause",
        type=float,
        default=0.65,
        help="Pause after ellipsis punctuation, in seconds.",
    )
    parser.add_argument(
        "--paragraph_pause",
        type=float,
        default=0.75,
        help="Minimum pause after paragraph-ending chunks, in seconds.",
    )
    parser.add_argument(
        "--default_pause",
        type=float,
        default=0.25,
        help="Pause after chunks without recognizable ending punctuation, in seconds.",
    )
    parser.add_argument(
        "--silence_between_chunks",
        type=float,
        default=0.2,
        help="Fallback fixed pause when --smart_pause false, in seconds.",
    )
    parser.add_argument("--wav_subtype", default="PCM_16")
    parser.add_argument("--audio_bitrate", default="192k")
    parser.add_argument(
        "--chunk_workers",
        type=positive_int,
        default=env_positive_int("OMNIVOICE_CHUNK_WORKERS", 1),
        help=(
            "Number of chunks to render concurrently inside one long-text job. "
            "Experimental; increase only if GPU VRAM is sufficient."
        ),
    )

    return parser


def main() -> None:
    formatter = "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    logging.basicConfig(format=formatter, level=logging.INFO, force=True)

    args = get_parser().parse_args()
    fix_random_seed(args.seed)

    work_dir = Path(args.work_dir)
    manifest_path = work_dir / "manifest.json"
    manifest = load_or_create_manifest(args)

    device = args.device or get_best_device()
    dtype = resolve_dtype(args.dtype, device)
    logger.info("Loading model from %s on %s with dtype=%s", args.model, device, dtype)
    model = OmniVoice.from_pretrained(
        args.model,
        device_map=device,
        dtype=dtype,
        load_asr=args.load_asr,
    )
    model.eval()
    logger.info("Model loaded. Sampling rate: %s", model.sampling_rate)

    try:
        manifest = render_chunks(model, args, manifest, manifest_path)
        if args.merge:
            merge_final_audio(args, manifest)
            atomic_write_json(manifest_path, manifest)
            logger.info("Final audio saved to %s", args.output)
        else:
            logger.info("Merge disabled. Chunks are saved under %s", work_dir / "chunks")
    finally:
        clear_device_cache()

    summary = manifest.get("summary", {})
    logger.info(
        "Summary: chunks=%s/%s failed=%s audio=%.2fs elapsed=%.2fs rtf=%s",
        summary.get("completed_chunks"),
        summary.get("total_chunks"),
        summary.get("failed_chunks"),
        float(summary.get("total_audio_duration") or 0.0),
        float(summary.get("total_elapsed") or 0.0),
        summary.get("average_rtf"),
    )


if __name__ == "__main__":
    main()
