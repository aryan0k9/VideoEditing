from __future__ import annotations

import ctypes
import hashlib
import io
import math
import os
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps


def ffmpeg_executable() -> str:
    """Return a bundled/system FFmpeg path for preview compatibility work."""
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg is unavailable for audio/video preview") from exc


class MCIAudioPlayer:
    """Small Windows MCI wrapper with play/pause/seek/volume support."""

    def __init__(self, alias: str | None = None) -> None:
        self.alias = alias or f"storycut_{os.getpid()}_{id(self):x}"
        self.path = ""
        self.playback_path = ""
        self.opened = False
        self.using_converted_audio = False
        self._mci = ctypes.windll.winmm.mciSendStringW

    def _command(self, command: str, return_text: bool = False) -> str:
        buffer = ctypes.create_unicode_buffer(512) if return_text else None
        result = self._mci(command, buffer, 511 if buffer else 0, 0)
        if result != 0:
            error = ctypes.create_unicode_buffer(512)
            ctypes.windll.winmm.mciGetErrorStringW(result, error, 511)
            raise RuntimeError(error.value or f"MCI error {result}")
        return buffer.value if buffer else ""

    def open(self, path: str) -> None:
        resolved = str(Path(path).resolve())
        if self.opened and resolved.casefold() == self.path.casefold():
            return
        self.close()
        try:
            self._open_mci(resolved)
            playback_path = resolved
            converted = False
        except RuntimeError as direct_error:
            self._safe_close_alias()
            try:
                playback_path = self._convert_for_preview(resolved)
                self._open_mci(playback_path)
                converted = True
            except Exception as fallback_error:
                self._safe_close_alias()
                raise RuntimeError(
                    f"Windows could not play this audio ({direct_error}). "
                    f"Compatibility conversion also failed: {fallback_error}"
                ) from fallback_error
        self.path = resolved
        self.playback_path = playback_path
        self.opened = True
        self.using_converted_audio = converted

    def _open_mci(self, path: str) -> None:
        self._command(f'open "{path}" alias {self.alias}')
        self._command(f"set {self.alias} time format milliseconds")

    def _safe_close_alias(self) -> None:
        try:
            self._command(f"close {self.alias}")
        except RuntimeError:
            pass

    def _convert_for_preview(self, source: str) -> str:
        source_path = Path(source)
        stat = source_path.stat()
        fingerprint = f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogatepass")
        filename = f"{hashlib.sha256(fingerprint).hexdigest()[:24]}.wav"
        roots = [
            Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "StoryCut" / "audio_preview",
            Path(tempfile.gettempdir()) / "StoryCut-audio-preview",
        ]
        errors: list[str] = []
        visited: set[str] = set()
        for cache_root in roots:
            key = str(cache_root).casefold()
            if key in visited:
                continue
            visited.add(key)
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                output = cache_root / filename
                if output.exists() and output.stat().st_size > 44:
                    return str(output)
                proc = subprocess.run(
                    [
                        ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(source_path), "-vn", "-acodec", "pcm_s16le",
                        "-ar", "44100", "-ac", "2", str(output),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode == 0 and output.exists() and output.stat().st_size > 44:
                    return str(output)
                errors.append(proc.stderr.strip() or f"Could not write {output}")
            except OSError as exc:
                errors.append(str(exc))
        raise RuntimeError(errors[-1] if errors else "FFmpeg audio conversion failed")

    def play(self, milliseconds: int = 0) -> None:
        if self.opened:
            self._command(f"play {self.alias} from {max(0, milliseconds)}")

    def pause(self) -> None:
        if self.opened:
            self._command(f"pause {self.alias}")

    def resume(self) -> None:
        if self.opened:
            self._command(f"resume {self.alias}")

    def stop(self) -> None:
        if self.opened:
            self._command(f"stop {self.alias}")

    def seek(self, milliseconds: int) -> None:
        if self.opened:
            self._command(f"seek {self.alias} to {max(0, milliseconds)}")

    def position(self) -> int:
        if not self.opened:
            return 0
        try:
            return int(self._command(f"status {self.alias} position", True) or 0)
        except (RuntimeError, ValueError):
            return 0

    def set_volume(self, percent: float) -> None:
        if self.opened:
            value = max(0, min(1000, round(percent * 10)))
            try:
                self._command(f"setaudio {self.alias} volume to {value}")
            except RuntimeError:
                pass

    def close(self) -> None:
        if self.opened:
            self._safe_close_alias()
        self.opened = False
        self.path = ""
        self.playback_path = ""
        self.using_converted_audio = False


class PreviewRenderer:
    def __init__(self, cache_size: int = 24) -> None:
        self.cache_size = cache_size
        self.image_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self.video_cache: OrderedDict[tuple[str, int], Image.Image] = OrderedDict()

    def _remember(self, cache: OrderedDict, key, value: Image.Image) -> Image.Image:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.cache_size:
            cache.popitem(last=False)
        return value

    def source_image(self, path: str) -> Image.Image:
        key = str(Path(path).resolve())
        cached = self.image_cache.get(key)
        if cached is not None:
            self.image_cache.move_to_end(key)
            return cached
        with Image.open(key) as source:
            image = source.convert("RGB").copy()
        return self._remember(self.image_cache, key, image)

    def video_frame(self, path: str, seconds: float) -> Image.Image | None:
        bucket = max(0, round(seconds * 5))
        key = (str(Path(path).resolve()), bucket)
        cached = self.video_cache.get(key)
        if cached is not None:
            self.video_cache.move_to_end(key)
            return cached
        proc = subprocess.run(
            [
                ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-ss", f"{bucket / 5:.3f}",
                "-i", key[0], "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode or not proc.stdout:
            return None
        with Image.open(io.BytesIO(proc.stdout)) as source:
            image = source.convert("RGB").copy()
        return self._remember(self.video_cache, key, image)

    @staticmethod
    def _cover(image: Image.Image, size: tuple[int, int], scale: float = 1.0) -> Image.Image:
        width, height = size
        target = (max(width, round(width * scale)), max(height, round(height * scale)))
        return ImageOps.fit(image, target, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    def render(
        self,
        path: str,
        kind: str,
        motion: str,
        progress: float,
        size: tuple[int, int],
        video_seconds: float = 0.0,
    ) -> Image.Image | None:
        width, height = max(16, size[0]), max(16, size[1])
        image = self.video_frame(path, video_seconds) if kind == "video" else self.source_image(path)
        if image is None:
            return None
        if kind != "image" or motion == "none":
            return ImageOps.pad(image, (width, height), method=Image.Resampling.LANCZOS, color="#080a0f")

        p = max(0.0, min(1.0, progress))
        if motion == "zoom_in":
            zoom = 1.0 + 0.10 * p
        elif motion == "zoom_out":
            zoom = 1.10 - 0.10 * p
        elif motion == "drift":
            zoom = 1.04 + 0.04 * p
        elif motion == "cosmic":
            zoom = 1.02 + 0.045 * p
        elif motion == "depth_2_5d":
            zoom = 1.03 + 0.035 * p
        else:
            zoom = 1.10
        large = self._cover(image, (width, height), zoom)
        max_x, max_y = max(0, large.width - width), max(0, large.height - height)
        x, y = max_x / 2, max_y / 2
        if motion == "pan_left":
            x = max_x * (1 - p)
        elif motion == "pan_right":
            x = max_x * p
        elif motion == "pan_up":
            y = max_y * (1 - p)
        elif motion == "pan_down":
            y = max_y * p
        elif motion == "drift":
            x = max_x * (0.15 + 0.70 * p)
            y = max_y * (0.75 - 0.50 * p)
        elif motion == "cosmic":
            x = max_x * (0.38 + 0.18 * math.sin(math.pi * p))
            y = max_y * (0.55 - 0.16 * math.sin(math.pi * p))
        elif motion == "depth_2_5d":
            x = max_x * (0.25 + 0.50 * p)
            y = max_y * (0.58 - 0.16 * p)
        return large.crop((round(x), round(y), round(x) + width, round(y) + height))
