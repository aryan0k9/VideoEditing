from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageFilter, ImageOps


DEPTH_MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
_PROCESSOR = None
_MODEL = None


class DepthMotionError(RuntimeError):
    pass


def _ffmpeg_executable() -> str:
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise DepthMotionError("FFmpeg is unavailable for 2.5D rendering") from exc


def _load_depth_model():
    global _PROCESSOR, _MODEL
    if _PROCESSOR is not None and _MODEL is not None:
        return _PROCESSOR, _MODEL
    try:
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        try:
            _PROCESSOR = AutoImageProcessor.from_pretrained(DEPTH_MODEL_ID, local_files_only=True)
            _MODEL = AutoModelForDepthEstimation.from_pretrained(DEPTH_MODEL_ID, local_files_only=True)
        except Exception:
            _PROCESSOR = AutoImageProcessor.from_pretrained(DEPTH_MODEL_ID)
            _MODEL = AutoModelForDepthEstimation.from_pretrained(DEPTH_MODEL_ID)
        _MODEL.eval()
        return _PROCESSOR, _MODEL
    except Exception as exc:
        raise DepthMotionError(
            "Could not load Depth Anything V2 Small. Connect to the internet for the first use, "
            "then try the export again."
        ) from exc


def estimate_depth(image: Image.Image) -> Image.Image:
    try:
        import torch
        import torch.nn.functional as functional

        processor, model = _load_depth_model()
        inputs = processor(images=image, return_tensors="pt")
        with torch.inference_mode():
            predicted = model(**inputs).predicted_depth
        predicted = functional.interpolate(
            predicted.unsqueeze(1), size=(image.height, image.width), mode="bicubic", align_corners=False,
        ).squeeze()
        values = predicted.cpu().numpy().astype(np.float32)
    except DepthMotionError:
        raise
    except Exception as exc:
        raise DepthMotionError(f"Depth estimation failed: {exc}") from exc
    low, high = np.percentile(values, (2.0, 98.0))
    normalized = np.clip((values - low) / max(float(high - low), 1e-6), 0.0, 1.0)
    return Image.fromarray(np.uint8(normalized * 255), mode="L")


def make_depth_layers(
    source: Image.Image, depth: Image.Image, size: tuple[int, int]
) -> tuple[Image.Image, Image.Image, Image.Image]:
    """Create a filled background and softly masked mid/near RGBA layers."""
    width, height = size
    fitted = ImageOps.fit(source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    depth_fit = ImageOps.fit(depth.convert("L"), (width, height), method=Image.Resampling.BICUBIC)
    values = np.asarray(depth_fit, dtype=np.float32) / 255.0

    def smooth_mask(start: float, end: float) -> Image.Image:
        mask = np.clip((values - start) / max(end - start, 1e-6), 0.0, 1.0)
        mask = mask * mask * (3.0 - 2.0 * mask)
        result = Image.fromarray(np.uint8(mask * 255), mode="L")
        return result.filter(ImageFilter.GaussianBlur(radius=max(2, round(min(width, height) / 180))))

    mid = fitted.convert("RGBA")
    mid.putalpha(smooth_mask(0.30, 0.62))
    near = fitted.convert("RGBA")
    near.putalpha(smooth_mask(0.60, 0.86))
    return fitted, mid, near


def render_depth_clip(
    image_path: str,
    output_path: str | Path,
    duration: float,
    width: int,
    height: int,
    fps: int,
    status: Callable[[str], None] | None = None,
) -> None:
    if status:
        status("Estimating depth…")
    with Image.open(image_path) as opened:
        source = opened.convert("RGB")
    depth = estimate_depth(source)
    background, middle, near = make_depth_layers(source, depth, (width, height))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    layer_dir = output.parent / f"{output.stem}_layers"
    layer_dir.mkdir(parents=True, exist_ok=True)
    background_path = layer_dir / "background.png"
    middle_path = layer_dir / "middle.png"
    near_path = layer_dir / "near.png"
    background.save(background_path)
    middle.save(middle_path)
    near.save(near_path)
    if status:
        status("Rendering 2.5D parallax…")

    safe_duration = max(0.12, float(duration))
    frames = max(1, round(safe_duration * fps))
    filter_graph = (
        f"[0:v]scale={width + 64}:{height + 64},"
        f"crop={width}:{height}:x='(in_w-out_w)/2+8*sin(PI*t/{safe_duration:.6f})':"
        f"y='(in_h-out_h)/2+5*cos(PI*t/{safe_duration:.6f})',fps={fps}[bg];"
        "[1:v]format=rgba[mid];[2:v]format=rgba[near];"
        f"[bg][mid]overlay=x='-10+20*t/{safe_duration:.6f}':"
        f"y='-5+10*t/{safe_duration:.6f}':shortest=1[tmp];"
        f"[tmp][near]overlay=x='-22+44*t/{safe_duration:.6f}':"
        f"y='-9+18*t/{safe_duration:.6f}':shortest=1,format=yuv420p[out]"
    )
    command = [_ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error"]
    for layer in (background_path, middle_path, near_path):
        command.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{safe_duration:.6f}", "-i", str(layer)])
    command.extend([
        "-filter_complex", filter_graph, "-map", "[out]", "-frames:v", str(frames),
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ])
    proc = subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode or not output.exists():
        raise DepthMotionError(proc.stderr.strip() or "2.5D clip rendering failed")
