from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from binFileTransfer_core import gpio_read, gpio_set, program_firmware


APP_TITLE = "SST39 Flash Programmer"
WINDOW_SIZE = "820x620"

LEVEL_TAGS = {
    "info": ("log_info", "#1a1a1a"),
    "ok": ("log_ok", "#1f7a1f"),
    "warn": ("log_warn", "#a06400"),
    "err": ("log_err", "#b00020"),
    "wait": ("log_wait", "#666666"),
}


# Sentinel posted on a tab's log_queue to signal worker completion.
class _DoneSentinel:
    __slots__ = ("success",)

    def __init__(self, success: bool) -> None:
        self.success = success


# Pin choices for the GPIO tab. Order matches README §3 wiring tables so the
# Due labels users see in the dropdown line up with what they read in the
# datasheet section. Each entry: (display_label, due_pin_number).
GPIO_PIN_CHOICES: list[tuple[str, int]] = [
    # Address bus (A0..A18)
    ("A0  (D44)", 44), ("A1  (D42)", 42), ("A2  (D40)", 40),
    ("A3  (D38)", 38), ("A4  (D36)", 36), ("A5  (D34)", 34),
    ("A6  (D32)", 32), ("A7  (D30)", 30), ("A8  (D33)", 33),
    ("A9  (D35)", 35), ("A10 (D41)", 41), ("A11 (D37)", 37),
    ("A12 (D28)", 28), ("A13 (D31)", 31), ("A14 (D29)", 29),
    ("A15 (D26)", 26), ("A16 (D24)", 24), ("A17 (D27)", 27),
    ("A18 (D22)", 22),
    # Data bus (DQ0..DQ7)
    ("DQ0 (D46)", 46), ("DQ1 (D48)", 48), ("DQ2 (D50)", 50),
    ("DQ3 (D53)", 53), ("DQ4 (D51)", 51), ("DQ5 (D49)", 49),
    ("DQ6 (D47)", 47), ("DQ7 (D45)", 45),
    # Control
    ("CE# (D43)", 43), ("OE# (D39)", 39), ("WE# (D25)", 25),
    # On-board LED
    ("LED (D13)", 13),
]


class _LoggedTab:
    """Common scaffolding for a tab: log queue + log widget + worker thread.
    Subclasses build their own controls in _build_controls(parent_frame)."""

    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.log_queue: queue.Queue[tuple[str, str] | _DoneSentinel] = queue.Queue()
        self.worker: threading.Thread | None = None

        controls = ttk.Frame(self.frame, padding=(10, 10, 10, 6))
        controls.pack(fill=tk.X)
        self._build_controls(controls)

        self._build_log_area()

        # Drain the queue periodically on the Tk event loop.
        self.app.root.after(50, self._drain_log_queue)

    # ---- subclass hooks ----------------------------------------------------

    def _build_controls(self, parent: ttk.Frame) -> None:
        raise NotImplementedError

    def _on_done(self, success: bool) -> None:
        """Called on the Tk thread when the current worker finishes."""
        pass

    # ---- shared infrastructure --------------------------------------------

    def _build_log_area(self) -> None:
        log_frame = ttk.Frame(self.frame, padding=(10, 0, 10, 10))
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

    def _log_callback_threadsafe(self, message: str, level: str) -> None:
        """Used by worker threads to push a log line — Tk-safe via queue."""
        self.log_queue.put((message, level))

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, _DoneSentinel):
                    self._on_done(item.success)
                else:
                    message, level = item
                    self._append_log(message, level)
        except queue.Empty:
            pass
        self.app.root.after(50, self._drain_log_queue)

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

    def is_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def submit_work(self, target_callable) -> bool:
        """Run `target_callable(log_cb)` in a worker. Returns False if a job
        is already running. The wrapped callable should return a bool."""
        if self.is_busy():
            return False
        if self.app.any_tab_busy():
            messagebox.showinfo(
                "Busy",
                "Another operation is already running on this device. "
                "Please wait for it to finish.",
            )
            return False

        def runner() -> None:
            try:
                success = bool(target_callable(self._log_callback_threadsafe))
            except Exception as e:
                self.log_queue.put((f"Unexpected error: {e}", "err"))
                success = False
            self.log_queue.put(_DoneSentinel(success))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()
        return True


# ---------------------------------------------------------------------------
# Flash ROM tab — same flow as before, just relocated inside a Notebook tab.
# ---------------------------------------------------------------------------
class FlashTab(_LoggedTab):
    def _build_controls(self, parent: ttk.Frame) -> None:
        # Row 1: firmware path + Browse
        self.firmware_path: str | None = None
        row1 = ttk.Frame(parent)
        row1.pack(fill=tk.X)
        self.path_var = tk.StringVar(value="(no file selected)")
        ttk.Label(row1, text="Firmware:").pack(side=tk.LEFT)
        ttk.Label(row1, textvariable=self.path_var, width=60, anchor="w").pack(
            side=tk.LEFT, padx=(6, 6)
        )
        self.browse_btn = ttk.Button(row1, text="Browse...", command=self._on_browse)
        self.browse_btn.pack(side=tk.LEFT)

        # Row 2: action buttons
        row2 = ttk.Frame(parent)
        row2.pack(fill=tk.X, pady=(8, 0))
        self.start_btn = ttk.Button(
            row2, text="Start Programming", command=self._on_start, state=tk.DISABLED
        )
        self.start_btn.pack(side=tk.LEFT)
        self.clear_btn = ttk.Button(row2, text="Clear Log", command=self._clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=(6, 0))

    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select firmware.bin",
            filetypes=[("Binary firmware", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        self.firmware_path = path
        self.path_var.set(_shorten(path))
        self.start_btn.config(state=tk.NORMAL)

    def _on_start(self) -> None:
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            self._append_log("Please select a valid firmware file first.", "err")
            return

        port = self.app.get_port()
        firmware_path = self.firmware_path

        def work(log_cb):
            return program_firmware(firmware_path, log_cb, port=port)

        if self.submit_work(work):
            self.app.set_status("Programming...", "#a06400")
            self.start_btn.config(state=tk.DISABLED)
            self.browse_btn.config(state=tk.DISABLED)
            self.app.lock_port_entry()

    def _on_done(self, success: bool) -> None:
        if success:
            self._append_log(
                f"{os.path.basename(self.firmware_path or '')} Program Successful.",
                "ok",
            )
            self.app.set_status("Success", "#1f7a1f")
        else:
            self.app.set_status("Error", "#b00020")
        self.start_btn.config(state=tk.NORMAL)
        self.browse_btn.config(state=tk.NORMAL)
        self.app.unlock_port_entry()


# ---------------------------------------------------------------------------
# GPIO 設定 tab — Plan A (single-pin manual test).
# ---------------------------------------------------------------------------
class GpioTab(_LoggedTab):
    def _build_controls(self, parent: ttk.Frame) -> None:
        # Row 1: Pin dropdown
        row1 = ttk.Frame(parent)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Pin:").pack(side=tk.LEFT)
        self.pin_var = tk.StringVar(value=GPIO_PIN_CHOICES[0][0])
        self.pin_combo = ttk.Combobox(
            row1,
            textvariable=self.pin_var,
            values=[label for label, _ in GPIO_PIN_CHOICES],
            state="readonly",
            width=18,
        )
        self.pin_combo.pack(side=tk.LEFT, padx=(6, 0))

        # Row 2: Mode radio
        row2 = ttk.Frame(parent)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="Mode:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="OUTPUT")
        ttk.Radiobutton(
            row2, text="INPUT", variable=self.mode_var, value="INPUT",
            command=self._on_mode_change,
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(
            row2, text="OUTPUT", variable=self.mode_var, value="OUTPUT",
            command=self._on_mode_change,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Row 3: Value radio (only meaningful when mode=OUTPUT)
        row3 = ttk.Frame(parent)
        row3.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row3, text="Value:").pack(side=tk.LEFT)
        self.value_var = tk.StringVar(value="HIGH")
        self.high_btn = ttk.Radiobutton(
            row3, text="HIGH", variable=self.value_var, value="HIGH"
        )
        self.high_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.low_btn = ttk.Radiobutton(
            row3, text="LOW", variable=self.value_var, value="LOW"
        )
        self.low_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Row 4: Action buttons
        row4 = ttk.Frame(parent)
        row4.pack(fill=tk.X, pady=(8, 0))
        self.apply_btn = ttk.Button(row4, text="Apply", command=self._on_apply)
        self.apply_btn.pack(side=tk.LEFT)
        self.read_btn = ttk.Button(row4, text="Read once", command=self._on_read)
        self.read_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.clear_btn = ttk.Button(row4, text="Clear Log", command=self._clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Row 5: Last read display
        row5 = ttk.Frame(parent)
        row5.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row5, text="Last read:").pack(side=tk.LEFT)
        self.last_read_var = tk.StringVar(value="(no read yet)")
        ttk.Label(
            row5, textvariable=self.last_read_var, foreground="#1a1a1a"
        ).pack(side=tk.LEFT, padx=(6, 0))

    def _on_mode_change(self) -> None:
        # Disable HIGH/LOW radios when INPUT (value is meaningless).
        new_state = tk.NORMAL if self.mode_var.get() == "OUTPUT" else tk.DISABLED
        self.high_btn.config(state=new_state)
        self.low_btn.config(state=new_state)

    def _selected_pin(self) -> int:
        label = self.pin_var.get()
        for lbl, pin in GPIO_PIN_CHOICES:
            if lbl == label:
                return pin
        # Should not happen because Combobox is readonly.
        return GPIO_PIN_CHOICES[0][1]

    def _on_apply(self) -> None:
        pin = self._selected_pin()
        mode = self.mode_var.get()
        value = self.value_var.get() if mode == "OUTPUT" else None
        port = self.app.get_port()

        def work(log_cb):
            return gpio_set(pin, mode, value, log_cb, port=port)

        if self.submit_work(work):
            self.app.set_status(f"Setting pin {pin} {mode}...", "#a06400")
            self._lock_controls(True)

    def _on_read(self) -> None:
        pin = self._selected_pin()
        port = self.app.get_port()
        # gpio_read returns "HIGH"/"LOW"/None — wrap so submit_work sees a bool.
        result_holder: dict[str, str | None] = {"value": None}

        def work(log_cb):
            v = gpio_read(pin, log_cb, port=port)
            result_holder["value"] = v
            return v is not None

        if self.submit_work(work):
            self.app.set_status(f"Reading pin {pin}...", "#a06400")
            self._lock_controls(True)
            # Stash the holder so _on_done can pull from it.
            self._pending_read = (pin, result_holder)
        # If submit_work failed (a previous job is still running) keep the
        # previous _pending_read intact so that job's result still updates
        # the "Last read" label on its own _on_done.

    def _on_done(self, success: bool) -> None:
        # If this was a read, surface the value into the "Last read" label.
        if getattr(self, "_pending_read", None) is not None:
            pin, holder = self._pending_read
            v = holder.get("value")
            if v is not None:
                ts = datetime.now().strftime("%H:%M:%S")
                self.last_read_var.set(f"Pin {pin} = {v}   (at {ts})")
            self._pending_read = None

        if success:
            self.app.set_status("OK", "#1f7a1f")
        else:
            self.app.set_status("Error", "#b00020")
        self._lock_controls(False)

    def _lock_controls(self, locked: bool) -> None:
        s = tk.DISABLED if locked else tk.NORMAL
        self.apply_btn.config(state=s)
        self.read_btn.config(state=s)
        self.pin_combo.config(state=tk.DISABLED if locked else "readonly")
        if locked:
            self.app.lock_port_entry()
        else:
            self.app.unlock_port_entry()
            # Re-respect mode radio for value buttons.
            self._on_mode_change()


# ---------------------------------------------------------------------------
# App: top-level container with shared port entry, notebook, status bar.
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)

        self.tabs: list[_LoggedTab] = []
        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        # Shared port row
        port_row = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        port_row.pack(fill=tk.X)
        ttk.Label(port_row, text="Port (optional):").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="")
        self.port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=20)
        self.port_entry.pack(side=tk.LEFT, padx=(6, 6))
        ttk.Label(
            port_row,
            text="(留空 = 自動偵測 Arduino Due Programming Port)",
            foreground="#666666",
        ).pack(side=tk.LEFT)

        # Notebook with two tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 0))

        self.flash_tab = FlashTab(self.notebook, self)
        self.gpio_tab = GpioTab(self.notebook, self)
        self.notebook.add(self.flash_tab.frame, text="燒錄 ROM")
        self.notebook.add(self.gpio_tab.frame, text="GPIO 設定")
        self.notebook.select(self.flash_tab.frame)  # default tab
        self.tabs.extend([self.flash_tab, self.gpio_tab])

        # Status bar at the bottom (shared across tabs).
        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(bottom, text="Status:").pack(side=tk.LEFT)
        self.status_label = ttk.Label(
            bottom, textvariable=self.status_var, foreground="#1a1a1a"
        )
        self.status_label.pack(side=tk.LEFT, padx=(6, 0))

    # ---- shared helpers used by tabs --------------------------------------

    def get_port(self) -> str | None:
        return self.port_var.get().strip() or None

    def lock_port_entry(self) -> None:
        self.port_entry.config(state=tk.DISABLED)

    def unlock_port_entry(self) -> None:
        # Only unlock if no other tab is busy.
        if not self.any_tab_busy():
            self.port_entry.config(state=tk.NORMAL)

    def set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.config(foreground=color)

    def any_tab_busy(self) -> bool:
        return any(t.is_busy() for t in self.tabs)

    def _on_close(self) -> None:
        if self.any_tab_busy():
            messagebox.showinfo(
                "Busy",
                "An operation is in progress. Please wait for it to finish "
                "before closing.",
            )
            return
        self.root.destroy()


def _shorten(path: str, max_len: int = 70) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
