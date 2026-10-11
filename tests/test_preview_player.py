from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from preview_player import MCIAudioPlayer


class AudioPreviewTests(TestCase):
    def test_mci_failure_uses_converted_wav_fallback(self):
        player = object.__new__(MCIAudioPlayer)
        player.alias = "test_audio"
        player.path = ""
        player.playback_path = ""
        player.opened = False
        player.using_converted_audio = False
        converted = str(Path("compatible.wav").resolve())
        player._convert_for_preview = Mock(return_value=converted)

        commands = []

        def command(value, return_text=False):
            commands.append(value)
            if value.startswith("open ") and "compatible.wav" not in value:
                raise RuntimeError("unsupported codec")
            return ""

        player._command = command
        player.open("narration.mp3")

        self.assertTrue(player.opened)
        self.assertTrue(player.using_converted_audio)
        self.assertEqual(player.playback_path, converted)
        player._convert_for_preview.assert_called_once()
        self.assertTrue(any("compatible.wav" in value for value in commands))
