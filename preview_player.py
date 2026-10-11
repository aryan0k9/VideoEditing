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

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps


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

    @staticmethod
    def _apply_effect(image: Image.Image, effect: str, strength: float, progress: float) -> Image.Image:
        s = max(0.0, min(1.0, strength))
        if effect == "brightness":
            return ImageEnhance.Brightness(image).enhance(1 + .28 * s)
        if effect == "contrast":
            return ImageEnhance.Contrast(image).enhance(1 + .55 * s)
        if effect == "saturation":
            return ImageEnhance.Color(image).enhance(1 + 1.1 * s)
        if effect == "exposure":
            return ImageEnhance.Contrast(ImageEnhance.Brightness(image).enhance(1 + .18 * s)).enhance(1 + .2 * s)
        if effect in {"warm", "cool", "cosmic_glow", "space_nebula", "cold_war", "horror"}:
            r, g, b = image.split()
            if effect == "warm":
                factors = (1 + .20 * s, 1 + .035 * s, 1 - .14 * s)
            elif effect in {"cool", "cold_war"}:
                factors = (1 - .12 * s, 1 + .04 * s, 1 + .20 * s)
            elif effect == "horror":
                factors = (1 + .13 * s, 1 - .12 * s, 1 - .12 * s)
            else:
                factors = (1 + .10 * s, 1 - .03 * s, 1 + .25 * s)
            image = Image.merge("RGB", tuple(channel.point(lambda value, f=f: min(255, value * f)) for channel, f in zip((r, g, b), factors)))
            if effect in {"cosmic_glow", "space_nebula"}:
                image = ImageEnhance.Color(image).enhance(1 + .55 * s)
            return image
        if effect == "sharpen":
            return image.filter(ImageFilter.UnsharpMask(radius=2, percent=round(80 + 160 * s), threshold=2))
        if effect == "blur":
            return image.filter(ImageFilter.GaussianBlur(.5 + 4 * s))
        if effect in {"black_white", "sepia", "vintage", "old_film", "historical"}:
            gray = ImageOps.grayscale(image)
            if effect == "black_white":
                return gray.convert("RGB")
            sepia = ImageOps.colorize(gray, "#24160d", "#f2d39b")
            mix = .9 if effect in {"sepia", "historical"} else .58 + .25 * s
            image = Image.blend(image, sepia, mix)
            if effect in {"vintage", "old_film", "historical"}:
                image = ImageEnhance.Contrast(image).enhance(1 + .2 * s)
            return image
        if effect in {"film_grain", "dust_scratches", "glitch", "old_film", "cold_war"}:
            noise = Image.effect_noise(image.size, 16 + 55 * s).convert("RGB")
            return Image.blend(image, noise, .025 + .10 * s)
        if effect == "cinematic":
            return ImageEnhance.Color(ImageEnhance.Contrast(image).enhance(1 + .28 * s)).enhance(1 + .12 * s)
        if effect in {"dark_dramatic", "horror"}:
            return ImageEnhance.Contrast(ImageEnhance.Brightness(image).enhance(1 - .16 * s)).enhance(1 + .48 * s)
        if effect in {"camera_shake", "handheld", "zoom_pulse"}:
            amplitude = {"camera_shake": 13, "handheld": 7, "zoom_pulse": 9}[effect] * s
            dx = round(amplitude * math.sin(progress * (35 if effect == "camera_shake" else 9)))
            dy = round(amplitude * math.cos(progress * (41 if effect == "camera_shake" else 11)))
            return ImageChops.offset(image, dx, dy)
        if effect == "flash":
            pulse = max(0.0, math.sin(progress * math.pi * 8)) ** 12
            return Image.blend(image, Image.new("RGB", image.size, "white"), pulse * (.25 + .5 * s))
        if effect == "flicker":
            return ImageEnhance.Brightness(image).enhance(1 + math.sin(progress * 80) * .07 * s)
        if effect == "rgb_split":
            offset = max(1, round(6 * s))
            r, g, b = image.split()
            return Image.merge("RGB", (ImageChops.offset(r, offset, 0), g, ImageChops.offset(b, -offset, 0)))
        if effect == "motion_blur":
            return image.filter(ImageFilter.GaussianBlur(.6 + 1.8 * s))
        if effect == "vignette":
            mask = Image.radial_gradient("L").resize(image.size)
            dark = ImageEnhance.Brightness(image).enhance(max(.25, 1 - .7 * s))
            return Image.composite(image, dark, mask)
        if effect == "cinematic_letterbox":
            result = image.copy()
            bar = max(1, round(result.height * .14 * s))
            draw = ImageDraw.Draw(result)
            draw.rectangle((0, 0, result.width, bar - 1), fill="black")
            draw.rectangle((0, result.height - bar, result.width, result.height), fill="black")
            return result
        return image

    @staticmethod
    def _apply_transition(image: Image.Image, transition: str, duration_fraction: float, progress: float) -> Image.Image:
        edge = max(.02, min(.49, duration_fraction))
        amount = max(0.0, 1 - progress / edge) if progress < edge else max(0.0, (progress - (1 - edge)) / edge)
        if amount <= 0 or transition == "none":
            return image
        color = "white" if transition in {"fade_white", "flash"} else "#ff6a22" if transition == "film_burn" else "black"
        if transition == "blur":
            return image.filter(ImageFilter.GaussianBlur(5 * amount))
        if transition in {"slide_left", "slide_right"}:
            distance = round(image.width * amount)
            if transition == "slide_left":
                distance = -distance
            shifted = ImageChops.offset(image, distance, 0)
            return Image.blend(shifted, Image.new("RGB", image.size, color), .25 * amount)
        if transition == "zoom":
            zoomed = ImageOps.fit(image, (image.width, image.height), method=Image.Resampling.LANCZOS, centering=(.5, .5))
            return Image.blend(zoomed, Image.new("RGB", image.size, "black"), .65 * amount)
        if transition == "glitch":
            shifted = ImageChops.offset(image, round(18 * amount), round(5 * amount))
            return Image.blend(shifted, Image.new("RGB", image.size, "black"), .30 * amount)
        return Image.blend(image, Image.new("RGB", image.size, color), min(1.0, amount))

    def render(
        self,
        path: str,
        kind: str,
        motion: str,
        progress: float,
        size: tuple[int, int],
        video_seconds: float = 0.0,
        effect: str = "none",
        effect_strength: float = 0.65,
        transition: str = "none",
        transition_duration_fraction: float = 0.1,
    ) -> Image.Image | None:
        width, height = max(16, size[0]), max(16, size[1])
        image = self.video_frame(path, video_seconds) if kind == "video" else self.source_image(path)
        if image is None:
            return None
        p = max(0.0, min(1.0, progress))
        if kind != "image" or motion == "none":
            result = ImageOps.pad(image, (width, height), method=Image.Resampling.LANCZOS, color="#080a0f")
        elif motion == "zoom_in":
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
        if kind == "image" and motion != "none":
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
            result = large.crop((round(x), round(y), round(x) + width, round(y) + height))
        result = self._apply_effect(result, effect, effect_strength, p)
        return self._apply_transition(result, transition, transition_duration_fraction, p)
