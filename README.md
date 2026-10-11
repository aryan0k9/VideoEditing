# StoryCut Studio

StoryCut is a local Windows editor for automatically combining numbered images and video clips with narration.

## What works

- Imports image/video files in natural numeric order (`1`, `2`, `3`, … `10`).
- Supports images, videos, or a mixed folder.
- Built-in Program Monitor with play/pause, stop, scrubbing, frame stepping, scene navigation, timecode, and volume.
- Graphical video/audio timeline with a movable playhead, horizontal scrolling, and adjustable timeline zoom.
- Ribbon workspace with dedicated Home, Edit, Motion, Effects, Captions, and Export tabs; the old right settings panel is no longer needed.
- Edit ribbon with Undo/Redo, Split at playhead, Trim Start/End, selected-clip deletion, and marked narration-range cutting.
- Context-aware `Ctrl+A` and `Delete`: select/remove one, many, or all media and timeline scenes without deleting source files from disk.
- Drag-and-drop scene reordering works in the Media list, scene table, and graphical video track.
- Narration range cutting creates a derived `_storycut_*.wav`; the original audio and all original media files remain untouched.
- Live preview shows still-image motion and source frames from video clips before export.
- Canvas selector supports 16:9 horizontal, 9:16 vertical, 1:1 square, and 4:5 portrait formats.
- The Program Monitor preserves the selected output aspect ratio; portrait formats appear as a tall, right-aligned canvas instead of being stretched horizontally.
- Fits the complete visual timeline exactly to the narration duration.
- `Narration pauses` mode detects speech pauses and places scene changes near them.
- `AI Content` mode matches visuals to transcript meaning, limits each automatic visual to 5 seconds by default, and creates described `NEEDS IMAGE` slots instead of stretching weak matches.
- `Filename timing` reads exact visual start times from names such as `0-00.png`, `0-03.webp`, `#14-24 scene.jpg`, or `1-02-15.mp4`. Files without readable timestamps can be skipped after confirmation; source files are never deleted.
- Select a `NEEDS IMAGE` slot and use **Fill selected NEEDS IMAGE** (or double-click the slot) to insert a generated image/video without changing its narration timing.
- `Equal fit` mode distributes every scene evenly.
- Lets you reorder/remove scenes and edit an individual scene duration while preserving total sync.
- Adds prompt-free motion to still images: zoom in/out, pan in four directions, gentle drift, `Cosmic Motion`, and AI-assisted `2.5D Depth`.
- Motion can be applied to Ctrl-selected scenes, an inclusive scene range, all images, or an automatic alternating cycle.
- Includes 30 local visual-effect presets: color/exposure controls, cinematic looks, film styles, camera shake, handheld, flash, flicker, glitch, RGB split, motion blur, documentary looks, and cosmic grades.
- Effects support strength control and can target selected scenes, a range, alternating scenes, or every image/video clip.
- Includes duration-controlled dissolve, black/white fade, zoom, slide, blur, flash, film-burn, and glitch scene transitions.
- Video clips support 0.25x–4x playback speed while the master timeline remains locked to narration.
- Optional same-language transcription through a local Whisper model.
- Exact-script caption mode accepts pasted text or imports `.txt`, `.md`, `.srt`, `.vtt`, and `.docx`, then aligns that text to narration while preserving the user's spelling.
- Script alignment is Unicode/language-neutral (including Indic, Arabic, Latin, Cyrillic, Chinese, Japanese, and Korean scripts) and uses local Whisper only as a timing guide.
- Word-level transcription drives karaoke captions: only the currently spoken word changes color.
- Captions can be dragged directly on the Program Monitor; position, size, and highlight color are saved with the project.
- Editable transcript panel, `.srt` export, and optional burned-in captions.
- Local MP4 export at 720p, 1080p, or vertical 1080×1920.
- Saves `.storycut` project files and timeline CSV files.
- Auto-saves active work, restores the last project and playhead after restart, and keeps recent projects in **Projects / Recovery**. `Ctrl+S` remains available for manual save.
- Uses FFmpeg locally; media is not uploaded.

## Start

For source/development mode, install the dependencies and then double-click `run_storycut.bat`, or run:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Packaged Windows builds are generated locally in `dist/` and are intentionally not committed because they include large runtime/model dependencies.

## Basic workflow

1. Name media `1.png`, `2.jpg`, `3.mp4`, etc.
2. In the **Home** ribbon, click **+ Files** or **+ Folder**.
3. Choose narration audio from **Home → Media → Narration**.
4. Generate a transcript and select **AI Content** for meaning-based matching. If media is named with timestamps, StoryCut automatically selects **Filename timing** instead. Narration pauses and Equal fit remain available.
5. Use the Program Monitor to play/pause, scrub, step frame-by-frame, or jump between scenes.
6. Use the **Edit** ribbon or `Ctrl+B` to split at the playhead; trim clip edges, mark an unwanted narration range, or delete selected scenes. Use `Ctrl+Z` to undo.
7. Drag scenes in the Media list, scene table, or graphical track to reorder them. `Ctrl+A` and `Delete` remove all selected project items.
8. In the **Motion** ribbon, choose a preset (including Cosmic Motion or 2.5D Depth) and apply it to selected scenes, a range, all images, or use **Alternate all**.
9. In the **Effects** ribbon, choose a visual preset and strength, add a transition, or change selected video-clip speed. Apply settings to selected scenes, a range, alternating scenes, or all clips.
10. In the **Captions** ribbon, either generate a transcript or use **Paste Script / Import Script → Align with Audio** for exact wording. Enable burned-in captions, then drag the caption on the Program Monitor to place it. Choose its size and spoken-word highlight color in the same ribbon.
11. Open the **Export** ribbon and click **Export MP4**.

The exported MP4 is accompanied by a `.timeline.csv`. When burned captions are enabled, the generated `.captions.srt` is also retained.

## Optional offline transcription

The packaged app includes the local transcription engine. The selected Whisper model downloads the first time it is used; subsequent transcription runs locally. `small` is the default balance for Hindi and English.

AI Content matching and 2.5D Depth also download their official models on first use, then reuse the local cache. Cosmic Motion does not require an AI model and is the faster option for large projects.

When running from source on another machine, double-click `install_transcription.bat` once to install `faster-whisper`.

Transcription is optional—editing and export work without it.

## Requirements

- Windows 10/11
- Python 3.10+
- FFmpeg and FFprobe available on `PATH`
- Python packages listed in `requirements.txt` (AI/transcription packages are only exercised when their corresponding feature is used)

## Current scope

Version 0.2 focuses on reliable numbered-media assembly, narration synchronization, live timeline preview, captions, motion, effects, transitions, and local export. Waveform display, multiple audio tracks, and proxy editing can be added in later iterations.
