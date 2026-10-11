from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from editor_core import (
    ExportSettings,
    TranscriptSegment,
    assign_durations,
    discover_media,
    duration_of,
    render_video,
    write_srt,
)


samples = root / ".test_artifacts"
items = discover_media([samples / "1.png", samples / "2.png", samples / "3.mp4", samples / "4.png"])
audio = samples / "narration.wav"
assign_durations(items, duration_of(audio), "equal", audio)
srt = samples / "हिंदी captions.srt"
write_srt(
    [TranscriptSegment(0.2, 2.7, "नमस्ते दुनिया"), TranscriptSegment(3.0, 5.7, "यह एक परीक्षण है")],
    srt,
)
output = samples / "caption_output.mp4"
render_video(
    items,
    audio,
    output,
    ExportSettings(width=640, height=360, fps=25, preset="ultrafast", crf=28, burn_captions=True),
    srt,
)
print(output)
