from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .capture import CaptureSettings, render_scroll_video_sync

DEFAULT_SETTINGS = CaptureSettings()


class ScrollVideoApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Scroll Web Video")
        self.geometry("560x420")
        self.minsize(520, 400)

        self._events: queue.Queue[tuple[str, str, float | None]] = queue.Queue()
        self._worker: threading.Thread | None = None

        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.cwd() / "scroll.mp4"))
        self.width_var = tk.StringVar(value=str(DEFAULT_SETTINGS.width))
        self.height_var = tk.StringVar(value=str(DEFAULT_SETTINGS.height))
        self.fps_var = tk.StringVar(value=str(DEFAULT_SETTINGS.fps))
        self.speed_var = tk.StringVar(value=str(DEFAULT_SETTINGS.speed))
        self.duration_var = tk.StringVar()
        self.wait_after_load_var = tk.StringVar(
            value=str(DEFAULT_SETTINGS.wait_after_load)
        )
        self.preload_var = tk.BooleanVar(value=True)
        self.sync_animations_var = tk.BooleanVar(value=True)
        self.easing_var = tk.StringVar(value="smoothstep")
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="URL").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Entry(root, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8)
        )

        ttk.Label(root, text="MP4").grid(row=1, column=0, sticky="w", pady=(0, 12))
        ttk.Entry(root, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", pady=(0, 12)
        )
        ttk.Button(root, text="Browse", command=self._choose_output).grid(
            row=1, column=2, sticky="ew", padx=(8, 0), pady=(0, 12)
        )

        options = ttk.LabelFrame(root, text="Video")
        options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        for column in range(4):
            options.columnconfigure(column, weight=1)

        self._field(options, "Width", self.width_var, 0, 0)
        self._field(options, "Height", self.height_var, 0, 1)
        self._field(options, "FPS", self.fps_var, 1, 0)
        self._field(options, "Speed px/s", self.speed_var, 1, 1)
        self._field(options, "Duration sec", self.duration_var, 2, 0)
        self._field(options, "Wait sec", self.wait_after_load_var, 3, 0)

        easing = ttk.Combobox(
            options,
            textvariable=self.easing_var,
            values=("smoothstep", "linear"),
            state="readonly",
        )
        ttk.Label(options, text="Easing").grid(row=2, column=2, sticky="w", padx=8)
        easing.grid(row=2, column=3, sticky="ew", padx=(0, 8), pady=8)

        ttk.Checkbutton(
            root,
            text="Preload lazy content before recording",
            variable=self.preload_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Checkbutton(
            root,
            text="Sync page animations to video time",
            variable=self.sync_animations_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.progress_bar = ttk.Progressbar(root, maximum=100)
        self.progress_bar.grid(row=5, column=0, columnspan=3, sticky="ew")

        ttk.Label(root, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 12)
        )

        self.start_button = ttk.Button(
            root,
            text="Create MP4",
            command=self._start_render,
        )
        self.start_button.grid(row=7, column=2, sticky="e")

    def _field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        pair_column: int,
    ) -> None:
        label_column = pair_column * 2
        entry_column = label_column + 1
        ttk.Label(parent, text=label).grid(
            row=row, column=label_column, sticky="w", padx=8, pady=8
        )
        ttk.Entry(parent, textvariable=variable, width=12).grid(
            row=row, column=entry_column, sticky="ew", padx=(0, 8), pady=8
        )

    def _choose_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Save MP4",
            defaultextension=".mp4",
            filetypes=(("MP4 video", "*.mp4"), ("All files", "*.*")),
        )
        if filename:
            self.output_var.set(filename)

    def _start_render(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        try:
            settings = self._settings_from_form()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        url = self.url_var.get().strip()
        output = self.output_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Enter a web page URL.")
            return
        if not output:
            messagebox.showerror("Missing output", "Choose an MP4 output path.")
            return

        self.start_button.configure(state=tk.DISABLED)
        self.progress_bar.configure(value=0)
        self.status_var.set("Starting")

        self._worker = threading.Thread(
            target=self._render_worker,
            args=(url, output, settings),
            daemon=True,
        )
        self._worker.start()

    def _settings_from_form(self) -> CaptureSettings:
        duration_text = self.duration_var.get().strip()
        duration = float(duration_text) if duration_text else None
        return CaptureSettings(
            width=int(self.width_var.get()),
            height=int(self.height_var.get()),
            fps=int(self.fps_var.get()),
            speed=float(self.speed_var.get()),
            duration=duration,
            wait_after_load=float(self.wait_after_load_var.get()),
            preload=self.preload_var.get(),
            sync_animations=self.sync_animations_var.get(),
            easing=self.easing_var.get(),
        )

    def _render_worker(
        self,
        url: str,
        output: str,
        settings: CaptureSettings,
    ) -> None:
        def progress(message: str, value: float | None) -> None:
            self._events.put(("progress", message, value))

        try:
            result = render_scroll_video_sync(url, output, settings, progress)
        except Exception as exc:
            self._events.put(("error", str(exc), None))
        else:
            self._events.put(("done", str(result), 1.0))

    def _drain_events(self) -> None:
        try:
            while True:
                event, message, value = self._events.get_nowait()
                if event == "progress":
                    self.status_var.set(message)
                    if value is not None:
                        self.progress_bar.configure(value=value * 100)
                elif event == "done":
                    self.status_var.set(f"Saved: {message}")
                    self.progress_bar.configure(value=100)
                    self.start_button.configure(state=tk.NORMAL)
                    messagebox.showinfo("Done", f"Saved MP4:\n{message}")
                elif event == "error":
                    self.status_var.set("Failed")
                    self.start_button.configure(state=tk.NORMAL)
                    messagebox.showerror("Error", message)
        except queue.Empty:
            pass

        self.after(100, self._drain_events)


def main() -> None:
    app = ScrollVideoApp()
    app.mainloop()
