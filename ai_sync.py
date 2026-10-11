from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import subprocess
import tempfile
import copy
import math
from pathlib import Path
from typing import Callable

from PIL import Image

from editor_core import EditorError, MediaItem, TranscriptSegment, _pick_transcript_boundaries, caption_word_timings


ProgressCallback = Callable[[float, str], None]
_AI_MODELS = None


def ai_sync_backend() -> str | None:
    return "Multilingual CLIP (local)" if importlib.util.find_spec("sentence_transformers") else None


def _load_models(progress: ProgressCallback | None):
    global _AI_MODELS
    if _AI_MODELS is not None:
        return _AI_MODELS
    if progress:
        progress(0.02, "Loading local image understanding models…")
    try:
        from sentence_transformers import SentenceTransformer
        try:
            image_model = SentenceTransformer("clip-ViT-B-32", device="cpu", local_files_only=True)
            text_model = SentenceTransformer(
                "sentence-transformers/clip-ViT-B-32-multilingual-v1", device="cpu",
                local_files_only=True,
            )
        except Exception:
            image_model = SentenceTransformer("clip-ViT-B-32", device="cpu")
            text_model = SentenceTransformer(
                "sentence-transformers/clip-ViT-B-32-multilingual-v1", device="cpu"
            )
    except Exception as exc:
        raise EditorError(
            "Could not load the local AI Sync models. Connect to the internet for the first model download, "
            "then try again.\n\n" + str(exc)
        ) from exc
    _AI_MODELS = image_model, text_model
    return _AI_MODELS


def _cache_directory() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "StoryCut" / "ai_embeddings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path(path: str) -> Path:
    source = Path(path)
    try:
        stat = source.stat()
        identity = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(source.resolve())
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()
    return _cache_directory() / f"{digest}.npy"


def _representative_image(item: MediaItem) -> Image.Image:
    if item.kind == "image":
        with Image.open(item.path) as image:
            return image.convert("RGB")
    start = max(0.0, item.source_start)
    end = item.source_end or item.source_duration
    timestamp = start + max(0.0, end - start) / 2
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
        "-i", item.path, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode or not proc.stdout:
        raise EditorError(f"Could not read a representative frame from {item.name}")
    with Image.open(io.BytesIO(proc.stdout)) as image:
        return image.convert("RGB")


def _speech_chunks(
    transcript: list[TranscriptSegment], total: float, image_count: int, max_duration: float = 5.0
) -> tuple[list[tuple[float, float, str]], list[float]]:
    words = [word for segment in transcript for word in caption_word_timings(segment)]
    desired = max(
        image_count,
        min(image_count * 2, max(image_count, len(words) // 3)),
        math.ceil(total / max(0.5, max_duration)),
    )
    initial = _pick_transcript_boundaries(total, desired, transcript, 0.22)
    boundaries = [initial[0]]
    word_ends = sorted({float(word.get("end", 0.0)) for word in words})
    for original_start, original_end in zip(initial, initial[1:]):
        start = original_start
        while original_end - start > max_duration + 0.001:
            target = start + max_duration
            candidates = [value for value in word_ends if start + 0.35 <= value <= target]
            split = max(candidates) if candidates else target
            boundaries.append(split)
            start = split
        boundaries.append(original_end)
    # Transcript boundary selection can occasionally return near-duplicates.
    boundaries = [value for index, value in enumerate(boundaries) if index == 0 or value > boundaries[index - 1] + 0.01]
    chunks: list[tuple[float, float, str]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        text = " ".join(
            str(word.get("word", "")).strip()
            for word in words
            if float(word.get("end", 0.0)) > start and float(word.get("start", 0.0)) < end
        ).strip()
        if not text:
            text = next(
                (segment.text for segment in transcript if segment.end > start and segment.start < end),
                "visual scene",
            )
        chunks.append((start, end, text))
    return chunks, boundaries


def _display_confidence(raw: float) -> float:
    """Convert CLIP's narrow cosine range into the meter shown in the editor."""
    return min(1.0, max(0.0, (raw - 0.10) / 0.25))


def _best_bounded_span(
    chunk_indices: list[int], chunks: list[tuple[float, float, str]], scores, max_duration: float
) -> list[int]:
    """Choose one contiguous, high-scoring use of an image without overstretching it."""
    best: list[int] = []
    best_score = -1e30
    for left in range(len(chunk_indices)):
        duration = 0.0
        weighted = 0.0
        for right in range(left, len(chunk_indices)):
            chunk_index = chunk_indices[right]
            chunk_duration = chunks[chunk_index][1] - chunks[chunk_index][0]
            if duration + chunk_duration > max_duration + 0.001:
                break
            duration += chunk_duration
            weighted += float(scores[chunk_index]) * chunk_duration
            # A tiny duration bonus prefers a coherent 4-5 second visual over a
            # nearly identical two-second fragment.
            candidate_score = weighted / max(duration, 0.001) + duration * 0.002
            if candidate_score > best_score:
                best_score = candidate_score
                best = chunk_indices[left:right + 1]
    return best or chunk_indices[:1]


def _build_ai_timeline(
    source_items: list[MediaItem],
    chunks: list[tuple[float, float, str]],
    assignments: list[int],
    similarities,
    max_duration: float,
    min_confidence: float,
) -> tuple[list[MediaItem], list[float]]:
    """Build a timeline with bounded visuals and explicit slots for missing imagery."""
    selected_owner: dict[int, int] = {}
    image_confidence: dict[int, float] = {}
    candidate_chunk: dict[int, int] = {}
    for image_index in range(len(source_items)):
        owned = [index for index, owner in enumerate(assignments) if owner == image_index]
        if not owned:
            continue
        scores = similarities[image_index]
        span = _best_bounded_span(owned, chunks, scores, max_duration)
        best_chunk = max(owned, key=lambda index: float(scores[index]))
        confidence = _display_confidence(float(scores[best_chunk]))
        image_confidence[image_index] = confidence
        candidate_chunk[image_index] = best_chunk
        if confidence >= min_confidence:
            for chunk_index in span:
                selected_owner[chunk_index] = image_index

    timeline: list[MediaItem] = []
    confidence_values: list[float] = []
    chunk_index = 0
    while chunk_index < len(chunks):
        start, end, text = chunks[chunk_index]
        owner = selected_owner.get(chunk_index)
        if owner is not None:
            last = chunk_index
            while last + 1 < len(chunks) and selected_owner.get(last + 1) == owner:
                last += 1
            item = copy.deepcopy(source_items[owner])
            item.assigned_duration = chunks[last][1] - start
            item.placeholder_text = ""
            item.ai_confidence = image_confidence[owner]
            item.candidate_path = ""
            item.candidate_kind = ""
            timeline.append(item)
            confidence_values.append(item.ai_confidence)
            chunk_index = last + 1
            continue

        gap = MediaItem(
            path="", kind="gap", assigned_duration=end - start,
            placeholder_text=text or "Visual needed for this narration",
            ai_confidence=0.0,
        )
        weak_owner = next(
            (image_index for image_index, chosen in candidate_chunk.items()
             if chosen == chunk_index and image_confidence.get(image_index, 0.0) < min_confidence),
            None,
        )
        if weak_owner is not None:
            gap.candidate_path = source_items[weak_owner].path
            gap.candidate_kind = source_items[weak_owner].kind
        timeline.append(gap)
        confidence_values.append(0.0)
        chunk_index += 1
    return timeline, confidence_values


def _image_embeddings(items: list[MediaItem], image_model, progress: ProgressCallback | None):
    import numpy as np

    result: list[object | None] = [None] * len(items)
    missing: list[tuple[int, Image.Image, Path]] = []
    for index, item in enumerate(items):
        cache = _cache_path(item.path)
        try:
            embedding = np.load(cache, allow_pickle=False)
            if embedding.shape == (512,):
                result[index] = embedding
                continue
        except Exception:
            pass
        missing.append((index, _representative_image(item), cache))

    batch_size = 12
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset:offset + batch_size]
        embeddings = image_model.encode(
            [entry[1] for entry in batch], batch_size=len(batch), convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=False,
        )
        for (index, image, cache), embedding in zip(batch, embeddings):
            result[index] = embedding
            np.save(cache, embedding, allow_pickle=False)
            image.close()
        if progress:
            done = min(len(missing), offset + len(batch))
            progress(0.12 + 0.48 * done / max(1, len(missing)), f"Understanding images · {done}/{len(missing)}")
    if not missing and progress:
        progress(0.60, f"Loaded {len(items)} image meanings from cache")
    return np.stack(result)


def _monotonic_alignment(similarities, chunk_count: int, image_count: int) -> list[int]:
    import numpy as np

    negative = -1e30
    dp = np.full((image_count, chunk_count), negative, dtype=np.float32)
    previous = np.full((image_count, chunk_count), -1, dtype=np.int8)
    dp[0, 0] = float(similarities[0, 0]) * 3.0
    for chunk_index in range(1, chunk_count):
        min_image = max(0, image_count - (chunk_count - chunk_index))
        max_image = min(image_count - 1, chunk_index)
        for image_index in range(min_image, max_image + 1):
            expected = image_index / max(1, image_count - 1)
            actual = chunk_index / max(1, chunk_count - 1)
            local = float(similarities[image_index, chunk_index]) * 3.0 - abs(expected - actual) * 0.85
            stay = dp[image_index, chunk_index - 1]
            advance = dp[image_index - 1, chunk_index - 1] + 0.06 if image_index else negative
            if advance > stay:
                dp[image_index, chunk_index] = advance + local
                previous[image_index, chunk_index] = 1
            else:
                dp[image_index, chunk_index] = stay + local
                previous[image_index, chunk_index] = 0

    assignments = [0] * chunk_count
    image_index = image_count - 1
    for chunk_index in range(chunk_count - 1, -1, -1):
        assignments[chunk_index] = image_index
        if chunk_index and previous[image_index, chunk_index] == 1:
            image_index -= 1
    return assignments


def semantic_sync(
    items: list[MediaItem],
    transcript: list[TranscriptSegment],
    audio_duration: float,
    progress: ProgressCallback | None = None,
    max_image_duration: float = 5.0,
    min_confidence: float = 0.30,
) -> tuple[list[float], list[float]]:
    """Return AI-aligned scene boundaries and per-image semantic confidence scores."""
    if not items:
        raise EditorError("Add media before AI Content Sync.")
    if not transcript:
        raise EditorError("Generate the transcript before AI Content Sync.")
    if audio_duration <= 0:
        raise EditorError("Add narration audio before AI Content Sync.")

    import numpy as np

    source_items: list[MediaItem] = []
    for item in items:
        if item.kind != "gap":
            source_items.append(copy.deepcopy(item))
        elif item.candidate_path and item.candidate_kind:
            recovered = copy.deepcopy(item)
            recovered.path = item.candidate_path
            recovered.kind = item.candidate_kind
            recovered.placeholder_text = ""
            recovered.candidate_path = ""
            recovered.candidate_kind = ""
            source_items.append(recovered)
    if not source_items:
        raise EditorError("Add at least one real image or video before AI Content Sync.")

    max_image_duration = min(30.0, max(1.0, float(max_image_duration)))
    min_confidence = min(1.0, max(0.0, float(min_confidence)))
    image_model, text_model = _load_models(progress)
    chunks, chunk_boundaries = _speech_chunks(
        transcript, audio_duration, len(source_items), max_image_duration
    )
    if progress:
        progress(0.09, f"Prepared {len(chunks)} spoken visual beats")
    image_embeddings = _image_embeddings(source_items, image_model, progress)
    if progress:
        progress(0.64, "Understanding transcript meaning…")
    text_embeddings = text_model.encode(
        [chunk[2] for chunk in chunks], batch_size=32, convert_to_numpy=True,
        normalize_embeddings=True, show_progress_bar=False,
    )
    similarities = np.asarray(image_embeddings) @ np.asarray(text_embeddings).T
    if progress:
        progress(0.82, "Matching images to spoken story…")
    assignments = _monotonic_alignment(similarities, len(chunks), len(source_items))
    timeline, confidence = _build_ai_timeline(
        source_items, chunks, assignments, similarities, max_image_duration, min_confidence
    )
    items[:] = timeline
    boundaries = [0.0]
    for item in items:
        boundaries.append(boundaries[-1] + item.assigned_duration)
    if progress:
        progress(1.0, "AI Content Sync complete")
    return boundaries, confidence
