from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from binFileTransfer_core import program_firmware


APP_TITLE = "SST39 Flash Programmer"
WINDOW_SIZE = "780x560"

LEVEL_TAGS = {
    "info": ("log_info", "#1a1a1a"),
    "ok": ("log_ok", "#1f7a1f"),
    "warn": ("log_warn", "#a06400"),
    "err": ("log_err", "#b00020"),
    "wait": ("log_wait", "#666666"),
}


# Sentinel posted on log_queue to signal worker thread completion. Using a
# unique object instead of a magic string avoids any collision with caller-
# provided log messages.
class _DoneSentinel:
    __slots__ = ("success",)

    def __init__(self, success: bool) -> None:
        self.success = success


class ProgrammerGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)

        self.firmware_path: str | None = None
        self.log_queue: queue.Queue[tuple[str, str] | _DoneSentinel] = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_widgets()
        self.root.after(50, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        self.path_var = tk.StringVar(value="(no file selected)")
        ttk.Label(top, text="Firmware:").pack(side=tk.LEFT)
        ttk.Label(top, textvariable=self.path_var, width=60, anchor="w").pack(
            side=tk.LEFT, padx=(6, 6)
        )
        self.browse_btn = ttk.Button(top, text="Browse...", command=self._on_browse)
        self.browse_btn.pack(side=tk.LEFT)

        port_row = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        port_row.pack(fill=tk.X)
        ttk.Label(port_row, text="Port (optional):").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="")
        self.port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=20)
        self.port_entry.pack(side=tk.LEFT, padx=(6, 6))
        ttk.Label(
            port_row,
            text="(leave blank to auto-detect Arduino Due Programming Port)",
            foreground="#666666",
        ).pack(side=tk.LEFT)

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid.pack(fill=tk.X)
        self.start_btn = ttk.Button(
            mid, text="Start Programming", command=self._on_start, state=tk.DISABLED
        )
        self.start_btn.pack(side=tk.LEFT)
        self.clear_btn = ttk.Button(mid, text="Clear Log", command=self._clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=(6, 0))

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            log_frame, wrap=tk.NONE, state=tk.DISABLED, font=("Consolas", 10)
        )
        scroll_y = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log_text.yview
        )
        scroll_x = ttk.Scrollbar(
            log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview
        )
        self.log_text.configure(
            yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set
        )
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        for tag, color in LEVEL_TAGS.values():
            self.log_text.tag_configure(tag, foreground=color)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(bottom, text="Status:").pack(side=tk.LEFT)
        self.status_label = ttk.Label(
            bottom, textvariable=self.status_var, foreground="#1a1a1a"
        )
        self.status_label.pack(side=tk.LEFT, padx=(6, 0))

    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select firmware.bin",
            filetypes=[("Binary firmware", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        self.firmware_path = path
        self.path_var.set(self._shorten(path))
        self.start_btn.config(state=tk.NORMAL)

    @staticmethod
    def _shorten(path: str, max_len: int = 70) -> str:
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3) :]

    def _on_start(self) -> None:
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            self._append_log("Please select a valid firmware file first.", "err")
            return
        if self.worker and self.worker.is_alive():
            return

        self._set_status("Programming...", "#a06400")
        self.start_btn.config(state=tk.DISABLED)
        self.browse_btn.config(state=tk.DISABLED)
        self.port_entry.config(state=tk.DISABLED)

        port = self.port_var.get().strip() or None

        self.worker = threading.Thread(
            target=self._run_transfer,
            args=(self.firmware_path, port),
            daemon=True,
        )
        self.worker.start()

    def _run_transfer(self, firmware_path: str, port: str | None) -> None:
        def log_cb(message: str, level: str) -> None:
            self.log_queue.put((message, level))

        try:
            success = program_firmware(firmware_path, log_cb, port=port)
        except Exception as e:
            self.log_queue.put((f"Unexpected error: {e}", "err"))
            success = False

        self.log_queue.put(_DoneSentinel(success))

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, _DoneSentinel):
                    if item.success:
                        self._set_status("Success", "#1f7a1f")
                        self._append_log(
                            f"{os.path.basename(self.firmware_path or '')} "
                            "Program Successful.",
                            "ok",
                        )
                    else:
                        self._set_status("Error", "#b00020")
                    self.start_btn.config(state=tk.NORMAL)
                    self.browse_btn.config(state=tk.NORMAL)
                    self.port_entry.config(state=tk.NORMAL)
                else:
                    message, level = item
                    self._append_log(message, level)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_log_queue)

    def _append_log(self, message: str, level: str) -> None:
        tag = LEVEL_TAGS.get(level, ("log_info", "#1a1a1a"))[0]
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.config(foreground=color)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(
                "Busy",
                "Programming is in progress. Please wait for it to finish "
                "before closing.",
            )
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ProgrammerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
