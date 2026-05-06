from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# pyserial and binFileTransfer_core are imported lazily inside the methods
# that need them. Both pull in Win32 COM enumeration code that's slow to
# import cold, and deferring keeps the Tk window visible within ~1 s of
# launch instead of waiting for those imports to finish first.


APP_TITLE = "SST39 Flash Programmer"
WINDOW_SIZE = "900x780"

# Port dropdown — first option is the catch-all auto-detect.
AUTO_DETECT_LABEL = "Auto-detect (Arduino Due Programming Port)"
DUE_TARGET_VID = 0x2341
DUE_TARGET_PID = 0x003D

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


# Pins consumed by the parallel-flash bit-bang in binFileProgram.ino. Listed
# here for cross-reference / documentation only — the GPIO panel does NOT
# gate on these; every Due GPIO is exposed below.
FLASH_ADDRESS_PINS = [44, 42, 40, 38, 36, 34, 32, 30, 33, 35,
                      41, 37, 28, 31, 29, 26, 24, 27, 22]   # A0..A18
FLASH_DATA_PINS    = [46, 48, 50, 53, 51, 49, 47, 45]       # DQ0..DQ7
FLASH_CE_PIN = 43
FLASH_OE_PIN = 39
FLASH_WE_PIN = 25

LED_PIN = 13   # on-board LED, also Arduino's LED_BUILTIN


# Pin layout for the GPIO tab. Flat list of every controllable Due GPIO,
# numeric order. D0–D53 are the digital pins; D54–D65 are A0–A11.
# Each entry: (display_label, due_pin_number).
GPIO_PINS: list[tuple[str, int]] = [(f"D{n}", n) for n in range(0, 66)]


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
            title="Select firmware file",
            filetypes=[("All files", "*.*"), ("Binary firmware", "*.bin")],
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

        # Disable immediately so a double-click can't fire two disconnects /
        # two flashes.
        self.start_btn.config(state=tk.DISABLED)
        self.browse_btn.config(state=tk.DISABLED)

        # If any other tab holds the serial port, close it first then proceed
        # with flashing. The release_port_then(callback) call chains through
        # GPIO and TDBG tabs sequentially.
        self.app.release_port_then(except_tab=self, on_done=self._do_start)

    def _do_start(self) -> None:
        port = self.app.get_port()
        firmware_path = self.firmware_path

        def work(log_cb):
            from binFileTransfer_core import program_firmware
            return program_firmware(firmware_path, log_cb, port=port)

        if self.submit_work(work):
            self.app.set_status("Programming...", "#a06400")
            self.app.lock_port_entry()
        else:
            # Worker refused (already busy). Re-enable buttons so user can retry.
            self.start_btn.config(state=tk.NORMAL)
            self.browse_btn.config(state=tk.NORMAL)

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
class _PinRow:
    """One row of the GPIO dashboard: label, mode, HIGH/LOW radios, read display.
    Constructed disabled; call set_enabled(True) when the GPIO session opens.
    Click handlers fire `on_set(pin, mode, value_or_None)` — the GpioTab is
    expected to enqueue the actual GPIO_SET round-trip on its worker thread.
    """

    def __init__(
        self,
        parent: ttk.Frame,
        label: str,
        pin: int,
        on_set,
    ) -> None:
        self.pin = pin
        self.on_set = on_set
        self._suppress_callbacks = False  # used while we programmatically set vars

        self.frame = ttk.Frame(parent)
        ttk.Label(self.frame, text=label, width=12, anchor="w").grid(
            row=0, column=0, sticky="w"
        )

        self.mode_var = tk.StringVar(value="INPUT")
        self.mode_combo = ttk.Combobox(
            self.frame,
            textvariable=self.mode_var,
            values=["OUTPUT", "INPUT"],
            state="disabled",
            width=8,
        )
        self.mode_combo.grid(row=0, column=1, padx=(6, 12))
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)

        self.value_var = tk.StringVar(value="")
        self.high_radio = ttk.Radiobutton(
            self.frame, text="HIGH", variable=self.value_var,
            value="HIGH", state="disabled", command=self._on_value_changed,
        )
        self.high_radio.grid(row=0, column=2, padx=(0, 4))
        self.low_radio = ttk.Radiobutton(
            self.frame, text="LOW", variable=self.value_var,
            value="LOW", state="disabled", command=self._on_value_changed,
        )
        self.low_radio.grid(row=0, column=3, padx=(0, 18))

        ttk.Label(self.frame, text="Read:").grid(row=0, column=4)
        self.read_var = tk.StringVar(value="??")
        self.read_label = ttk.Label(
            self.frame, textvariable=self.read_var,
            width=6, foreground="#666666", anchor="w",
        )
        self.read_label.grid(row=0, column=5, padx=(4, 0))

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.mode_combo.config(state="disabled")
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")
            return
        self.mode_combo.config(state="readonly")
        if self.mode_var.get() == "OUTPUT":
            self.high_radio.config(state="normal")
            self.low_radio.config(state="normal")
        else:
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")

    def _on_mode_changed(self, _event=None) -> None:
        if self._suppress_callbacks:
            return
        mode = self.mode_var.get()
        if mode == "OUTPUT":
            self.high_radio.config(state="normal")
            self.low_radio.config(state="normal")
        else:
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")
            # Clear stale value selection so radios visually match disabled state.
            self._suppress_callbacks = True
            self.value_var.set("")
            self._suppress_callbacks = False
        # Tell controller — value=None means "just switch mode, don't drive".
        self.on_set(self.pin, mode, None)

    def _on_value_changed(self) -> None:
        if self._suppress_callbacks:
            return
        value = self.value_var.get()
        if value in ("HIGH", "LOW"):
            self.on_set(self.pin, "OUTPUT", value)

    def set_read_value(self, value: str) -> None:
        """Update the right-most "Read:" cell. Coloured for readability."""
        self.read_var.set(value)
        self.read_label.config(
            foreground="#1f7a1f" if value == "HIGH" else "#1a1a1a"
        )


class GpioTab(_LoggedTab):
    """Plan B: persistent connection + every-pin-visible dashboard."""

    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        # Initialise persistent-worker state before super().__init__ runs
        # _build_controls (which references some of these).
        self._session: "GpioSession | None" = None
        self._cmd_queue: queue.Queue = queue.Queue()
        self._busy = False
        self._auto_refresh_after_id: str | None = None
        self._pin_rows: dict[int, _PinRow] = {}
        # Pin panel is heavy (66 rows × ~5 widgets) and most users start on
        # the Flash tab, so we defer construction until the GPIO tab is first
        # shown. App._on_tab_changed triggers _ensure_pin_panel_built().
        self._pin_panel_parent: ttk.Frame | None = None
        self._pin_panel_built = False
        # Set on disconnect so an in-flight Read All loop bails out between
        # pins instead of running all 66 × 5 s timeouts to completion.
        self._abort_event = threading.Event()
        super().__init__(parent, app)
        # Persistent worker thread that drains _cmd_queue forever.
        self._worker_thread = threading.Thread(target=self._cmd_loop, daemon=True)
        self._worker_thread.start()

    # ---- _LoggedTab overrides ---------------------------------------------

    def is_busy(self) -> bool:
        # The persistent thread is always alive; "busy" means a command is
        # actively running. Auto-refresh and per-click sets all flow through.
        return self._busy

    def submit_work(self, target_callable) -> bool:
        # GpioTab does not use submit_work — everything goes via _cmd_queue.
        # Defensive override: refuse so the parent's spawn-a-thread path
        # never gets used here.
        raise RuntimeError("GpioTab uses _cmd_queue, not submit_work")

    def _build_controls(self, parent: ttk.Frame) -> None:
        # Row 1: Connection status + Connect / Disconnect
        conn_row = ttk.Frame(parent)
        conn_row.pack(fill=tk.X)
        ttk.Label(conn_row, text="Connection:").pack(side=tk.LEFT)
        self._conn_status_var = tk.StringVar(value="Disconnected")
        self._conn_status_label = ttk.Label(
            conn_row, textvariable=self._conn_status_var, foreground="#b00020"
        )
        self._conn_status_label.pack(side=tk.LEFT, padx=(6, 12))
        self._connect_btn = ttk.Button(
            conn_row, text="Connect", command=self._on_connect
        )
        self._connect_btn.pack(side=tk.LEFT)
        self._disconnect_btn = ttk.Button(
            conn_row, text="Disconnect",
            command=self._on_disconnect, state=tk.DISABLED,
        )
        self._disconnect_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Row 2: Read All + Auto-refresh
        action_row = ttk.Frame(parent)
        action_row.pack(fill=tk.X, pady=(8, 0))
        self._read_all_btn = ttk.Button(
            action_row, text="Read All",
            command=self._on_read_all, state=tk.DISABLED,
        )
        self._read_all_btn.pack(side=tk.LEFT)

        self._auto_refresh_var = tk.BooleanVar(value=False)
        self._auto_refresh_check = ttk.Checkbutton(
            action_row, text="Auto-refresh every",
            variable=self._auto_refresh_var,
            command=self._on_toggle_auto_refresh, state=tk.DISABLED,
        )
        self._auto_refresh_check.pack(side=tk.LEFT, padx=(16, 0))

        self._auto_refresh_interval_var = tk.StringVar(value="1.0")
        self._interval_spin = ttk.Spinbox(
            action_row, from_=0.2, to=60.0, increment=0.5,
            textvariable=self._auto_refresh_interval_var, width=5,
        )
        self._interval_spin.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(action_row, text="s").pack(side=tk.LEFT, padx=(2, 12))

        self._clear_log_btn = ttk.Button(
            action_row, text="Clear Log", command=self._clear_log
        )
        self._clear_log_btn.pack(side=tk.LEFT)

        # Row 3+: pin panel — deferred. Stored parent is used by
        # _ensure_pin_panel_built() the first time the user selects this tab.
        self._pin_panel_parent = parent

    def _ensure_pin_panel_built(self) -> None:
        """Lazy-build the 66-pin dashboard on first GPIO-tab activation."""
        if self._pin_panel_built or self._pin_panel_parent is None:
            return
        self._pin_panel_built = True
        self._build_pin_panel(self._pin_panel_parent)
        # If the session was somehow opened before the panel was built (it
        # currently can't happen via the UI, but be defensive), reflect it.
        if self._session is not None:
            for row in self._pin_rows.values():
                row.set_enabled(True)

    def _build_pin_panel(self, parent: ttk.Frame) -> None:
        # Container with a Canvas that hosts an inner Frame; scrollable
        # vertically. Cross-platform mousewheel handling included.
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        canvas = tk.Canvas(wrap, highlightthickness=0, height=420)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(inner_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mousewheel scroll while pointer is over the panel.
        def _on_mousewheel(event):
            # Windows / macOS: event.delta in multiples of 120
            # Linux: <Button-4>/<Button-5> handled separately below
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(_e=None):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        def _unbind_wheel(_e=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        for label, pin in GPIO_PINS:
            row = _PinRow(inner, label, pin, on_set=self._on_pin_set)
            row.frame.pack(fill=tk.X, anchor="w", padx=(8, 0))
            self._pin_rows[pin] = row

    def _build_log_area(self) -> None:
        # Compact log (3-row visible height) — operations are fast so a big
        # log eats too much real estate from the pin panel.
        log_frame = ttk.Frame(self.frame, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.X)
        self.log_text = tk.Text(
            log_frame, wrap=tk.NONE, state=tk.DISABLED,
            font=("Consolas", 9), height=4,
        )
        scroll_y = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        for tag, color in LEVEL_TAGS.values():
            self.log_text.tag_configure(tag, foreground=color)

    # ---- queue / worker ---------------------------------------------------

    def _enqueue(self, cmd) -> None:
        """Schedule `cmd()` (callable taking no args) on the persistent worker."""
        self._cmd_queue.put(cmd)

    def _cmd_loop(self) -> None:
        while True:
            cmd = self._cmd_queue.get()
            if cmd is None:
                break
            self._busy = True
            try:
                cmd()
            except Exception as e:
                self._log_callback_threadsafe(f"GPIO cmd error: {e}", "err")
            finally:
                self._busy = False

    # ---- connection handling ---------------------------------------------

    def is_connected(self) -> bool:
        return self._session is not None

    def _set_conn_status(self, text: str, color: str) -> None:
        self._conn_status_var.set(text)
        self._conn_status_label.config(foreground=color)

    def _on_connect(self) -> None:
        if self._session is not None:
            return
        if self.app.any_other_tab_holding_port(self):
            messagebox.showinfo(
                "Busy",
                "Another tab is holding the serial port. Disconnect it first.",
            )
            return
        if self.app.any_tab_busy():
            messagebox.showinfo(
                "Busy",
                "Another tab has an operation in progress. Please wait.",
            )
            return
        port = self.app.get_port()
        self._set_conn_status("Connecting...", "#a06400")
        self._connect_btn.config(state=tk.DISABLED)
        self.app.lock_port_entry()
        self.app.set_status("GPIO connecting...", "#a06400")

        def cmd():
            from binFileTransfer_core import GpioSession
            session = GpioSession(self._log_callback_threadsafe, port=port)
            success = session.open()
            self.app.root.after(
                0,
                lambda: self._on_connect_done(session if success else None),
            )

        self._enqueue(cmd)

    def _on_connect_done(self, session: GpioSession | None) -> None:
        if session is None:
            self._set_conn_status("Disconnected", "#b00020")
            self._connect_btn.config(state=tk.NORMAL)
            self.app.unlock_port_entry()
            self.app.set_status("GPIO connect failed", "#b00020")
            return

        self._session = session
        self._abort_event.clear()
        self._set_conn_status("Connected", "#1f7a1f")
        self._disconnect_btn.config(state=tk.NORMAL)
        self._read_all_btn.config(state=tk.NORMAL)
        self._auto_refresh_check.config(state=tk.NORMAL)
        for row in self._pin_rows.values():
            row.set_enabled(True)
        self.app.set_status("GPIO Connected", "#1f7a1f")

    def _on_disconnect(self) -> None:
        if self._session is None:
            return
        self._begin_disconnect()
        # Plain disconnect — no chained callback.
        self._enqueue(self._do_close_session)

    def _begin_disconnect(self) -> None:
        # Stop auto-refresh before tearing down the connection.
        if self._auto_refresh_after_id is not None:
            try:
                self.app.root.after_cancel(self._auto_refresh_after_id)
            except Exception:
                pass
            self._auto_refresh_after_id = None
        self._auto_refresh_var.set(False)
        # Signal any in-flight Read All loop to bail between pins, and drop
        # any commands still queued behind it so we don't sit through 66 ×
        # serial timeouts before the close runs.
        self._abort_event.set()
        try:
            while True:
                self._cmd_queue.get_nowait()
        except queue.Empty:
            pass
        # Disable everything that needs the session.
        self._disconnect_btn.config(state=tk.DISABLED)
        self._read_all_btn.config(state=tk.DISABLED)
        self._auto_refresh_check.config(state=tk.DISABLED)
        for row in self._pin_rows.values():
            row.set_enabled(False)
        self._set_conn_status("Disconnecting...", "#a06400")

    def _do_close_session(self) -> None:
        if self._session is not None:
            self._session.close()
        self.app.root.after(0, self._on_disconnect_done)

    def _on_disconnect_done(self) -> None:
        self._session = None
        self._set_conn_status("Disconnected", "#b00020")
        self._connect_btn.config(state=tk.NORMAL)
        self.app.unlock_port_entry()
        self.app.set_status("GPIO Disconnected", "#666666")

    def disconnect_for_other(self, on_done) -> None:
        """Close the GPIO session (if open) then call on_done() on the Tk
        thread. Used by FlashTab / TdbgTab so they can take over the serial
        port without the user manually clicking Disconnect first."""
        if self._session is None:
            on_done()
            return
        self._begin_disconnect()

        def cmd():
            self._do_close_session()
            self.app.root.after(0, on_done)

        # Replace the simple close with the chained variant.
        self._enqueue(cmd)

    # ---- per-pin set / read all ------------------------------------------

    def _on_pin_set(self, pin: int, mode: str, value: str | None) -> None:
        if self._session is None:
            return

        def cmd():
            success = self._session.set_pin(pin, mode, value)
            if success:
                if value is not None:
                    # OUTPUT-driven pin reads back what we wrote (no need
                    # for an extra GPIO_READ round-trip).
                    self.app.root.after(
                        0, lambda: self._pin_rows[pin].set_read_value(value)
                    )
                else:
                    # Mode change without a value — refresh the read so the
                    # display reflects the new physical state.
                    v = self._session.read_pin(pin)
                    if v is not None:
                        self.app.root.after(
                            0, lambda vv=v: self._pin_rows[pin].set_read_value(vv)
                        )

        self._enqueue(cmd)

    def _on_read_all(self) -> None:
        if self._session is None:
            return

        def cmd():
            for pin, _row in self._pin_rows.items():
                if self._abort_event.is_set():
                    break
                v = self._session.read_pin(pin) if self._session is not None else None
                if v is not None:
                    self.app.root.after(
                        0,
                        lambda pp=pin, vv=v: self._pin_rows[pp].set_read_value(vv),
                    )

        self._enqueue(cmd)

    # ---- auto-refresh -----------------------------------------------------

    def _on_toggle_auto_refresh(self) -> None:
        if self._auto_refresh_var.get() and self._session is not None:
            self._schedule_auto_refresh()
        else:
            if self._auto_refresh_after_id is not None:
                try:
                    self.app.root.after_cancel(self._auto_refresh_after_id)
                except Exception:
                    pass
                self._auto_refresh_after_id = None

    def _schedule_auto_refresh(self) -> None:
        try:
            interval_ms = int(float(self._auto_refresh_interval_var.get()) * 1000)
        except (ValueError, TypeError):
            interval_ms = 1000
        interval_ms = max(200, min(60000, interval_ms))
        self._auto_refresh_after_id = self.app.root.after(
            interval_ms, self._auto_refresh_tick
        )

    def _auto_refresh_tick(self) -> None:
        if not self._auto_refresh_var.get() or self._session is None:
            self._auto_refresh_after_id = None
            return
        # Don't pile up: skip the tick if the worker is already chewing on
        # something. The next tick will catch up.
        if not self._busy:
            self._on_read_all()
        self._schedule_auto_refresh()


# ---------------------------------------------------------------------------
# TDBG tab — replays a single hard-coded waveform on a chosen Due GPIO with
# cycle-accurate timing. The pattern is embedded in this module (see
# _TDBG_BUILTIN_TXT below) and parsed lazily on first Send.
# ---------------------------------------------------------------------------

# Annotate flash-bus pins so the user knows which selections will disturb
# the parallel-flash idle state. Selection is still allowed.
def _tdbg_pin_annotation(pin: int) -> str:
    if pin in FLASH_ADDRESS_PINS:
        return f" (A{FLASH_ADDRESS_PINS.index(pin)})"
    if pin in FLASH_DATA_PINS:
        return f" (DQ{FLASH_DATA_PINS.index(pin)})"
    if pin == FLASH_CE_PIN:
        return " (CE#)"
    if pin == FLASH_OE_PIN:
        return " (OE#)"
    if pin == FLASH_WE_PIN:
        return " (WE#)"
    if pin == LED_PIN:
        return " (LED)"
    return ""


TDBG_PIN_LABELS = [f"D{n}{_tdbg_pin_annotation(n)}" for n in range(0, 66)]


# Built-in waveform — captured by the user from their target system. Playback
# is 1340 ms total (3 transition clusters separated by ~670 ms gaps), 336
# transitions, minimum pulse width 381 ns. To replace, paste a new Acute
# .txt export below; parse runs at first Send so a malformed pattern shows
# up as a log error rather than blocking GUI startup.
_TDBG_BUILTIN_TXT = """\
Timestamp,CH-00
-40000,1
583175170000,0
583175555000,1
583175970000,0
583176355000,1
583176765000,0
583177155000,1
583177570000,0
583177950000,1
583178365000,0
583178755000,1
583179170000,0
583179555000,1
583179970000,0
583180360000,1
583180775000,0
583181155000,1
583181570000,0
583181955000,1
583182370000,0
583182755000,1
583183170000,0
583183555000,1
583183970000,0
583184360000,1
583184770000,0
583185155000,1
583185570000,0
583185960000,1
583186375000,0
583186760000,1
583187175000,0
583187560000,1
583187975000,0
583188365000,1
583188775000,0
583189160000,1
583189570000,0
583189960000,1
583190375000,0
583190760000,1
583191175000,0
583191555000,1
583191970000,0
583192360000,1
583192775000,0
583193160000,1
583193570000,0
583193955000,1
583194370000,0
583194755000,1
583195170000,0
583195555000,1
583195965000,0
583196355000,1
583196765000,0
583197150000,1
583197565000,0
583197955000,1
583198370000,0
583198755000,1
583199170000,0
583199560000,1
583199975000,0
583200360000,1
583200770000,0
583201545000,1
583202360000,0
583202745000,1
583203555000,0
583203945000,1
583204360000,0
583204750000,1
583205560000,0
583205945000,1
583206360000,0
583206750000,1
583207560000,0
583207950000,1
583208360000,0
583208745000,1
583209960000,0
583210345000,1
583211160000,0
583211545000,1
583211960000,0
583212345000,1
583213555000,0
583213945000,1
583214360000,0
583214745000,1
583215960000,0
583216345000,1
583217155000,0
583217545000,1
583217960000,0
583218345000,1
583219160000,0
583219545000,1
583219960000,0
583220350000,1
583221165000,0
583221550000,1
583221960000,0
583222345000,1
583223160000,0
583223550000,1
583225160000,0
583225545000,1
583225960000,0
583226345000,1
583227155000,0
583227545000,1
1253458640000,0
1253459025000,1
1253459435000,0
1253459820000,1
1253460235000,0
1253460625000,1
1253461035000,0
1253461425000,1
1253461840000,0
1253462225000,1
1253462640000,0
1253463025000,1
1253463435000,0
1253463820000,1
1253464235000,0
1253464620000,1
1253465035000,0
1253465425000,1
1253465835000,0
1253466220000,1
1253466635000,0
1253467020000,1
1253467430000,0
1253467820000,1
1253468235000,0
1253468620000,1
1253469030000,0
1253469420000,1
1253469835000,0
1253470220000,1
1253470635000,0
1253471020000,1
1253471430000,0
1253471820000,1
1253472235000,0
1253472625000,1
1253473035000,0
1253473420000,1
1253473835000,0
1253474225000,1
1253474640000,0
1253475025000,1
1253475440000,0
1253475825000,1
1253476240000,0
1253476625000,1
1253477040000,0
1253477425000,1
1253477840000,0
1253478225000,1
1253478640000,0
1253479025000,1
1253479435000,0
1253479825000,1
1253480240000,0
1253480625000,1
1253481040000,0
1253481425000,1
1253481840000,0
1253482225000,1
1253482640000,0
1253483025000,1
1253483440000,0
1253483830000,1
1253484245000,0
1253485020000,1
1253485830000,0
1253486220000,1
1253487030000,0
1253487415000,1
1253487830000,0
1253488215000,1
1253489025000,0
1253489415000,1
1253489830000,0
1253490220000,1
1253491030000,0
1253491420000,1
1253491835000,0
1253492220000,1
1253493435000,0
1253493825000,1
1253494635000,0
1253495020000,1
1253495435000,0
1253495825000,1
1253497035000,0
1253497425000,1
1253497840000,0
1253498225000,1
1253499440000,0
1253499825000,1
1253500640000,0
1253501025000,1
1253501435000,0
1253501825000,1
1253502640000,0
1253503025000,1
1253503440000,0
1253503830000,1
1253504640000,0
1253505025000,1
1253505440000,0
1253505830000,1
1253506640000,0
1253507025000,1
1253508640000,0
1253509030000,1
1253509440000,0
1253509825000,1
1253510640000,0
1253511030000,1
1923775290000,0
1923775680000,1
1923776090000,0
1923776480000,1
1923776895000,0
1923777285000,1
1923777695000,0
1923778085000,1
1923778500000,0
1923778885000,1
1923779300000,0
1923779685000,1
1923780095000,0
1923780485000,1
1923780900000,0
1923781285000,1
1923781700000,0
1923782085000,1
1923782495000,0
1923782885000,1
1923783300000,0
1923783685000,1
1923784100000,0
1923784490000,1
1923784895000,0
1923785285000,1
1923785700000,0
1923786085000,1
1923786500000,0
1923786885000,1
1923787295000,0
1923787685000,1
1923788100000,0
1923788485000,1
1923788900000,0
1923789285000,1
1923789695000,0
1923790085000,1
1923790500000,0
1923790890000,1
1923791300000,0
1923791685000,1
1923792100000,0
1923792490000,1
1923792900000,0
1923793290000,1
1923793700000,0
1923794090000,1
1923794505000,0
1923794890000,1
1923795305000,0
1923795690000,1
1923796105000,0
1923796495000,1
1923796905000,0
1923797290000,1
1923797705000,0
1923798095000,1
1923798510000,0
1923798895000,1
1923799305000,0
1923799695000,1
1923800110000,0
1923800495000,1
1923800910000,0
1923801685000,1
1923802500000,0
1923802885000,1
1923803700000,0
1923804085000,1
1923804500000,0
1923804885000,1
1923805695000,0
1923806080000,1
1923806495000,0
1923806885000,1
1923807700000,0
1923808090000,1
1923808500000,0
1923808885000,1
1923810105000,0
1923810490000,1
1923811300000,0
1923811690000,1
1923812100000,0
1923812490000,1
1923813705000,0
1923814090000,1
1923814500000,0
1923814885000,1
1923816100000,0
1923816490000,1
1923817305000,0
1923817685000,1
1923818100000,0
1923818490000,1
1923819300000,0
1923819690000,1
1923820105000,0
1923820490000,1
1923821300000,0
1923821685000,1
1923822100000,0
1923822485000,1
1923823300000,0
1923823690000,1
1923825305000,0
1923825690000,1
1923826105000,0
1923826490000,1
1923827305000,0
1923827695000,1
"""

# Cached parse result. Lazy-populated on first _on_send to keep cold-start
# unaffected by the pattern parse (and the core.py / pyserial import it
# pulls in).
_BUILTIN_INITIAL_STATE: int | None = None
_BUILTIN_EVENTS: list[tuple[int, int]] | None = None


def _ensure_builtin_parsed() -> tuple[int, list[tuple[int, int]]]:
    global _BUILTIN_INITIAL_STATE, _BUILTIN_EVENTS
    if _BUILTIN_EVENTS is None:
        from binFileTransfer_core import parse_acute_txt
        _BUILTIN_INITIAL_STATE, _BUILTIN_EVENTS = parse_acute_txt(_TDBG_BUILTIN_TXT)
    return _BUILTIN_INITIAL_STATE, _BUILTIN_EVENTS


class TdbgTab(_LoggedTab):
    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        # Persistent worker thread — same pattern as GpioTab.
        self._session = None
        self._cmd_queue: queue.Queue = queue.Queue()
        self._busy = False
        super().__init__(parent, app)
        self._worker_thread = threading.Thread(target=self._cmd_loop, daemon=True)
        self._worker_thread.start()

    # ---- _LoggedTab overrides ---------------------------------------------

    def is_busy(self) -> bool:
        return self._busy

    def submit_work(self, target_callable) -> bool:
        raise RuntimeError("TdbgTab uses _cmd_queue, not submit_work")

    def _build_controls(self, parent: ttk.Frame) -> None:
        # Row 1: Connection status + Connect / Disconnect
        conn_row = ttk.Frame(parent)
        conn_row.pack(fill=tk.X)
        ttk.Label(conn_row, text="Connection:").pack(side=tk.LEFT)
        self._conn_status_var = tk.StringVar(value="Disconnected")
        self._conn_status_label = ttk.Label(
            conn_row, textvariable=self._conn_status_var, foreground="#b00020"
        )
        self._conn_status_label.pack(side=tk.LEFT, padx=(6, 12))
        self._connect_btn = ttk.Button(
            conn_row, text="Connect", command=self._on_connect
        )
        self._connect_btn.pack(side=tk.LEFT)
        self._disconnect_btn = ttk.Button(
            conn_row, text="Disconnect",
            command=self._on_disconnect, state=tk.DISABLED,
        )
        self._disconnect_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Row 2: Pin + Send + Clear Log
        send_row = ttk.Frame(parent)
        send_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(send_row, text="Pin:").pack(side=tk.LEFT)
        self._pin_var = tk.StringVar(value="")
        self._pin_combo = ttk.Combobox(
            send_row, textvariable=self._pin_var,
            values=TDBG_PIN_LABELS, state="disabled", width=14,
        )
        self._pin_combo.pack(side=tk.LEFT, padx=(6, 12))
        self._send_btn = ttk.Button(
            send_row, text="Send", command=self._on_send, state=tk.DISABLED,
        )
        self._send_btn.pack(side=tk.LEFT)
        self._clear_log_btn = ttk.Button(
            send_row, text="Clear Log", command=self._clear_log
        )
        self._clear_log_btn.pack(side=tk.LEFT, padx=(12, 0))

    # ---- queue / worker ---------------------------------------------------

    def _enqueue(self, cmd) -> None:
        self._cmd_queue.put(cmd)

    def _cmd_loop(self) -> None:
        while True:
            cmd = self._cmd_queue.get()
            if cmd is None:
                break
            self._busy = True
            try:
                cmd()
            except Exception as e:
                self._log_callback_threadsafe(f"TDBG cmd error: {e}", "err")
            finally:
                self._busy = False

    # ---- connection -------------------------------------------------------

    def is_connected(self) -> bool:
        return self._session is not None

    def _set_conn_status(self, text: str, color: str) -> None:
        self._conn_status_var.set(text)
        self._conn_status_label.config(foreground=color)

    def _on_connect(self) -> None:
        if self._session is not None:
            return
        if self.app.any_other_tab_holding_port(self):
            messagebox.showinfo(
                "Busy",
                "Another tab is holding the serial port. Disconnect it first.",
            )
            return
        if self.app.any_tab_busy():
            messagebox.showinfo(
                "Busy",
                "Another tab has an operation in progress. Please wait.",
            )
            return
        port = self.app.get_port()
        self._set_conn_status("Connecting...", "#a06400")
        self._connect_btn.config(state=tk.DISABLED)
        self.app.lock_port_entry()
        self.app.set_status("TDBG connecting...", "#a06400")

        def cmd():
            from binFileTransfer_core import TdbgSession
            session = TdbgSession(self._log_callback_threadsafe, port=port)
            success = session.open()
            self.app.root.after(
                0,
                lambda: self._on_connect_done(session if success else None),
            )

        self._enqueue(cmd)

    def _on_connect_done(self, session) -> None:
        if session is None:
            self._set_conn_status("Disconnected", "#b00020")
            self._connect_btn.config(state=tk.NORMAL)
            self.app.unlock_port_entry()
            self.app.set_status("TDBG connect failed", "#b00020")
            return
        self._session = session
        self._set_conn_status("Connected", "#1f7a1f")
        self._disconnect_btn.config(state=tk.NORMAL)
        self._pin_combo.config(state="readonly")
        self._send_btn.config(state=tk.NORMAL)
        self.app.set_status("TDBG Connected", "#1f7a1f")

    def _on_disconnect(self) -> None:
        if self._session is None:
            return
        self._begin_disconnect()
        self._enqueue(self._do_close_session)

    def _begin_disconnect(self) -> None:
        self._disconnect_btn.config(state=tk.DISABLED)
        self._send_btn.config(state=tk.DISABLED)
        self._pin_combo.config(state="disabled")
        self._set_conn_status("Disconnecting...", "#a06400")

    def _do_close_session(self) -> None:
        if self._session is not None:
            self._session.close()
        self.app.root.after(0, self._on_disconnect_done)

    def _on_disconnect_done(self) -> None:
        self._session = None
        self._set_conn_status("Disconnected", "#b00020")
        self._connect_btn.config(state=tk.NORMAL)
        self.app.unlock_port_entry()
        self.app.set_status("TDBG Disconnected", "#666666")

    def disconnect_for_other(self, on_done) -> None:
        """Close the TDBG session (if open) then call on_done() on Tk thread.
        Used by FlashTab and GpioTab when they need to take the port."""
        if self._session is None:
            on_done()
            return
        self._begin_disconnect()

        def cmd():
            self._do_close_session()
            self.app.root.after(0, on_done)

        self._enqueue(cmd)

    # ---- send -------------------------------------------------------------

    def _selected_pin(self) -> int | None:
        label = self._pin_var.get()
        if not label:
            return None
        # Labels look like "D13 (LED)" or "D14"
        try:
            return int(label.split(" ", 1)[0].lstrip("D"))
        except ValueError:
            return None

    def _on_send(self) -> None:
        if self._session is None:
            return
        pin = self._selected_pin()
        if pin is None:
            messagebox.showinfo("Pin", "Select an output pin first.")
            return
        try:
            initial, events = _ensure_builtin_parsed()
        except ValueError as e:
            self._append_log(f"builtin pattern parse error: {e}", "err")
            return
        from binFileTransfer_core import DUE_CPU_HZ
        duration_s = sum(d for d, _ in events) / DUE_CPU_HZ

        self._send_btn.config(state=tk.DISABLED)
        self._pin_combo.config(state="disabled")
        self._disconnect_btn.config(state=tk.DISABLED)
        self.app.set_status("TDBG playing...", "#a06400")

        def cmd():
            sess = self._session
            if sess is None:
                return
            ok = sess.load(pin=pin, initial_state=initial, events=events)
            if ok:
                ok = sess.play(iterations=1, total_duration_s=duration_s)
            self.app.root.after(0, lambda: self._on_send_done(ok))

        self._enqueue(cmd)

    def _on_send_done(self, success: bool) -> None:
        if self._session is not None:
            self._send_btn.config(state=tk.NORMAL)
            self._pin_combo.config(state="readonly")
            self._disconnect_btn.config(state=tk.NORMAL)
        self.app.set_status(
            "TDBG done" if success else "TDBG error",
            "#1f7a1f" if success else "#b00020",
        )


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
        # Populate port dropdown right after widgets exist, default-selects
        # the Due Programming Port if one is plugged in at startup.
        self._refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        # Shared port row — dropdown auto-scanned at startup.
        port_row = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        port_row.pack(fill=tk.X)
        ttk.Label(port_row, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=AUTO_DETECT_LABEL)
        self.port_combo = ttk.Combobox(
            port_row, textvariable=self.port_var,
            values=[AUTO_DETECT_LABEL],
            state="readonly", width=55,
        )
        self.port_combo.pack(side=tk.LEFT, padx=(6, 6))
        self.port_refresh_btn = ttk.Button(
            port_row, text="↻ Refresh", command=self._refresh_ports,
        )
        self.port_refresh_btn.pack(side=tk.LEFT)
        # Initial scan happens after notebook is built so any error logs
        # have somewhere to go (we keep this simple and silent for now).

        # Notebook with two tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 0))

        self.flash_tab = FlashTab(self.notebook, self)
        self.gpio_tab = GpioTab(self.notebook, self)
        self.tdbg_tab = TdbgTab(self.notebook, self)
        self.notebook.add(self.flash_tab.frame, text="燒錄 ROM")
        self.notebook.add(self.gpio_tab.frame, text="GPIO 設定")
        self.notebook.add(self.tdbg_tab.frame, text="TDBG")
        self.notebook.select(self.flash_tab.frame)  # default tab
        self.tabs.extend([self.flash_tab, self.gpio_tab, self.tdbg_tab])
        # Defer GPIO pin panel construction until that tab is first shown.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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
        """Return the user-selected port string, or None for auto-detect."""
        label = self.port_var.get()
        if label == AUTO_DETECT_LABEL or not label:
            return None
        # Dropdown labels look like "COM4 — Arduino Due (...)".
        # Pull the device name in front of " — " separator.
        return label.split(" — ", 1)[0]

    def _scan_ports(self) -> tuple[list[str], str]:
        """(dropdown_values, default_label).

        Always starts with AUTO_DETECT_LABEL. Adds every comports() entry as
        "<device> — <description>". If a Due Programming Port (VID/PID match)
        is found, default_label points at it; otherwise default is auto-detect.

        Imports pyserial lazily — on Windows comports() pulls in WMI / SetupAPI
        enumeration, which we don't want blocking the main thread at startup.
        """
        import serial.tools.list_ports
        values: list[str] = [AUTO_DETECT_LABEL]
        default = AUTO_DETECT_LABEL
        for p in serial.tools.list_ports.comports():
            label = f"{p.device} — {p.description or 'unknown'}"
            values.append(label)
            if (
                default == AUTO_DETECT_LABEL
                and p.vid == DUE_TARGET_VID
                and p.pid == DUE_TARGET_PID
            ):
                default = label
        return values, default

    def _refresh_ports(self) -> None:
        """Kick off a background port scan; UI stays responsive in the meantime.

        Disables the Refresh button while scanning so a user can't fire two
        scans in parallel. The result is applied back on the Tk thread via
        root.after(0, ...). Called once at startup and on every Refresh click.
        """
        try:
            self.port_refresh_btn.config(state=tk.DISABLED)
        except (AttributeError, tk.TclError):
            pass

        def worker() -> None:
            try:
                values, default = self._scan_ports()
            except Exception as e:  # never let a scan error kill the UI
                self.root.after(0, lambda: self._apply_port_scan_error(e))
                return
            self.root.after(0, lambda: self._apply_port_scan(values, default))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_port_scan(self, values: list[str], default: str) -> None:
        self.port_combo["values"] = values
        # Keep the current selection if it still exists; otherwise reset.
        if self.port_var.get() not in values:
            self.port_var.set(default)
        # Only re-enable Refresh if we're not in a state that locked the port
        # entry for other reasons (busy worker, GPIO connected).
        self._maybe_unlock_refresh()

    def _apply_port_scan_error(self, exc: Exception) -> None:
        # Surface the failure but keep the dropdown usable with auto-detect.
        if not self.port_var.get():
            self.port_var.set(AUTO_DETECT_LABEL)
        if not self.port_combo["values"]:
            self.port_combo["values"] = [AUTO_DETECT_LABEL]
        self.set_status(f"Port scan failed: {exc}", "#b00020")
        self._maybe_unlock_refresh()

    def _maybe_unlock_refresh(self) -> None:
        if self.any_tab_busy():
            return
        if hasattr(self, "gpio_tab") and self.gpio_tab.is_connected():
            return
        if hasattr(self, "tdbg_tab") and self.tdbg_tab.is_connected():
            return
        try:
            self.port_refresh_btn.config(state=tk.NORMAL)
        except tk.TclError:
            pass

    def lock_port_entry(self) -> None:
        self.port_combo.config(state=tk.DISABLED)
        self.port_refresh_btn.config(state=tk.DISABLED)

    def unlock_port_entry(self) -> None:
        # Stay locked if another tab is busy OR if any persistent-session
        # tab (GPIO / TDBG) is still holding the serial connection open
        # (would conflict with any other use until disconnected).
        if self.any_tab_busy():
            return
        if hasattr(self, "gpio_tab") and self.gpio_tab.is_connected():
            return
        if hasattr(self, "tdbg_tab") and self.tdbg_tab.is_connected():
            return
        self.port_combo.config(state="readonly")
        self.port_refresh_btn.config(state=tk.NORMAL)

    def set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.config(foreground=color)

    def any_tab_busy(self) -> bool:
        return any(t.is_busy() for t in self.tabs)

    def any_other_tab_holding_port(self, requesting_tab: "_LoggedTab") -> bool:
        """True if another tab currently has the serial port open. Used by
        GpioTab / TdbgTab connect to refuse if a sibling already holds it."""
        if requesting_tab is not self.gpio_tab and self.gpio_tab.is_connected():
            return True
        if requesting_tab is not self.tdbg_tab and self.tdbg_tab.is_connected():
            return True
        return False

    def release_port_then(self, except_tab, on_done) -> None:
        """Sequentially close any persistent-session tab (GPIO, TDBG) that
        currently holds the port, then invoke on_done() on the Tk thread.
        FlashTab uses this before starting a flash flow."""
        # Build a chain of releases that ends with on_done().
        steps = []
        if except_tab is not self.gpio_tab and self.gpio_tab.is_connected():
            steps.append(self.gpio_tab.disconnect_for_other)
        if except_tab is not self.tdbg_tab and self.tdbg_tab.is_connected():
            steps.append(self.tdbg_tab.disconnect_for_other)

        def chain(idx: int):
            if idx >= len(steps):
                on_done()
                return
            steps[idx](on_done=lambda: chain(idx + 1))

        chain(0)

    def _on_tab_changed(self, _event=None) -> None:
        try:
            selected = self.notebook.select()
        except tk.TclError:
            return
        if selected == str(self.gpio_tab.frame):
            self.gpio_tab._ensure_pin_panel_built()

    def _on_close(self) -> None:
        if self.any_tab_busy():
            messagebox.showinfo(
                "Busy",
                "An operation is in progress. Please wait for it to finish "
                "before closing.",
            )
            return
        # If GPIO / TDBG tabs still hold the serial port open, close them
        # cleanly so the OS releases the COM port. session.close() is fast
        # (no Due round-trip), safe to do synchronously here.
        if hasattr(self, "gpio_tab") and self.gpio_tab.is_connected():
            try:
                self.gpio_tab._do_close_session()
            except Exception:
                pass
        if hasattr(self, "tdbg_tab") and self.tdbg_tab.is_connected():
            try:
                self.tdbg_tab._do_close_session()
            except Exception:
                pass
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
