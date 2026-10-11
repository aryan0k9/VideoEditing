from __future__ import annotations

import bisect
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import ImageTk

from editor_core import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    EditorError,
    ExportSettings,
    MediaItem,
    TranscriptSegment,
    align_script_to_transcript,
    assign_durations,
    assign_filename_timing,
    caption_word_timings,
    discover_media,
    duration_of,
    format_time,
    filename_timestamp,
    has_filename_timing,
    load_project,
    media_kind,
    numeric_sort_key,
    read_script_file,
    render_video,
    save_project,
    transcript_backend,
    transcribe_audio,
    youtube_video_bitrate_kbps,
    write_srt,
    write_karaoke_ass,
    write_timeline_csv,
)
from preview_player import MCIAudioPlayer, PreviewRenderer
from ai_sync import ai_sync_backend, semantic_sync


APP_NAME = "StoryCut Studio"
APP_VERSION = "0.2.0"
BG = "#0b0e14"
PANEL = "#121722"
PANEL_2 = "#181f2d"
TEXT = "#f4f6fb"
MUTED = "#9aa5b5"
ACCENT = "#7c5cff"
ACCENT_2 = "#a48cff"
GREEN = "#36d399"
RED = "#ff6685"

MOTION_CODES = {
    "None": "none",
    "Zoom In": "zoom_in",
    "Zoom Out": "zoom_out",
    "Pan Left": "pan_left",
    "Pan Right": "pan_right",
    "Pan Up": "pan_up",
    "Pan Down": "pan_down",
    "Gentle Drift": "drift",
    "Cosmic Motion": "cosmic",
    "2.5D Depth": "depth_2_5d",
}
MOTION_LABELS = {value: key for key, value in MOTION_CODES.items()}
MOTION_CYCLE = ("zoom_in", "pan_right", "zoom_out", "pan_left", "pan_up", "drift", "pan_down")
EFFECT_CODES = {
    "None": "none", "Brightness": "brightness", "Contrast": "contrast",
    "Saturation": "saturation", "Exposure": "exposure", "Warm": "warm", "Cool": "cool",
    "Sharpen": "sharpen", "Blur": "blur", "Vignette": "vignette",
    "Film Grain": "film_grain", "Black & White": "black_white", "Sepia": "sepia",
    "Vintage": "vintage", "Cinematic": "cinematic", "Camera Shake": "camera_shake",
    "Handheld": "handheld", "Slow Zoom Pulse": "zoom_pulse", "Flash": "flash",
    "Flicker": "flicker", "Glitch": "glitch", "RGB Split": "rgb_split",
    "Motion Blur": "motion_blur", "Old Film": "old_film", "Dust & Scratches": "dust_scratches",
    "Historical": "historical", "Dark Dramatic": "dark_dramatic", "Cold War": "cold_war",
    "Horror / Mystery": "horror", "Cosmic Glow": "cosmic_glow", "Space Nebula": "space_nebula",
}
EFFECT_LABELS = {value: key for key, value in EFFECT_CODES.items()}
EFFECT_CYCLE = ("cinematic", "warm", "cool", "vintage", "dark_dramatic", "cosmic_glow")
TRANSITION_CODES = {
    "None": "none", "Cross Dissolve": "cross_dissolve", "Fade to Black": "fade_black",
    "Fade to White": "fade_white", "Zoom": "zoom", "Slide Left": "slide_left",
    "Slide Right": "slide_right", "Blur": "blur", "Flash": "flash",
    "Film Burn": "film_burn", "Glitch": "glitch",
}
TRANSITION_LABELS = {value: key for key, value in TRANSITION_CODES.items()}
OUTPUT_FORMATS = {
    "16:9 Horizontal": (16, 9, "1920×1080"),
    "9:16 Vertical": (9, 16, "1080×1920"),
    "1:1 Square": (1, 1, "1080×1080"),
    "4:5 Portrait": (4, 5, "1080×1350"),
}
CAPTION_HIGHLIGHTS = {
    "Yellow": "FFD400",
    "Green": "3DFF8A",
    "Cyan": "2DE2FF",
    "Orange": "FF8A2B",
    "Pink": "FF4FA3",
}
LANGUAGE_CODES = {
    "Auto detect (all)": None, "Auto detect": None,
    "Hindi": "hi", "English": "en", "Urdu": "ur", "Bengali": "bn",
    "Marathi": "mr", "Tamil": "ta", "Telugu": "te", "Gujarati": "gu",
    "Punjabi": "pa", "Malayalam": "ml", "Kannada": "kn", "Arabic": "ar",
    "Persian": "fa", "Spanish": "es", "French": "fr", "German": "de",
    "Portuguese": "pt", "Russian": "ru", "Turkish": "tr", "Indonesian": "id",
    "Chinese": "zh", "Japanese": "ja", "Korean": "ko",
}


class StoryCutApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} · Local automatic editor")
        self.geometry("1500x940")
        self.minsize(1180, 760)
        self.configure(bg=BG)

        self.items: list[MediaItem] = []
        self.audio_path = ""
        self.audio_duration = 0.0
        self.transcript: list[TranscriptSegment] = []
        self.transcript_language = ""
        self.loaded_script_text = ""
        self.script_alignment_confidence = -1.0
        self.project_path = ""
        self.session_id = uuid.uuid4().hex
        self._last_autosave_signature = ""
        self._autosave_after_id: str | None = None
        self._restoring_session = False
        self.last_export = ""
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.preview_renderer = PreviewRenderer()
        self.audio_player = MCIAudioPlayer()
        self.audio_preview_error_shown = False
        self.preview_playing = False
        self.playhead_seconds = 0.0
        self.preview_after_id: str | None = None
        self.preview_photo = None
        self._play_wall_start = 0.0
        self._audio_clock_active = False
        self._updating_seek = False
        self._last_preview_key = None

        self.sync_mode = tk.StringVar(value="pause")
        self.resolution = tk.StringVar(value="1920×1080")
        self.fps = tk.StringVar(value="30")
        self.quality = tk.StringVar(value="YouTube Recommended")
        self.caption_enabled = tk.BooleanVar(value=False)
        self.caption_size = tk.IntVar(value=44)
        self.caption_highlight = tk.StringVar(value="Yellow")
        self.caption_x = 0.5
        self.caption_y = 0.86
        self._dragging_caption = False
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.mark_in: float | None = None
        self.mark_out: float | None = None
        self.range_text = tk.StringVar(value="In: --  Out: --")
        self._tree_drag_source: tuple[str, int, int] | None = None
        self._track_drag_index: int | None = None
        self._track_drag_x = 0.0
        self.whisper_model = tk.StringVar(value="small")
        self.language = tk.StringVar(value="Auto detect (all)")
        self.motion_choice = tk.StringVar(value="Zoom In")
        self.effect_choice = tk.StringVar(value="Cinematic")
        self.effect_strength = tk.IntVar(value=65)
        self.transition_choice = tk.StringVar(value="Cross Dissolve")
        self.transition_duration = tk.DoubleVar(value=0.35)
        self.video_speed = tk.DoubleVar(value=1.0)
        self.preview_format = tk.StringVar(value="16:9 Horizontal")
        self.preview_time_text = tk.StringVar(value="00:00:00.000 / 00:00:00.000")
        self.seek_value = tk.DoubleVar(value=0.0)
        self.volume = tk.DoubleVar(value=80.0)
        self.timeline_zoom = tk.DoubleVar(value=10.0)
        self.track_hover_text = tk.StringVar(value="Hover a clip to see its full filename")
        self.status_text = tk.StringVar(value="Ready — add numbered images or videos")
        self.audio_text = tk.StringVar(value="No narration selected")
        self.summary_text = tk.StringVar(value="0 scenes  ·  00:00:00.000")
        self.transcript_progress_text = tk.StringVar(value="Ready to generate transcript")
        self.export_progress_text = tk.StringVar(value="Ready to export")
        self.ai_progress_text = tk.StringVar(value="AI Sync ready")
        self.ai_max_duration = tk.DoubleVar(value=5.0)
        self.ai_min_confidence = tk.IntVar(value=30)
        self.current_operation = ""
        self.ai_sync_confidence: list[float] = []

        self._configure_styles()
        self._build_menu()
        self._build_ui()
        self._update_project_title()
        self.bind_all("<space>", self._shortcut_play)
        self.bind_all("<Left>", lambda event: self._shortcut_step(event, -1))
        self.bind_all("<Right>", lambda event: self._shortcut_step(event, 1))
        self.bind_all("<Control-a>", self._select_all_context)
        self.bind_all("<Delete>", self._delete_context)
        self.bind_all("<BackSpace>", self._delete_context)
        self.bind_all("<Control-z>", lambda event: self.undo_edit())
        self.bind_all("<Control-y>", lambda event: self.redo_edit())
        self.bind_all("<Control-b>", lambda event: self.split_at_playhead())
        self.after(100, self._drain_events)
        self.after(700, self._restore_last_session)
        self._autosave_after_id = self.after(5000, self._autosave_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL_2)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 21))
        style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI Semibold", 11))
        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", "#2a3243")], foreground=[("disabled", "#667085")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(16, 9))
        style.map("Accent.TButton", background=[("active", ACCENT_2), ("disabled", "#3d3759")])
        style.configure("Danger.TButton", background="#3a2029", foreground="#ff9db0")
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TCombobox", fieldbackground="#202838", background="#202838", foreground=TEXT,
                        arrowcolor=TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", "#202838")],
                  foreground=[("readonly", TEXT)])
        style.configure("Treeview", background="#10151f", fieldbackground="#10151f", foreground=TEXT,
                        rowheight=30, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#1d2534", foreground="#cfd6e4",
                        relief="flat", font=("Segoe UI Semibold", 9), padding=7)
        style.map("Treeview", background=[("selected", "#352b68")])
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", background="#171d29", foreground=MUTED, padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor="#202635", background=ACCENT, bordercolor=BG)
        style.configure("Ribbon.TFrame", background=PANEL_2)
        style.configure("Ribbon.TLabel", background=PANEL_2, foreground=TEXT, font=("Segoe UI", 9))
        style.configure("RibbonMuted.TLabel", background=PANEL_2, foreground=MUTED, font=("Segoe UI", 8))
        style.configure("Ribbon.TCheckbutton", background=PANEL_2, foreground=TEXT, font=("Segoe UI", 9))
        style.map("Ribbon.TCheckbutton", background=[("active", PANEL_2)])
        style.configure("Ribbon.TRadiobutton", background=PANEL_2, foreground=TEXT, font=("Segoe UI", 9))
        style.map("Ribbon.TRadiobutton", background=[("active", PANEL_2)])

    def _build_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="New project", accelerator="Ctrl+N", command=self.new_project)
        file_menu.add_command(label="Open project...", accelerator="Ctrl+O", command=self.open_project)
        file_menu.add_command(label="Projects...", command=self.show_project_hub)
        file_menu.add_command(label="Save project", accelerator="Ctrl+S", command=self.save_project)
        file_menu.add_separator()
        file_menu.add_command(label="Export MP4...", accelerator="Ctrl+E", command=self.export_video)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menu, tearoff=False)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo_edit)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo_edit)
        edit_menu.add_separator()
        edit_menu.add_command(label="Split at playhead", accelerator="Ctrl+B", command=self.split_at_playhead)
        edit_menu.add_command(label="Trim clip start", command=self.trim_clip_start)
        edit_menu.add_command(label="Trim clip end", command=self.trim_clip_end)
        edit_menu.add_command(label="Delete selected", accelerator="Delete", command=self.delete_timeline_selection)
        menu.add_cascade(label="Edit", menu=edit_menu)

        playback = tk.Menu(menu, tearoff=False)
        playback.add_command(label="Play / Pause", accelerator="Space", command=self.toggle_playback)
        playback.add_command(label="Stop", command=self.stop_preview)
        playback.add_separator()
        playback.add_command(label="Previous frame", accelerator="Left", command=lambda: self.step_frame(-1))
        playback.add_command(label="Next frame", accelerator="Right", command=lambda: self.step_frame(1))
        playback.add_command(label="Previous scene", command=lambda: self.go_scene(-1))
        playback.add_command(label="Next scene", command=lambda: self.go_scene(1))
        menu.add_cascade(label="Playback", menu=playback)
        self.configure(menu=menu)
        self.bind_all("<Control-n>", lambda event: self.new_project())
        self.bind_all("<Control-o>", lambda event: self.open_project())
        self.bind_all("<Control-s>", lambda event: self.save_project())
        self.bind_all("<Control-e>", lambda event: self.export_video())

    def _ribbon_group(self, parent: ttk.Frame, title: str) -> ttk.Frame:
        group = ttk.Frame(parent, style="Ribbon.TFrame", padding=(10, 4))
        group.pack(side="left", fill="y")
        ttk.Label(group, text=title.upper(), style="RibbonMuted.TLabel").pack(anchor="w", pady=(0, 5))
        return group

    def _scrollable_ribbon_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        """Create a ribbon page whose tools remain reachable on narrow windows."""
        tab = ttk.Frame(notebook, style="Ribbon.TFrame")
        notebook.add(tab, text=title)
        canvas = tk.Canvas(tab, height=142, bg=PANEL_2, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="horizontal", command=canvas.xview)
        canvas.pack(fill="both", expand=True)
        scrollbar.pack(fill="x")
        content = ttk.Frame(canvas, style="Ribbon.TFrame", padding=6)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            requested = content.winfo_reqwidth()
            canvas.itemconfigure(window_id, width=max(requested, canvas.winfo_width()))

        content.bind("<Configure>", update_region)
        canvas.bind("<Configure>", update_region)
        canvas.bind(
            "<Shift-MouseWheel>",
            lambda event: canvas.xview_scroll(-max(-3, min(3, int(event.delta / 120))), "units"),
        )
        return content

    def _build_ribbon(self) -> None:
        ribbon = ttk.Notebook(self)
        ribbon.pack(fill="x", padx=20, pady=(0, 10))
        self.ribbon = ribbon
        home = self._scrollable_ribbon_tab(ribbon, "Home")
        edit = self._scrollable_ribbon_tab(ribbon, "Edit")
        motion = self._scrollable_ribbon_tab(ribbon, "Motion")
        effects = self._scrollable_ribbon_tab(ribbon, "Effects")
        captions = self._scrollable_ribbon_tab(ribbon, "Captions")
        export = self._scrollable_ribbon_tab(ribbon, "Export")

        project = self._ribbon_group(home, "Project")
        row = ttk.Frame(project, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="New", command=self.new_project).pack(side="left")
        ttk.Button(row, text="Open", command=self.open_project).pack(side="left", padx=4)
        ttk.Button(row, text="Save", command=self.save_project).pack(side="left")
        ttk.Button(project, text="Projects / Recovery", command=self.show_project_hub).pack(
            fill="x", pady=(4, 0)
        )

        media = self._ribbon_group(home, "Media")
        row = ttk.Frame(media, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="+ Files", command=self.add_files).pack(side="left")
        ttk.Button(row, text="+ Folder", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(row, text="Narration", command=self.choose_audio).pack(side="left")
        ttk.Button(row, text="Remove audio", command=self.remove_audio).pack(side="left", padx=(4, 0))
        ttk.Button(media, text="Fill selected NEEDS IMAGE", command=self.fill_selected_gap).pack(
            fill="x", pady=(4, 0)
        )

        arrange = self._ribbon_group(home, "Arrange")
        row = ttk.Frame(arrange, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Up", command=lambda: self.move_items(-1)).pack(side="left")
        ttk.Button(row, text="Down", command=lambda: self.move_items(1)).pack(side="left", padx=4)
        ttk.Button(row, text="Sort 1-N", command=self.sort_items).pack(side="left")
        ttk.Button(row, text="Remove", style="Danger.TButton", command=self.remove_items).pack(side="left", padx=(4, 0))

        sync = self._ribbon_group(home, "Timeline sync")
        sync_row_one = ttk.Frame(sync, style="Ribbon.TFrame")
        sync_row_one.pack(anchor="w")
        ttk.Radiobutton(sync_row_one, text="Pauses", variable=self.sync_mode, value="pause",
                        style="Ribbon.TRadiobutton", command=self.calculate_timeline).pack(side="left")
        ttk.Radiobutton(sync_row_one, text="Speech timing", variable=self.sync_mode, value="speech",
                        style="Ribbon.TRadiobutton", command=self.calculate_timeline).pack(side="left", padx=4)
        ttk.Radiobutton(sync_row_one, text="Filename timing", variable=self.sync_mode, value="filename",
                        style="Ribbon.TRadiobutton", command=self.calculate_timeline).pack(side="left", padx=4)
        sync_row_two = ttk.Frame(sync, style="Ribbon.TFrame")
        sync_row_two.pack(anchor="w", pady=(2, 0))
        ttk.Radiobutton(sync_row_two, text="AI Content", variable=self.sync_mode, value="ai",
                        style="Ribbon.TRadiobutton", command=self.start_ai_sync).pack(side="left")
        ttk.Radiobutton(sync_row_two, text="Equal", variable=self.sync_mode, value="equal",
                        style="Ribbon.TRadiobutton", command=self.calculate_timeline).pack(side="left", padx=4)
        ttk.Button(sync_row_two, text="Recalculate", command=self.calculate_timeline).pack(side="left")
        ai_progress_row = ttk.Frame(sync, style="Ribbon.TFrame")
        ai_progress_row.pack(fill="x", pady=(5, 0))
        self.ai_progress = ttk.Progressbar(ai_progress_row, maximum=100, mode="determinate", length=155)
        self.ai_progress.pack(side="left")
        ttk.Label(ai_progress_row, textvariable=self.ai_progress_text,
                  style="RibbonMuted.TLabel", width=23).pack(side="left", padx=(6, 0))
        ai_rules = ttk.Frame(sync, style="Ribbon.TFrame")
        ai_rules.pack(anchor="w", pady=(3, 0))
        ttk.Label(ai_rules, text="Max image", style="RibbonMuted.TLabel").pack(side="left")
        ttk.Spinbox(ai_rules, from_=2, to=10, increment=0.5, width=4,
                    textvariable=self.ai_max_duration).pack(side="left", padx=(4, 2))
        ttk.Label(ai_rules, text="sec   Min match", style="RibbonMuted.TLabel").pack(side="left")
        ttk.Spinbox(ai_rules, from_=0, to=100, increment=5, width=4,
                    textvariable=self.ai_min_confidence).pack(side="left", padx=(4, 2))
        ttk.Label(ai_rules, text="%", style="RibbonMuted.TLabel").pack(side="left")

        history = self._ribbon_group(edit, "History")
        row = ttk.Frame(history, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Undo  Ctrl+Z", command=self.undo_edit).pack(side="left")
        ttk.Button(row, text="Redo  Ctrl+Y", command=self.redo_edit).pack(side="left", padx=4)

        clip_edit = self._ribbon_group(edit, "Selected clip")
        row = ttk.Frame(clip_edit, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Split at playhead", command=self.split_at_playhead).pack(side="left")
        ttk.Button(row, text="Trim start", command=self.trim_clip_start).pack(side="left", padx=4)
        ttk.Button(row, text="Trim end", command=self.trim_clip_end).pack(side="left")
        ttk.Button(row, text="Delete", style="Danger.TButton",
                   command=self.delete_timeline_selection).pack(side="left", padx=(4, 0))

        range_edit = self._ribbon_group(edit, "Audio range")
        row = ttk.Frame(range_edit, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Mark In", command=self.set_mark_in).pack(side="left")
        ttk.Button(row, text="Mark Out", command=self.set_mark_out).pack(side="left", padx=4)
        ttk.Button(row, text="Cut range", style="Danger.TButton", command=self.cut_marked_audio).pack(side="left")
        ttk.Label(range_edit, textvariable=self.range_text, style="RibbonMuted.TLabel").pack(anchor="w", pady=(4, 0))

        preset = self._ribbon_group(motion, "Motion preset")
        ttk.Combobox(
            preset, textvariable=self.motion_choice, state="readonly", width=16,
            values=("None", "Auto cycle", "Zoom In", "Zoom Out", "Pan Left", "Pan Right", "Pan Up", "Pan Down", "Gentle Drift", "Cosmic Motion", "2.5D Depth"),
        ).pack()
        ttk.Label(
            preset, text="2.5D uses a local depth model on export",
            style="RibbonMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        apply_group = self._ribbon_group(motion, "Apply to")
        row = ttk.Frame(apply_group, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Selected scenes", command=self.apply_motion_selected).pack(side="left")
        ttk.Button(row, text="Scene range...", command=self.apply_motion_range).pack(side="left", padx=4)
        ttk.Button(row, text="All images", command=self.apply_motion_all).pack(side="left")
        ttk.Button(row, text="Alternate all", command=self.apply_motion_alternate).pack(side="left", padx=(4, 0))

        look = self._ribbon_group(effects, "Visual effect")
        ttk.Combobox(
            look, textvariable=self.effect_choice, state="readonly", width=19,
            values=tuple(EFFECT_CODES),
        ).pack(fill="x")
        strength_row = ttk.Frame(look, style="Ribbon.TFrame")
        strength_row.pack(fill="x", pady=(4, 0))
        ttk.Label(strength_row, text="Strength", style="RibbonMuted.TLabel").pack(side="left")
        ttk.Scale(strength_row, from_=0, to=100, variable=self.effect_strength, length=120).pack(side="left", padx=4)
        ttk.Label(strength_row, textvariable=self.effect_strength, style="RibbonMuted.TLabel", width=3).pack(side="left")

        effect_apply = self._ribbon_group(effects, "Apply effect to")
        row = ttk.Frame(effect_apply, style="Ribbon.TFrame")
        row.pack()
        ttk.Button(row, text="Selected", command=self.apply_effect_selected).pack(side="left")
        ttk.Button(row, text="Range...", command=self.apply_effect_range).pack(side="left", padx=4)
        ttk.Button(row, text="All clips", command=self.apply_effect_all).pack(side="left")
        ttk.Button(row, text="Alternate", command=self.apply_effect_alternate).pack(side="left", padx=4)
        ttk.Button(row, text="Clear", command=self.clear_effect_selected).pack(side="left")

        transition = self._ribbon_group(effects, "Transition")
        ttk.Combobox(
            transition, textvariable=self.transition_choice, state="readonly", width=18,
            values=tuple(TRANSITION_CODES),
        ).pack(fill="x")
        duration_row = ttk.Frame(transition, style="Ribbon.TFrame")
        duration_row.pack(fill="x", pady=(4, 0))
        ttk.Label(duration_row, text="Duration", style="RibbonMuted.TLabel").pack(side="left")
        ttk.Spinbox(duration_row, from_=0.1, to=1.5, increment=.05, width=5,
                    textvariable=self.transition_duration).pack(side="left", padx=4)
        ttk.Label(duration_row, text="sec", style="RibbonMuted.TLabel").pack(side="left")
        row = ttk.Frame(transition, style="Ribbon.TFrame")
        row.pack(pady=(4, 0))
        ttk.Button(row, text="Selected", command=self.apply_transition_selected).pack(side="left")
        ttk.Button(row, text="Range...", command=self.apply_transition_range).pack(side="left", padx=4)
        ttk.Button(row, text="All clips", command=self.apply_transition_all).pack(side="left")
        ttk.Button(row, text="Alternate", command=self.apply_transition_alternate).pack(side="left", padx=4)
        ttk.Button(row, text="Clear", command=self.clear_transition_selected).pack(side="left")

        speed = self._ribbon_group(effects, "Video speed")
        row = ttk.Frame(speed, style="Ribbon.TFrame")
        row.pack()
        ttk.Combobox(row, textvariable=self.video_speed, state="readonly", width=7,
                     values=(0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)).pack(side="left")
        ttk.Button(row, text="Apply selected videos", command=self.apply_video_speed).pack(side="left", padx=4)
        ttk.Label(speed, text="Timeline duration stays synced to narration", style="RibbonMuted.TLabel").pack(anchor="w", pady=(4, 0))

        options = self._ribbon_group(captions, "Caption options")
        ttk.Checkbutton(options, text="Burn transcript into video", variable=self.caption_enabled,
                        style="Ribbon.TCheckbutton", command=self._on_caption_style_change).pack(anchor="w")
        ttk.Label(options, text="Drag caption directly on the preview", style="RibbonMuted.TLabel").pack(anchor="w")
        backend = transcript_backend()
        backend_text = f"Local: {backend}" if backend else "Local transcript engine unavailable"
        ttk.Label(options, text=backend_text, background=PANEL_2,
                  foreground=GREEN if backend else "#f5b942", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))
        recognition = self._ribbon_group(captions, "Recognition")
        row = ttk.Frame(recognition, style="Ribbon.TFrame")
        row.pack()
        ttk.Label(row, text="Model", style="Ribbon.TLabel").pack(side="left", padx=(0, 4))
        ttk.Combobox(row, textvariable=self.whisper_model, state="readonly", width=9,
                     values=("tiny", "base", "small", "medium", "large-v3")).pack(side="left")
        ttk.Label(row, text="Language", style="Ribbon.TLabel").pack(side="left", padx=(10, 4))
        ttk.Combobox(row, textvariable=self.language, state="readonly", width=15,
                     values=tuple(LANGUAGE_CODES)).pack(side="left")
        appearance = self._ribbon_group(captions, "Appearance")
        row = ttk.Frame(appearance, style="Ribbon.TFrame")
        row.pack()
        ttk.Label(row, text="Size", style="Ribbon.TLabel").pack(side="left", padx=(0, 4))
        size_control = ttk.Spinbox(row, from_=18, to=96, increment=2, textvariable=self.caption_size,
                                   width=5, command=self._on_caption_style_change)
        size_control.pack(side="left")
        size_control.bind("<KeyRelease>", self._on_caption_style_change)
        ttk.Label(row, text="Highlight", style="Ribbon.TLabel").pack(side="left", padx=(10, 4))
        highlight_control = ttk.Combobox(row, textvariable=self.caption_highlight, state="readonly", width=9,
                                         values=tuple(CAPTION_HIGHLIGHTS))
        highlight_control.pack(side="left")
        highlight_control.bind("<<ComboboxSelected>>", self._on_caption_style_change)
        ttk.Button(row, text="Reset position", command=self._reset_caption_position).pack(side="left", padx=(8, 0))
        transcript = self._ribbon_group(captions, "Transcript")
        row = ttk.Frame(transcript, style="Ribbon.TFrame")
        row.pack()
        self.generate_button = ttk.Button(row, text="Generate", command=self.generate_transcript)
        self.generate_button.pack(side="left")
        ttk.Button(row, text="Export SRT", command=self.export_srt).pack(side="left", padx=4)
        script_row = ttk.Frame(transcript, style="Ribbon.TFrame")
        script_row.pack(anchor="w", pady=(4, 0))
        ttk.Button(script_row, text="Paste Script", command=self.paste_script).pack(side="left")
        ttk.Button(script_row, text="Import Script", command=self.import_script).pack(side="left", padx=4)
        self.align_script_button = ttk.Button(
            script_row, text="Align with Audio", command=self.align_loaded_script
        )
        self.align_script_button.pack(side="left")
        caption_progress = self._ribbon_group(captions, "Progress")
        self.transcript_progress = ttk.Progressbar(caption_progress, maximum=100, mode="determinate", length=210)
        self.transcript_progress.pack(fill="x")
        ttk.Label(caption_progress, textvariable=self.transcript_progress_text,
                  style="RibbonMuted.TLabel", width=30).pack(anchor="w")

        video = self._ribbon_group(export, "Video")
        row = ttk.Frame(video, style="Ribbon.TFrame")
        row.pack()
        ttk.Label(row, text="Size", style="Ribbon.TLabel").pack(side="left", padx=(0, 4))
        self.resolution_combo = ttk.Combobox(
            row, textvariable=self.resolution, state="readonly", width=11,
            values=("1280×720", "1920×1080", "3840×2160", "1080×1920", "1080×1080", "1080×1350"),
        )
        self.resolution_combo.pack(side="left")
        self.resolution_combo.bind("<<ComboboxSelected>>", self._on_resolution_change)
        ttk.Label(row, text="FPS", style="Ribbon.TLabel").pack(side="left", padx=(10, 4))
        ttk.Combobox(row, textvariable=self.fps, state="readonly", width=5,
                     values=("24", "25", "30", "60")).pack(side="left")
        ttk.Label(row, text="Quality", style="Ribbon.TLabel").pack(side="left", padx=(10, 4))
        ttk.Combobox(row, textvariable=self.quality, state="readonly", width=18,
                     values=("YouTube Recommended", "High (CRF 17)", "Balanced (CRF 20)",
                             "Small file (CRF 24)")).pack(side="left")
        output = self._ribbon_group(export, "Output")
        row = ttk.Frame(output, style="Ribbon.TFrame")
        row.pack()
        self.export_button = ttk.Button(row, text="Export MP4", style="Accent.TButton", command=self.export_video)
        self.export_button.pack(side="left")
        ttk.Button(row, text="Preview", command=self.preview_export).pack(side="left", padx=4)
        ttk.Button(row, text="Timeline CSV", command=self.export_timeline).pack(side="left")
        export_progress = self._ribbon_group(export, "Export progress")
        self.export_progress = ttk.Progressbar(export_progress, maximum=100, mode="determinate", length=240)
        self.export_progress.pack(fill="x")
        ttk.Label(export_progress, textvariable=self.export_progress_text,
                  style="RibbonMuted.TLabel", width=32).pack(anchor="w")

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(22, 17, 22, 11))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  Numbered media → narration → finished video", style="Muted.TLabel").pack(side="left", pady=(8, 0))
        self._build_ribbon()

        workspace_shell = ttk.Frame(self)
        workspace_shell.pack(fill="both", expand=True)
        self.workspace_canvas = tk.Canvas(workspace_shell, bg=BG, highlightthickness=0)
        workspace_scroll = ttk.Scrollbar(workspace_shell, orient="vertical", command=self.workspace_canvas.yview)
        workspace_scroll.pack(side="right", fill="y")
        self.workspace_canvas.pack(side="left", fill="both", expand=True)
        self.workspace_canvas.configure(yscrollcommand=workspace_scroll.set)
        workspace = ttk.Frame(self.workspace_canvas)
        self._workspace_window = self.workspace_canvas.create_window((0, 0), window=workspace, anchor="nw")
        workspace.bind("<Configure>", self._update_workspace_scrollregion)
        self.workspace_canvas.bind("<Configure>", self._resize_workspace)
        self.bind_all("<MouseWheel>", self._route_mousewheel, add="+")

        body = ttk.Panedwindow(workspace, orient="horizontal")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        left = ttk.Frame(body, style="Panel.TFrame", padding=16)
        center = ttk.Frame(body, style="Panel.TFrame", padding=16)
        body.add(left, weight=3)
        body.add(center, weight=9)
        self.main_panes = body
        self._build_media_panel(left)
        self._build_timeline_panel(center)
        self.after(250, self._set_initial_sashes)

        status = ttk.Frame(workspace, padding=(20, 0, 20, 15))
        status.pack(fill="x")
        self.progress = ttk.Progressbar(status, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 14))
        ttk.Label(status, textvariable=self.status_text, style="Muted.TLabel", width=52).pack(side="left")

    def _resize_workspace(self, event) -> None:
        self.workspace_canvas.itemconfigure(self._workspace_window, width=max(1, event.width))
        self._update_workspace_scrollregion()

    def _update_workspace_scrollregion(self, _event=None) -> None:
        bbox = self.workspace_canvas.bbox("all")
        if bbox:
            self.workspace_canvas.configure(scrollregion=bbox)

    def _scroll_workspace(self, event) -> str:
        if event.state & 0x0001:
            return "break"
        self.workspace_canvas.yview_scroll((-1 if event.delta > 0 else 1) * 3, "units")
        return "break"

    def _route_mousewheel(self, event) -> str | None:
        if event.state & 0x0001:
            return None
        if isinstance(event.widget, (ttk.Treeview, tk.Text)):
            return None
        widget = event.widget
        while widget is not None:
            if widget is self.workspace_canvas:
                return self._scroll_workspace(event)
            try:
                parent_name = widget.winfo_parent()
                widget = widget._nametowidget(parent_name) if parent_name else None
            except (KeyError, tk.TclError):
                widget = None
        return None

    def _set_initial_sashes(self) -> None:
        width = self.main_panes.winfo_width()
        if width > 900:
            left_width = max(300, min(365, round(width * 0.23)))
            self.main_panes.sashpos(0, left_width)

    def _build_media_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="1  MEDIA", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Files are naturally sorted by their number: 1, 2, 3…",
                  style="PanelMuted.TLabel", wraplength=310).pack(anchor="w", pady=(3, 12))

        columns = ("number", "name", "kind")
        media_wrap = ttk.Frame(parent, style="Panel.TFrame")
        media_wrap.pack(fill="both", expand=True)
        self.media_tree = ttk.Treeview(media_wrap, columns=columns, show="headings", selectmode="extended")
        media_y = ttk.Scrollbar(media_wrap, orient="vertical", command=self.media_tree.yview)
        media_x = ttk.Scrollbar(media_wrap, orient="horizontal", command=self.media_tree.xview)
        self.media_tree.configure(yscrollcommand=media_y.set, xscrollcommand=media_x.set)
        self.media_tree.heading("number", text="#")
        self.media_tree.heading("name", text="FILE")
        self.media_tree.heading("kind", text="TYPE")
        self.media_tree.column("number", width=42, anchor="center", stretch=False)
        self.media_tree.column("name", width=235, anchor="w")
        self.media_tree.column("kind", width=64, anchor="center", stretch=False)
        self.media_tree.grid(row=0, column=0, sticky="nsew")
        media_y.grid(row=0, column=1, sticky="ns")
        media_x.grid(row=1, column=0, sticky="ew")
        media_wrap.rowconfigure(0, weight=1)
        media_wrap.columnconfigure(0, weight=1)
        self.media_tree.bind("<Double-1>", self._preview_selected_source)
        self.media_tree.bind("<ButtonPress-1>", lambda event: self._tree_drag_start(event, "media"), add="+")
        self.media_tree.bind("<B1-Motion>", self._tree_drag_motion, add="+")
        self.media_tree.bind("<ButtonRelease-1>", lambda event: self._tree_drag_end(event, "media"), add="+")

        ttk.Separator(parent).pack(fill="x", pady=(12, 13))
        ttk.Label(parent, text="2  NARRATION", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, textvariable=self.audio_text, style="PanelMuted.TLabel", wraplength=310).pack(anchor="w", pady=(5, 9))
        ttk.Label(parent, text="Use Home → Media → Narration to choose audio.",
                  style="PanelMuted.TLabel", wraplength=310).pack(anchor="w")

    def _build_timeline_panel(self, parent: ttk.Frame) -> None:
        heading = ttk.Frame(parent, style="Panel.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, text="TIMELINE", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, textvariable=self.summary_text, style="PanelMuted.TLabel").pack(side="right")

        monitor = ttk.Frame(parent, style="Panel2.TFrame", padding=8)
        monitor.pack(fill="x", pady=(10, 0))
        self.preview_canvas = tk.Canvas(
            monitor, height=220, bg="#03050a", highlightthickness=1, highlightbackground="#30394b",
        )
        self.preview_canvas.pack(fill="x")
        self.preview_canvas.bind("<Configure>", lambda event: self._render_preview_at(self.playhead_seconds, force=True))
        self.preview_canvas.bind("<ButtonPress-1>", self._caption_drag_start)
        self.preview_canvas.bind("<B1-Motion>", self._caption_drag_move)
        self.preview_canvas.bind("<ButtonRelease-1>", self._caption_drag_end)

        seek_line = ttk.Frame(monitor, style="Panel2.TFrame")
        seek_line.pack(fill="x", pady=(8, 4))
        self.seek_scale = ttk.Scale(
            seek_line, from_=0.0, to=1.0, variable=self.seek_value, command=self._on_seek_slider,
        )
        self.seek_scale.pack(side="left", fill="x", expand=True)
        ttk.Label(seek_line, text="Volume", background=PANEL_2, foreground=MUTED).pack(side="left", padx=(12, 4))
        ttk.Scale(seek_line, from_=0, to=100, variable=self.volume, command=self._on_volume,
                  length=90).pack(side="left")
        transport = ttk.Frame(monitor, style="Panel2.TFrame")
        transport.pack(fill="x")
        transport_buttons = ttk.Frame(transport, style="Panel2.TFrame")
        transport_buttons.pack(fill="x")
        ttk.Button(transport_buttons, text="|<", width=3, command=lambda: self.go_scene(-1)).pack(side="left")
        ttk.Button(transport_buttons, text="<|", width=3, command=lambda: self.step_frame(-1)).pack(side="left", padx=(4, 0))
        self.play_button = ttk.Button(transport_buttons, text="Play", width=7, command=self.toggle_playback)
        self.play_button.pack(side="left", padx=4)
        ttk.Button(transport_buttons, text="Stop", width=6, command=self.stop_preview).pack(side="left")
        ttk.Button(transport_buttons, text="|>", width=3, command=lambda: self.step_frame(1)).pack(side="left", padx=(4, 0))
        ttk.Button(transport_buttons, text=">|", width=3, command=lambda: self.go_scene(1)).pack(side="left", padx=(4, 0))
        transport_info = ttk.Frame(transport, style="Panel2.TFrame")
        transport_info.pack(fill="x", pady=(4, 0))
        ttk.Label(transport_info, textvariable=self.preview_time_text, background=PANEL_2,
                  foreground=TEXT, font=("Cascadia Mono", 9)).pack(side="right", padx=(12, 0))
        self.preview_format_combo = ttk.Combobox(
            transport_info, textvariable=self.preview_format, state="readonly", width=17,
            values=tuple(OUTPUT_FORMATS),
        )
        self.preview_format_combo.pack(side="right", padx=(8, 0))
        self.preview_format_combo.bind("<<ComboboxSelected>>", self._on_preview_format_change)
        ttk.Label(transport_info, text="Canvas:", background=PANEL_2, foreground=MUTED).pack(side="right", padx=(8, 0))

        track_header = ttk.Frame(parent, style="Panel.TFrame")
        track_header.pack(fill="x", pady=(10, 0))
        ttk.Label(track_header, text="VIDEO / AUDIO TRACKS", style="PanelMuted.TLabel").pack(side="left")
        ttk.Label(track_header, text="Zoom", style="PanelMuted.TLabel").pack(side="right")
        ttk.Scale(track_header, from_=3, to=40, variable=self.timeline_zoom, command=self._on_timeline_zoom,
                  length=100).pack(side="right", padx=6)
        ttk.Label(track_header, textvariable=self.track_hover_text, style="PanelMuted.TLabel").pack(
            side="right", padx=(8, 16)
        )
        track_wrap = ttk.Frame(parent, style="Panel2.TFrame")
        track_wrap.pack(fill="x", pady=(4, 9))
        self.track_canvas = tk.Canvas(track_wrap, height=58, bg="#0d121b", highlightthickness=0)
        self.track_canvas.pack(fill="x", expand=True)
        track_scroll = ttk.Scrollbar(track_wrap, orient="horizontal", command=self.track_canvas.xview)
        track_scroll.pack(fill="x")
        self.track_canvas.configure(xscrollcommand=track_scroll.set)
        self.track_canvas.bind("<ButtonPress-1>", self._on_track_press)
        self.track_canvas.bind("<B1-Motion>", self._on_track_drag)
        self.track_canvas.bind("<ButtonRelease-1>", self._on_track_release)
        self.track_canvas.bind("<Motion>", self._on_track_hover, add="+")
        self.track_canvas.bind("<Leave>", self._on_track_leave, add="+")
        self.track_canvas.bind("<Configure>", lambda event: self._redraw_track())

        columns = ("scene", "file", "type", "motion", "effect", "transition", "match", "start", "duration", "end")
        timeline_wrap = ttk.Frame(parent, style="Panel.TFrame")
        timeline_wrap.pack(fill="both", expand=True)
        self.timeline_tree = ttk.Treeview(timeline_wrap, columns=columns, show="headings", selectmode="extended")
        timeline_y = ttk.Scrollbar(timeline_wrap, orient="vertical", command=self.timeline_tree.yview)
        timeline_x = ttk.Scrollbar(timeline_wrap, orient="horizontal", command=self.timeline_tree.xview)
        self.timeline_tree.configure(yscrollcommand=timeline_y.set, xscrollcommand=timeline_x.set)
        labels = {"scene": "SCENE", "file": "MEDIA", "type": "TYPE", "motion": "MOTION",
                  "effect": "EFFECT", "transition": "TRANSITION", "match": "AI MATCH",
                  "start": "START", "duration": "LENGTH", "end": "END"}
        widths = {"scene": 50, "file": 130, "type": 55, "motion": 72, "effect": 90,
                  "transition": 88, "match": 65, "start": 83, "duration": 68, "end": 83}
        for name in columns:
            self.timeline_tree.heading(name, text=labels[name])
            self.timeline_tree.column(name, width=widths[name], anchor="w" if name == "file" else "center")
        self.timeline_tree.configure(height=2)
        self.timeline_tree.grid(row=0, column=0, sticky="nsew")
        timeline_y.grid(row=0, column=1, sticky="ns")
        timeline_x.grid(row=1, column=0, sticky="ew")
        timeline_wrap.rowconfigure(0, weight=1)
        timeline_wrap.columnconfigure(0, weight=1)
        self.timeline_tree.bind("<Double-1>", self.edit_scene_duration)
        self.timeline_tree.bind("<<TreeviewSelect>>", self._on_timeline_selection)
        self.timeline_tree.bind("<ButtonPress-1>", lambda event: self._tree_drag_start(event, "timeline"), add="+")
        self.timeline_tree.bind("<B1-Motion>", self._tree_drag_motion, add="+")
        self.timeline_tree.bind("<ButtonRelease-1>", lambda event: self._tree_drag_end(event, "timeline"), add="+")

        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=False, pady=(12, 0))
        transcript_tab = ttk.Frame(notebook, style="Panel2.TFrame", padding=10)
        log_tab = ttk.Frame(notebook, style="Panel2.TFrame", padding=10)
        notebook.add(transcript_tab, text="Captions / Transcript")
        notebook.add(log_tab, text="Activity")

        self.transcript_info = ttk.Label(transcript_tab, text="No transcript generated",
                                         background=PANEL_2, foreground=MUTED)
        self.transcript_info.pack(anchor="w")
        self.transcript_text = tk.Text(transcript_tab, height=3, bg="#0f141d", fg=TEXT, insertbackground=TEXT,
                                       relief="flat", wrap="word", font=("Segoe UI", 10), padx=10, pady=8)
        transcript_scroll = ttk.Scrollbar(transcript_tab, orient="vertical", command=self.transcript_text.yview)
        self.transcript_text.configure(yscrollcommand=transcript_scroll.set)
        transcript_scroll.pack(side="right", fill="y", pady=(8, 0))
        self.transcript_text.pack(side="left", fill="both", expand=True, pady=(8, 0))

        self.log_text = tk.Text(log_tab, height=5, bg="#0f141d", fg="#b8c1d1", insertbackground=TEXT,
                                relief="flat", wrap="word", font=("Cascadia Mono", 9), padx=10, pady=8)
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_settings_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="3  EXPORT", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Resolution", style="PanelMuted.TLabel").pack(anchor="w", pady=(14, 4))
        ttk.Combobox(parent, textvariable=self.resolution, state="readonly",
                     values=("1280×720", "1920×1080", "1080×1920")).pack(fill="x")
        ttk.Label(parent, text="Frame rate", style="PanelMuted.TLabel").pack(anchor="w", pady=(12, 4))
        ttk.Combobox(parent, textvariable=self.fps, state="readonly", values=("24", "25", "30", "60")).pack(fill="x")
        ttk.Label(parent, text="Quality", style="PanelMuted.TLabel").pack(anchor="w", pady=(12, 4))
        ttk.Combobox(parent, textvariable=self.quality, state="readonly",
                     values=("YouTube Recommended", "High (CRF 17)", "Balanced (CRF 20)",
                             "Small file (CRF 24)")).pack(fill="x")

        ttk.Separator(parent).pack(fill="x", pady=17)
        ttk.Label(parent, text="CAPTIONS", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(parent, text="Burn transcript into video", variable=self.caption_enabled).pack(anchor="w", pady=(9, 5))
        ttk.Label(parent, text="Captions are optional. SRT can also be exported separately.",
                  style="PanelMuted.TLabel", wraplength=250).pack(anchor="w")

        ttk.Label(parent, text="Whisper model", style="PanelMuted.TLabel").pack(anchor="w", pady=(12, 4))
        ttk.Combobox(parent, textvariable=self.whisper_model, state="readonly",
                     values=("tiny", "base", "small", "medium", "large-v3")).pack(fill="x")
        ttk.Label(parent, text="Spoken language", style="PanelMuted.TLabel").pack(anchor="w", pady=(12, 4))
        ttk.Combobox(parent, textvariable=self.language, state="readonly",
                     values=("Auto detect", "Hindi", "English", "Spanish", "French", "German", "Urdu")).pack(fill="x")

        backend = transcript_backend()
        backend_text = f"Local transcript: {backend}" if backend else "Local transcript engine not installed"
        backend_color = GREEN if backend else "#f5b942"
        ttk.Label(parent, text=backend_text, background=PANEL, foreground=backend_color,
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))

        ttk.Separator(parent).pack(fill="x", pady=17)
        self.export_button = ttk.Button(parent, text="Export MP4", style="Accent.TButton", command=self.export_video)
        self.export_button.pack(fill="x")
        self.export_progress = ttk.Progressbar(parent, maximum=100, mode="determinate")
        self.export_progress.pack(fill="x", pady=(9, 3))
        ttk.Label(parent, textvariable=self.export_progress_text, style="PanelMuted.TLabel",
                  anchor="w").pack(fill="x")
        ttk.Button(parent, text="Preview last export", command=self.preview_export).pack(fill="x", pady=(8, 0))
        ttk.Button(parent, text="Export timeline CSV", command=self.export_timeline).pack(fill="x", pady=(8, 0))

        ttk.Label(parent, text="Everything is rendered locally with FFmpeg.",
                  style="PanelMuted.TLabel", wraplength=250).pack(anchor="w", pady=(15, 0))

    def _timeline_positions(self) -> tuple[list[float], list[float], float]:
        starts: list[float] = []
        ends: list[float] = []
        cursor = 0.0
        for item in self.items:
            starts.append(cursor)
            duration = max(0.001, item.assigned_duration or 0.001)
            cursor += duration
            ends.append(cursor)
        total = self.audio_duration or cursor
        return starts, ends, total

    def _on_preview_format_change(self, event=None) -> None:
        width_ratio, height_ratio, resolution = OUTPUT_FORMATS[self.preview_format.get()]
        self.resolution.set(resolution)
        self._last_preview_key = None
        self._render_preview_at(self.playhead_seconds, force=True)
        self.status_text.set(f"Canvas changed to {width_ratio}:{height_ratio}")

    def _on_resolution_change(self, event=None) -> None:
        try:
            width, height = (int(value) for value in self.resolution.get().split("×"))
        except (TypeError, ValueError):
            return
        ratio = width / max(1, height)
        closest = min(
            OUTPUT_FORMATS,
            key=lambda name: abs(ratio - OUTPUT_FORMATS[name][0] / OUTPUT_FORMATS[name][1]),
        )
        self.preview_format.set(closest)
        self._last_preview_key = None
        self._render_preview_at(self.playhead_seconds, force=True)

    def _preview_frame_rect(self, width: int, height: int) -> tuple[int, int, int, int]:
        ratio_width, ratio_height, _ = OUTPUT_FORMATS.get(
            self.preview_format.get(), OUTPUT_FORMATS["16:9 Horizontal"],
        )
        margin = 10
        available_width = max(32, width - margin * 2)
        available_height = max(32, height - margin * 2)
        scale = min(available_width / ratio_width, available_height / ratio_height)
        frame_width = max(16, round(ratio_width * scale))
        frame_height = max(16, round(ratio_height * scale))
        portrait = ratio_height > ratio_width
        frame_x = width - frame_width - margin if portrait else (width - frame_width) // 2
        frame_y = (height - frame_height) // 2
        return frame_x, frame_y, frame_width, frame_height

    def _on_caption_style_change(self, event=None) -> None:
        self._draw_caption_preview(self.playhead_seconds)

    def _reset_caption_position(self) -> None:
        self.caption_x = 0.5
        self.caption_y = 0.86
        self._draw_caption_preview(self.playhead_seconds)
        self.status_text.set("Caption position reset to bottom center")

    def _caption_segment_at(self, seconds: float):
        for segment in self.transcript:
            if segment.start <= seconds < segment.end:
                return segment
        return None

    def _draw_caption_preview(self, seconds: float) -> None:
        if not hasattr(self, "preview_canvas"):
            return
        canvas = self.preview_canvas
        canvas.delete("caption")
        if not self.caption_enabled.get() or not self.transcript:
            return
        segment = self._caption_segment_at(seconds)
        if segment is None:
            return
        words = caption_word_timings(segment)
        if not words:
            return
        active_index = next(
            (index for index, word in enumerate(words)
             if float(word["start"]) <= seconds < float(word["end"])),
            None,
        )
        canvas_width = max(64, canvas.winfo_width())
        canvas_height = max(64, canvas.winfo_height())
        frame_x, frame_y, frame_width, frame_height = self._preview_frame_rect(canvas_width, canvas_height)
        try:
            chosen_size = int(self.caption_size.get())
        except (tk.TclError, ValueError):
            chosen_size = 44
        try:
            output_height = max(1, int(self.resolution.get().split("×")[1]))
        except (IndexError, TypeError, ValueError):
            output_height = 1080
        preview_size = max(9, round(frame_height * chosen_size / output_height))
        font = tkfont.Font(family="Segoe UI Semibold", size=preview_size)
        space_width = font.measure(" ")
        max_line_width = max(40, frame_width * 0.86)
        lines: list[list[tuple[int, str, int, bool]]] = []
        current: list[tuple[int, str, int, bool]] = []
        current_width = 0
        for index, entry in enumerate(words):
            word = str(entry["word"])
            measured = font.measure(word)
            spaced = bool(entry.get("space_before", True))
            gap = space_width if current and spaced else 0
            proposed = measured if not current else current_width + gap + measured
            if current and proposed > max_line_width:
                lines.append(current)
                current = []
                current_width = 0
                gap = 0
            current.append((index, word, measured, spaced))
            current_width = measured if len(current) == 1 else current_width + gap + measured
        if current:
            lines.append(current)

        line_height = font.metrics("linespace") + 2
        total_height = max(line_height, len(lines) * line_height)
        center_x = frame_x + self.caption_x * frame_width
        center_y = frame_y + self.caption_y * frame_height
        top = center_y - total_height / 2
        line_widths = [
            sum(item[2] for item in line) +
            sum(space_width for position, item in enumerate(line) if position and item[3])
            for line in lines
        ]
        widest = max(line_widths, default=20)
        pad_x, pad_y = 7, 4
        canvas.create_rectangle(
            center_x - widest / 2 - pad_x, top - pad_y,
            center_x + widest / 2 + pad_x, top + total_height + pad_y,
            fill="#080b12", outline="#39445a", width=1, stipple="gray75",
            tags=("caption", "caption_drag"),
        )
        highlight = "#" + CAPTION_HIGHLIGHTS.get(self.caption_highlight.get(), "FFD400")
        for line_index, line in enumerate(lines):
            line_width = line_widths[line_index]
            cursor = center_x - line_width / 2
            y = top + line_index * line_height + line_height / 2
            for position, (word_index, word, measured, spaced) in enumerate(line):
                if position and spaced:
                    cursor += space_width
                canvas.create_text(
                    cursor + measured / 2, y, text=word, font=font,
                    fill=highlight if word_index == active_index else "#ffffff",
                    tags=("caption", "caption_drag"),
                )
                cursor += measured
        canvas.tag_raise("caption")

    def _caption_drag_start(self, event) -> None:
        overlapping = self.preview_canvas.find_overlapping(event.x - 2, event.y - 2, event.x + 2, event.y + 2)
        self._dragging_caption = any(
            "caption_drag" in self.preview_canvas.gettags(item_id) for item_id in overlapping
        )

    def _caption_drag_move(self, event) -> None:
        if not self._dragging_caption:
            return
        width = max(64, self.preview_canvas.winfo_width())
        height = max(64, self.preview_canvas.winfo_height())
        frame_x, frame_y, frame_width, frame_height = self._preview_frame_rect(width, height)
        self.caption_x = max(0.05, min(0.95, (event.x - frame_x) / max(1, frame_width)))
        self.caption_y = max(0.08, min(0.92, (event.y - frame_y) / max(1, frame_height)))
        self._draw_caption_preview(self.playhead_seconds)

    def _caption_drag_end(self, event=None) -> None:
        if self._dragging_caption:
            self.status_text.set(
                f"Caption position: {round(self.caption_x * 100)}% x, {round(self.caption_y * 100)}% y"
            )
        self._dragging_caption = False

    def _scene_at(self, seconds: float) -> tuple[int, float, float] | None:
        starts, ends, _ = self._timeline_positions()
        if not ends:
            return None
        index = min(len(ends) - 1, bisect.bisect_right(ends, max(0.0, seconds)))
        duration = max(0.001, ends[index] - starts[index])
        local = max(0.0, min(duration, seconds - starts[index]))
        return index, local, local / duration

    def _render_preview_at(self, seconds: float, force: bool = False) -> None:
        if not hasattr(self, "preview_canvas"):
            return
        canvas = self.preview_canvas
        width = max(64, canvas.winfo_width())
        height = max(64, canvas.winfo_height())
        frame_x, frame_y, frame_width, frame_height = self._preview_frame_rect(width, height)
        _, _, total = self._timeline_positions()
        seconds = max(0.0, min(total, seconds)) if total else 0.0
        self.playhead_seconds = seconds
        self.preview_time_text.set(f"{format_time(seconds)} / {format_time(total)}")
        self._updating_seek = True
        self.seek_value.set(seconds)
        self._updating_seek = False
        self._update_track_playhead()

        location = self._scene_at(seconds)
        canvas.delete("overlay")
        canvas.delete("caption")
        canvas.delete("stage")
        canvas.create_rectangle(
            frame_x - 1, frame_y - 1, frame_x + frame_width + 1, frame_y + frame_height + 1,
            fill="#000000", outline="#465166", width=1, tags="stage",
        )
        canvas.tag_lower("stage")
        if frame_x > 180:
            canvas.create_text(
                frame_x / 2, height / 2, text=self.preview_format.get().replace(" ", "\n", 1),
                fill="#536078", font=("Segoe UI Semibold", 11), justify="center", tags="overlay",
            )
        if location is None:
            canvas.delete("preview")
            canvas.create_text(frame_x + frame_width / 2, frame_y + frame_height / 2,
                               text="Add media to preview your edit",
                               fill=MUTED, font=("Segoe UI", 12), tags="overlay")
            self.preview_photo = None
            return
        index, local, progress = location
        item = self.items[index]
        if item.kind == "gap":
            self._last_preview_key = (index, "gap", frame_width, frame_height, item.placeholder_text)
            canvas.delete("preview")
            self.preview_photo = None
            canvas.create_text(
                frame_x + frame_width / 2, frame_y + frame_height / 2,
                text=f"NEEDS IMAGE\n\n{item.placeholder_text}", fill="#ff9db0",
                font=("Segoe UI Semibold", 12), justify="center",
                width=max(120, round(frame_width * 0.75)), tags="preview",
            )
            canvas.create_rectangle(
                frame_x, frame_y, frame_x + frame_width, frame_y + frame_height,
                outline="#ff6685", width=2, dash=(7, 4), tags="overlay",
            )
            self._draw_caption_preview(seconds)
            return
        if item.kind == "video" and item.source_duration:
            source_end = item.source_end or item.source_duration
            video_time = min(
                item.source_start + local * max(.25, min(4.0, item.playback_speed)),
                max(item.source_start, source_end - 0.05),
            )
        else:
            video_time = local
        bucket = round(video_time * 5) if item.kind == "video" else round(progress * 100)
        key = (index, bucket, frame_width, frame_height, item.motion, item.effect,
               round(item.effect_strength, 2), item.transition, round(item.transition_duration, 2),
               round(item.playback_speed, 2), self.preview_format.get())
        if not force and key == self._last_preview_key:
            canvas.create_rectangle(frame_x, frame_y, frame_x + frame_width, frame_y + frame_height,
                                    outline="#465166", width=1, tags="overlay")
            label_x, label_y = frame_x + 8, frame_y + 8
            canvas.create_rectangle(label_x, label_y, label_x + 118, label_y + 24,
                                    fill="#080b12", outline="", tags="overlay")
            canvas.create_text(label_x + 10, label_y + 12, anchor="w",
                               text=f"Scene {index + 1} / {len(self.items)}",
                               fill=TEXT, font=("Segoe UI Semibold", 9), tags="overlay")
            self._draw_caption_preview(seconds)
            return
        self._last_preview_key = key
        try:
            frame = self.preview_renderer.render(
                item.path, item.kind, item.motion, progress, (frame_width, frame_height), video_time,
                item.effect, item.effect_strength, item.transition,
                item.transition_duration / max(item.assigned_duration, .01),
            )
        except Exception as exc:
            frame = None
            self.status_text.set(f"Preview unavailable: {exc}")
        canvas.delete("preview")
        if frame is not None:
            self.preview_photo = ImageTk.PhotoImage(frame)
            canvas.create_image(frame_x + frame_width / 2, frame_y + frame_height / 2,
                                image=self.preview_photo, tags="preview")
            canvas.tag_raise("preview", "stage")
        else:
            canvas.create_text(frame_x + frame_width / 2, frame_y + frame_height / 2,
                               text="Preview unavailable",
                               fill=MUTED, font=("Segoe UI", 12), tags="preview")
        canvas.create_rectangle(frame_x, frame_y, frame_x + frame_width, frame_y + frame_height,
                                outline="#465166", width=1, tags="overlay")
        label_x = frame_x + 8
        label_y = frame_y + 8
        canvas.create_rectangle(label_x, label_y, label_x + 118, label_y + 24,
                                fill="#080b12", outline="", tags="overlay")
        canvas.create_text(label_x + 10, label_y + 12, anchor="w",
                           text=f"Scene {index + 1} / {len(self.items)}",
                           fill=TEXT, font=("Segoe UI Semibold", 9), tags="overlay")
        self._draw_caption_preview(seconds)

    def _start_audio(self) -> bool:
        if not self.audio_path:
            return False
        try:
            self.audio_player.open(self.audio_path)
            self.audio_player.set_volume(self.volume.get())
            self.audio_player.play(round(self.playhead_seconds * 1000))
            if self.audio_player.using_converted_audio:
                self.status_text.set("Audio playing in compatibility mode")
                self._log("Audio preview: using a local WAV compatibility copy")
            self.audio_preview_error_shown = False
            return True
        except Exception as exc:
            self.audio_player.close()
            self.status_text.set("Audio preview failed · check Activity for details")
            self._log(f"Audio preview failed: {exc}")
            if not self.audio_preview_error_shown:
                self.audio_preview_error_shown = True
                messagebox.showwarning(
                    APP_NAME,
                    "The video preview will continue, but narration sound could not be played.\n\n"
                    f"{exc}\n\nTry choosing the narration file again.",
                )
            return False

    def toggle_playback(self) -> None:
        if not self.items:
            messagebox.showinfo(APP_NAME, "Add media before starting preview.")
            return
        _, _, total = self._timeline_positions()
        if self.preview_playing:
            self.preview_playing = False
            self._audio_clock_active = False
            try:
                self.audio_player.pause()
            except Exception:
                pass
            self.play_button.configure(text="Play")
            return
        if total and self.playhead_seconds >= total - 0.001:
            self.playhead_seconds = 0.0
        self._audio_clock_active = self._start_audio()
        # Start the fallback clock only after opening/converting the audio. Otherwise
        # that preparation delay makes captions jump ahead before sound begins.
        self._play_wall_start = time.monotonic() - self.playhead_seconds
        self.preview_playing = True
        self.play_button.configure(text="Pause")
        self._preview_tick()

    def _preview_tick(self) -> None:
        if not self.preview_playing:
            return
        _, _, total = self._timeline_positions()
        position = max(0.0, time.monotonic() - self._play_wall_start)
        if self._audio_clock_active and self.audio_player.opened:
            audio_position = self.audio_player.position() / 1000.0
            # MCI can briefly report zero immediately after a seek. Do not jump a
            # non-zero playhead back to the beginning during that startup window.
            if audio_position > 0.0 or self.playhead_seconds < 0.25:
                position = audio_position
                self._play_wall_start = time.monotonic() - position
        if total and position >= total:
            self.playhead_seconds = total
            self._render_preview_at(total, force=True)
            self.preview_playing = False
            self._audio_clock_active = False
            self.play_button.configure(text="Play")
            try:
                self.audio_player.stop()
            except Exception:
                pass
            return
        self._render_preview_at(position)
        self.preview_after_id = self.after(40, self._preview_tick)

    def stop_preview(self) -> None:
        self.preview_playing = False
        self._audio_clock_active = False
        if self.preview_after_id:
            try:
                self.after_cancel(self.preview_after_id)
            except Exception:
                pass
            self.preview_after_id = None
        try:
            self.audio_player.stop()
        except Exception:
            pass
        if hasattr(self, "play_button"):
            self.play_button.configure(text="Play")
        self._last_preview_key = None
        self._render_preview_at(0.0, force=True)

    def seek_preview(self, seconds: float) -> None:
        _, _, total = self._timeline_positions()
        self.playhead_seconds = max(0.0, min(total, seconds)) if total else 0.0
        was_playing = self.preview_playing
        if was_playing:
            try:
                self.audio_player.stop()
            except Exception:
                pass
            self._audio_clock_active = self._start_audio()
            self._play_wall_start = time.monotonic() - self.playhead_seconds
        self._last_preview_key = None
        self._render_preview_at(self.playhead_seconds, force=True)

    def step_frame(self, direction: int) -> None:
        self.seek_preview(self.playhead_seconds + direction / max(1, int(self.fps.get())))

    def go_scene(self, direction: int) -> None:
        starts, _, _ = self._timeline_positions()
        if not starts:
            return
        current = bisect.bisect_right(starts, self.playhead_seconds + 0.001) - 1
        target = max(0, min(len(starts) - 1, current + direction))
        self.seek_preview(starts[target])

    def _on_seek_slider(self, value: str) -> None:
        if not self._updating_seek:
            self.seek_preview(float(value))

    def _on_volume(self, value: str) -> None:
        try:
            self.audio_player.set_volume(float(value))
        except Exception:
            pass

    def _shortcut_play(self, event) -> str | None:
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return None
        self.toggle_playback()
        return "break"

    def _shortcut_step(self, event, direction: int) -> str | None:
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return None
        self.step_frame(direction)
        return "break"

    def _snapshot(self) -> dict:
        return {
            "items": copy.deepcopy(self.items),
            "audio_path": self.audio_path,
            "audio_duration": self.audio_duration,
            "transcript": copy.deepcopy(self.transcript),
            "transcript_language": self.transcript_language,
        }

    def _push_undo(self) -> None:
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-50]
        self.redo_stack.clear()

    def _restore_snapshot(self, state: dict) -> None:
        self.preview_playing = False
        self.audio_player.close()
        self.items = copy.deepcopy(state["items"])
        self.audio_path = state["audio_path"]
        self.audio_duration = state["audio_duration"]
        self.transcript = copy.deepcopy(state["transcript"])
        self.transcript_language = state["transcript_language"]
        self.audio_text.set(
            f"{Path(self.audio_path).name}  ·  {format_time(self.audio_duration)}"
            if self.audio_path else "No narration selected"
        )
        self._refresh_media()
        self._refresh_timeline()
        self._refresh_transcript()
        self.seek_preview(0.0)

    def undo_edit(self) -> str:
        if not self.undo_stack:
            self.status_text.set("Nothing to undo")
            return "break"
        self.redo_stack.append(self._snapshot())
        self._restore_snapshot(self.undo_stack.pop())
        self.status_text.set("Undo complete")
        return "break"

    def redo_edit(self) -> str:
        if not self.redo_stack:
            self.status_text.set("Nothing to redo")
            return "break"
        self.undo_stack.append(self._snapshot())
        self._restore_snapshot(self.redo_stack.pop())
        self.status_text.set("Redo complete")
        return "break"

    def _select_all_context(self, event) -> str | None:
        widget = event.widget
        if widget is self.media_tree:
            self.media_tree.selection_set(self.media_tree.get_children())
            return "break"
        if widget is self.timeline_tree or widget is self.track_canvas:
            self.timeline_tree.selection_set(self.timeline_tree.get_children())
            return "break"
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            return "break"
        return None

    def _delete_context(self, event) -> str | None:
        widget = event.widget
        if isinstance(widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, ttk.Spinbox)):
            return None
        if widget is self.media_tree:
            self.remove_items()
            return "break"
        if widget is self.timeline_tree or widget is self.track_canvas:
            self.delete_timeline_selection()
            return "break"
        return None

    def _tree_drag_start(self, event, source: str) -> None:
        tree = self.media_tree if source == "media" else self.timeline_tree
        row_id = tree.identify_row(event.y)
        if row_id:
            self._tree_drag_source = (source, tree.index(row_id), event.y)

    def _tree_drag_motion(self, event) -> None:
        if self._tree_drag_source and abs(event.y - self._tree_drag_source[2]) > 4:
            event.widget.configure(cursor="fleur")

    def _tree_drag_end(self, event, source: str) -> None:
        tree = self.media_tree if source == "media" else self.timeline_tree
        tree.configure(cursor="")
        drag = self._tree_drag_source
        self._tree_drag_source = None
        if not drag or drag[0] != source or abs(event.y - drag[2]) <= 4:
            return
        target_id = tree.identify_row(event.y)
        if not target_id:
            target_index = len(self.items)
        else:
            target_index = tree.index(target_id)
        selected = sorted(tree.index(item_id) for item_id in tree.selection())
        if not selected:
            selected = [drag[1]]
        self._reorder_indices(selected, target_index)

    def _reorder_indices(self, selected: list[int], target_index: int) -> None:
        selected = sorted(set(index for index in selected if 0 <= index < len(self.items)))
        if not selected:
            return
        moving = [self.items[index] for index in selected]
        remaining = [item for index, item in enumerate(self.items) if index not in selected]
        adjusted = target_index - sum(index < target_index for index in selected)
        adjusted = max(0, min(len(remaining), adjusted))
        reordered = remaining[:adjusted] + moving + remaining[adjusted:]
        if reordered == self.items:
            return
        self._push_undo()
        self.items = reordered
        self._refresh_media()
        self._refresh_timeline()
        self.status_text.set(f"Moved {len(moving)} scene(s)")

    def _on_track_press(self, event) -> None:
        self.track_canvas.focus_set()
        x = self.track_canvas.canvasx(event.x)
        seconds = x / max(1.0, self.timeline_zoom.get())
        location = self._scene_at(seconds)
        self._track_drag_index = location[0] if location else None
        if location:
            children = self.timeline_tree.get_children()
            if location[0] < len(children):
                self.timeline_tree.selection_set(children[location[0]])
        self._track_drag_x = x
        self.seek_preview(seconds)

    def _on_track_drag(self, event) -> None:
        if self._track_drag_index is not None and abs(self.track_canvas.canvasx(event.x) - self._track_drag_x) > 5:
            self.track_canvas.configure(cursor="fleur")

    def _on_track_release(self, event) -> None:
        self.track_canvas.configure(cursor="")
        source = self._track_drag_index
        self._track_drag_index = None
        x = self.track_canvas.canvasx(event.x)
        if source is None or abs(x - self._track_drag_x) <= 5:
            return
        location = self._scene_at(x / max(1.0, self.timeline_zoom.get()))
        target = location[0] if location else len(self.items)
        self._reorder_indices([source], target)

    def _selected_timeline_indices(self) -> list[int]:
        selected = sorted(self.timeline_tree.index(item_id) for item_id in self.timeline_tree.selection())
        if selected:
            return selected
        location = self._scene_at(self.playhead_seconds)
        return [location[0]] if location else []

    def delete_timeline_selection(self) -> None:
        indices = self._selected_timeline_indices()
        if not indices:
            return
        self._push_undo()
        for index in reversed(indices):
            self.items.pop(index)
        self._refresh_media()
        if self.items and self.audio_path:
            self.calculate_timeline()
        else:
            self._refresh_timeline()
        self.status_text.set(f"Deleted {len(indices)} scene(s) · Ctrl+Z to undo")

    def split_at_playhead(self) -> None:
        location = self._scene_at(self.playhead_seconds)
        if location is None:
            return
        index, local, _ = location
        item = self.items[index]
        if local < 0.12 or item.assigned_duration - local < 0.12:
            messagebox.showinfo(APP_NAME, "Move the playhead inside a clip, away from its edges, before splitting.")
            return
        self._push_undo()
        second = copy.deepcopy(item)
        original_duration = item.assigned_duration
        item.assigned_duration = local
        second.assigned_duration = original_duration - local
        if item.kind == "video":
            source_end = item.source_end or item.source_duration
            split_source = min(source_end, item.source_start + local)
            item.source_end = split_source
            second.source_start = split_source
            second.source_end = source_end
        self.items.insert(index + 1, second)
        self._refresh_media()
        self._refresh_timeline()
        self.status_text.set(f"Scene {index + 1} split at {local:.2f}s")

    def _transfer_trimmed_duration(self, index: int, duration: float, prefer_next: bool) -> None:
        if duration <= 0 or len(self.items) < 2:
            return
        if prefer_next and index + 1 < len(self.items):
            target = index + 1
        elif not prefer_next and index > 0:
            target = index - 1
        else:
            target = index - 1 if index > 0 else index + 1
        self.items[target].assigned_duration += duration

    def trim_clip_start(self) -> None:
        location = self._scene_at(self.playhead_seconds)
        if location is None:
            return
        index, local, _ = location
        item = self.items[index]
        if local < 0.12 or item.assigned_duration - local < 0.12:
            messagebox.showinfo(APP_NAME, "Place the playhead inside the selected clip to trim its start.")
            return
        self._push_undo()
        item.assigned_duration -= local
        if item.kind == "video":
            source_end = item.source_end or item.source_duration
            item.source_start = min(source_end - 0.01, item.source_start + local)
        self._transfer_trimmed_duration(index, local, prefer_next=False)
        self._refresh_timeline()
        self.status_text.set(f"Trimmed {local:.2f}s from scene {index + 1} start")

    def trim_clip_end(self) -> None:
        location = self._scene_at(self.playhead_seconds)
        if location is None:
            return
        index, local, _ = location
        item = self.items[index]
        removed = item.assigned_duration - local
        if local < 0.12 or removed < 0.12:
            messagebox.showinfo(APP_NAME, "Place the playhead inside the selected clip to trim its end.")
            return
        self._push_undo()
        item.assigned_duration = local
        if item.kind == "video":
            source_end = item.source_end or item.source_duration
            item.source_end = min(source_end, item.source_start + local)
        self._transfer_trimmed_duration(index, removed, prefer_next=True)
        self._refresh_timeline()
        self.status_text.set(f"Trimmed {removed:.2f}s from scene {index + 1} end")

    def set_mark_in(self) -> None:
        self.mark_in = self.playhead_seconds
        self._refresh_range_text()

    def set_mark_out(self) -> None:
        self.mark_out = self.playhead_seconds
        self._refresh_range_text()

    def _refresh_range_text(self) -> None:
        start = format_time(self.mark_in) if self.mark_in is not None else "--"
        end = format_time(self.mark_out) if self.mark_out is not None else "--"
        self.range_text.set(f"In: {start}  Out: {end}")
        self._redraw_track()

    def cut_marked_audio(self) -> None:
        if not self.audio_path:
            messagebox.showwarning(APP_NAME, "Choose narration audio first.")
            return
        if self.mark_in is None or self.mark_out is None:
            messagebox.showinfo(APP_NAME, "Set Mark In and Mark Out around the unwanted audio first.")
            return
        start, end = sorted((self.mark_in, self.mark_out))
        if end - start < 0.05 or end > self.audio_duration:
            messagebox.showwarning(APP_NAME, "The marked audio range is not valid.")
            return
        self.stop_preview()
        self._push_undo()
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
        source = Path(self.audio_path)
        output = source.with_name(f"{source.stem}_storycut_{stamp}.wav")
        self._thread(self._cut_audio_operation, start, end, str(output))

    def _cut_audio_operation(self, start: float, end: float, output_path: str) -> tuple[str, float]:
        self._progress_callback(0.1, "Cutting narration range…")
        if start <= 0.001:
            command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{end:.6f}",
                       "-i", self.audio_path, "-vn", "-c:a", "pcm_s16le", output_path]
        elif end >= self.audio_duration - 0.001:
            command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", self.audio_path,
                       "-t", f"{start:.6f}", "-vn", "-c:a", "pcm_s16le", output_path]
        else:
            graph = (
                f"[0:a]atrim=0:{start:.6f},asetpts=PTS-STARTPTS[a0];"
                f"[0:a]atrim={end:.6f},asetpts=PTS-STARTPTS[a1];"
                "[a0][a1]concat=n=2:v=0:a=1[aout]"
            )
            command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", self.audio_path,
                       "-filter_complex", graph, "-map", "[aout]", "-c:a", "pcm_s16le", output_path]
        proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise EditorError(proc.stderr.strip() or "Audio cut failed")
        duration = duration_of(output_path)
        self._progress_callback(1.0, "Narration range removed")
        return str(Path(output_path).resolve()), duration

    def remove_audio(self) -> None:
        if not self.audio_path:
            return
        self.stop_preview()
        self._push_undo()
        self.audio_path = ""
        self.audio_duration = 0.0
        self.transcript = []
        self.transcript_language = ""
        self.audio_text.set("No narration selected")
        self._refresh_timeline()
        self._refresh_transcript()
        self.status_text.set("Narration removed · original file was not deleted")

    def _on_timeline_zoom(self, value: str) -> None:
        self._redraw_track()

    def _on_track_hover(self, event) -> None:
        if event.y > 31 or not self.items:
            self.track_hover_text.set("Hover a clip to see its full filename")
            return
        seconds = self.track_canvas.canvasx(event.x) / max(1.0, self.timeline_zoom.get())
        location = self._scene_at(seconds)
        if not location:
            return
        index = location[0]
        if 0 <= index < len(self.items):
            item = self.items[index]
            self.track_hover_text.set(f"Scene {index + 1}: {item.name}")

    def _on_track_leave(self, event=None) -> None:
        self.track_hover_text.set("Hover a clip to see its full filename")

    def _redraw_track(self) -> None:
        if not hasattr(self, "track_canvas"):
            return
        canvas = self.track_canvas
        canvas.delete("all")
        starts, ends, total = self._timeline_positions()
        pps = max(1.0, self.timeline_zoom.get())
        width = max(canvas.winfo_width(), round(total * pps) + 40)
        colors = ("#4d3f91", "#315f86", "#6a467d", "#326971", "#765237")
        for index, (item, start, end) in enumerate(zip(self.items, starts, ends)):
            x1, x2 = start * pps, max(start * pps + 3, end * pps)
            fill = "#5a202e" if item.kind == "gap" else colors[index % len(colors)]
            outline = "#ff6685" if item.kind == "gap" else "#8e9ab0"
            canvas.create_rectangle(x1, 4, x2, 30, fill=fill, outline=outline)
            if x2 - x1 > 24:
                # On narrow clips show the real file stem, not only the scene
                # position (which made e.g. scene 20 / file 19.png look missing).
                if item.kind == "gap":
                    label = "NEEDS IMAGE"
                else:
                    label = f"{index + 1}  {item.name}" if x2 - x1 > 105 else Path(item.path).stem
                canvas.create_text(x1 + 5, 17, text=label, anchor="w", fill="#ffffff",
                                   font=("Segoe UI", 8), width=max(20, x2 - x1 - 8))
        if total:
            canvas.create_rectangle(0, 34, total * pps, 54, fill="#1f6655", outline="#42b899")
            canvas.create_text(7, 44, text="Narration", anchor="w", fill="#e8fff8", font=("Segoe UI", 8))
        if self.mark_in is not None and self.mark_out is not None:
            start, end = sorted((self.mark_in, self.mark_out))
            canvas.create_rectangle(start * pps, 34, end * pps, 54,
                                    fill="#8b263d", outline="#ff708c", stipple="gray50")
        if self.mark_in is not None:
            mark_x = self.mark_in * pps
            canvas.create_line(mark_x, 31, mark_x, 57, fill="#4de1ff", width=2)
            canvas.create_text(mark_x + 3, 32, text="IN", anchor="sw", fill="#4de1ff", font=("Segoe UI", 7))
        if self.mark_out is not None:
            mark_x = self.mark_out * pps
            canvas.create_line(mark_x, 31, mark_x, 57, fill="#ff708c", width=2)
            canvas.create_text(mark_x - 3, 32, text="OUT", anchor="se", fill="#ff708c", font=("Segoe UI", 7))
        canvas.configure(scrollregion=(0, 0, width, 58))
        canvas.create_line(0, 2, 0, 56, fill="#ffcc4d", width=2, tags="playhead")
        self._update_track_playhead()

    def _update_track_playhead(self) -> None:
        if not hasattr(self, "track_canvas"):
            return
        x = self.playhead_seconds * max(1.0, self.timeline_zoom.get())
        self.track_canvas.coords("playhead", x, 2, x, 56)
        self.track_canvas.tag_raise("playhead")

    def _thread(self, operation, *args) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish.")
            return
        self.busy = True
        self.current_operation = operation.__name__
        self.export_button.configure(state="disabled")
        self.progress["value"] = 0

        def runner() -> None:
            try:
                result = operation(*args)
                self.events.put(("done", operation.__name__, result))
            except Exception as exc:
                self.events.put(("error", operation.__name__, exc, traceback.format_exc()))

        threading.Thread(target=runner, daemon=True).start()

    def _progress_callback(self, value: float, message: str) -> None:
        self.events.put(("progress", value, message))

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self.progress["value"] = float(event[1]) * 100
                    self.status_text.set(str(event[2]))
                    if self.current_operation in {"_transcribe_operation", "_align_script_operation"}:
                        self._update_transcript_progress(float(event[1]), str(event[2]))
                    elif self.current_operation == "_render_operation":
                        self._update_export_progress(float(event[1]), str(event[2]))
                    elif self.current_operation == "_timeline_operation" and self.sync_mode.get() == "ai":
                        percent = round(float(event[1]) * 100)
                        self.ai_progress["value"] = percent
                        self.ai_progress_text.set(f"{percent}% · {event[2]}")
                elif kind == "done":
                    self.busy = False
                    self.export_button.configure(state="normal")
                    self._operation_done(event[1], event[2])
                    self.current_operation = ""
                elif kind == "error":
                    self.busy = False
                    self.export_button.configure(state="normal")
                    if event[1] in {"_transcribe_operation", "_align_script_operation"}:
                        self.transcript_progress.stop()
                        self.transcript_progress.configure(mode="determinate", value=0)
                        self.transcript_progress_text.set(
                            "Script alignment failed" if event[1] == "_align_script_operation"
                            else "Transcription failed"
                        )
                        self.generate_button.configure(state="normal", text="Generate")
                        self.align_script_button.configure(state="normal", text="Align with Audio")
                    elif event[1] == "_render_operation":
                        self.export_progress.configure(value=0)
                        self.export_progress_text.set("Export failed")
                        self.export_button.configure(text="Export MP4")
                    elif event[1] == "_cut_audio_operation" and self.undo_stack:
                        self.undo_stack.pop()
                    elif event[1] == "_timeline_operation" and self.sync_mode.get() == "ai":
                        self.ai_progress.configure(value=0)
                        self.ai_progress_text.set("AI Sync failed")
                    self.current_operation = ""
                    self.status_text.set("Task failed")
                    self._log(event[3])
                    messagebox.showerror(APP_NAME, str(event[2]))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _operation_done(self, operation: str, result) -> None:
        if operation == "_discover_operation":
            existing = {item.path.casefold() for item in self.items}
            additions = [item for item in result if item.path.casefold() not in existing]
            if additions:
                self._push_undo()
            self.items.extend(additions)
            timestamp_detected = has_filename_timing(self.items)
            if timestamp_detected:
                self.sync_mode.set("filename")
            self.sort_items(remember=False)
            duplicate_details = "" if timestamp_detected else self._duplicate_number_details()
            if duplicate_details:
                self.status_text.set(
                    f"Added {len(additions)} media files · duplicate number(s): {duplicate_details}"
                )
            elif timestamp_detected:
                self.status_text.set(
                    f"Added {len(additions)} media files · filename timestamps detected automatically"
                )
            else:
                self.status_text.set(f"Added {len(additions)} media files · sorted 1-N")
            self._log(f"Imported {len(result)} media files")
        elif operation == "_audio_operation":
            self._push_undo()
            self.audio_path, self.audio_duration = result
            self.audio_preview_error_shown = False
            self.transcript = []
            self.transcript_language = ""
            self.loaded_script_text = ""
            self._refresh_transcript()
            self.audio_text.set(f"{Path(self.audio_path).name}  ·  {format_time(self.audio_duration)}")
            self.status_text.set("Narration ready")
            self.calculate_timeline()
        elif operation == "_cut_audio_operation":
            self.audio_player.close()
            self.audio_path, self.audio_duration = result
            self.audio_preview_error_shown = False
            self.audio_text.set(f"{Path(self.audio_path).name}  ·  {format_time(self.audio_duration)}")
            self.transcript = []
            self.transcript_language = ""
            self.loaded_script_text = ""
            self.mark_in = None
            self.mark_out = None
            self._refresh_range_text()
            self._refresh_transcript()
            self.status_text.set("Audio range removed · regenerate transcript for updated word timing")
            self.calculate_timeline()
        elif operation == "_timeline_operation":
            self._refresh_media()
            self._refresh_timeline()
            if isinstance(result, list):
                self.ai_sync_confidence = result
                real_matches = [
                    item.ai_confidence for item in self.items
                    if item.kind != "gap" and item.ai_confidence >= 0
                ]
                average = sum(real_matches) / max(1, len(real_matches))
                gap_count = sum(item.kind == "gap" for item in self.items)
                self.ai_progress.configure(value=100)
                self.ai_progress_text.set(f"100% · {gap_count} gaps · match {average:.0%}")
                self.status_text.set(f"AI Sync complete · {gap_count} NEEDS IMAGE gap(s) · match {average:.0%}")
                self._log(f"AI Content Sync · {len(result)} scenes · {gap_count} gaps · average similarity {average:.1%}")
            elif isinstance(result, dict) and "filename_skipped" in result:
                self.ai_sync_confidence = []
                skipped = int(result.get("filename_skipped", 0))
                if skipped:
                    self.status_text.set(
                        f"Filename timeline adjusted · skipped {skipped} invalid-name file(s)"
                    )
                    self._log(
                        f"Filename timing skipped {skipped} file(s); original files were not deleted"
                    )
                else:
                    self.status_text.set("Timeline synchronized from filename timestamps")
            else:
                self.ai_sync_confidence = []
                if self.sync_mode.get() == "filename":
                    self.status_text.set("Timeline synchronized from filename timestamps")
                else:
                    self.status_text.set("Timeline synchronized to narration")
        elif operation == "_transcribe_operation":
            self.transcript_language, self.transcript = result
            self.loaded_script_text = ""
            self._refresh_transcript()
            self.progress["value"] = 100
            self.transcript_progress.stop()
            self.transcript_progress.configure(mode="determinate", value=100)
            self.transcript_progress_text.set(f"100% · {len(self.transcript)} captions")
            self.generate_button.configure(state="normal", text="Generate again")
            self.sync_mode.set("speech")
            self.status_text.set("Transcript ready · syncing images to speech")
            self.calculate_timeline()
        elif operation == "_align_script_operation":
            self.transcript_language, self.transcript, self.script_alignment_confidence = result
            self.loaded_script_text = ""
            self._refresh_transcript()
            self.progress["value"] = 100
            self.transcript_progress.stop()
            self.transcript_progress.configure(mode="determinate", value=100)
            self.transcript_progress_text.set(
                f"100% · Script aligned · timing match {self.script_alignment_confidence:.0%}"
            )
            self.generate_button.configure(state="normal", text="Generate again")
            self.align_script_button.configure(state="normal", text="Align with Audio")
            self.sync_mode.set("speech")
            self.status_text.set(
                f"Exact script captions ready · timing match {self.script_alignment_confidence:.0%}"
            )
            self.calculate_timeline()
        elif operation == "_render_operation":
            self.progress["value"] = 100
            self.export_progress["value"] = 100
            self.export_progress_text.set("100% · Export complete")
            self.export_button.configure(text="Export again")
            self.status_text.set("Export complete")
            self._log(f"Exported: {self.last_export}")
            messagebox.showinfo(APP_NAME, f"Video exported successfully:\n{self.last_export}")

    def _log(self, message: str) -> None:
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")

    def add_files(self) -> None:
        extensions = " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS))
        paths = filedialog.askopenfilenames(title="Choose numbered images or videos",
                                            filetypes=[("Images and videos", extensions), ("All files", "*.*")])
        if paths:
            self._thread(self._discover_operation, list(paths))

    def fill_selected_gap(self) -> None:
        indices = self._selected_timeline_indices()
        if not indices or self.items[indices[0]].kind != "gap":
            messagebox.showinfo(APP_NAME, "Select a NEEDS IMAGE slot in the timeline first.")
            return
        index = indices[0]
        extensions = " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS))
        path = filedialog.askopenfilename(
            title="Choose an image or video for this narration slot",
            filetypes=[("Images and videos", extensions), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            discovered = discover_media([path])
            if not discovered:
                raise EditorError("Choose a supported image or video file.")
            replacement = discovered[0]
            gap = self.items[index]
            replacement.assigned_duration = gap.assigned_duration
            replacement.ai_confidence = -1.0
            self._push_undo()
            self.items[index] = replacement
            self._refresh_media()
            self._refresh_timeline()
            children = self.timeline_tree.get_children()
            if index < len(children):
                self.timeline_tree.selection_set(children[index])
            self.status_text.set(
                f"Filled NEEDS IMAGE slot {index + 1} · duration kept at {replacement.assigned_duration:.2f}s"
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not fill this slot:\n{exc}")

    def add_folder(self) -> None:
        path = filedialog.askdirectory(title="Choose numbered media folder")
        if path:
            self._thread(self._discover_operation, [path])

    def _discover_operation(self, paths: list[str]) -> list[MediaItem]:
        self._progress_callback(0.15, "Reading media files…")
        result = discover_media(paths)
        self._progress_callback(1.0, "Media imported")
        return result

    def choose_audio(self) -> None:
        extensions = " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
        path = filedialog.askopenfilename(title="Choose narration audio",
                                          filetypes=[("Audio", extensions), ("All files", "*.*")])
        if path:
            self._thread(self._audio_operation, path)

    def _audio_operation(self, path: str) -> tuple[str, float]:
        self._progress_callback(0.2, "Reading narration…")
        value = duration_of(path)
        self._progress_callback(1.0, "Narration loaded")
        return str(Path(path).resolve()), value

    def sort_items(self, remember: bool = True) -> None:
        before = [item.path for item in self.items]
        if self.sync_mode.get() == "filename" and has_filename_timing(self.items):
            gaps = [item for item in self.items if item.kind == "gap"]
            real_items = [item for item in self.items if item.kind != "gap"]
            sorted_items = gaps + sorted(
                real_items, key=lambda item: (filename_timestamp(item.path) or 0.0, numeric_sort_key(item.path))
            )
        else:
            sorted_items = sorted(self.items, key=lambda item: numeric_sort_key(item.path))
        if remember and before != [item.path for item in sorted_items]:
            self._push_undo()
        self.items = sorted_items
        self._refresh_media()
        if self.audio_path:
            self.calculate_timeline()

        duplicate_details = "" if self.sync_mode.get() == "filename" else self._duplicate_number_details()
        if duplicate_details:
            self.status_text.set(f"Sorted 1-N · duplicate number(s): {duplicate_details}")
        elif self.sync_mode.get() == "filename" and has_filename_timing(self.items):
            self.status_text.set("Sorted by filename timestamps")
        else:
            self.status_text.set("Sorted media in numeric order (file extension ignored)")

    def _duplicate_number_details(self) -> str:
        numbers: dict[int, list[str]] = {}
        for item in self.items:
            match = re.search(r"\d+", Path(item.path).stem)
            if match:
                numbers.setdefault(int(match.group()), []).append(item.name)
        duplicates = {number: names for number, names in numbers.items() if len(names) > 1}
        if duplicates:
            return ", ".join(
                f"{number}: {' / '.join(names)}" for number, names in list(duplicates.items())[:3]
            )
        return ""

    def move_items(self, direction: int) -> None:
        selected = list(self.media_tree.selection())
        if not selected:
            return
        indices = sorted((self.media_tree.index(iid) for iid in selected), reverse=direction > 0)
        if not any(0 <= index + direction < len(self.items) for index in indices):
            return
        self._push_undo()
        for index in indices:
            target = index + direction
            if 0 <= target < len(self.items):
                self.items[index], self.items[target] = self.items[target], self.items[index]
        self._refresh_media()
        if self.audio_path:
            self.calculate_timeline()

    def remove_items(self) -> None:
        selected_indices = sorted((self.media_tree.index(iid) for iid in self.media_tree.selection()), reverse=True)
        if not selected_indices:
            return
        self._push_undo()
        for index in selected_indices:
            self.items.pop(index)
        self._refresh_media()
        if self.audio_path and self.items:
            self.calculate_timeline()
        else:
            self._refresh_timeline()

    def _refresh_media(self) -> None:
        self.ai_sync_confidence = []
        if hasattr(self, "ai_progress"):
            self.ai_progress.configure(value=0)
            self.ai_progress_text.set("AI Sync ready")
        self.media_tree.delete(*self.media_tree.get_children())
        for index, item in enumerate(self.items, 1):
            self.media_tree.insert("", "end", values=(index, item.name, item.kind.upper()))
        self.summary_text.set(f"{len(self.items)} scenes  ·  {format_time(self.audio_duration)}")

    def calculate_timeline(self) -> None:
        if not self.items or not self.audio_path or self.busy:
            self._refresh_timeline()
            return
        if self.sync_mode.get() == "ai" and not self.transcript:
            self.sync_mode.set("speech")
            messagebox.showinfo(APP_NAME, "Generate the transcript first, then select AI Content again.")
            return
        mode = self.sync_mode.get()
        skip_invalid_filenames = False
        if mode == "filename":
            real_items = [item for item in self.items if item.kind != "gap"]
            invalid_items = [item for item in real_items if filename_timestamp(item.path) is None]
            if invalid_items:
                valid_count = len(real_items) - len(invalid_items)
                if valid_count == 0:
                    messagebox.showerror(
                        APP_NAME,
                        "None of the media filenames contains a usable timestamp.\n\n"
                        "Use names such as 0-00, 0-03, 14-24, or 1-02-15.",
                    )
                    return
                examples = "\n".join(f"• {item.name}" for item in invalid_items[:5])
                extra = f"\n…and {len(invalid_items) - 5} more" if len(invalid_items) > 5 else ""
                skip_invalid_filenames = messagebox.askyesno(
                    "Skip files without timestamps?",
                    f"{len(invalid_items)} file(s) cannot be placed by filename timing:\n\n"
                    f"{examples}{extra}\n\n"
                    f"Skip them and adjust the remaining {valid_count} timestamped file(s)?\n\n"
                    "The skipped files will leave this project timeline, but the original files "
                    "on your computer will not be deleted.",
                )
                if not skip_invalid_filenames:
                    self.status_text.set("Filename timing cancelled · no files were changed")
                    return
                self._push_undo()
        if mode == "ai":
            self._push_undo()
        try:
            ai_max = float(self.ai_max_duration.get())
            ai_min = float(self.ai_min_confidence.get()) / 100.0
        except (tk.TclError, ValueError):
            ai_max, ai_min = 5.0, 0.30
            self.ai_max_duration.set(ai_max)
            self.ai_min_confidence.set(round(ai_min * 100))
        self._thread(
            self._timeline_operation, mode, ai_max, ai_min, skip_invalid_filenames,
        )

    def start_ai_sync(self) -> None:
        if not self.transcript:
            self.sync_mode.set("speech")
            messagebox.showinfo(APP_NAME, "Generate the transcript first, then select AI Content again.")
            return
        if not ai_sync_backend():
            self.sync_mode.set("speech")
            messagebox.showerror(APP_NAME, "Local AI Content Sync components are not installed.")
            return
        self.ai_progress.configure(value=0)
        self.ai_progress_text.set("0% · Starting AI Sync")
        self.calculate_timeline()

    def _timeline_operation(
        self, mode: str, ai_max_duration: float = 5.0, ai_min_confidence: float = 0.30,
        skip_invalid_filenames: bool = False,
    ) -> list[float] | dict[str, int] | None:
        if mode == "ai":
            _, confidence = semantic_sync(
                self.items, self.transcript, self.audio_duration, self._progress_callback,
                max_image_duration=ai_max_duration,
                min_confidence=ai_min_confidence,
            )
            return confidence
        if mode == "filename":
            self._progress_callback(0.2, "Reading timestamps from media filenames…")
            invalid_count = sum(
                item.kind != "gap" and filename_timestamp(item.path) is None
                for item in self.items
            )
            assign_filename_timing(
                self.items, self.audio_duration, skip_invalid=skip_invalid_filenames,
            )
            message = "Filename-timed timeline ready"
            if invalid_count:
                message += f" · skipped {invalid_count} file(s)"
            self._progress_callback(1.0, message)
            return {"filename_skipped": invalid_count}
        message = "Aligning images to transcript words…" if mode == "speech" else "Analyzing narration pauses…"
        self._progress_callback(0.15, message)
        assign_durations(
            self.items, self.audio_duration, mode, self.audio_path,
            transcript=self.transcript,
        )
        self._progress_callback(1.0, "Timeline ready")
        return None

    def _refresh_timeline(self) -> None:
        self.timeline_tree.delete(*self.timeline_tree.get_children())
        cursor = 0.0
        for index, item in enumerate(self.items, 1):
            end = cursor + item.assigned_duration
            confidence = item.ai_confidence
            if confidence < 0 and index <= len(self.ai_sync_confidence):
                confidence = self.ai_sync_confidence[index - 1]
            match = "NEEDS IMAGE" if item.kind == "gap" else (f"{confidence:.0%}" if confidence >= 0 else "—")
            self.timeline_tree.insert("", "end", values=(
                index, item.name, item.kind.upper(), MOTION_LABELS.get(item.motion, item.motion),
                EFFECT_LABELS.get(item.effect, item.effect),
                TRANSITION_LABELS.get(item.transition, item.transition), match, format_time(cursor),
                f"{item.assigned_duration:.2f}s", format_time(end),
            ))
            cursor = end
        self.summary_text.set(f"{len(self.items)} scenes  ·  {format_time(self.audio_duration)}")

        _, _, total = self._timeline_positions()
        self.seek_scale.configure(to=max(0.001, total))
        self.playhead_seconds = min(self.playhead_seconds, total) if total else 0.0
        self._last_preview_key = None
        self.after_idle(self._redraw_track)
        self.after_idle(lambda: self._render_preview_at(self.playhead_seconds, force=True))

    def _on_timeline_selection(self, event=None) -> None:
        selection = self.timeline_tree.selection()
        if not selection:
            return
        index = self.timeline_tree.index(selection[0])
        starts, _, _ = self._timeline_positions()
        if index < len(starts):
            self.seek_preview(starts[index])

    def _apply_motion_indices(self, indices: list[int], alternate: bool = False) -> None:
        image_indices = [index for index in indices if 0 <= index < len(self.items) and self.items[index].kind == "image"]
        if not image_indices:
            messagebox.showinfo(APP_NAME, "Select at least one image scene. Motion is not applied to video clips.")
            return
        self._push_undo()
        selected_label = self.motion_choice.get()
        if not alternate and selected_label == "2.5D Depth" and len(image_indices) > 12:
            proceed = messagebox.askyesno(
                APP_NAME,
                f"Apply 2.5D Depth to {len(image_indices)} images?\n\n"
                "Depth scenes are generated locally during export and will take longer. "
                "For a large universe project, Cosmic Motion is faster for most scenes.",
            )
            if not proceed:
                return
        auto_cycle = alternate or selected_label == "Auto cycle"
        for order, index in enumerate(image_indices):
            self.items[index].motion = MOTION_CYCLE[order % len(MOTION_CYCLE)] if auto_cycle else MOTION_CODES[selected_label]
        self._refresh_timeline()
        effect = "alternating motion" if auto_cycle else selected_label
        self.status_text.set(f"Applied {effect} to {len(image_indices)} image scenes")

    def apply_motion_selected(self) -> None:
        indices = [self.timeline_tree.index(item_id) for item_id in self.timeline_tree.selection()]
        if not indices:
            messagebox.showinfo(APP_NAME, "Select one or more timeline rows first. Use Ctrl-click for scenes such as 2 and 5.")
            return
        self._apply_motion_indices(sorted(indices))

    def apply_motion_range(self) -> None:
        if not self.items:
            return
        start = simpledialog.askinteger("Motion range", "First scene number:", minvalue=1, maxvalue=len(self.items))
        if start is None:
            return
        end = simpledialog.askinteger("Motion range", "Last scene number:", initialvalue=start,
                                      minvalue=start, maxvalue=len(self.items))
        if end is None:
            return
        self._apply_motion_indices(list(range(start - 1, end)))

    def apply_motion_all(self) -> None:
        self._apply_motion_indices(list(range(len(self.items))))

    def apply_motion_alternate(self) -> None:
        self._apply_motion_indices(list(range(len(self.items))), alternate=True)

    def _selected_timeline_indices(self) -> list[int]:
        return sorted({self.timeline_tree.index(item_id) for item_id in self.timeline_tree.selection()})

    def _ask_scene_range(self, title: str) -> list[int]:
        if not self.items:
            return []
        start = simpledialog.askinteger(title, "First scene number:", minvalue=1, maxvalue=len(self.items))
        if start is None:
            return []
        end = simpledialog.askinteger(
            title, "Last scene number:", initialvalue=start, minvalue=start, maxvalue=len(self.items),
        )
        return list(range(start - 1, end)) if end is not None else []

    def _apply_effect_indices(self, indices: list[int], alternate: bool = False, clear: bool = False) -> None:
        targets = [index for index in indices if 0 <= index < len(self.items) and self.items[index].kind != "gap"]
        if alternate:
            targets = targets[::2]
        if not targets:
            messagebox.showinfo(APP_NAME, "Select at least one image or video scene first.")
            return
        self._push_undo()
        chosen = "none" if clear else EFFECT_CODES[self.effect_choice.get()]
        strength = max(0.0, min(1.0, self.effect_strength.get() / 100.0))
        for order, index in enumerate(targets):
            self.items[index].effect = EFFECT_CYCLE[order % len(EFFECT_CYCLE)] if alternate and not clear else chosen
            self.items[index].effect_strength = strength
        self._refresh_timeline()
        label = "alternating presets" if alternate and not clear else ("removed effect" if clear else self.effect_choice.get())
        self.status_text.set(f"Applied {label} to {len(targets)} scene(s)")

    def apply_effect_selected(self) -> None:
        self._apply_effect_indices(self._selected_timeline_indices())

    def apply_effect_range(self) -> None:
        self._apply_effect_indices(self._ask_scene_range("Effect range"))

    def apply_effect_all(self) -> None:
        self._apply_effect_indices(list(range(len(self.items))))

    def apply_effect_alternate(self) -> None:
        self._apply_effect_indices(list(range(len(self.items))), alternate=True)

    def clear_effect_selected(self) -> None:
        self._apply_effect_indices(self._selected_timeline_indices(), clear=True)

    def _apply_transition_indices(self, indices: list[int], alternate: bool = False, clear: bool = False) -> None:
        targets = [index for index in indices if 0 <= index < len(self.items) and self.items[index].kind != "gap"]
        if alternate:
            targets = targets[::2]
        if not targets:
            messagebox.showinfo(APP_NAME, "Select at least one image or video scene first.")
            return
        self._push_undo()
        chosen = "none" if clear else TRANSITION_CODES[self.transition_choice.get()]
        duration = max(.1, min(1.5, float(self.transition_duration.get())))
        for index in targets:
            self.items[index].transition = chosen
            self.items[index].transition_duration = duration
        self._refresh_timeline()
        label = "removed transition" if clear else self.transition_choice.get()
        self.status_text.set(f"Applied {label} to {len(targets)} scene(s)")

    def apply_transition_selected(self) -> None:
        self._apply_transition_indices(self._selected_timeline_indices())

    def apply_transition_range(self) -> None:
        self._apply_transition_indices(self._ask_scene_range("Transition range"))

    def apply_transition_all(self) -> None:
        self._apply_transition_indices(list(range(len(self.items))))

    def apply_transition_alternate(self) -> None:
        self._apply_transition_indices(list(range(len(self.items))), alternate=True)

    def clear_transition_selected(self) -> None:
        self._apply_transition_indices(self._selected_timeline_indices(), clear=True)

    def apply_video_speed(self) -> None:
        targets = [index for index in self._selected_timeline_indices() if self.items[index].kind == "video"]
        if not targets:
            messagebox.showinfo(APP_NAME, "Select one or more video scenes first.")
            return
        self._push_undo()
        speed = max(.25, min(4.0, float(self.video_speed.get())))
        for index in targets:
            self.items[index].playback_speed = speed
        self._refresh_timeline()
        self.status_text.set(f"Applied {speed:g}x speed to {len(targets)} video scene(s)")

    def edit_scene_duration(self, event=None) -> None:
        selection = self.timeline_tree.selection()
        if not selection:
            return
        index = self.timeline_tree.index(selection[0])
        if self.items[index].kind == "gap":
            self.fill_selected_gap()
            return
        if not self.audio_path or len(self.items) < 2:
            return
        current = self.items[index].assigned_duration
        value = simpledialog.askfloat("Scene duration", "Duration in seconds:", initialvalue=current,
                                      minvalue=0.12, maxvalue=self.audio_duration)
        if value is None:
            return
        difference = value - current
        other_index = len(self.items) - 1 if index != len(self.items) - 1 else len(self.items) - 2
        if self.items[other_index].assigned_duration - difference < 0.12:
            messagebox.showwarning(APP_NAME, "Not enough duration in the final scene to keep audio sync.")
            return
        self._push_undo()
        self.items[index].assigned_duration = value
        self.items[other_index].assigned_duration -= difference
        self._refresh_timeline()
        self.status_text.set("Scene duration changed; total audio sync preserved")

    def _show_loaded_script(self, script: str, source: str) -> None:
        cleaned = script.strip()
        if not cleaned:
            messagebox.showwarning(APP_NAME, "The selected script is empty.")
            return
        self.loaded_script_text = cleaned
        self.transcript_text.delete("1.0", "end")
        self.transcript_text.insert("1.0", cleaned)
        self.transcript_info.configure(
            text=f"{source} loaded · {len(cleaned.split())} spaced tokens · click Align with Audio"
        )
        self.transcript_progress_text.set("Script ready for audio alignment")
        self.status_text.set(f"{source} loaded · align it with narration audio")

    def paste_script(self) -> None:
        window = tk.Toplevel(self)
        window.title("Paste exact caption script")
        window.geometry("780x540")
        window.minsize(620, 420)
        window.configure(bg=BG)
        window.transient(self)
        window.grab_set()
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Paste the exact script in any language", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="StoryCut will keep these spellings exactly and use Whisper only for audio timing.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 10))
        editor = tk.Text(
            frame, bg="#0f141d", fg=TEXT, insertbackground=TEXT, relief="flat",
            wrap="word", font=("Segoe UI", 11), padx=12, pady=10,
        )
        editor.pack(fill="both", expand=True)
        if self.loaded_script_text:
            editor.insert("1.0", self.loaded_script_text)
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(12, 0))

        def accept(align_now: bool) -> None:
            script = editor.get("1.0", "end").strip()
            if not script:
                messagebox.showwarning(APP_NAME, "Paste a script first.", parent=window)
                return
            window.destroy()
            self._show_loaded_script(script, "Pasted script")
            if align_now:
                self.align_loaded_script()

        ttk.Button(controls, text="Use & Align", style="Accent.TButton",
                   command=lambda: accept(True)).pack(side="left")
        ttk.Button(controls, text="Use without aligning",
                   command=lambda: accept(False)).pack(side="left", padx=6)
        ttk.Button(controls, text="Cancel", command=window.destroy).pack(side="right")
        editor.focus_set()

    def import_script(self) -> None:
        path = filedialog.askopenfilename(
            title="Import exact caption script",
            filetypes=[
                ("Script files", "*.txt *.md *.srt *.vtt *.docx"),
                ("Text", "*.txt *.md"), ("Subtitles", "*.srt *.vtt"),
                ("Word document", "*.docx"), ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            self._show_loaded_script(read_script_file(path), Path(path).name)
            self.align_loaded_script()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not import script:\n{exc}")

    def align_loaded_script(self) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish.")
            return
        if not self.audio_path:
            messagebox.showwarning(APP_NAME, "Choose narration audio before aligning the script.")
            return
        if not self.loaded_script_text.strip():
            messagebox.showinfo(APP_NAME, "Paste or import the exact script first.")
            return
        guide = copy.deepcopy(self.transcript)
        self.transcript_progress.configure(mode="indeterminate", value=0)
        self.transcript_progress.start(12)
        self.transcript_progress_text.set(
            "Using existing word timings…" if guide else "Listening for script timing…"
        )
        self.align_script_button.configure(state="disabled", text="Aligning…")
        self._thread(
            self._align_script_operation,
            self.loaded_script_text,
            self.whisper_model.get(),
            LANGUAGE_CODES.get(self.language.get()),
            guide,
            self.transcript_language,
        )

    def _align_script_operation(
        self, script: str, model_name: str, language: str | None,
        guide: list[TranscriptSegment], existing_language: str,
    ) -> tuple[str, list[TranscriptSegment], float]:
        detected_language = existing_language or language or "auto"
        if not guide:
            def transcription_progress(value: float, message: str) -> None:
                self._progress_callback(value * 0.88, message)

            detected_language, guide = transcribe_audio(
                self.audio_path, model_name, language, transcription_progress
            )
        self._progress_callback(0.92, "Aligning exact script words to narration…")
        segments, confidence = align_script_to_transcript(
            script, guide, self.audio_duration
        )
        self._progress_callback(1.0, f"Exact script aligned · {len(segments)} captions")
        return detected_language, segments, confidence

    def generate_transcript(self) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish.")
            return
        if not self.audio_path:
            messagebox.showwarning(APP_NAME, "Choose narration audio first.")
            return
        self.transcript_progress.configure(mode="indeterminate", value=0)
        self.transcript_progress.start(12)
        self.transcript_progress_text.set("Loading Whisper model…")
        self.generate_button.configure(state="disabled", text="Generating…")
        self._thread(
            self._transcribe_operation,
            self.whisper_model.get(),
            LANGUAGE_CODES.get(self.language.get()),
        )

    def _update_transcript_progress(self, value: float, message: str) -> None:
        if value < 0.08:
            if str(self.transcript_progress.cget("mode")) != "indeterminate":
                self.transcript_progress.configure(mode="indeterminate")
                self.transcript_progress.start(12)
            self.transcript_progress_text.set(message)
            return
        if str(self.transcript_progress.cget("mode")) != "determinate":
            self.transcript_progress.stop()
            self.transcript_progress.configure(mode="determinate")
        percent = max(0, min(100, round(value * 100)))
        self.transcript_progress["value"] = percent
        self.transcript_progress_text.set(f"{percent}% · {message}")

    def _update_export_progress(self, value: float, message: str) -> None:
        percent = max(0, min(100, round(value * 100)))
        self.export_progress["value"] = percent
        self.export_progress_text.set(f"{percent}% · {message}")

    def _transcribe_operation(self, model_name: str, language: str | None):
        return transcribe_audio(self.audio_path, model_name, language, self._progress_callback)

    def _refresh_transcript(self) -> None:
        self.transcript_text.delete("1.0", "end")
        for segment in self.transcript:
            self.transcript_text.insert("end", f"[{format_time(segment.start)}] {segment.text}\n")
        self.transcript_info.configure(text=f"{self.transcript_language} · {len(self.transcript)} caption segments")
        self._draw_caption_preview(self.playhead_seconds)

    def _sync_transcript_text_edits(self) -> None:
        lines = [line.strip() for line in self.transcript_text.get("1.0", "end").splitlines() if line.strip()]
        if len(lines) != len(self.transcript):
            return
        for segment, line in zip(self.transcript, lines):
            updated = re_sub_timestamp(line)
            if updated != segment.text:
                segment.text = updated
                segment.words = []

    def export_srt(self) -> None:
        if not self.transcript:
            messagebox.showwarning(APP_NAME, "Generate a transcript first.")
            return
        self._sync_transcript_text_edits()
        path = filedialog.asksaveasfilename(defaultextension=".srt", filetypes=[("SubRip subtitles", "*.srt")])
        if path:
            write_srt(self.transcript, path)
            self.status_text.set("SRT exported")

    def export_timeline(self) -> None:
        if not self.items or not self.audio_path:
            messagebox.showwarning(APP_NAME, "Add media and narration first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV timeline", "*.csv")])
        if path:
            write_timeline_csv(self.items, path)
            self.status_text.set("Timeline CSV exported")

    def _settings(self) -> ExportSettings:
        width, height = (int(x) for x in self.resolution.get().split("×"))
        quality = self.quality.get()
        crf = {"High (CRF 17)": 17, "Balanced (CRF 20)": 20, "Small file (CRF 24)": 24}.get(quality, 20)
        fps = int(self.fps.get())
        bitrate = youtube_video_bitrate_kbps(width, height, fps) if quality == "YouTube Recommended" else 0
        return ExportSettings(width=width, height=height, fps=fps, crf=crf,
                              video_bitrate_kbps=bitrate,
                              burn_captions=self.caption_enabled.get(), caption_font_size=self.caption_size.get(),
                              caption_x=self.caption_x, caption_y=self.caption_y,
                              caption_highlight_color=CAPTION_HIGHLIGHTS.get(
                                  self.caption_highlight.get(), "FFD400"
                              ),
                              ai_max_image_duration=float(self.ai_max_duration.get()),
                              ai_min_confidence=float(self.ai_min_confidence.get()) / 100.0)

    def export_video(self) -> None:
        if not self.items or not self.audio_path:
            messagebox.showwarning(APP_NAME, "Add numbered media and narration first.")
            return
        if any(item.assigned_duration <= 0 for item in self.items):
            self.calculate_timeline()
            messagebox.showinfo(APP_NAME, "Timeline is being calculated. Export again when it is ready.")
            return
        if self.caption_enabled.get() and not self.transcript:
            messagebox.showwarning(APP_NAME, "Generate a transcript first, or turn captions off.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if not path:
            return
        self.last_export = path
        self.export_progress.configure(value=0)
        self.export_progress_text.set("0% · Preparing export")
        self.export_button.configure(text="Exporting…")
        settings = self._settings()
        subtitle_path = None
        if settings.burn_captions:
            self._sync_transcript_text_edits()
            srt_path = str(Path(path).with_suffix(".captions.srt"))
            subtitle_path = str(Path(path).with_suffix(".captions.ass"))
            write_srt(self.transcript, srt_path)
            write_karaoke_ass(self.transcript, subtitle_path, settings)
        self._thread(self._render_operation, path, settings, subtitle_path)

    def _render_operation(self, output_path: str, settings: ExportSettings, subtitle_path: str | None) -> None:
        render_video(self.items, self.audio_path, output_path, settings, subtitle_path,
                     self._progress_callback)
        write_timeline_csv(self.items, str(Path(output_path).with_suffix(".timeline.csv")))

    def _preview_selected_source(self, event=None) -> None:
        selected = self.media_tree.selection()
        if not selected:
            return
        item = self.items[self.media_tree.index(selected[0])]
        if item.kind == "gap":
            children = self.timeline_tree.get_children()
            index = self.media_tree.index(selected[0])
            if index < len(children):
                self.timeline_tree.selection_set(children[index])
            self.fill_selected_gap()
            return
        path = item.path
        self._open_with_player(path)

    def preview_export(self) -> None:
        if not self.last_export or not Path(self.last_export).exists():
            messagebox.showinfo(APP_NAME, "No exported video yet.")
            return
        self._open_with_player(self.last_export)

    def _open_with_player(self, path: str) -> None:
        ffplay = shutil.which("ffplay")
        if ffplay:
            subprocess.Popen([ffplay, "-autoexit", path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.startfile(path)  # type: ignore[attr-defined]

    def _session_root(self) -> Path:
        root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "StoryCut" / "sessions"
        (root / "autosaves").mkdir(parents=True, exist_ok=True)
        return root

    def _read_session_index(self) -> dict:
        path = self._session_root() / "projects.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects", []), list):
                return data
        except Exception:
            pass
        return {"version": 1, "last_session_id": "", "projects": []}

    def _write_session_index(self, data: dict) -> None:
        path = self._session_root() / "projects.json"
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _autosave_signature(self) -> str:
        payload = {
            "project_path": self.project_path,
            "items": [
                (item.path, item.kind, item.assigned_duration, item.motion,
                 item.source_start, item.source_end, item.source_duration,
                 item.placeholder_text, item.ai_confidence, item.candidate_path, item.candidate_kind,
                 item.effect, item.effect_strength, item.transition, item.transition_duration,
                 item.playback_speed)
                for item in self.items
            ],
            "audio": (self.audio_path, self.audio_duration),
            "sync": self.sync_mode.get(),
            "transcript": [
                (segment.start, segment.end, segment.text, segment.words)
                for segment in self.transcript
            ],
            "language": self.transcript_language,
            "settings": {
                "resolution": self.resolution.get(), "fps": self.fps.get(),
                "quality": self.quality.get(), "captions": self.caption_enabled.get(),
                "caption_size": self.caption_size.get(), "caption_x": self.caption_x,
                "caption_y": self.caption_y, "highlight": self.caption_highlight.get(),
                "ai_max_duration": self.ai_max_duration.get(),
                "ai_min_confidence": self.ai_min_confidence.get(),
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _autosave_now(
        self, force: bool = False, show_status: bool = True, allow_busy: bool = False
    ) -> bool:
        if self._restoring_session or (self.busy and not allow_busy) or not (
            self.items or self.audio_path or self.transcript
        ):
            return False
        try:
            self._sync_transcript_text_edits()
            signature = self._autosave_signature()
            if not force and signature == self._last_autosave_signature:
                return False
            root = self._session_root()
            autosave_path = root / "autosaves" / f"{self.session_id}.storycut"
            save_project(
                autosave_path, self.items, self.audio_path, self.sync_mode.get(), self._settings(),
                self.transcript, self.transcript_language,
            )
            manual_save_error = ""
            if self.project_path:
                try:
                    save_project(
                        self.project_path, self.items, self.audio_path, self.sync_mode.get(), self._settings(),
                        self.transcript, self.transcript_language,
                    )
                except Exception as exc:
                    manual_save_error = str(exc)
            index = self._read_session_index()
            projects = [entry for entry in index.get("projects", []) if entry.get("id") != self.session_id]
            old_entry = next(
                (entry for entry in index.get("projects", []) if entry.get("id") == self.session_id), {}
            )
            project_name = Path(self.project_path).stem if self.project_path else old_entry.get(
                "name", f"Untitled {self.session_id[:6]}"
            )
            entry = {
                "id": self.session_id,
                "name": project_name,
                "project_path": self.project_path,
                "autosave_path": str(autosave_path),
                "updated": time.time(),
                "playhead": self.playhead_seconds,
                "scene_count": len(self.items),
                "audio_name": Path(self.audio_path).name if self.audio_path else "",
            }
            projects.insert(0, entry)
            index.update({"version": 1, "last_session_id": self.session_id, "projects": projects[:50]})
            self._write_session_index(index)
            self._last_autosave_signature = signature
            if show_status:
                if manual_save_error:
                    self.status_text.set(f"Recovery saved · project file unavailable: {manual_save_error}")
                else:
                    self.status_text.set(f"Auto-saved · {time.strftime('%H:%M:%S')}")
            return True
        except Exception as exc:
            if show_status:
                self.status_text.set(f"Auto-save failed: {exc}")
            return False

    def _autosave_tick(self) -> None:
        self._autosave_after_id = None
        self._autosave_now()
        self._autosave_after_id = self.after(5000, self._autosave_tick)

    def _restore_last_session(self) -> None:
        if self.items or self.audio_path or self.transcript or self.project_path:
            return
        index = self._read_session_index()
        session_id = str(index.get("last_session_id", ""))
        entry = next((item for item in index.get("projects", []) if item.get("id") == session_id), None)
        if not entry:
            return
        autosave = Path(str(entry.get("autosave_path", "")))
        original = Path(str(entry.get("project_path", ""))) if entry.get("project_path") else None
        source = autosave if autosave.exists() else original
        if not source or not source.exists():
            return
        try:
            self._load_project_path(
                str(source), display_path=str(entry.get("project_path", "")),
                session_id=session_id, playhead=float(entry.get("playhead", 0.0)),
            )
            self.status_text.set(f"Restored automatically · {entry.get('name', 'project')}")
        except Exception as exc:
            self.status_text.set(f"Could not restore last session: {exc}")

    def show_project_hub(self) -> None:
        self._autosave_now(force=True, show_status=False)
        index = self._read_session_index()
        window = tk.Toplevel(self)
        window.title("StoryCut Projects")
        window.geometry("850x470")
        window.minsize(680, 360)
        window.configure(bg=BG)
        window.transient(self)
        window.grab_set()

        header = ttk.Frame(window, padding=(22, 18, 22, 10))
        header.pack(fill="x")
        ttk.Label(header, text="Projects & Auto-Recovery", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header, text="Every project below has an automatic recovery copy.", style="Muted.TLabel"
        ).pack(anchor="w", pady=(3, 0))

        columns = ("name", "scenes", "audio", "updated", "location")
        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "PROJECT", "scenes": "SCENES", "audio": "AUDIO", "updated": "LAST SAVED", "location": "LOCATION"}
        widths = {"name": 160, "scenes": 65, "audio": 150, "updated": 130, "location": 290}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="center" if column == "scenes" else "w")
        tree.pack(side="left", fill="both", expand=True)
        project_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        project_scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=project_scroll.set)

        entries: dict[str, dict] = {}
        for entry in sorted(index.get("projects", []), key=lambda item: item.get("updated", 0), reverse=True):
            source = Path(str(entry.get("autosave_path", "")))
            original = str(entry.get("project_path", ""))
            if not source.exists() and not (original and Path(original).exists()):
                continue
            item_id = tree.insert("", "end", values=(
                entry.get("name", "Untitled"), entry.get("scene_count", 0),
                entry.get("audio_name", ""),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(float(entry.get("updated", 0)))),
                original or "Auto-recovery",
            ))
            entries[item_id] = entry
            if entry.get("id") == self.session_id:
                tree.selection_set(item_id)

        def open_selected(event=None) -> None:
            selected = tree.selection()
            if not selected:
                return
            entry = entries[selected[0]]
            autosave = Path(str(entry.get("autosave_path", "")))
            original = Path(str(entry.get("project_path", ""))) if entry.get("project_path") else None
            source = autosave if autosave.exists() else original
            if not source:
                return
            window.destroy()
            try:
                self._load_project_path(
                    str(source), display_path=str(entry.get("project_path", "")),
                    session_id=str(entry.get("id", uuid.uuid4().hex)),
                    playhead=float(entry.get("playhead", 0.0)),
                )
                self.status_text.set(f"Project opened · {entry.get('name', 'Untitled')}")
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))

        tree.bind("<Double-1>", open_selected)
        buttons = ttk.Frame(window, padding=(22, 0, 22, 18))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Continue selected", style="Accent.TButton", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="Open project file…", command=lambda: (window.destroy(), self.open_project())).pack(side="left", padx=6)
        ttk.Button(buttons, text="New project", command=lambda: (window.destroy(), self.new_project())).pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")

    def _update_project_title(self) -> None:
        name = Path(self.project_path).stem if self.project_path else "Untitled"
        self.title(f"{APP_NAME} · {name} · Local automatic editor")

    def save_project(self) -> None:
        path = self.project_path or filedialog.asksaveasfilename(
            defaultextension=".storycut", filetypes=[("StoryCut project", "*.storycut")]
        )
        if not path:
            return
        try:
            self._sync_transcript_text_edits()
            save_project(path, self.items, self.audio_path, self.sync_mode.get(), self._settings(),
                         self.transcript, self.transcript_language)
            self.project_path = str(Path(path).resolve())
            self._update_project_title()
            self._last_autosave_signature = ""
            self._autosave_now(force=True, show_status=False, allow_busy=True)
            self.status_text.set(f"Project saved · {Path(path).name}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save project:\n{exc}")

    def open_project(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("StoryCut project", "*.storycut")])
        if not path:
            return
        self._autosave_now(force=True, show_status=False)
        try:
            self._load_project_path(path, display_path=str(Path(path).resolve()))
            self.status_text.set(f"Project loaded · {Path(path).stem}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _load_project_path(
        self,
        path: str,
        display_path: str | None = None,
        session_id: str | None = None,
        playhead: float = 0.0,
    ) -> None:
        self._restoring_session = True
        try:
            self.stop_preview()
            self.audio_player.close()
            items, audio, mode, settings, transcript, language = load_project(path)
            self.items = items
            self.audio_path = audio
            self.audio_duration = duration_of(audio) if audio and Path(audio).exists() else 0.0
            self.sync_mode.set(mode)
            self.transcript = transcript
            self.transcript_language = language
            self.loaded_script_text = ""
            self.resolution.set(f"{settings.width}×{settings.height}")
            self._on_resolution_change()
            self.fps.set(str(settings.fps))
            self.caption_enabled.set(settings.burn_captions)
            self.caption_size.set(settings.caption_font_size)
            self.caption_x = settings.caption_x
            self.caption_y = settings.caption_y
            self.ai_max_duration.set(settings.ai_max_image_duration)
            self.ai_min_confidence.set(round(settings.ai_min_confidence * 100))
            self.caption_highlight.set(next(
                (name for name, color in CAPTION_HIGHLIGHTS.items()
                 if color.casefold() == settings.caption_highlight_color.casefold()),
                "Yellow",
            ))
            if settings.video_bitrate_kbps > 0:
                self.quality.set("YouTube Recommended")
            else:
                self.quality.set({17: "High (CRF 17)", 20: "Balanced (CRF 20)", 24: "Small file (CRF 24)"}.get(settings.crf, "Balanced (CRF 20)"))
            self.project_path = display_path if display_path is not None else str(Path(path).resolve())
            self.session_id = session_id or uuid.uuid4().hex
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.mark_in = None
            self.mark_out = None
            self.playhead_seconds = max(0.0, float(playhead))
            self._refresh_range_text()
            self.audio_text.set(f"{Path(audio).name}  ·  {format_time(self.audio_duration)}" if audio else "No narration selected")
            self._refresh_media()
            self._refresh_timeline()
            self._refresh_transcript()
            self._last_autosave_signature = self._autosave_signature()
            self._update_project_title()
        finally:
            self._restoring_session = False

    def new_project(self) -> None:
        if (self.items or self.audio_path or self.transcript) and not messagebox.askyesno(
            APP_NAME, "Start a new project? Your current work will remain available in Projects / Recovery."
        ):
            return
        self._autosave_now(force=True, show_status=False)
        self.stop_preview()
        self.audio_player.close()
        self.items = []
        self.audio_path = ""
        self.audio_duration = 0.0
        self.transcript = []
        self.transcript_language = ""
        self.loaded_script_text = ""
        self.project_path = ""
        self.session_id = uuid.uuid4().hex
        self._last_autosave_signature = ""
        self.last_export = ""
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.mark_in = None
        self.mark_out = None
        self._refresh_range_text()
        self.caption_x = 0.5
        self.caption_y = 0.86
        self.caption_highlight.set("Yellow")
        self.playhead_seconds = 0.0
        self.audio_text.set("No narration selected")
        self._refresh_media()
        self._refresh_timeline()
        self._refresh_transcript()
        self._update_project_title()
        self.status_text.set("New project")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_NAME, "A task is still running. Close anyway?"):
            return
        self._autosave_now(force=True, show_status=False, allow_busy=True)
        self.preview_playing = False
        if self._autosave_after_id:
            try:
                self.after_cancel(self._autosave_after_id)
            except Exception:
                pass
        if self.preview_after_id:
            try:
                self.after_cancel(self.preview_after_id)
            except Exception:
                pass
        self.audio_player.close()
        self.destroy()


def re_sub_timestamp(line: str) -> str:
    import re
    return re.sub(r"^\[[0-9:.]+\]\s*", "", line).strip()


if __name__ == "__main__":
    app = StoryCutApp()
    app.mainloop()
