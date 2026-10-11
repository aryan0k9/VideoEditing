import tempfile
import unittest
from pathlib import Path

from editor_core import (
    ExportSettings,
    MediaItem,
    _effect_filter,
    _transition_filter,
    load_project,
    save_project,
)


EFFECTS = (
    "brightness", "contrast", "saturation", "exposure", "warm", "cool", "sharpen",
    "blur", "vignette", "film_grain", "black_white", "sepia", "vintage", "cinematic",
    "camera_shake", "handheld", "zoom_pulse", "flash", "flicker", "glitch", "rgb_split",
    "motion_blur", "old_film", "dust_scratches", "historical", "dark_dramatic",
    "cold_war", "horror", "cosmic_glow", "space_nebula",
)
TRANSITIONS = (
    "cross_dissolve", "fade_black", "fade_white", "zoom", "slide_left", "slide_right",
    "blur", "flash", "film_burn", "glitch",
)


class EffectsTests(unittest.TestCase):
    def test_every_effect_builds_an_export_filter(self):
        settings = ExportSettings(width=1280, height=720, fps=30)
        self.assertTrue(all(_effect_filter(effect, .65, settings) for effect in EFFECTS))
        self.assertEqual(_effect_filter("none", .65, settings), "")

    def test_every_transition_builds_a_duration_preserving_filter(self):
        self.assertTrue(all(_transition_filter(value, .35, 3.0) for value in TRANSITIONS))
        self.assertEqual(_transition_filter("none", .35, 3.0), "")

    def test_effect_settings_survive_project_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "effects.storycut"
            item = MediaItem(
                "1.png", "image", assigned_duration=2.0, effect="cosmic_glow",
                effect_strength=.8, transition="film_burn", transition_duration=.45,
                playback_speed=1.5,
            )
            save_project(path, [item], "", "equal", ExportSettings(), [])
            loaded, *_ = load_project(path)
            self.assertEqual(loaded[0].effect, "cosmic_glow")
            self.assertAlmostEqual(loaded[0].effect_strength, .8)
            self.assertEqual(loaded[0].transition, "film_burn")
            self.assertAlmostEqual(loaded[0].transition_duration, .45)
            self.assertAlmostEqual(loaded[0].playback_speed, 1.5)


if __name__ == "__main__":
    unittest.main()
