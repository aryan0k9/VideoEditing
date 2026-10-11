import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from ai_sync import _build_ai_timeline, _monotonic_alignment, _speech_chunks

from editor_core import (
    EditorError,
    ExportSettings,
    MediaItem,
    TranscriptSegment,
    align_script_to_transcript,
    _pick_pause_boundaries,
    _pick_transcript_boundaries,
    assign_durations,
    assign_filename_timing,
    caption_word_timings,
    filename_timestamp,
    format_time,
    has_filename_timing,
    numeric_sort_key,
    read_script_file,
    load_project,
    save_project,
    _motion_filter,
    _youtube_audio_encode_arguments,
    _youtube_video_encode_arguments,
    _video_input_arguments,
    build_ffmpeg_command,
    youtube_video_bitrate_kbps,
    write_karaoke_ass,
    write_srt,
)


class EditorCoreTests(unittest.TestCase):
    def test_ai_alignment_preserves_story_order(self):
        similarities = np.array([
            [0.9, 0.8, 0.1, 0.1, 0.0, 0.0],
            [0.1, 0.2, 0.9, 0.8, 0.1, 0.0],
            [0.0, 0.0, 0.1, 0.2, 0.8, 0.9],
        ])
        assignments = _monotonic_alignment(similarities, 6, 3)
        self.assertEqual(assignments, sorted(assignments))
        self.assertEqual(set(assignments), {0, 1, 2})
        self.assertEqual(assignments[0], 0)
        self.assertEqual(assignments[-1], 2)

    def test_ai_timeline_caps_images_and_creates_described_gaps(self):
        items = [MediaItem("1.png", "image"), MediaItem("2.png", "image")]
        chunks = [
            (0.0, 3.0, "first visual"),
            (3.0, 6.0, "second visual"),
            (6.0, 9.0, "missing visual"),
            (9.0, 12.0, "another missing visual"),
        ]
        similarities = np.array([
            [0.32, 0.30, 0.10, 0.08],
            [0.10, 0.11, 0.14, 0.13],
        ])
        timeline, confidence = _build_ai_timeline(
            items, chunks, [0, 0, 1, 1], similarities,
            max_duration=5.0, min_confidence=0.30,
        )
        self.assertAlmostEqual(sum(item.assigned_duration for item in timeline), 12.0)
        self.assertTrue(all(item.assigned_duration <= 5.0 for item in timeline if item.kind != "gap"))
        self.assertGreaterEqual(sum(item.kind == "gap" for item in timeline), 2)
        self.assertTrue(any("missing visual" in item.placeholder_text for item in timeline if item.kind == "gap"))
        self.assertEqual(len(confidence), len(timeline))

    def test_ai_speech_chunks_never_exceed_selected_limit(self):
        transcript = [TranscriptSegment(
            0.0, 14.0, "one two three four five six seven",
            [
                {"start": index * 2.0, "end": (index + 1) * 2.0, "word": word}
                for index, word in enumerate("one two three four five six seven".split())
            ],
        )]
        chunks, _ = _speech_chunks(transcript, 14.0, 2, max_duration=5.0)
        self.assertTrue(chunks)
        self.assertTrue(all(end - start <= 5.001 for start, end, _ in chunks))

    def test_gap_uses_generated_black_video_input(self):
        settings = ExportSettings(width=1280, height=720, fps=30, background="111318")
        command = build_ffmpeg_command(
            [MediaItem("", "gap", assigned_duration=3.0, placeholder_text="new image needed")],
            "voice.mp3", "output.mp4", settings,
        )
        self.assertIn("lavfi", command)
        self.assertTrue(any("color=c=0x111318:s=1280x720:r=30" in part for part in command))

    def test_numeric_sort(self):
        names = ["10.png", "2.png", "1.png", "scene.png", "3-extra.mp4"]
        self.assertEqual(
            sorted(names, key=numeric_sort_key),
            ["1.png", "2.png", "3-extra.mp4", "10.png", "scene.png"],
        )

    def test_numeric_sort_ignores_extension_and_invisible_marks(self):
        names = ["260.webp", "\ufeff2.webp", "10.png", "1.png", "\u200b3.jpg"]
        self.assertEqual(
            sorted(names, key=numeric_sort_key),
            ["1.png", "\ufeff2.webp", "\u200b3.jpg", "10.png", "260.webp"],
        )

    def test_filename_timestamp_formats_and_automatic_detection(self):
        self.assertEqual(filename_timestamp("0-00.png"), 0.0)
        self.assertEqual(filename_timestamp("0-03 forest.webp"), 3.0)
        self.assertEqual(filename_timestamp("#14-24 village.jpg"), 14 * 60 + 24)
        self.assertEqual(filename_timestamp("1-02-15.mp4"), 3600 + 120 + 15)
        self.assertIsNone(filename_timestamp("260.png"))
        self.assertIsNone(filename_timestamp("2-75.png"))
        self.assertTrue(has_filename_timing([
            MediaItem("0-00.png", "image"), MediaItem("0-03.png", "image")
        ]))

    def test_filename_timing_sets_exact_scene_lengths(self):
        items = [
            MediaItem("0-08.png", "image"),
            MediaItem("0-00.png", "image"),
            MediaItem("0-03.png", "image"),
        ]
        assign_filename_timing(items, 12.0)
        self.assertEqual([item.name for item in items], ["0-00.png", "0-03.png", "0-08.png"])
        self.assertEqual([item.assigned_duration for item in items], [3.0, 5.0, 4.0])

    def test_filename_timing_creates_leading_gap_when_first_timestamp_is_late(self):
        items = [MediaItem("0-05.png", "image"), MediaItem("0-09.png", "image")]
        assign_filename_timing(items, 12.0)
        self.assertEqual(items[0].kind, "gap")
        self.assertEqual(items[0].assigned_duration, 5.0)
        self.assertEqual([item.assigned_duration for item in items[1:]], [4.0, 3.0])

    def test_filename_timing_can_skip_unreadable_names(self):
        items = [
            MediaItem("0-00.webp", "image"),
            MediaItem("844860577_1100659396038021_n.webp", "image"),
            MediaItem("0-05.webp", "image"),
        ]
        assign_filename_timing(items, 10.0, skip_invalid=True)
        self.assertEqual([item.name for item in items], ["0-00.webp", "0-05.webp"])
        self.assertEqual([item.assigned_duration for item in items], [5.0, 5.0])

    def test_filename_timing_still_rejects_invalid_names_without_confirmation(self):
        items = [MediaItem("0-00.webp", "image"), MediaItem("random.webp", "image")]
        with self.assertRaises(EditorError):
            assign_filename_timing(items, 10.0)

    def test_equal_duration_matches_audio_exactly(self):
        items = [MediaItem(f"{i}.png", "image") for i in range(3)]
        boundaries = assign_durations(items, 10.0, "equal")
        self.assertAlmostEqual(sum(i.assigned_duration for i in items), 10.0)
        self.assertEqual(len(boundaries), 4)

    def test_pause_boundaries_are_ordered(self):
        boundaries = _pick_pause_boundaries(
            12.0, 4, [(2.7, 3.1), (5.8, 6.2), (8.6, 9.1)], 0.5
        )
        self.assertEqual(boundaries[0], 0.0)
        self.assertEqual(boundaries[-1], 12.0)
        self.assertEqual(boundaries, sorted(boundaries))
        self.assertEqual(len(boundaries), 5)

    def test_speech_boundaries_follow_word_endings(self):
        transcript = [TranscriptSegment(
            0.0, 12.0, "one two three four",
            [
                {"start": 0.0, "end": 2.7, "word": "one."},
                {"start": 2.9, "end": 5.8, "word": "two."},
                {"start": 6.1, "end": 9.2, "word": "three."},
                {"start": 9.4, "end": 12.0, "word": "four."},
            ],
        )]
        boundaries = _pick_transcript_boundaries(12.0, 4, transcript, 0.5)
        self.assertEqual(boundaries, [0.0, 2.7, 5.8, 9.2, 12.0])

    def test_srt_utf8_and_time_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "captions.srt"
            write_srt([TranscriptSegment(1.25, 2.5, "नमस्ते दुनिया")], path)
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("00:00:01,250 --> 00:00:02,500", text)
            self.assertIn("नमस्ते दुनिया", text)
        self.assertEqual(format_time(3661.125), "01:01:01.125")

    def test_exact_hindi_script_replaces_recognition_but_keeps_word_timing(self):
        guide = [TranscriptSegment(
            0.0, 3.0, "राम गर गया",
            [
                {"start": 0.0, "end": 0.8, "word": "राम"},
                {"start": 0.8, "end": 1.6, "word": "गर"},
                {"start": 1.6, "end": 3.0, "word": "गया"},
            ],
        )]
        segments, confidence = align_script_to_transcript("राम घर गया", guide, 3.0)
        self.assertEqual(" ".join(segment.text for segment in segments), "राम घर गया")
        words = [word for segment in segments for word in segment.words]
        self.assertEqual([word["word"] for word in words], ["राम", "घर", "गया"])
        self.assertEqual(words[0]["start"], 0.0)
        self.assertEqual(words[-1]["end"], 3.0)
        self.assertGreater(confidence, 0.5)

    def test_cjk_script_keeps_compact_spacing_in_captions(self):
        guide = [TranscriptSegment(
            0.0, 2.0, "你好世界",
            [{"start": 0.0, "end": 2.0, "word": "你好世界"}],
        )]
        segments, _ = align_script_to_transcript("你好世界", guide, 2.0)
        self.assertEqual("".join(segment.text for segment in segments), "你好世界")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "compact.ass"
            write_karaoke_ass(segments, output, ExportSettings())
            text = output.read_text(encoding="utf-8-sig")
            self.assertTrue(all(character in text for character in "你好世界"))
            self.assertNotIn("你 好 世 界", text)

    def test_script_import_reads_srt_and_docx_unicode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            srt = root / "हिंदी.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nनमस्ते दुनिया\n", encoding="utf-8")
            self.assertEqual(read_script_file(srt), "नमस्ते दुनिया")

            docx = root / "script.docx"
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>বাংলা লেখা</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr("word/document.xml", xml)
            self.assertEqual(read_script_file(docx), "বাংলা লেখা")

    def test_motion_filters(self):
        settings = ExportSettings(width=1920, height=1080, fps=30)
        for motion in (
            "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down",
            "drift", "cosmic", "depth_2_5d",
        ):
            value = _motion_filter(motion, 90, settings)
            self.assertIn("zoompan=", value)
            self.assertIn("s=1920x1080", value)
        self.assertEqual(_motion_filter("none", 90, settings), "")

    def test_youtube_export_settings(self):
        self.assertEqual(youtube_video_bitrate_kbps(1920, 1080, 30), 8_000)
        self.assertEqual(youtube_video_bitrate_kbps(1080, 1920, 60), 12_000)
        settings = ExportSettings(width=1920, height=1080, fps=30, video_bitrate_kbps=8_000)
        video_args = _youtube_video_encode_arguments(settings)
        audio_args = _youtube_audio_encode_arguments()
        self.assertEqual(video_args[video_args.index("-profile:v") + 1], "high")
        self.assertEqual(video_args[video_args.index("-b:v") + 1], "8000k")
        self.assertEqual(video_args[video_args.index("-g") + 1], "15")
        self.assertIn("bt709", video_args)
        self.assertEqual(audio_args[audio_args.index("-b:a") + 1], "384k")
        self.assertEqual(audio_args[audio_args.index("-ar") + 1], "48000")

    def test_trimmed_video_uses_source_in_and_out_points(self):
        item = MediaItem("clip.mp4", "video", source_duration=10.0,
                         assigned_duration=4.0, source_start=2.0, source_end=5.5)
        arguments = _video_input_arguments(item, 4.0)
        self.assertEqual(arguments[:4], ["-ss", "2.000000", "-t", "3.500000"])

    def test_karaoke_ass_has_position_and_current_word_highlight(self):
        segment = TranscriptSegment(
            0.0, 2.0, "Ram ghar gaya",
            [
                {"start": 0.0, "end": 0.5, "word": "Ram"},
                {"start": 0.5, "end": 1.2, "word": "ghar"},
                {"start": 1.2, "end": 2.0, "word": "gaya"},
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "captions.ass"
            settings = ExportSettings(
                width=1080, height=1920, caption_x=0.75, caption_y=0.25,
                caption_highlight_color="FFD400",
            )
            write_karaoke_ass([segment], path, settings)
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("PlayResX: 1080", text)
            self.assertIn("PlayResY: 1920", text)
            self.assertIn(r"\pos(810,480)", text)
            self.assertIn(r"{\1c&H0000D4FF&}Ram", text)
            self.assertEqual(text.count("Dialogue:"), 3)

    def test_word_timings_are_estimated_for_old_projects(self):
        words = caption_word_timings(TranscriptSegment(1.0, 3.0, "one two three"))
        self.assertEqual([word["word"] for word in words], ["one", "two", "three"])
        self.assertEqual(words[0]["start"], 1.0)
        self.assertEqual(words[-1]["end"], 3.0)

    def test_caption_position_and_word_timings_survive_project_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.storycut"
            segment = TranscriptSegment(
                0.0, 1.0, "hello",
                [{"start": 0.0, "end": 1.0, "word": "hello"}],
            )
            settings = ExportSettings(caption_x=0.72, caption_y=0.31, caption_highlight_color="3DFF8A")
            item = MediaItem("clip.mp4", "video", 8.0, 2.0, "none", 1.5, 3.5)
            save_project(path, [item], "", "equal", settings, [segment], "en")
            items, _, _, loaded_settings, transcript, _ = load_project(path)
            self.assertEqual((loaded_settings.caption_x, loaded_settings.caption_y), (0.72, 0.31))
            self.assertEqual(loaded_settings.caption_highlight_color, "3DFF8A")
            self.assertEqual(transcript[0].words[0]["word"], "hello")
            self.assertEqual((items[0].source_start, items[0].source_end), (1.5, 3.5))


if __name__ == "__main__":
    unittest.main()
