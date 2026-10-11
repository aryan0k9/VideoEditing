from pathlib import Path
import os
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from editor_core import ExportSettings, MediaItem, assign_durations, duration_of, render_video


samples = root / ".test_artifacts"
sources = [samples / "1.png", samples / "2.png", samples / "3.mp4", samples / "4.png"]
scene_count = int(os.environ.get("STORYCUT_SCENES", "48"))
items = [
    MediaItem(str(sources[index % len(sources)]), "video" if sources[index % len(sources)].suffix == ".mp4" else "image")
    for index in range(scene_count)
]
motions = ("zoom_in", "pan_right", "zoom_out", "pan_left", "pan_up", "drift", "pan_down")
for index, item in enumerate(items):
    if item.kind == "image":
        item.motion = motions[index % len(motions)]
audio = samples / "narration.wav"
assign_durations(items, duration_of(audio), "equal")
output = samples / f"large_chunked_{scene_count}_output.mp4"
render_video(
    items,
    audio,
    output,
    ExportSettings(width=320, height=180, fps=10, preset="ultrafast", crf=30),
    progress=lambda value, message: print(f"{value:.0%} {message}"),
)
print(output)
