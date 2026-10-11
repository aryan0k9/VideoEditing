from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from editor_core import ExportSettings, MediaItem, render_video
from test_effects import EFFECTS, TRANSITIONS


samples = root / ".test_artifacts"
items = []
for index, effect in enumerate(EFFECTS):
    items.append(MediaItem(
        str(samples / "1.png"), "image", assigned_duration=.24,
        effect=effect, effect_strength=.65,
        transition=TRANSITIONS[index % len(TRANSITIONS)], transition_duration=.1,
    ))
items.extend([
    MediaItem(
        str(samples / "3.mp4"), "video", source_duration=3.0, assigned_duration=.5,
        source_start=0.0, source_end=3.0, playback_speed=.5,
        effect="motion_blur", transition="fade_black", transition_duration=.12,
    ),
    MediaItem(
        str(samples / "3.mp4"), "video", source_duration=3.0, assigned_duration=.5,
        source_start=0.0, source_end=3.0, playback_speed=2.0,
        effect="cinematic", transition="glitch", transition_duration=.12,
    ),
])

output = samples / "all_effects_smoke.mp4"
render_video(
    items,
    samples / "narration.wav",
    output,
    ExportSettings(width=320, height=180, fps=12, preset="ultrafast", crf=30),
    progress=lambda value, message: print(f"{value:.0%} {message}"),
)
print(output)
