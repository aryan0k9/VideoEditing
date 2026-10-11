from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from editor_core import ExportSettings, assign_durations, discover_media, duration_of, render_video


samples = root / ".test_artifacts"
items = discover_media([samples / "1.png", samples / "2.png", samples / "3.mp4", samples / "4.png"])
items[0].motion = "zoom_in"
items[1].motion = "pan_right"
items[3].motion = "drift"
audio = samples / "narration.wav"
audio_duration = duration_of(audio)
assign_durations(items, audio_duration, "pause", audio)
output = samples / "smoke_output.mp4"
render_video(
    items,
    audio,
    output,
    ExportSettings(width=640, height=360, fps=25, preset="ultrafast", crf=28),
    progress=lambda value, message: print(f"{value:.0%} {message}"),
)
print(output)
