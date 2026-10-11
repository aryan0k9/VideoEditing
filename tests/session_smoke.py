from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import StoryCutApp
from editor_core import MediaItem


def close_test_app(app: StoryCutApp) -> None:
    if app._autosave_after_id:
        app.after_cancel(app._autosave_after_id)
    app.destroy()


def main() -> None:
    artifact_root = Path(__file__).resolve().parent.parent / ".test_artifacts"
    artifact_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session_smoke_", dir=artifact_root) as temporary:
        os.environ["LOCALAPPDATA"] = temporary
        manual_path = Path(temporary) / "Saved Project.storycut"

        app = StoryCutApp()
        app.withdraw()
        app.items = [MediaItem(path=str(Path(temporary) / "1.png"), kind="image", assigned_duration=2.5)]
        app.project_path = str(manual_path)
        app.playhead_seconds = 1.25
        assert app._autosave_now(force=True, show_status=False)

        index_path = Path(temporary) / "StoryCut" / "sessions" / "projects.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entry = index["projects"][0]
        assert manual_path.exists()
        assert Path(entry["autosave_path"]).exists()
        assert entry["playhead"] == 1.25
        close_test_app(app)

        restored = StoryCutApp()
        restored.withdraw()
        restored._restore_last_session()
        assert len(restored.items) == 1
        assert restored.project_path == str(manual_path)
        assert restored.playhead_seconds == 1.25
        close_test_app(restored)

    print("session autosave and restore smoke test passed")


if __name__ == "__main__":
    main()
