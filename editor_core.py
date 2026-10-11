from __future__ import annotations

import csv
import difflib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


class EditorError(RuntimeError):
    pass


@dataclass
class MediaItem:
    path: str
    kind: str
    source_duration: float = 0.0
    assigned_duration: float = 0.0
    motion: str = "none"
    source_start: float = 0.0
    source_end: float = 0.0
    placeholder_text: str = ""
    ai_confidence: float = -1.0
    candidate_path: str = ""
    candidate_kind: str = ""
    effect: str = "none"
    effect_strength: float = 0.65
    transition: str = "none"
    transition_duration: float = 0.35
    playback_speed: float = 1.0

    @property
    def name(self) -> str:
        if self.kind == "gap":
            summary = " ".join(self.placeholder_text.split())
            return f"NEEDS IMAGE — {summary[:54]}" if summary else "NEEDS IMAGE"
        return Path(self.path).name


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    words: list[dict] = field(default_factory=list)


@dataclass
class ExportSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    crf: int = 20
    video_bitrate_kbps: int = 0
    preset: str = "medium"
    background: str = "111318"
    burn_captions: bool = False
    caption_font_size: int = 22
    caption_margin: int = 42
    caption_x: float = 0.5
    caption_y: float = 0.86
    caption_color: str = "FFFFFF"
    caption_highlight_color: str = "FFD400"
    ai_max_image_duration: float = 5.0
    ai_min_confidence: float = 0.30


def youtube_video_bitrate_kbps(width: int, height: int, fps: int) -> int:
    """Return YouTube's recommended SDR upload bitrate for common output sizes."""
    short_edge = min(width, height)
    long_edge = max(width, height)
    high_frame_rate = fps >= 48
    if long_edge >= 7680 or short_edge >= 4320:
        return 120_000 if high_frame_rate else 120_000
    if long_edge >= 3840 or short_edge >= 2160:
        return 60_000 if high_frame_rate else 40_000
    if long_edge >= 2560 or short_edge >= 1440:
        return 24_000 if high_frame_rate else 16_000
    if long_edge >= 1920 or short_edge >= 1080:
        return 12_000 if high_frame_rate else 8_000
    if long_edge >= 1280 or short_edge >= 720:
        return 7_500 if high_frame_rate else 5_000
    if long_edge >= 854 or short_edge >= 480:
        return 4_000 if high_frame_rate else 2_500
    return 1_500 if high_frame_rate else 1_000


ProgressCallback = Callable[[float, str], None]


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise EditorError(f"Missing required tools: {', '.join(missing)}")


def numeric_sort_key(path: str | Path) -> tuple:
    """Natural numeric order independent of extension and invisible filename marks."""
    stem = unicodedata.normalize("NFKC", Path(path).stem).casefold()
    stem = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", stem).strip()
    chunks = [chunk for chunk in re.split(r"(\d+)", stem) if chunk]
    numbers = [int(chunk) for chunk in chunks if chunk.isdigit()]
    natural = tuple((0, int(chunk)) if chunk.isdigit() else (1, chunk) for chunk in chunks)
    # The first number is the user's scene number.  Keeping it ahead of the
    # remaining natural tokens also handles names such as "image 260.webp".
    first_number = numbers[0] if numbers else 0
    return (0 if numbers else 1, first_number, natural, stem, Path(path).suffix.casefold())


def filename_timestamp(path: str | Path) -> float | None:
    """Read a leading M-SS or H-MM-SS timestamp such as 0-03 or #14-24."""
    stem = unicodedata.normalize("NFKC", Path(path).stem).strip()
    three_part = re.match(r"^#?\s*(\d+)[-_:](\d{2})[-_:](\d{2})(?=\D|$)", stem)
    if three_part:
        hours, minutes, seconds = (int(value) for value in three_part.groups())
        if minutes >= 60 or seconds >= 60:
            return None
        return float(hours * 3600 + minutes * 60 + seconds)
    two_part = re.match(r"^#?\s*(\d+)[-_:](\d{2})(?=\D|$)", stem)
    if two_part:
        minutes, seconds = (int(value) for value in two_part.groups())
        if seconds >= 60:
            return None
        return float(minutes * 60 + seconds)
    return None


def has_filename_timing(items: list[MediaItem]) -> bool:
    real_items = [item for item in items if item.kind != "gap"]
    return len(real_items) >= 2 and all(filename_timestamp(item.path) is not None for item in real_items)


def assign_filename_timing(
    items: list[MediaItem], audio_duration: float, *, skip_invalid: bool = False
) -> list[float]:
    """Place media at the exact timestamps encoded at the start of each filename."""
    if audio_duration <= 0:
        raise EditorError("Audio duration must be greater than zero")
    real_items = [item for item in items if item.kind != "gap"]
    if not real_items:
        raise EditorError("Add timestamped images or videos first.")
    parsed = [(filename_timestamp(item.path), item) for item in real_items]
    invalid = [item.name for timestamp, item in parsed if timestamp is None]
    if invalid:
        if not skip_invalid:
            raise EditorError(
                "Filename timing needs names such as 0-00, 0-03, 14-24, or 1-02-15. "
                f"Could not read: {', '.join(invalid[:5])}"
            )
        parsed = [(timestamp, item) for timestamp, item in parsed if timestamp is not None]
        if not parsed:
            raise EditorError("No timestamped media remains after skipping invalid filenames.")
    ordered = sorted(((float(timestamp), item) for timestamp, item in parsed), key=lambda pair: pair[0])
    timestamps = [timestamp for timestamp, _ in ordered]
    if len(set(timestamps)) != len(timestamps):
        raise EditorError("Two or more media files have the same filename timestamp.")
    if timestamps[-1] >= audio_duration:
        raise EditorError(
            f"Last media timestamp {format_time(timestamps[-1])} is outside narration "
            f"ending at {format_time(audio_duration)}."
        )

    timeline: list[MediaItem] = []
    if timestamps[0] > 0.01:
        timeline.append(MediaItem(
            path="", kind="gap", assigned_duration=timestamps[0],
            placeholder_text=f"No timestamped visual before {format_time(timestamps[0])}",
        ))
    for index, (start, item) in enumerate(ordered):
        end = timestamps[index + 1] if index + 1 < len(timestamps) else audio_duration
        item.assigned_duration = end - start
        item.ai_confidence = -1.0
        timeline.append(item)
    items[:] = timeline

    boundaries = [0.0]
    for item in items:
        boundaries.append(boundaries[-1] + item.assigned_duration)
    return boundaries


def media_kind(path: str | Path) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "unsupported"


def run_json(command: list[str]) -> dict:
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode:
        raise EditorError(proc.stderr.strip() or "Command failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EditorError("Could not read media metadata") from exc


def probe(path: str | Path) -> dict:
    require_tools()
    return run_json([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ])


def duration_of(path: str | Path) -> float:
    data = probe(path)
    try:
        value = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        for stream in data.get("streams", []):
            try:
                value = max(value, float(stream.get("duration", 0)))
            except (TypeError, ValueError):
                pass
    if value <= 0:
        raise EditorError(f"Could not determine duration: {path}")
    return value


def discover_media(paths: Iterable[str | Path]) -> list[MediaItem]:
    expanded: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            expanded.extend(p for p in path.iterdir() if p.is_file())
        elif path.is_file():
            expanded.append(path)
    valid = [p for p in expanded if media_kind(p) in {"image", "video"}]
    items: list[MediaItem] = []
    for path in sorted(dict.fromkeys(valid), key=numeric_sort_key):
        kind = media_kind(path)
        source_duration = duration_of(path) if kind == "video" else 0.0
        items.append(MediaItem(str(path.resolve()), kind, source_duration))
    return items


def format_time(seconds: float, srt: bool = False) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    millis = int(round((seconds - math.floor(seconds)) * 1000))
    if millis == 1000:
        whole_seconds += 1
        millis = 0
    sep = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{sep}{millis:03d}"


def detect_silence(audio_path: str | Path, noise_db: int = -35, minimum: float = 0.28) -> list[tuple[float, float]]:
    require_tools()
    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={minimum}", "-f", "null", os.devnull,
    ]
    proc = subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode:
        raise EditorError(proc.stderr.strip() or "Silence detection failed")
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", proc.stderr)]
    ends = [
        (float(end), float(duration))
        for end, duration in re.findall(r"silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)", proc.stderr)
    ]
    pairs: list[tuple[float, float]] = []
    for index, (end, measured_duration) in enumerate(ends):
        start = starts[index] if index < len(starts) else max(0.0, end - measured_duration)
        pairs.append((start, end))
    return pairs


def _pick_pause_boundaries(
    total: float,
    count: int,
    silences: list[tuple[float, float]],
    minimum_scene: float,
) -> list[float]:
    if count <= 1:
        return [0.0, total]
    targets = [total * i / count for i in range(1, count)]
    candidates = [
        (end, end - start)
        for start, end in silences
        if minimum_scene <= end <= total - minimum_scene
    ]
    used: set[int] = set()
    selected: list[float] = []
    search_window = max(2.0, total / count * 0.8)
    for target in targets:
        best_index = None
        best_score = float("inf")
        for index, (position, silence_length) in enumerate(candidates):
            if index in used or abs(position - target) > search_window:
                continue
            if any(abs(position - existing) < minimum_scene for existing in selected):
                continue
            score = abs(position - target) - min(silence_length, 2.0) * 0.35
            if score < best_score:
                best_score = score
                best_index = index
        if best_index is None:
            position = target
        else:
            used.add(best_index)
            position = candidates[best_index][0]
        selected.append(position)
    boundaries = [0.0, *sorted(selected), total]
    # Guard against duplicate/near-zero segments after fallback placement.
    for index in range(1, len(boundaries) - 1):
        lower = boundaries[index - 1] + 0.12
        upper = boundaries[index + 1] - 0.12
        boundaries[index] = min(max(boundaries[index], lower), upper)
    return boundaries


def _pick_transcript_boundaries(
    total: float,
    count: int,
    transcript: list[TranscriptSegment],
    minimum_scene: float,
) -> list[float]:
    """Place sequential scene changes on nearby words, punctuation, and speech gaps."""
    if count <= 1:
        return [0.0, total]
    if not transcript:
        raise EditorError("Generate the transcript before using Speech timing.")

    minimum_scene = min(minimum_scene, total / count * 0.45)
    average_scene = total / count
    candidates: dict[float, float] = {}
    all_words: list[dict] = []
    for segment in transcript:
        words = caption_word_timings(segment)
        all_words.extend(words)
        if minimum_scene <= segment.end <= total - minimum_scene:
            candidates[round(segment.end, 3)] = max(candidates.get(round(segment.end, 3), 0.0), 3.0)

    sentence_marks = (".", "!", "?", "।", "॥")
    clause_marks = (",", ";", ":")
    for index, word in enumerate(all_words):
        position = float(word.get("end", 0.0))
        if not minimum_scene <= position <= total - minimum_scene:
            continue
        text = str(word.get("word", "")).strip()
        strength = 0.35
        if text.endswith(sentence_marks):
            strength = 3.5
        elif text.endswith(clause_marks):
            strength = 1.8
        if index + 1 < len(all_words):
            gap = max(0.0, float(all_words[index + 1].get("start", position)) - position)
            strength += min(2.5, gap * 5.0)
        key = round(position, 3)
        candidates[key] = max(candidates.get(key, 0.0), strength)

    ranked = sorted(candidates.items())
    boundaries = [0.0]
    used: set[int] = set()
    search_window = max(1.25, average_scene * 0.85)
    for scene_index in range(1, count):
        target = total * scene_index / count
        lower = boundaries[-1] + minimum_scene
        upper = total - (count - scene_index) * minimum_scene
        best_index = None
        best_score = float("inf")
        for candidate_index, (position, strength) in enumerate(ranked):
            if candidate_index in used or not lower <= position <= upper:
                continue
            distance = abs(position - target)
            if distance > search_window:
                continue
            score = distance - strength * average_scene * 0.12
            if score < best_score:
                best_score = score
                best_index = candidate_index
        if best_index is None:
            position = min(max(target, lower), upper)
        else:
            used.add(best_index)
            position = ranked[best_index][0]
        boundaries.append(position)
    boundaries.append(total)
    return boundaries


def assign_durations(
    items: list[MediaItem],
    audio_duration: float,
    mode: str,
    audio_path: str | Path | None = None,
    minimum_scene: float = 0.5,
    transcript: list[TranscriptSegment] | None = None,
) -> list[float]:
    if not items:
        raise EditorError("Add at least one image or video")
    if audio_duration <= 0:
        raise EditorError("Audio duration must be greater than zero")
    if mode == "speech":
        boundaries = _pick_transcript_boundaries(
            audio_duration, len(items), transcript or [], minimum_scene
        )
    elif mode == "pause" and audio_path:
        boundaries = _pick_pause_boundaries(
            audio_duration, len(items), detect_silence(audio_path), minimum_scene
        )
    else:
        boundaries = [audio_duration * i / len(items) for i in range(len(items) + 1)]
    durations = [boundaries[i + 1] - boundaries[i] for i in range(len(items))]
    # Eliminate floating point drift so the final timeline exactly matches audio.
    durations[-1] += audio_duration - sum(durations)
    for item, assigned in zip(items, durations):
        item.assigned_duration = assigned
    return boundaries


def transcript_backend() -> str | None:
    if importlib.util.find_spec("faster_whisper"):
        return "faster-whisper"
    if shutil.which("whisper"):
        return "openai-whisper-cli"
    return None


def decode_audio_for_whisper(audio_path: str | Path, sampling_rate: int = 16000):
    """Decode with FFmpeg to avoid PyAV/version-specific container issues."""
    import numpy as np

    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
            "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(sampling_rate), "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise EditorError(proc.stderr.decode("utf-8", "replace").strip() or "Audio decoding failed")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_audio(
    audio_path: str | Path,
    model_name: str = "small",
    language: str | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[str, list[TranscriptSegment]]:
    backend = transcript_backend()
    if progress:
        progress(0.02, "Loading transcription model…")
    if backend == "faster-whisper":
        from faster_whisper import WhisperModel  # type: ignore

        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        if progress:
            progress(0.04, "Model ready · decoding audio…")
        audio_duration = duration_of(audio_path)
        decoded_audio = decode_audio_for_whisper(audio_path)
        if progress:
            progress(0.07, "Audio decoded · starting transcription…")
        segments, info = model.transcribe(
            decoded_audio, language=language, vad_filter=True, word_timestamps=True,
        )
        result: list[TranscriptSegment] = []
        for segment in segments:
            words = [
                {"start": float(word.start), "end": float(word.end), "word": word.word.strip()}
                for word in (segment.words or [])
                if word.start is not None and word.end is not None and word.word.strip()
            ]
            result.append(TranscriptSegment(
                float(segment.start), float(segment.end), segment.text.strip(), words,
            ))
            if progress:
                completed = min(0.98, max(0.08, segment.end / max(audio_duration, 0.01)))
                progress(completed, f"Transcribing · {len(result)} captions")
        return info.language, result
    if backend == "openai-whisper-cli":
        with tempfile.TemporaryDirectory(prefix="storycut_whisper_") as temp_dir:
            command = [
                "whisper", str(audio_path), "--model", model_name, "--output_format", "json",
                "--output_dir", temp_dir, "--word_timestamps", "True",
            ]
            if language:
                command.extend(["--language", language])
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.returncode:
                raise EditorError(proc.stdout.strip() or "Transcription failed")
            output = Path(temp_dir) / (Path(audio_path).stem + ".json")
            data = json.loads(output.read_text(encoding="utf-8"))
            result = [
                TranscriptSegment(
                    float(s["start"]), float(s["end"]), str(s["text"]).strip(),
                    [
                        {"start": float(w["start"]), "end": float(w["end"]),
                         "word": str(w.get("word", "")).strip()}
                        for w in s.get("words", [])
                        if w.get("start") is not None and w.get("end") is not None
                        and str(w.get("word", "")).strip()
                    ],
                )
                for s in data.get("segments", [])
            ]
            return str(data.get("language", language or "auto")), result
    raise EditorError(
        "No local transcription engine found. Install faster-whisper with: "
        "python -m pip install faster-whisper"
    )


def _is_compact_script_character(character: str) -> bool:
    value = ord(character)
    return (
        0x3040 <= value <= 0x30FF or  # Japanese kana
        0x3400 <= value <= 0x9FFF or  # CJK ideographs
        0xAC00 <= value <= 0xD7AF     # Korean Hangul
    )


def script_tokens(text: str) -> list[dict]:
    """Tokenize Unicode script while preserving whether a visible space preceded each token."""
    tokens: list[dict] = []
    buffer = ""
    buffer_space = False
    pending_space = False

    def flush() -> None:
        nonlocal buffer, buffer_space
        if buffer:
            tokens.append({"word": buffer, "space_before": buffer_space and bool(tokens)})
            buffer = ""
            buffer_space = False

    for character in unicodedata.normalize("NFC", text.strip()):
        if character.isspace():
            flush()
            pending_space = True
            continue
        category = unicodedata.category(character)
        if _is_compact_script_character(character):
            flush()
            tokens.append({"word": character, "space_before": pending_space and bool(tokens)})
            pending_space = False
        elif category.startswith(("P", "S")):
            if buffer:
                buffer += character
            elif tokens:
                tokens[-1]["word"] += character
            else:
                buffer, buffer_space = character, pending_space
            pending_space = False
        else:
            if not buffer:
                buffer_space = pending_space and bool(tokens)
            buffer += character
            pending_space = False
    flush()
    return tokens


def _normalized_alignment_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized
        if not unicodedata.category(character).startswith(("P", "S", "Z"))
    )


def _timed_guide_tokens(transcript: list[TranscriptSegment]) -> list[dict]:
    result: list[dict] = []
    for segment in transcript:
        for timed_word in caption_word_timings(segment):
            pieces = script_tokens(str(timed_word["word"])) or [{"word": str(timed_word["word"])}]
            start, end = float(timed_word["start"]), float(timed_word["end"])
            weights = [max(1, len(_normalized_alignment_token(piece["word"]))) for piece in pieces]
            total = max(1, sum(weights))
            cursor = start
            for index, (piece, weight) in enumerate(zip(pieces, weights)):
                piece_end = end if index == len(pieces) - 1 else cursor + (end - start) * weight / total
                result.append({"word": piece["word"], "start": cursor, "end": piece_end})
                cursor = piece_end
    return result


def _join_script_words(words: list[dict]) -> str:
    result = ""
    for index, entry in enumerate(words):
        if index and entry.get("space_before", True):
            result += " "
        result += str(entry["word"])
    return result


def align_script_to_transcript(
    script: str,
    guide_transcript: list[TranscriptSegment],
    audio_duration: float,
    max_segment_duration: float = 4.0,
    max_words: int = 9,
) -> tuple[list[TranscriptSegment], float]:
    """Keep exact user script text while transferring timings from recognized speech."""
    requested = script_tokens(script)
    if not requested:
        raise EditorError("The imported script is empty.")
    guide = _timed_guide_tokens(guide_transcript)
    script_keys = [_normalized_alignment_token(entry["word"]) for entry in requested]
    guide_keys = [_normalized_alignment_token(entry["word"]) for entry in guide]
    timed: list[dict | None] = [None] * len(requested)
    matcher = difflib.SequenceMatcher(None, script_keys, guide_keys, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            source = guide[block.b + offset]
            requested_entry = requested[block.a + offset]
            timed[block.a + offset] = {
                **requested_entry, "start": float(source["start"]), "end": float(source["end"]),
            }

    cursor = 0
    guide_start = float(guide[0]["start"]) if guide else 0.0
    guide_end = float(guide[-1]["end"]) if guide else audio_duration
    while cursor < len(timed):
        if timed[cursor] is not None:
            cursor += 1
            continue
        run_start = cursor
        while cursor < len(timed) and timed[cursor] is None:
            cursor += 1
        run_end = cursor
        start = float(timed[run_start - 1]["end"]) if run_start else guide_start
        end = float(timed[run_end]["start"]) if run_end < len(timed) else guide_end
        if end <= start:
            end = min(audio_duration, start + max(0.08, 0.25 * (run_end - run_start)))
        weights = [
            max(1, len(_normalized_alignment_token(requested[index]["word"])))
            for index in range(run_start, run_end)
        ]
        total = max(1, sum(weights))
        word_cursor = start
        for index, weight in zip(range(run_start, run_end), weights):
            word_end = end if index == run_end - 1 else word_cursor + (end - start) * weight / total
            timed[index] = {**requested[index], "start": word_cursor, "end": word_end}
            word_cursor = word_end

    aligned = [entry for entry in timed if entry is not None]
    # Enforce monotonic, usable timings even when Whisper produced overlapping words.
    previous = 0.0
    for entry in aligned:
        latest_start = max(0.0, audio_duration - 0.01)
        entry["start"] = max(previous, min(latest_start, float(entry["start"])))
        entry["end"] = min(
            audio_duration, max(entry["start"] + 0.01, float(entry["end"]))
        )
        previous = entry["end"]

    segments: list[TranscriptSegment] = []
    current: list[dict] = []
    for entry in aligned:
        would_exceed = current and (
            len(current) >= max_words or
            float(entry["end"]) - float(current[0]["start"]) > max_segment_duration or
            len(_join_script_words(current + [entry])) > 72
        )
        if would_exceed:
            segments.append(TranscriptSegment(
                float(current[0]["start"]), float(current[-1]["end"]),
                _join_script_words(current), current,
            ))
            current = []
        current.append(entry)
    if current:
        segments.append(TranscriptSegment(
            float(current[0]["start"]), float(current[-1]["end"]),
            _join_script_words(current), current,
        ))
    return segments, matcher.ratio() if guide else 0.0


def read_script_file(path: str | Path) -> str:
    """Read plain text, SRT/VTT, or DOCX without changing the original script."""
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(source) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = []
            for paragraph in root.iter(namespace + "p"):
                text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
                if text:
                    paragraphs.append(text)
            return "\n".join(paragraphs)
        except Exception as exc:
            raise EditorError(f"Could not read DOCX script: {exc}") from exc
    data = source.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if suffix in {".srt", ".vtt"}:
        kept = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.isdigit() or "-->" in stripped or stripped.upper() == "WEBVTT":
                continue
            kept.append(stripped)
        return "\n".join(kept)
    return text.strip()


def write_srt(segments: list[TranscriptSegment], output_path: str | Path) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments, 1):
        lines.extend([
            str(index),
            f"{format_time(segment.start, True)} --> {format_time(segment.end, True)}",
            segment.text.strip(),
            "",
        ])
    Path(output_path).write_text("\n".join(lines), encoding="utf-8-sig")


def caption_word_timings(segment: TranscriptSegment) -> list[dict]:
    """Return usable word timestamps, estimating them for old/non-word-level transcripts."""
    explicit: list[dict] = []
    for entry in segment.words or []:
        try:
            start = max(segment.start, float(entry["start"]))
            end = min(segment.end, float(entry["end"]))
            word = str(entry.get("word", "")).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if word and end > start:
            explicit.append({
                "start": start, "end": end, "word": word,
                "space_before": bool(entry.get("space_before", True)),
            })
    if explicit:
        return sorted(explicit, key=lambda value: (value["start"], value["end"]))

    words = re.findall(r"\S+", segment.text.strip())
    if not words:
        return []
    duration = max(0.01, segment.end - segment.start)
    weights = [max(1, len(re.sub(r"[^\w]", "", word, flags=re.UNICODE))) for word in words]
    total_weight = max(1, sum(weights))
    result: list[dict] = []
    cursor = segment.start
    for index, (word, weight) in enumerate(zip(words, weights)):
        end = segment.end if index == len(words) - 1 else cursor + duration * weight / total_weight
        result.append({"start": cursor, "end": end, "word": word})
        cursor = end
    return result


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _ass_color(rgb: str) -> str:
    value = re.sub(r"[^0-9A-Fa-f]", "", rgb)[-6:].zfill(6).upper()
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}&"


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _karaoke_line(words: list[dict], active_index: int | None, normal: str, highlight: str) -> str:
    parts: list[str] = []
    for index, entry in enumerate(words):
        word = _ass_escape(str(entry["word"]))
        prefix = " " if index and entry.get("space_before", True) else ""
        if index == active_index:
            parts.append(f"{prefix}{{\\1c{highlight}}}{word}{{\\1c{normal}}}")
        else:
            parts.append(prefix + word)
    return "".join(parts)


def write_karaoke_ass(
    segments: list[TranscriptSegment],
    output_path: str | Path,
    settings: ExportSettings,
) -> None:
    """Write movable ASS captions where only the currently spoken word is highlighted."""
    normal = _ass_color(settings.caption_color)
    highlight = _ass_color(settings.caption_highlight_color)
    x = round(settings.width * max(0.0, min(1.0, settings.caption_x)))
    y = round(settings.height * max(0.0, min(1.0, settings.caption_y)))
    font_size = max(12, settings.caption_font_size)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {settings.width}",
        f"PlayResY: {settings.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{font_size},{normal},{normal},&H00101010,&H90000000,"
        "-1,0,0,0,100,100,0,0,3,2,0,5,24,24,20,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    position = f"{{\\pos({x},{y})}}"
    for segment in segments:
        words = caption_word_timings(segment)
        if not words:
            continue
        cursor = segment.start
        normal_line = _karaoke_line(words, None, normal, highlight)
        for index, word in enumerate(words):
            start = max(cursor, float(word["start"]))
            end = max(start + 0.01, min(segment.end, float(word["end"])))
            if start > cursor + 0.005:
                lines.append(
                    f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(start)},Default,,0,0,0,,{position}{normal_line}"
                )
            highlighted = _karaoke_line(words, index, normal, highlight)
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{position}{highlighted}"
            )
            cursor = end
        if segment.end > cursor + 0.005:
            lines.append(
                f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(segment.end)},Default,,0,0,0,,{position}{normal_line}"
            )
    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def write_timeline_csv(items: list[MediaItem], output_path: str | Path) -> None:
    cursor = 0.0
    with Path(output_path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["scene", "filename", "type", "motion", "effect", "effect_strength",
                         "transition", "transition_duration", "playback_speed", "source_in", "source_out",
                         "start", "end", "duration"])
        for index, item in enumerate(items, 1):
            end = cursor + item.assigned_duration
            writer.writerow([
                index, item.name, item.kind, item.motion, item.effect,
                f"{item.effect_strength:.2f}", item.transition,
                f"{item.transition_duration:.2f}", f"{item.playback_speed:.2f}",
                format_time(item.source_start),
                format_time(item.source_end or item.source_duration) if item.kind == "video" else "",
                format_time(cursor), format_time(end),
                f"{item.assigned_duration:.3f}",
            ])
            cursor = end


def _escape_subtitle_path(path: str | Path) -> str:
    value = str(Path(path).resolve()).replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'")
    return value


def _subtitle_filter(path: str | Path, settings: ExportSettings) -> str:
    escaped = _escape_subtitle_path(path)
    if Path(path).suffix.casefold() == ".ass":
        return f"subtitles=filename='{escaped}'"
    style = (
        f"FontName=Arial,FontSize={settings.caption_font_size},PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00101010,BorderStyle=3,Outline=1,Shadow=0,"
        f"MarginV={settings.caption_margin},Alignment=2"
    )
    return f"subtitles=filename='{escaped}':force_style='{style}'"


def _scene_frame_counts(items: list[MediaItem], fps: int) -> list[int]:
    """Quantize cumulative boundaries so rounding drift does not grow per scene."""
    counts: list[int] = []
    cursor = 0.0
    previous_frame = 0
    for item in items:
        cursor += item.assigned_duration
        boundary_frame = round(cursor * fps)
        frames = max(1, boundary_frame - previous_frame)
        counts.append(frames)
        previous_frame += frames
    return counts


def _motion_filter(motion: str, frames: int, settings: ExportSettings) -> str:
    """Return a prompt-free Ken Burns/parallax-style motion filter."""
    denominator = max(1, frames - 1)
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if motion == "zoom_in":
        zoom, x, y = f"1.0+0.10*on/{denominator}", center_x, center_y
    elif motion == "zoom_out":
        zoom, x, y = f"1.10-0.10*on/{denominator}", center_x, center_y
    elif motion == "pan_left":
        zoom, x, y = "1.10", f"(iw-iw/zoom)*(1-on/{denominator})", center_y
    elif motion == "pan_right":
        zoom, x, y = "1.10", f"(iw-iw/zoom)*on/{denominator}", center_y
    elif motion == "pan_up":
        zoom, x, y = "1.10", center_x, f"(ih-ih/zoom)*(1-on/{denominator})"
    elif motion == "pan_down":
        zoom, x, y = "1.10", center_x, f"(ih-ih/zoom)*on/{denominator}"
    elif motion == "drift":
        zoom = f"1.04+0.04*on/{denominator}"
        x = f"(iw-iw/zoom)*(0.15+0.70*on/{denominator})"
        y = f"(ih-ih/zoom)*(0.75-0.50*on/{denominator})"
    elif motion == "cosmic":
        zoom = f"1.02+0.045*on/{denominator}"
        x = f"(iw-iw/zoom)*(0.38+0.18*sin(PI*on/{denominator}))"
        y = f"(ih-ih/zoom)*(0.55-0.16*sin(PI*on/{denominator}))"
    elif motion == "depth_2_5d":
        # Normal exports replace this scene with a locally rendered depth clip.
        # This subtle fallback also keeps standalone command generation usable.
        zoom = f"1.03+0.035*on/{denominator}"
        x = f"(iw-iw/zoom)*(0.25+0.50*on/{denominator})"
        y = f"(ih-ih/zoom)*(0.58-0.16*on/{denominator})"
    else:
        return ""
    return (
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:"
        f"s={settings.width}x{settings.height}:fps={settings.fps}"
    )


def _effect_filter(effect: str, strength: float, settings: ExportSettings) -> str:
    """Return a broadly-supported FFmpeg filter chain for a scene effect preset."""
    s = max(0.0, min(1.0, float(strength)))
    if effect == "brightness":
        return f"eq=brightness={0.18 * s:.4f}"
    if effect == "contrast":
        return f"eq=contrast={1 + 0.55 * s:.4f}"
    if effect == "saturation":
        return f"eq=saturation={1 + 1.1 * s:.4f}"
    if effect == "exposure":
        return f"eq=brightness={0.10 * s:.4f}:contrast={1 + 0.22 * s:.4f}"
    if effect == "warm":
        return f"colorbalance=rs={0.16 * s:.4f}:gs={0.035 * s:.4f}:bs={-0.12 * s:.4f}"
    if effect == "cool":
        return f"colorbalance=rs={-0.10 * s:.4f}:gs={0.025 * s:.4f}:bs={0.16 * s:.4f}"
    if effect == "sharpen":
        return f"unsharp=5:5:{0.35 + 1.25 * s:.3f}:5:5:0"
    if effect == "blur":
        return f"gblur=sigma={0.5 + 4.0 * s:.3f}"
    if effect == "vignette":
        return f"vignette=angle={0.30 + 0.55 * s:.3f}"
    if effect == "cinematic_letterbox":
        bar = max(1, round(settings.height * .14 * s))
        return (
            f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,"
            f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill"
        )
    if effect == "film_grain":
        return f"noise=alls={3 + round(18 * s)}:allf=t+u"
    if effect == "black_white":
        return "hue=s=0"
    if effect == "sepia":
        return "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    if effect == "vintage":
        return f"eq=contrast={1 + .18 * s:.3f}:saturation={1 - .32 * s:.3f}:brightness={.025 * s:.3f},colorbalance=rs={.10 * s:.3f}:bs={-.08 * s:.3f},vignette=angle={.40 + .28 * s:.3f}"
    if effect == "cinematic":
        return f"eq=contrast={1 + .28 * s:.3f}:saturation={1 + .12 * s:.3f}:brightness={-.025 * s:.3f},colorbalance=rs={.045 * s:.3f}:bs={.065 * s:.3f}"
    if effect == "camera_shake":
        border = max(4, round(18 * s))
        return f"crop=iw-{border * 2}:ih-{border * 2}:x={border}+{border}*sin(n*1.73):y={border}+{border}*sin(n*2.11),scale={settings.width}:{settings.height}"
    if effect == "handheld":
        border = max(3, round(10 * s))
        return f"crop=iw-{border * 2}:ih-{border * 2}:x={border}+{border}*sin(n*.23):y={border}+{border}*sin(n*.31),scale={settings.width}:{settings.height}"
    if effect == "zoom_pulse":
        border = max(4, round(22 * s))
        return f"crop=iw-{border * 2}:ih-{border * 2}:x={border}+{border}*sin(n*.12):y={border}+{border}*cos(n*.12),scale={settings.width}:{settings.height}"
    if effect == "flash":
        return f"eq=brightness='{.08 + .22 * s:.3f}*lt(mod(t\\,2)\\,.14)'"
    if effect == "flicker":
        return f"eq=brightness='{.018 + .045 * s:.3f}*sin(22*t)'"
    if effect == "glitch":
        return f"noise=alls={2 + round(10 * s)}:allf=t,colorbalance=rs={.07 * s:.3f}:bs={-.06 * s:.3f}"
    if effect == "rgb_split":
        return f"colorbalance=rs={.12 * s:.3f}:gs={-.035 * s:.3f}:bs={.11 * s:.3f},unsharp=3:3:{.35 + .4 * s:.3f}"
    if effect == "motion_blur":
        return "tmix=frames=3:weights='1 2 1'"
    if effect == "old_film":
        return f"hue=s={1 - .48 * s:.3f},eq=contrast={1 + .24 * s:.3f}:brightness={-.035 * s:.3f},noise=alls={3 + round(14 * s)}:allf=t+u,vignette=angle={.42 + .25 * s:.3f}"
    if effect == "dust_scratches":
        return f"eq=contrast={1 + .10 * s:.3f}:saturation={1 - .20 * s:.3f},noise=alls={5 + round(22 * s)}:allf=t+u"
    if effect == "historical":
        return f"colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast={1 + .12 * s:.3f},vignette=angle={.38 + .22 * s:.3f}"
    if effect == "dark_dramatic":
        return f"eq=brightness={-.10 * s:.3f}:contrast={1 + .48 * s:.3f}:saturation={1 - .15 * s:.3f},vignette=angle={.42 + .35 * s:.3f}"
    if effect == "cold_war":
        return f"eq=contrast={1 + .30 * s:.3f}:saturation={1 - .40 * s:.3f},colorbalance=rs={-.06 * s:.3f}:gs={.07 * s:.3f}:bs={.10 * s:.3f},noise=alls={2 + round(9 * s)}:allf=t"
    if effect == "horror":
        return f"eq=brightness={-.12 * s:.3f}:contrast={1 + .55 * s:.3f}:saturation={1 - .38 * s:.3f},colorbalance=rs={.10 * s:.3f}:gs={-.08 * s:.3f}:bs={-.10 * s:.3f},vignette=angle={.48 + .28 * s:.3f}"
    if effect == "cosmic_glow":
        return f"eq=contrast={1 + .20 * s:.3f}:saturation={1 + .55 * s:.3f},colorbalance=rs={.08 * s:.3f}:bs={.18 * s:.3f},unsharp=5:5:{.30 + .65 * s:.3f}"
    if effect == "space_nebula":
        return f"hue=h={18 * s:.2f}:s={1 + .60 * s:.3f},eq=contrast={1 + .25 * s:.3f}:brightness={-.025 * s:.3f},colorbalance=bs={.14 * s:.3f}:rs={.09 * s:.3f}"
    return ""


def _transition_filter(transition: str, duration: float, scene_duration: float) -> str:
    """Create duration-preserving scene-edge transitions for chunk-safe export."""
    d = min(max(0.08, float(duration)), max(0.08, scene_duration / 2.0))
    out_start = max(0.0, scene_duration - d)
    if transition == "cross_dissolve":
        return f"fade=t=in:st=0:d={d:.3f},fade=t=out:st={out_start:.3f}:d={d:.3f}"
    if transition == "fade_black":
        return f"fade=t=in:st=0:d={d:.3f}:color=black,fade=t=out:st={out_start:.3f}:d={d:.3f}:color=black"
    if transition == "fade_white":
        return f"fade=t=in:st=0:d={d:.3f}:color=white,fade=t=out:st={out_start:.3f}:d={d:.3f}:color=white"
    if transition == "zoom":
        return f"fade=t=in:st=0:d={d:.3f},fade=t=out:st={out_start:.3f}:d={d:.3f},unsharp=3:3:.45"
    if transition in {"slide_left", "slide_right"}:
        return f"fade=t=in:st=0:d={d:.3f},fade=t=out:st={out_start:.3f}:d={d:.3f}"
    if transition == "blur":
        return f"fade=t=in:st=0:d={d:.3f},fade=t=out:st={out_start:.3f}:d={d:.3f},gblur=sigma=.65"
    if transition == "flash":
        flash_start = max(0.0, out_start - min(.08, d / 3))
        return f"fade=t=in:st=0:d={min(.12, d):.3f}:color=white,fade=t=out:st={flash_start:.3f}:d={min(.12, d):.3f}:color=white"
    if transition == "film_burn":
        return f"fade=t=in:st=0:d={d:.3f}:color=0xFF6A22,fade=t=out:st={out_start:.3f}:d={d:.3f}:color=0xFF6A22"
    if transition == "glitch":
        return f"fade=t=in:st=0:d={min(.15, d):.3f},fade=t=out:st={out_start:.3f}:d={min(.15, d):.3f},noise=alls=3:allf=t"
    return ""


def _scene_filter_chain(index: int, item: MediaItem, frames: int, settings: ExportSettings) -> str:
    input_duration = (frames + 1) / settings.fps
    chain = (
        f"[{index}:v]scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,"
        f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2:color=0x{settings.background},"
        f"setsar=1,fps={settings.fps}"
    )
    if item.kind == "image":
        motion_filter = _motion_filter(item.motion, frames, settings)
        if motion_filter:
            chain += f",{motion_filter}"
    if item.kind == "video":
        speed = max(0.25, min(4.0, float(item.playback_speed)))
        if abs(speed - 1.0) > 0.001:
            chain += f",setpts=PTS/{speed:.4f}"
        chain += f",tpad=stop_mode=clone:stop_duration={input_duration:.6f}"
    effect_filter = _effect_filter(item.effect, item.effect_strength, settings)
    if effect_filter:
        chain += f",{effect_filter}"
    transition_filter = _transition_filter(
        item.transition, item.transition_duration, frames / settings.fps,
    )
    if transition_filter:
        chain += f",{transition_filter}"
    # Some crop/scale effects can change sample aspect ratio; concat requires
    # every scene to expose identical geometry and SAR.
    chain += ",setsar=1"
    chain += (
        f",trim=end_frame={frames},setpts=N/({settings.fps}*TB),"
        f"tpad=stop_mode=clone:stop_duration={1 / settings.fps:.9f}[v{index}]"
    )
    return chain


def _video_input_arguments(item: MediaItem, input_duration: float) -> list[str]:
    start = max(0.0, item.source_start)
    source_end = item.source_end or item.source_duration
    available = max(0.01, source_end - start) if source_end else input_duration
    speed = max(0.25, min(4.0, float(item.playback_speed)))
    duration = min(input_duration * speed, available)
    return ["-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-i", item.path]


def _gap_input_arguments(input_duration: float, settings: ExportSettings) -> list[str]:
    return [
        "-f", "lavfi", "-t", f"{input_duration:.6f}", "-i",
        f"color=c=0x{settings.background}:s={settings.width}x{settings.height}:r={settings.fps}",
    ]


def _youtube_video_encode_arguments(settings: ExportSettings) -> list[str]:
    """H.264 SDR settings aligned with YouTube's recommended upload format."""
    gop = max(1, round(settings.fps / 2))
    arguments = ["-c:v", "libx264", "-preset", settings.preset]
    if settings.video_bitrate_kbps > 0:
        bitrate = settings.video_bitrate_kbps
        arguments.extend([
            "-b:v", f"{bitrate}k", "-maxrate", f"{round(bitrate * 1.5)}k",
            "-bufsize", f"{bitrate * 2}k",
        ])
    else:
        arguments.extend(["-crf", str(settings.crf)])
    arguments.extend([
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(settings.fps),
        "-bf", "2", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-x264-params", "open-gop=0", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
    ])
    return arguments


def _youtube_audio_encode_arguments() -> list[str]:
    return [
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "384k",
        "-ar", "48000", "-ac", "2",
    ]


def build_ffmpeg_command(
    items: list[MediaItem],
    audio_path: str | Path,
    output_path: str | Path,
    settings: ExportSettings,
    subtitle_path: str | Path | None = None,
) -> list[str]:
    if not items:
        raise EditorError("No media to render")
    command = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error"]
    frame_counts = _scene_frame_counts(items, settings.fps)
    for item, frames in zip(items, frame_counts):
        input_duration = (frames + 1) / settings.fps
        if item.kind == "image":
            command.extend([
                "-loop", "1", "-framerate", str(settings.fps),
                "-t", f"{input_duration:.6f}", "-i", item.path,
            ])
        elif item.kind == "gap":
            command.extend(_gap_input_arguments(input_duration, settings))
        else:
            command.extend(_video_input_arguments(item, input_duration))
    audio_index = len(items)
    command.extend(["-i", str(audio_path)])

    filters: list[str] = []
    labels: list[str] = []
    for index, (item, frames) in enumerate(zip(items, frame_counts)):
        filters.append(_scene_filter_chain(index, item, frames, settings))
        labels.append(f"[v{index}]")
    total_frames = sum(frame_counts)
    filters.append(f"{''.join(labels)}concat=n={len(items)}:v=1:a=0[vcat]")
    filters.append(f"[vcat]trim=end_frame={total_frames},setpts=N/({settings.fps}*TB)[vbase]")
    final_label = "vbase"
    if settings.burn_captions and subtitle_path:
        filters.append(f"[vbase]{_subtitle_filter(subtitle_path, settings)}[vout]")
        final_label = "vout"
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{final_label}]", "-map", f"{audio_index}:a:0",
        *_youtube_video_encode_arguments(settings),
        *_youtube_audio_encode_arguments(), "-shortest", "-movflags", "+faststart",
        "-progress", "pipe:1", str(output_path),
    ])
    return command


def build_video_chunk_command(
    items: list[MediaItem],
    output_path: str | Path,
    settings: ExportSettings,
    frame_counts: list[int] | None = None,
) -> list[str]:
    """Build a bounded command for one normalized, video-only timeline chunk."""
    command = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error"]
    frame_counts = frame_counts or _scene_frame_counts(items, settings.fps)
    for item, frames in zip(items, frame_counts):
        input_duration = (frames + 1) / settings.fps
        if item.kind == "image":
            command.extend([
                "-loop", "1", "-framerate", str(settings.fps),
                "-t", f"{input_duration:.6f}", "-i", item.path,
            ])
        elif item.kind == "gap":
            command.extend(_gap_input_arguments(input_duration, settings))
        else:
            command.extend(_video_input_arguments(item, input_duration))

    filters: list[str] = []
    labels: list[str] = []
    for index, (item, frames) in enumerate(zip(items, frame_counts)):
        filters.append(_scene_filter_chain(index, item, frames, settings))
        labels.append(f"[v{index}]")
    total_frames = sum(frame_counts)
    filters.append(f"{''.join(labels)}concat=n={len(items)}:v=1:a=0[vcat]")
    filters.append(f"[vcat]trim=end_frame={total_frames},setpts=N/({settings.fps}*TB)[vout]")
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[vout]",
        "-an", *_youtube_video_encode_arguments(settings), "-movflags", "+faststart",
        str(output_path),
    ])
    return command


def _run_checked(command: list[str]) -> None:
    proc = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode:
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        raise EditorError(tail or "FFmpeg render step failed")


def _run_checked_with_progress(
    command: list[str],
    stage_duration: float,
    completed_steps: int,
    total_steps: int,
    message: str,
    progress: ProgressCallback | None,
) -> None:
    progress_command = [*command[:-1], "-progress", "pipe:1", command[-1]]
    proc = subprocess.Popen(
        progress_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key not in {"out_time_us", "out_time_ms"}:
            continue
        try:
            seconds = float(value) / 1_000_000
        except ValueError:
            continue
        local = min(1.0, seconds / max(stage_duration, 0.01))
        if progress:
            progress((completed_steps + local) / total_steps, message)
    stderr = proc.stderr.read() if proc.stderr else ""
    return_code = proc.wait()
    if return_code:
        tail = "\n".join(stderr.strip().splitlines()[-20:])
        raise EditorError(tail or "FFmpeg render step failed")


def _concat_file_line(path: str | Path) -> str:
    # FFmpeg concat files use forward slashes; a single quote is escaped as '\''.
    value = str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''")
    return f"file '{value}'"


def render_video_chunked(
    items: list[MediaItem],
    audio_path: str | Path,
    output_path: str | Path,
    settings: ExportSettings,
    subtitle_path: str | Path | None,
    progress: ProgressCallback | None,
    chunk_size: int = 24,
) -> None:
    """Render large projects without exceeding Windows' command-line limit."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    narration_duration = duration_of(audio_path)
    chunks = [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]
    caption_step = 1 if settings.burn_captions and subtitle_path else 0
    total_steps = len(chunks) + 1 + caption_step

    with tempfile.TemporaryDirectory(prefix="storycut_render_", dir=str(output_path.parent)) as temp:
        temp_dir = Path(temp)
        chunk_paths: list[Path] = []
        chunk_settings = ExportSettings(**asdict(settings))
        chunk_settings.burn_captions = False
        global_frame_counts = _scene_frame_counts(items, settings.fps)
        frame_offset = 0
        for index, chunk in enumerate(chunks, 1):
            if progress:
                progress((index - 1) / total_steps, f"Rendering batch {index}/{len(chunks)}…")
            chunk_path = temp_dir / f"chunk_{index:04d}.mp4"
            chunk_frame_counts = global_frame_counts[frame_offset:frame_offset + len(chunk)]
            chunk_duration = sum(chunk_frame_counts) / settings.fps
            _run_checked_with_progress(
                build_video_chunk_command(chunk, chunk_path, chunk_settings, chunk_frame_counts),
                chunk_duration,
                index - 1,
                total_steps,
                f"Rendering batch {index}/{len(chunks)}",
                progress,
            )
            chunk_paths.append(chunk_path)
            frame_offset += len(chunk)

        concat_list = temp_dir / "chunks.ffconcat"
        concat_list.write_text(
            "ffconcat version 1.0\n" + "\n".join(_concat_file_line(path) for path in chunk_paths) + "\n",
            encoding="utf-8",
        )
        if progress:
            progress(len(chunks) / total_steps, "Joining timeline and narration…")

        joined_output = temp_dir / "joined_with_audio.mp4" if caption_step else output_path
        _run_checked([
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *_youtube_audio_encode_arguments(),
            "-t", f"{narration_duration:.6f}", "-shortest", "-movflags", "+faststart", str(joined_output),
        ])

        if caption_step:
            if progress:
                progress((len(chunks) + 1) / total_steps, "Burning captions…")
            caption_command = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(joined_output), "-vf", _subtitle_filter(subtitle_path, settings),
                *_youtube_video_encode_arguments(settings), "-c:a", "copy",
                "-movflags", "+faststart", str(output_path),
            ]
            _run_checked_with_progress(
                caption_command,
                narration_duration,
                len(chunks) + 1,
                total_steps,
                "Burning captions",
                progress,
            )
    if progress:
        progress(1.0, "Export complete")


def render_video(
    items: list[MediaItem],
    audio_path: str | Path,
    output_path: str | Path,
    settings: ExportSettings,
    subtitle_path: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    require_tools()
    depth_items = [item for item in items if item.kind == "image" and item.motion == "depth_2_5d"]
    if depth_items:
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            from depth_motion import DepthMotionError, render_depth_clip
        except Exception as exc:
            raise EditorError(f"2.5D Depth support could not start: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="storycut_depth_", dir=str(output.parent)) as temporary:
            prepared: list[MediaItem] = []
            depth_number = 0
            depth_total = len(depth_items)
            for item in items:
                if item.kind != "image" or item.motion != "depth_2_5d":
                    prepared.append(replace(item))
                    continue
                depth_number += 1
                clip_path = Path(temporary) / f"depth_{depth_number:04d}.mp4"
                if progress:
                    progress(
                        0.35 * (depth_number - 1) / depth_total,
                        f"Creating 2.5D scene {depth_number}/{depth_total}…",
                    )
                try:
                    render_depth_clip(
                        item.path, clip_path, item.assigned_duration,
                        settings.width, settings.height, settings.fps,
                        (
                            lambda message, number=depth_number: progress(
                                0.35 * (number - 0.5) / depth_total,
                                f"2.5D scene {number}/{depth_total} · {message}",
                            )
                        ) if progress else None,
                    )
                except DepthMotionError as exc:
                    raise EditorError(str(exc)) from exc
                prepared.append(replace(
                    item, path=str(clip_path), kind="video", motion="none",
                    source_duration=item.assigned_duration, source_start=0.0,
                    source_end=item.assigned_duration,
                ))
            if progress:
                progress(0.35, "2.5D scenes ready · rendering timeline…")

            def remaining_progress(value: float, message: str) -> None:
                if progress:
                    progress(0.35 + 0.65 * value, message)

            render_video(
                prepared, audio_path, output, settings, subtitle_path,
                remaining_progress if progress else None,
            )
        return
    # A few dozen full paths plus a filter graph can exceed CreateProcess limits
    # on Windows. Chunking also keeps FFmpeg memory use bounded for long projects.
    direct_command = build_ffmpeg_command(items, audio_path, output_path, settings, subtitle_path)
    estimated_length = sum(len(part) + 3 for part in direct_command)
    if len(items) > 40 or estimated_length > 24000:
        render_video_chunked(items, audio_path, output_path, settings, subtitle_path, progress)
        return
    total = sum(item.assigned_duration for item in items)
    command = direct_command
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    last_message = "Preparing render…"
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key in {"out_time_us", "out_time_ms"}:
            # FFmpeg currently reports microseconds for both keys depending on build.
            try:
                raw = float(value or 0)
            except ValueError:
                continue
            seconds = raw / 1_000_000
            if progress:
                progress(min(0.99, seconds / max(total, 0.01)), "Rendering video…")
        elif key == "progress":
            last_message = value
    stderr = proc.stderr.read() if proc.stderr else ""
    return_code = proc.wait()
    if return_code:
        tail = "\n".join(stderr.strip().splitlines()[-20:])
        raise EditorError(tail or f"FFmpeg failed ({last_message})")
    if progress:
        progress(1.0, "Export complete")


def save_project(
    output_path: str | Path,
    items: list[MediaItem],
    audio_path: str,
    sync_mode: str,
    settings: ExportSettings,
    transcript: list[TranscriptSegment],
    transcript_language: str = "",
) -> None:
    data = {
        "version": 1,
        "items": [asdict(item) for item in items],
        "audio_path": audio_path,
        "sync_mode": sync_mode,
        "settings": asdict(settings),
        "transcript": [asdict(segment) for segment in transcript],
        "transcript_language": transcript_language,
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


def load_project(path: str | Path) -> tuple[list[MediaItem], str, str, ExportSettings, list[TranscriptSegment], str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [MediaItem(**item) for item in data.get("items", [])]
    settings = ExportSettings(**data.get("settings", {}))
    transcript = [TranscriptSegment(**segment) for segment in data.get("transcript", [])]
    return (
        items,
        str(data.get("audio_path", "")),
        str(data.get("sync_mode", "pause")),
        settings,
        transcript,
        str(data.get("transcript_language", "")),
    )
