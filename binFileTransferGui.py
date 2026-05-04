import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from binFileTransfer_core import program_firmware


APP_TITLE = "SST39 Flash Programmer"
WINDOW_SIZE = "780x520"

LEVEL_TAGS = {
    "info": ("log_info", "#1a1a1a"),
    "ok": ("log_ok", "#1f7a1f"),
    "warn": ("log_warn", "#a06400"),
    "err": ("log_err", "#b00020"),
    "wait": ("log_wait", "#666666"),
}


class ProgrammerGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)

        self.firmware_path: str | None = None
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
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
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

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

        self.worker = threading.Thread(
            target=self._run_transfer, args=(self.firmware_path,), daemon=True
        )
        self.worker.start()

    def _run_transfer(self, firmware_path: str) -> None:
        def log_cb(message: str, level: str) -> None:
            self.log_queue.put((message, level))

        try:
            success = program_firmware(firmware_path, log_cb)
        except Exception as e:
            self.log_queue.put((f"Unexpected error: {e}", "err"))
            success = False

        self.log_queue.put((("__DONE_OK__" if success else "__DONE_FAIL__"), "ctrl"))

    def _drain_log_queue(self) -> None:
        try:
            while True:
                message, level = self.log_queue.get_nowait()
                if level == "ctrl":
                    if message == "__DONE_OK__":
                        self._set_status("Success", "#1f7a1f")
                    else:
                        self._set_status("Error", "#b00020")
                    self.start_btn.config(state=tk.NORMAL)
                    self.browse_btn.config(state=tk.NORMAL)
                else:
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
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ProgrammerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
