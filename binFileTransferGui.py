from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# pyserial and binFileTransfer_core are imported lazily inside the methods
# that need them. Both pull in Win32 COM enumeration code that's slow to
# import cold, and deferring keeps the Tk window visible within ~1 s of
# launch instead of waiting for those imports to finish first.


# Real font-family name behind TkDefaultFont, resolved once in App.__init__
# (needs a live Tk root). Used wherever a (family, size, weight) tuple is
# needed — passing the named-font string "TkDefaultFont" as the family is a
# bug (no family by that name → ugly fallback). Placeholder until resolved.
_UI_FAMILY = "TkDefaultFont"


def _resource_path(rel: str) -> str:
    """Resolve a path relative to either the script dir (dev) or
    PyInstaller's _MEIPASS extraction dir (built EXE)."""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


_DPI_SCALE_CACHE: float | None = None


def _scale(px: int) -> int:
    """Scale a hardcoded pixel size for the current DPI. Tk's
    `tk scaling` returns points-per-pixel (1.0 at 72 DPI logical scale,
    ~1.33 at the standard 96 DPI Windows desktop, higher on 4K). We
    normalise to 1.33 so that values authored against the standard Windows
    desktop look unchanged there but grow proportionally on high-DPI
    monitors. Cached after first call — Tk needs to be initialised
    before `tk scaling` is queryable, so we lazy-init."""
    global _DPI_SCALE_CACHE
    if _DPI_SCALE_CACHE is None:
        try:
            scaling = float(tk._default_root.tk.call("tk", "scaling"))
        except (AttributeError, tk.TclError, ValueError, TypeError):
            scaling = 1.33
        _DPI_SCALE_CACHE = max(1.0, scaling / 1.33)
    return int(round(px * _DPI_SCALE_CACHE))


class _Tooltip:
    """Lightweight hover tooltip — shows `text` in a borderless Toplevel
    near the cursor when the pointer enters `widget`, hides on leave.
    No external deps."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _show(self, _event=None) -> None:
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx() + self._widget.winfo_width() + 6
        y = self._widget.winfo_rooty() + self._widget.winfo_height() // 2 - 8
        tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        # macOS-style dark tooltip — light grey on charcoal, no border.
        tk.Label(
            tw, text=self._text,
            background="#2c2c2e", foreground="#ffffff",
            borderwidth=0, padx=8, pady=3,
            font=(_UI_FAMILY, 9),
        ).pack()
        self._tip = tw

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


APP_TITLE = "Arduino應用軟體"
WINDOW_SIZE = "900x780"

# Port dropdown — first option is the catch-all auto-detect.
AUTO_DETECT_LABEL = "Auto-detect (Arduino Due Programming Port)"
DUE_TARGET_VID = 0x2341
DUE_TARGET_PID = 0x003D

# macOS-inspired palette. Single source of truth — every status colour and
# every Notebook-tab style references entries from here. The dark-canvas /
# orange-waveform combination used by the TDBG and RECORD preview popups is
# intentionally kept (logic-analyzer style, separate visual layer).
#
# success/danger/warning/accent are the bright "system colour" hues meant for
# the accent text on a Notebook tab and for indicator dots on dark surfaces.
# success_dark / danger_dark / warning_dark / accent_dark are the ≥4.5:1 WCAG
# AA contrast variants for ordinary status text on a white background — the
# bright hues clock in around 2.2-3.5:1 against #ffffff, which is hard to read.
_COLORS = {
    "window_bg":      "#ffffff",
    "surface_2":      "#f5f5f7",
    "surface_hover":  "#ebebed",
    "text_primary":   "#1d1d1f",
    "text_secondary": "#6e6e73",
    "text_disabled":  "#c7c7cc",
    "accent":         "#007aff",
    "accent_dark":    "#0050b3",
    "success":        "#34c759",
    "success_dark":   "#1f7a1f",
    "danger":         "#ff3b30",
    "danger_dark":    "#c41a1a",
    "warning":        "#ff9500",
    "warning_dark":   "#a06400",
    "separator":      "#d2d2d7",
    "canvas_bg":      "#1a1a1a",
    "wave_orange":    "#ff9933",
    "wave_label":     "#ffe680",
}

LEVEL_TAGS = {
    "info": ("log_info", _COLORS["text_primary"]),
    "ok":   ("log_ok",   _COLORS["success_dark"]),
    "warn": ("log_warn", _COLORS["warning_dark"]),
    "err":  ("log_err",  _COLORS["danger_dark"]),
    "wait": ("log_wait", _COLORS["text_secondary"]),
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
        tag = LEVEL_TAGS.get(level, ("log_info", _COLORS["text_primary"]))[0]
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
            row2, text="Start Programming", command=self._on_start,
            state=tk.DISABLED, style="Accent.TButton",
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

        # Flashing is destructive (chip erase ends the MCU's idle loop), so
        # it can't share the persistent GPIO/TDBG/RECORD connection. Release
        # that shared connection first, then run the one-shot flash flow on
        # its own freshly-opened port.
        self.app.disconnect_then(self._do_start)

    def _do_start(self) -> None:
        port = self.app.get_port()
        firmware_path = self.firmware_path

        def work(log_cb):
            from binFileTransfer_core import program_firmware
            return program_firmware(firmware_path, log_cb, port=port)

        if self.submit_work(work):
            self.app.set_status("Programming...", _COLORS["warning_dark"])
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
            self.app.set_status("Success", _COLORS["success_dark"])
        else:
            self.app.set_status("Error", _COLORS["danger_dark"])
        self.start_btn.config(state=tk.NORMAL)
        self.browse_btn.config(state=tk.NORMAL)
        self.app.unlock_port_entry()


# ---------------------------------------------------------------------------
# GPIO 設定 tab — Plan A (single-pin manual test).
# ---------------------------------------------------------------------------
class _PinRow:
    """One row of the GPIO dashboard: label, mode toggle, HIGH/LOW radios,
    read display. Constructed disabled; call set_enabled(True) when the GPIO
    session opens. Click handlers fire `on_set(pin, mode, value_or_None)` —
    the GpioTab is expected to enqueue the actual GPIO_SET round-trip on
    its worker thread.

    Mode is rendered as a single ttk.Button (text "OUT" / "IN") rather than
    a Combobox. Combobox is one of the heaviest ttk widgets on Windows
    (Entry + Listbox + dropdown menu + popup grab) and constructing 66 of
    them dominated GPIO panel open time (~500 ms). A Button is a single
    element and shaves ~5 ms per row.
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
        ttk.Label(self.frame, text=label, width=10, anchor="w").grid(
            row=0, column=0, sticky="w"
        )

        # Mode is internally still "OUTPUT" / "INPUT" so the existing on_set
        # contract (passes mode string) doesn't change. The button just
        # cycles between the two and renders a 4-char abbreviation.
        self.mode_var = tk.StringVar(value="INPUT")
        self.mode_btn = ttk.Button(
            self.frame, text="--", width=4, state="disabled",
            command=self._on_mode_click,
        )
        self.mode_btn.grid(row=0, column=1, padx=(6, 10))

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
        self.low_radio.grid(row=0, column=3, padx=(0, 14))

        ttk.Label(self.frame, text="Read:").grid(row=0, column=4)
        self.read_var = tk.StringVar(value="??")
        self.read_label = ttk.Label(
            self.frame, textvariable=self.read_var,
            width=4, foreground=_COLORS["text_secondary"], anchor="w",
        )
        self.read_label.grid(row=0, column=5, padx=(4, 0))

    def _refresh_mode_btn(self, enabled: bool) -> None:
        if not enabled:
            self.mode_btn.config(text="--", state="disabled")
            return
        mode = self.mode_var.get()
        self.mode_btn.config(
            text="OUT" if mode == "OUTPUT" else "IN",
            state="normal",
        )

    def set_enabled(self, enabled: bool) -> None:
        self._refresh_mode_btn(enabled)
        if not enabled:
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")
            return
        if self.mode_var.get() == "OUTPUT":
            self.high_radio.config(state="normal")
            self.low_radio.config(state="normal")
        else:
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")

    def _on_mode_click(self) -> None:
        if self._suppress_callbacks:
            return
        new_mode = "INPUT" if self.mode_var.get() == "OUTPUT" else "OUTPUT"
        self.mode_var.set(new_mode)
        self._refresh_mode_btn(enabled=True)
        if new_mode == "OUTPUT":
            self.high_radio.config(state="normal")
            self.low_radio.config(state="normal")
        else:
            self.high_radio.config(state="disabled")
            self.low_radio.config(state="disabled")
            # Clear stale value selection so radios visually match disabled
            # state. try/finally so a Tk error can't leave the flag stuck at
            # True — that would silently swallow every later HIGH/LOW click
            # via the guard at the top of _on_value_changed.
            self._suppress_callbacks = True
            try:
                self.value_var.set("")
            finally:
                self._suppress_callbacks = False
        # Tell controller — value=None means "just switch mode, don't drive".
        self.on_set(self.pin, new_mode, None)

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
            foreground=_COLORS["success_dark"] if value == "HIGH" else _COLORS["text_primary"]
        )


class GpioTab(_LoggedTab):
    """Plan B: persistent connection + every-pin-visible dashboard."""

    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        # Initialise persistent-worker state before super().__init__ runs
        # _build_controls (which references some of these).
        # The serial connection is owned by the App now (one shared link for
        # GPIO / TDBG / RECORD); _session is a property delegating to it.
        self._cmd_queue: queue.Queue = queue.Queue()
        self._busy = False
        self._auto_refresh_after_id: str | None = None
        self._pin_rows: dict[int, _PinRow] = {}
        # Pin panel is heavy (66 rows × ~5 widgets) and most users start on
        # the Flash tab, so we defer construction until the GPIO tab is first
        # shown. App._on_tab_changed triggers _ensure_pin_panel_built().
        # `_pin_panel_filling` distinguishes "panel exists but rows still
        # being added incrementally" from "panel fully built" — used to gate
        # Read All so it doesn't iterate over a partial pin set.
        self._pin_panel_parent: ttk.Frame | None = None
        self._pin_panel_built = False
        self._pin_panel_filling = False
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

    @property
    def _session(self):
        """The shared GPIO session, owned by the App. None when the App-level
        connection is closed."""
        return self.app.gpio_session

    def set_connected(self, connected: bool) -> None:
        """Called by App when the shared connection opens/closes. Enables or
        disables this tab's operation controls (the Connect/Disconnect
        buttons now live at App level)."""
        if connected:
            self._abort_event.clear()
            if not getattr(self, "_pin_panel_filling", False):
                self._read_all_btn.config(state=tk.NORMAL)
            self._auto_refresh_check.config(state=tk.NORMAL)
            for row in self._pin_rows.values():
                row.set_enabled(True)
        else:
            # Stop auto-refresh + signal any in-flight Read All to bail.
            if self._auto_refresh_after_id is not None:
                try:
                    self.app.root.after_cancel(self._auto_refresh_after_id)
                except Exception:
                    pass
                self._auto_refresh_after_id = None
            self._auto_refresh_var.set(False)
            self._abort_event.set()
            try:
                while True:
                    self._cmd_queue.get_nowait()
            except queue.Empty:
                pass
            self._read_all_btn.config(state=tk.DISABLED)
            self._auto_refresh_check.config(state=tk.DISABLED)
            for row in self._pin_rows.values():
                row.set_enabled(False)

    def _build_controls(self, parent: ttk.Frame) -> None:
        # Connection is managed at App level now (shared Connect / Disconnect
        # under the Port row). This tab only builds its operation controls.
        # Row: Read All + Auto-refresh
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
        """Lazy-build the 66-pin dashboard on first GPIO-tab activation.
        Construction is incremental — see _build_pin_panel."""
        if self._pin_panel_built or self._pin_panel_parent is None:
            return
        self._pin_panel_built = True
        self._build_pin_panel(self._pin_panel_parent)

    def _build_pin_panel(self, parent: ttk.Frame) -> None:
        """Build the 66-pin dashboard.

        Two design choices for "doesn't freeze on open":

        1. Two-column grid (33 rows × 2 columns, column-major). Halves the
           scroll height, doesn't change widget count but cuts perceived
           density.
        2. Incremental fill. _PinRow construction is the slow step (~5 ms
           each on Windows even after dropping the Combobox). Building all
           66 inline froze the main thread for ~300-900 ms. Instead we
           build the canvas/scrollbar shell synchronously (cheap), then
           hand off pin-row creation to a chain of `after(0, ...)` calls
           that adds PINS_PER_TICK rows per Tk idle tick. Each tick is
           bounded at ~16 ms so the event loop stays responsive — user
           sees the first pins almost immediately and can start scrolling
           / clicking before the rest finish painting.
        """
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        canvas = tk.Canvas(wrap, highlightthickness=0, height=_scale(420))
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

        # Kick off the incremental fill.
        self._pin_panel_filling = True
        # Disable Read All while filling — its iteration over self._pin_rows
        # would otherwise visit a partial set.
        try:
            self._read_all_btn.config(state=tk.DISABLED)
        except (AttributeError, tk.TclError):
            pass
        self.app.root.after(0, lambda: self._fill_pin_panel(0, inner))

    # Two-column column-major grid: pin index 0..32 in column 0, 33..65 in
    # column 1. Reads top-to-bottom on the left, then top-to-bottom on the
    # right — matches how D-numbered pins are usually written down.
    _PIN_PANEL_ROWS_PER_COL = 33
    _PIN_PANEL_PINS_PER_TICK = 6

    def _fill_pin_panel(self, start_idx: int, inner: ttk.Frame) -> None:
        end = min(
            start_idx + self._PIN_PANEL_PINS_PER_TICK, len(GPIO_PINS)
        )
        for idx in range(start_idx, end):
            label, pin = GPIO_PINS[idx]
            row = _PinRow(inner, label, pin, on_set=self._on_pin_set)
            grid_row = idx % self._PIN_PANEL_ROWS_PER_COL
            grid_col = idx // self._PIN_PANEL_ROWS_PER_COL
            # Wider gap between columns; standard left margin on the first.
            row.frame.grid(
                row=grid_row, column=grid_col,
                sticky="w",
                padx=(8 if grid_col == 0 else 24, 0),
                pady=(0, 1),
            )
            self._pin_rows[pin] = row
        if end < len(GPIO_PINS):
            self.app.root.after(0, lambda: self._fill_pin_panel(end, inner))
            return

        # Fill complete. Apply current session state and re-enable Read All
        # so the user can interact with the full set.
        self._pin_panel_filling = False
        if self._session is not None:
            for row in self._pin_rows.values():
                row.set_enabled(True)
            try:
                self._read_all_btn.config(state=tk.NORMAL)
            except (AttributeError, tk.TclError):
                pass

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

    # ---- per-pin set / read all ------------------------------------------

    def _on_pin_set(self, pin: int, mode: str, value: str | None) -> None:
        if self._session is None:
            self._log_callback_threadsafe(f"尚未連線，無法設定 D{pin}", "warn")
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
            self._log_callback_threadsafe("尚未連線，無法讀取", "warn")
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


# Threshold for splitting events into "clusters" for the preview window.
# Anything bigger than this is treated as an inter-burst idle gap (the
# user's pattern has ~670 ms gaps between three 52 µs bursts). 10 ms in
# CPU cycles at 84 MHz.
_TDBG_CLUSTER_GAP_CYCLES = 840_000


def _split_into_clusters(
    initial_state: int,
    events: list[tuple[int, int]],
    gap_threshold: int = _TDBG_CLUSTER_GAP_CYCLES,
) -> list[tuple[int, int, list[tuple[int, int]]]]:
    """Split a flat event list at any delta exceeding `gap_threshold`.

    Returns: [(cluster_start_cycles, cluster_initial_state, cluster_events), ...]
    where cluster_events have the first event's delta reset to 0 (so each
    cluster's playback timeline starts at t=0 of that cluster). Joining the
    clusters back via their start-cycles offsets recovers the original timing.
    """
    clusters: list[tuple[int, int, list[tuple[int, int]]]] = []
    current_initial = initial_state
    current: list[tuple[int, int]] = []
    overall = 0
    cluster_start = 0

    for delta, state in events:
        if current and delta > gap_threshold:
            clusters.append((cluster_start, current_initial, current))
            current_initial = current[-1][1]
            cluster_start = overall + delta
            current = [(0, state)]
        else:
            current.append((delta, state))
        overall += delta

    if current:
        clusters.append((cluster_start, current_initial, current))
    return clusters


def _format_duration_ns(ns: float) -> str:
    """Pretty-print a duration in ns for waveform labels."""
    if ns < 1000:
        return f"{int(round(ns))} ns"
    if ns < 1_000_000:
        return f"{ns / 1000:.2f} µs"   # µs
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.3f} ms"
    return f"{ns / 1_000_000_000:.3f} s"


class TdbgTab(_LoggedTab):
    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        # Persistent worker thread — same pattern as GpioTab. The serial
        # connection is owned by the App; _session is a property delegating
        # to it.
        self._cmd_queue: queue.Queue = queue.Queue()
        self._busy = False
        # Modal waveform-preview window; only one at a time.
        self._preview_window: tk.Toplevel | None = None
        super().__init__(parent, app)
        self._worker_thread = threading.Thread(target=self._cmd_loop, daemon=True)
        self._worker_thread.start()

    # ---- _LoggedTab overrides ---------------------------------------------

    def is_busy(self) -> bool:
        return self._busy

    def submit_work(self, target_callable) -> bool:
        raise RuntimeError("TdbgTab uses _cmd_queue, not submit_work")

    @property
    def _session(self):
        """The shared TDBG session, owned by the App."""
        return self.app.tdbg_session

    def set_connected(self, connected: bool) -> None:
        """Enable/disable TDBG operation controls when the shared App
        connection opens/closes."""
        if connected:
            self._pin_combo.config(state="readonly")
            self._send_btn.config(state=tk.NORMAL)
            self._calib_btn.config(state=tk.NORMAL)
        else:
            self._pin_combo.config(state="disabled")
            self._send_btn.config(state=tk.DISABLED)
            self._calib_btn.config(state=tk.DISABLED)

    def _build_controls(self, parent: ttk.Frame) -> None:
        # Connection managed at App level. This tab builds operation controls.
        # Row: Pin + Clear Log
        pin_row = ttk.Frame(parent)
        pin_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(pin_row, text="Pin:").pack(side=tk.LEFT)
        self._pin_var = tk.StringVar(value="")
        self._pin_combo = ttk.Combobox(
            pin_row, textvariable=self._pin_var,
            values=TDBG_PIN_LABELS, state="disabled", width=14,
        )
        self._pin_combo.pack(side=tk.LEFT, padx=(6, 12))
        self._clear_log_btn = ttk.Button(
            pin_row, text="Clear Log", command=self._clear_log
        )
        self._clear_log_btn.pack(side=tk.LEFT)

        # Row 3: pattern card — bordered box, single horizontal line:
        # title + small clickable waveform thumbnail + Send button.
        card = ttk.Frame(parent, relief="groove", borderwidth=1, padding=8)
        card.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(
            card, text="TDBG 密碼1",
            font=(_UI_FAMILY, 10, "bold"),
        ).pack(side=tk.LEFT)
        self._preview_canvas = tk.Canvas(
            card, width=_scale(20), height=_scale(20),
            background=_COLORS["canvas_bg"], relief="raised", borderwidth=1,
            highlightthickness=0, cursor="hand2",
        )
        self._preview_canvas.pack(side=tk.LEFT, padx=(10, 8))
        self._preview_canvas.bind("<Button-1>", self._open_preview)
        _Tooltip(self._preview_canvas, "顯示波形")
        self._draw_thumbnail()
        self._send_btn = ttk.Button(
            card, text="送出", command=self._on_send, state=tk.DISABLED,
            style="Accent.TButton",
        )
        self._send_btn.pack(side=tk.LEFT)
        # Calibration pattern — four bursts at 100/200/500/1000-cycle deltas
        # separated by 1 ms gaps. Use this as a sanity check on the TC
        # playback engine: if the captured trace shows four distinct
        # period groups, the engine honours deltas above its ~60-cycle
        # floor. Hosted in core's tdbg_calibration_pattern() so it stays
        # in sync if delta semantics change.
        self._calib_btn = ttk.Button(
            card, text="校準", command=self._on_send_calibration,
            state=tk.DISABLED,
        )
        self._calib_btn.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(self._calib_btn,
                 "送出校準圖樣(100/200/500/1000 cycle 方波,驗證 TC 引擎)")

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
        # Source captures sit at ~380 ns deltas (32 cycles), well below the
        # TC engine's ~700 ns ISR floor — replaying as-is would slip every
        # short pulse onto a counter wrap (~1.56 ms penalty each) and lose
        # the cluster shape entirely. Retime the within-cluster deltas so
        # the minimum half-period clears the floor; long inter-cluster
        # gaps are passed through, keeping total runtime close to the
        # original capture. The receiver's PLL locks on whatever clean
        # period it sees in the 32-cycle preamble, so absolute timing
        # doesn't matter.
        from binFileTransfer_core import DUE_CPU_HZ, tdbg_retime_for_engine
        original_duration_s = sum(d for d, _ in events) / DUE_CPU_HZ
        events, scale = tdbg_retime_for_engine(events)
        duration_s = sum(d for d, _ in events) / DUE_CPU_HZ
        if scale != 1.0:
            self._append_log(
                f"retimed: {scale:.2f}× (engine-floor scale, "
                f"{original_duration_s*1000:.0f} ms → "
                f"{duration_s*1000:.0f} ms)",
                "info",
            )

        self._send_btn.config(state=tk.DISABLED)
        self._calib_btn.config(state=tk.DISABLED)
        self._pin_combo.config(state="disabled")
        self.app.set_status("TDBG sending...", _COLORS["warning_dark"])

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
            self._calib_btn.config(state=tk.NORMAL)
            self._pin_combo.config(state="readonly")
        self.app.set_status(
            "TDBG sent — D23 driving" if success else "TDBG error",
            _COLORS["success_dark"] if success else _COLORS["danger_dark"],
        )

    def _on_send_calibration(self) -> None:
        """Send the built-in calibration pattern instead of the user's
        TDBG password 1. Shares the same play-once flow as _on_send,
        just substitutes events. Useful for verifying TC engine timing
        with a logic analyzer."""
        if self._session is None:
            return
        pin = self._selected_pin()
        if pin is None:
            messagebox.showinfo("Pin", "Select an output pin first.")
            return
        from binFileTransfer_core import (
            tdbg_calibration_pattern, DUE_CPU_HZ,
        )
        initial, events = tdbg_calibration_pattern()
        duration_s = sum(d for d, _ in events) / DUE_CPU_HZ

        self._send_btn.config(state=tk.DISABLED)
        self._calib_btn.config(state=tk.DISABLED)
        self._pin_combo.config(state="disabled")
        self.app.set_status("TDBG sending calibration...", _COLORS["warning_dark"])

        def cmd():
            sess = self._session
            if sess is None:
                return
            ok = sess.load(pin=pin, initial_state=initial, events=events)
            if ok:
                ok = sess.play(iterations=1, total_duration_s=duration_s)
            self.app.root.after(0, lambda: self._on_send_done(ok))

        self._enqueue(cmd)

    # ---- mini thumbnail + preview popup -----------------------------------

    def _draw_thumbnail(self) -> None:
        """Render a small square-wave shape on the row-3 20x20 canvas. The
        canvas is the click target that opens the full preview popup —
        timing accuracy isn't important, the alternating shape just says
        "this is a digital waveform"."""
        canvas = self._preview_canvas
        canvas.delete("all")
        try:
            init, events = _ensure_builtin_parsed()
        except ValueError:
            canvas.create_text(
                10, 10, text="!", fill=_COLORS["danger"],
                font=(_UI_FAMILY, 10, "bold"),
            )
            return

        n_show = min(4, len(events))
        if n_show < 2:
            return
        sub = events[:n_show]

        margin_x = 2
        margin_y = 2
        w = int(canvas.cget("width"))
        h = int(canvas.cget("height"))
        usable_w = w - 2 * margin_x
        y_high = margin_y
        y_low = h - margin_y - 1

        step = usable_w / n_show
        x = margin_x
        state = init
        prev_y = y_high if state else y_low

        for i, (_, new_state) in enumerate(sub):
            new_x = margin_x + (i + 1) * step
            new_y = y_high if new_state else y_low
            canvas.create_line(x, prev_y, new_x, prev_y, fill=_COLORS["wave_orange"], width=1)
            canvas.create_line(new_x, prev_y, new_x, new_y, fill=_COLORS["wave_orange"], width=1)
            x = new_x
            prev_y = new_y

    def _open_preview(self, _event=None) -> None:
        if self._preview_window is not None and self._preview_window.winfo_exists():
            self._preview_window.lift()
            return
        try:
            init, events = _ensure_builtin_parsed()
        except ValueError as e:
            messagebox.showerror("Pattern", f"Parse error: {e}")
            return

        from binFileTransfer_core import DUE_CPU_HZ
        clusters = _split_into_clusters(init, events)
        total_cycles = sum(d for d, _ in events)
        duration_ns = total_cycles / DUE_CPU_HZ * 1e9

        win = tk.Toplevel(self.app.root)
        win.title("TDBG 密碼1 — Waveform Preview")
        win.geometry("1100x420")
        win.transient(self.app.root)
        # Modal: while the preview is open, the main window is grabbed so
        # the user can't accidentally close it or fire conflicting actions.
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_preview())
        self._preview_window = win

        summary = (
            f"{len(events)} transitions · "
            f"total active duration {_format_duration_ns(duration_ns)} · "
            f"initial = {'HIGH' if init else 'LOW'} · "
            f"split into {len(clusters)} cluster(s) at gaps ≥ 10 ms"
        )
        ttk.Label(win, text=summary, padding=(10, 8)).pack(fill=tk.X)

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        for idx, (start_cycles, c_init, c_events) in enumerate(clusters):
            frame = ttk.Frame(nb)
            c_total_ns = sum(d for d, _ in c_events) / DUE_CPU_HZ * 1e9
            tab_label = (
                f"Cluster {idx + 1} "
                f"({len(c_events)} edges, {_format_duration_ns(c_total_ns)})"
            )
            nb.add(frame, text=tab_label)
            start_ns = start_cycles / DUE_CPU_HZ * 1e9
            self._build_cluster_canvas(frame, c_init, c_events, start_ns)

        ttk.Button(
            win, text="Close", command=self._close_preview
        ).pack(pady=(0, 8))

    def _close_preview(self) -> None:
        win = self._preview_window
        self._preview_window = None
        if win is None:
            return
        try:
            win.grab_release()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    def _build_cluster_canvas(
        self,
        parent: ttk.Frame,
        initial_state: int,
        events: list[tuple[int, int]],
        start_ns: float,
    ) -> None:
        """Draw one cluster on a horizontally-scrollable Canvas."""
        from binFileTransfer_core import DUE_CPU_HZ

        # Header showing where in the original capture this cluster sits.
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Label(
            header,
            text=f"Cluster starts at t = {_format_duration_ns(start_ns)} of playback",
            foreground=_COLORS["text_secondary"],
        ).pack(side=tk.LEFT)

        # Geometry — pick a per-ns scale that makes the shortest pulse
        # roughly readable (~40 px wide for the user's 380 ns minimum).
        PIX_PER_NS = 0.10
        HEIGHT = 240
        Y_HIGH = 80
        Y_LOW = 170
        LEFT_PAD = 40
        RIGHT_PAD = 40
        LABEL_Y_TOP = Y_HIGH - 22       # pulse-width labels above
        AXIS_Y = Y_LOW + 38             # absolute timestamps below

        cum_cycles = [0]
        for delta, _ in events:
            cum_cycles.append(cum_cycles[-1] + delta)
        total_ns = cum_cycles[-1] / DUE_CPU_HZ * 1e9
        canvas_w = max(800, int(LEFT_PAD + total_ns * PIX_PER_NS + RIGHT_PAD))

        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        canvas = tk.Canvas(
            wrap, height=HEIGHT, background=_COLORS["canvas_bg"],
            scrollregion=(0, 0, canvas_w, HEIGHT),
            highlightthickness=0,
        )
        hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(xscrollcommand=hsb.set)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        # Mouse-wheel = horizontal scroll while pointer is over canvas.
        def _on_wheel(event):
            canvas.xview_scroll(int(-event.delta / 120), "units")

        def _bind(_e=None):
            canvas.bind_all("<MouseWheel>", _on_wheel)
            canvas.bind_all("<Button-4>", lambda e: canvas.xview_scroll(-1, "units"))
            canvas.bind_all("<Button-5>", lambda e: canvas.xview_scroll(1, "units"))

        def _unbind(_e=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind)
        canvas.bind("<Leave>", _unbind)

        # Y-axis labels.
        canvas.create_text(
            LEFT_PAD - 6, Y_HIGH, text="HIGH",
            fill="#aaaaaa", anchor="e", font=("Consolas", 9),
        )
        canvas.create_text(
            LEFT_PAD - 6, Y_LOW, text="LOW",
            fill="#aaaaaa", anchor="e", font=("Consolas", 9),
        )

        # Walk the events drawing one segment + vertical edge each.
        state = initial_state
        prev_x = LEFT_PAD
        prev_y = Y_HIGH if state else Y_LOW

        for i, (delta, new_state) in enumerate(events):
            seg_ns = delta / DUE_CPU_HZ * 1e9
            x = LEFT_PAD + (cum_cycles[i + 1] / DUE_CPU_HZ * 1e9) * PIX_PER_NS
            # Horizontal segment from prev_x to x at prev_y.
            if x > prev_x:
                canvas.create_line(
                    prev_x, prev_y, x, prev_y, fill=_COLORS["wave_orange"], width=2,
                )
            # Pulse-width label centred above the segment (skip the i=0
            # zero-width "anchor" segment).
            if seg_ns > 0 and x - prev_x >= 12:
                canvas.create_text(
                    (prev_x + x) / 2, LABEL_Y_TOP,
                    text=_format_duration_ns(seg_ns),
                    fill=_COLORS["wave_label"], font=("Consolas", 8),
                )
            # Vertical edge.
            new_y = Y_HIGH if new_state else Y_LOW
            canvas.create_line(
                x, prev_y, x, new_y, fill=_COLORS["wave_orange"], width=2,
            )
            prev_x, prev_y = x, new_y
            state = new_state

        # Tail run — extend the final state out to the right edge.
        tail_x = canvas_w - RIGHT_PAD
        if prev_x < tail_x:
            canvas.create_line(
                prev_x, prev_y, tail_x, prev_y, fill=_COLORS["wave_orange"], width=2,
            )

        # Time axis ticks at the canvas bottom — every 5 µs of cluster time.
        tick_step_ns = 5000.0
        tick_count = int(total_ns / tick_step_ns) + 1
        for k in range(tick_count + 1):
            tick_ns = k * tick_step_ns
            tx = LEFT_PAD + tick_ns * PIX_PER_NS
            canvas.create_line(
                tx, AXIS_Y, tx, AXIS_Y + 4, fill=_COLORS["text_secondary"],
            )
            canvas.create_text(
                tx, AXIS_Y + 14,
                text=_format_duration_ns(tick_ns),
                fill="#888888", font=("Consolas", 8),
            )


# ---------------------------------------------------------------------------
# 波形錄製 tab — multi-pin live recorder. ISR-driven on the MCU; this side
# orchestrates the start/stop dance, displays live HIGH/LOW state per pin
# while recording, and renders the captured timeline in a modal popup.
# ---------------------------------------------------------------------------

# Pin labels reused from TDBG (D0..D65 with flash-bus annotations).

class _RecordPinRow:
    """One selectable pin slot in the RecordTab. Holds a combo for the pin
    number plus a coloured indicator that reflects live state during a
    recording. Created and destroyed dynamically via the [+] / [⊖] buttons.
    """

    DOT_HIGH = _COLORS["success_dark"]
    DOT_LOW = _COLORS["text_secondary"]
    DOT_UNKNOWN = _COLORS["text_disabled"]

    def __init__(self, parent: ttk.Frame, on_remove, allow_remove: bool) -> None:
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(self.frame, text="Pin:").pack(side=tk.LEFT)
        self.pin_var = tk.StringVar(value="")
        self.pin_combo = ttk.Combobox(
            self.frame, textvariable=self.pin_var,
            values=TDBG_PIN_LABELS, state="readonly", width=14,
        )
        self.pin_combo.pack(side=tk.LEFT, padx=(6, 12))

        # ttk.Frame doesn't expose `-background` via cget — its background
        # is controlled by the ttk theme, so we have to ask the style
        # system. Fall back to a reasonable clam-ish grey if lookup fails.
        try:
            bg = ttk.Style().lookup("TFrame", "background") or "#dcdad5"
        except tk.TclError:
            bg = "#dcdad5"
        self.dot = tk.Canvas(
            self.frame, width=14, height=14,
            highlightthickness=0, background=bg,
        )
        self.dot.pack(side=tk.LEFT)
        self._draw_dot(self.DOT_UNKNOWN)
        self.state_var = tk.StringVar(value="—")
        ttk.Label(
            self.frame, textvariable=self.state_var, width=5, anchor="w",
        ).pack(side=tk.LEFT, padx=(4, 12))

        self.remove_btn = ttk.Button(
            self.frame, text="⊖", width=3,
            command=lambda: on_remove(self),
        )
        if allow_remove:
            self.remove_btn.pack(side=tk.LEFT)
        else:
            # Reserve space so layout doesn't shift when more rows are added.
            self.remove_btn.pack_forget()

    def _draw_dot(self, color: str) -> None:
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 12, 12, fill=color, outline="")

    def set_live_state(self, state: bool | None) -> None:
        if state is None:
            self._draw_dot(self.DOT_UNKNOWN)
            self.state_var.set("—")
        elif state:
            self._draw_dot(self.DOT_HIGH)
            self.state_var.set("HIGH")
        else:
            self._draw_dot(self.DOT_LOW)
            self.state_var.set("LOW")

    def selected_pin(self) -> int | None:
        label = self.pin_var.get()
        if not label:
            return None
        try:
            return int(label.split(" ", 1)[0].lstrip("D"))
        except ValueError:
            return None

    def set_combo_enabled(self, enabled: bool) -> None:
        self.pin_combo.config(state="readonly" if enabled else "disabled")
        self.remove_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def show_remove(self, show: bool) -> None:
        if show:
            self.remove_btn.pack(side=tk.LEFT)
        else:
            self.remove_btn.pack_forget()

    def destroy(self) -> None:
        self.frame.destroy()


class RecordTab(_LoggedTab):
    MAX_PINS = 4

    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        # Serial connection owned by the App; _session is a property.
        self._cmd_queue: queue.Queue = queue.Queue()
        self._busy = False
        self._recording = False
        # Captured recording — populated when 結束 succeeds.
        self._recorded_pins: list[int] | None = None
        self._recorded_events: list | None = None
        self._preview_window: tk.Toplevel | None = None
        self._pin_rows: list[_RecordPinRow] = []
        super().__init__(parent, app)
        self._worker_thread = threading.Thread(target=self._cmd_loop, daemon=True)
        self._worker_thread.start()

    def is_busy(self) -> bool:
        return self._busy

    def submit_work(self, target_callable) -> bool:
        raise RuntimeError("RecordTab uses _cmd_queue, not submit_work")

    @property
    def _session(self):
        """The shared RECORD session, owned by the App."""
        return self.app.record_session

    def set_connected(self, connected: bool) -> None:
        """Enable/disable RECORD controls when the shared App connection
        opens/closes."""
        if connected:
            self._refresh_start_button()   # enables 開始 if a pin is selected
        else:
            self._recording = False
            self._start_btn.config(state=tk.DISABLED)
            self._stop_btn.config(state=tk.DISABLED)
            for row in self._pin_rows:
                row.set_live_state(None)

    def is_recording(self) -> bool:
        return self._recording

    def _build_controls(self, parent: ttk.Frame) -> None:
        # Connection managed at App level.
        # Row: 開始 / 結束 / (post-stop) waveform thumbnail / Clear Log
        action_row = ttk.Frame(parent)
        action_row.pack(fill=tk.X, pady=(8, 0))
        self._start_btn = ttk.Button(
            action_row, text="開始", command=self._on_start, state=tk.DISABLED,
            style="Accent.TButton",
        )
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(
            action_row, text="結束", command=self._on_stop, state=tk.DISABLED,
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Thumbnail canvas (only visible after a successful recording).
        self._preview_canvas = tk.Canvas(
            action_row, width=_scale(20), height=_scale(20),
            background=_COLORS["canvas_bg"], relief="raised", borderwidth=1,
            highlightthickness=0, cursor="hand2",
        )
        # Reserved but not packed yet — we pack it after the first 結束.
        self._preview_canvas.bind("<Button-1>", self._open_preview)
        _Tooltip(self._preview_canvas, "顯示波形")

        self._clear_log_btn = ttk.Button(
            action_row, text="Clear Log", command=self._clear_log
        )
        self._clear_log_btn.pack(side=tk.RIGHT)

        # Row 3+: pin list — one row per pin slot. The first row is always
        # present; [+] adds another up to MAX_PINS.
        self._pins_frame = ttk.Frame(parent)
        self._pins_frame.pack(fill=tk.X, pady=(8, 0))
        self._add_pin_btn = ttk.Button(
            parent, text="+ 加 pin", command=self._on_add_pin,
        )
        self._add_pin_btn.pack(anchor="w", pady=(2, 0))

        # Seed with one pin row.
        self._add_pin_row(allow_remove=False)

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
                self._log_callback_threadsafe(f"RECORD cmd error: {e}", "err")
            finally:
                self._busy = False

    # ---- pin row management ----------------------------------------------

    def _add_pin_row(self, allow_remove: bool = True) -> None:
        row = _RecordPinRow(
            self._pins_frame,
            on_remove=self._on_remove_pin,
            allow_remove=allow_remove,
        )
        # When the user picks a pin in this row, we may need to re-enable
        # the 開始 button.
        row.pin_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._refresh_start_button(),
        )
        self._pin_rows.append(row)
        self._refresh_pin_buttons()
        self._refresh_start_button()

    def _on_add_pin(self) -> None:
        if len(self._pin_rows) >= self.MAX_PINS:
            return
        self._add_pin_row(allow_remove=True)

    def _on_remove_pin(self, row: _RecordPinRow) -> None:
        if len(self._pin_rows) <= 1:
            return
        self._pin_rows.remove(row)
        row.destroy()
        self._refresh_pin_buttons()
        self._refresh_start_button()

    def _refresh_pin_buttons(self) -> None:
        # Hide ⊖ on first row when it's the only one; show on all otherwise.
        for i, row in enumerate(self._pin_rows):
            row.show_remove(len(self._pin_rows) > 1)
        # Disable + at MAX_PINS.
        if len(self._pin_rows) >= self.MAX_PINS:
            self._add_pin_btn.config(state=tk.DISABLED)
        else:
            self._add_pin_btn.config(state=tk.NORMAL)

    # ---- start / stop -----------------------------------------------------

    def _refresh_start_button(self) -> None:
        ready = (
            self._session is not None
            and not self._recording
            and any(r.selected_pin() is not None for r in self._pin_rows)
        )
        self._start_btn.config(state=tk.NORMAL if ready else tk.DISABLED)

    def _on_start(self) -> None:
        if self._session is None or self._recording:
            return
        # Collect unique selected pins from rows in order.
        pins: list[int] = []
        seen: set[int] = set()
        for row in self._pin_rows:
            p = row.selected_pin()
            if p is None:
                continue
            if p in seen:
                continue
            pins.append(p)
            seen.add(p)
        if not pins:
            messagebox.showinfo("Pin", "Pick at least one pin first.")
            return
        if len(pins) > self.MAX_PINS:
            messagebox.showinfo("Pin", f"Max {self.MAX_PINS} pins.")
            return

        # Lock the UI for recording.
        self._recording = True
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        # Gate the other tabs — RECORD's live_loop owns the serial while
        # capturing, so GPIO/TDBG must not write to it concurrently.
        self.app.set_recording(True)
        for row in self._pin_rows:
            row.set_combo_enabled(False)
            row.set_live_state(None)
        self._add_pin_btn.config(state=tk.DISABLED)
        # Clear stale recording / hide thumbnail.
        self._recorded_pins = None
        self._recorded_events = None
        try:
            self._preview_canvas.pack_forget()
        except Exception:
            pass
        self.app.set_status("RECORD recording...", _COLORS["warning_dark"])

        # Build a row→pin mapping for live-callback dispatch.
        pin_to_row = {row.selected_pin(): row for row in self._pin_rows
                      if row.selected_pin() is not None}

        def on_live(states: dict[int, bool]) -> None:
            # Called from the RecordSession's live thread — marshal to Tk.
            def apply():
                for pin, state in states.items():
                    row = pin_to_row.get(pin)
                    if row is not None:
                        row.set_live_state(state)
            try:
                self.app.root.after(0, apply)
            except Exception:
                pass

        def cmd():
            sess = self._session
            if sess is None:
                return
            ok = sess.start(pins, on_live=on_live)
            if not ok:
                self.app.root.after(0, lambda: self._on_recording_failed())

        self._enqueue(cmd)

    def _on_recording_failed(self) -> None:
        self._recording = False
        self._stop_btn.config(state=tk.DISABLED)
        for row in self._pin_rows:
            row.set_combo_enabled(True)
        self._add_pin_btn.config(
            state=tk.NORMAL if len(self._pin_rows) < self.MAX_PINS else tk.DISABLED
        )
        self.app.set_recording(False)
        self._refresh_start_button()
        self.app.set_status("RECORD start failed", _COLORS["danger_dark"])

    def _on_stop(self) -> None:
        if not self._recording:
            return
        self._stop_btn.config(state=tk.DISABLED)
        self.app.set_status("RECORD stopping...", _COLORS["warning_dark"])

        def cmd():
            sess = self._session
            if sess is None:
                return
            result = sess.stop()
            self.app.root.after(0, lambda: self._on_stop_done(result))

        self._enqueue(cmd)

    def _on_stop_done(self, result) -> None:
        self._recording = False
        for row in self._pin_rows:
            row.set_combo_enabled(True)
        self._add_pin_btn.config(
            state=tk.NORMAL if len(self._pin_rows) < self.MAX_PINS else tk.DISABLED
        )
        self.app.set_recording(False)
        self._refresh_start_button()

        if result is None:
            self.app.set_status("RECORD stop error", _COLORS["danger_dark"])
            return
        pins, events = result
        self._recorded_pins = pins
        self._recorded_events = events
        # Reveal the thumbnail in the action row. Clear Log is packed RIGHT,
        # so plain side=LEFT here puts the canvas after 結束 (the last
        # LEFT-packed widget) and before Clear Log. No need for in_/after.
        self._draw_thumbnail()
        self._preview_canvas.pack(side=tk.LEFT, padx=(8, 0))
        if events:
            total_us = sum(d for d, _ in events)
            self.app.set_status(
                f"RECORD done: {len(events)} edges, {total_us / 1000:.3f} ms",
                _COLORS["success_dark"],
            )
        else:
            self.app.set_status("RECORD done: no edges captured", _COLORS["warning_dark"])

    # ---- thumbnail + preview popup ---------------------------------------

    def _draw_thumbnail(self) -> None:
        canvas = self._preview_canvas
        canvas.delete("all")
        events = self._recorded_events or []
        pins = self._recorded_pins or []
        if not events or not pins:
            canvas.create_text(
                10, 10, text="—", fill="#888888",
                font=(_UI_FAMILY, 10, "bold"),
            )
            return
        # Compress the first ~6 transitions of the first pin onto 20x20.
        n_show = min(6, len(events))
        sub = events[:n_show]
        target_pin = pins[0]
        margin_x = 2
        margin_y = 2
        w = 20
        h = 20
        usable_w = w - 2 * margin_x
        y_high = margin_y
        y_low = h - margin_y - 1

        step = usable_w / max(1, n_show)
        x = margin_x
        prev_state = sub[0][1].get(target_pin, False)
        prev_y = y_high if prev_state else y_low

        for i, (_, states) in enumerate(sub):
            new_state = states.get(target_pin, prev_state)
            new_x = margin_x + (i + 1) * step
            new_y = y_high if new_state else y_low
            canvas.create_line(x, prev_y, new_x, prev_y, fill=_COLORS["wave_orange"], width=1)
            canvas.create_line(new_x, prev_y, new_x, new_y, fill=_COLORS["wave_orange"], width=1)
            x = new_x
            prev_y = new_y

    def _open_preview(self, _event=None) -> None:
        if self._recorded_events is None or self._recorded_pins is None:
            return
        if self._preview_window is not None and self._preview_window.winfo_exists():
            self._preview_window.lift()
            return

        from binFileTransfer_core import DUE_CPU_HZ  # noqa: F401  (kept for parity)
        events = self._recorded_events
        pins = self._recorded_pins
        total_us = sum(d for d, _ in events)

        win = tk.Toplevel(self.app.root)
        win.title("波形錄製 — Captured Waveform")
        win.geometry("1100x460")
        win.transient(self.app.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_preview())
        self._preview_window = win

        pin_list = ", ".join(f"D{p}" for p in pins)
        summary = (
            f"{len(events)} edges · "
            f"total {_format_duration_ns(total_us * 1000.0)} · "
            f"{len(pins)} pins ({pin_list})"
        )
        ttk.Label(win, text=summary, padding=(10, 8)).pack(fill=tk.X)

        # Split into clusters at gaps ≥10 ms (10_000 µs). Same idea as TDBG.
        clusters = self._split_record_into_clusters(events, gap_us=10_000)
        if len(clusters) == 1:
            holder = ttk.Frame(win)
            holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
            self._build_record_canvas(holder, pins, clusters[0])
        else:
            nb = ttk.Notebook(win)
            nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
            for idx, c_events in enumerate(clusters):
                frame = ttk.Frame(nb)
                c_total = sum(d for d, _ in c_events)
                nb.add(
                    frame,
                    text=f"Cluster {idx + 1} "
                         f"({len(c_events)} edges, "
                         f"{_format_duration_ns(c_total * 1000.0)})",
                )
                self._build_record_canvas(frame, pins, c_events)

        ttk.Button(
            win, text="Close", command=self._close_preview
        ).pack(pady=(0, 8))

    def _close_preview(self) -> None:
        win = self._preview_window
        self._preview_window = None
        if win is None:
            return
        try:
            win.grab_release()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    @staticmethod
    def _split_record_into_clusters(events, gap_us: int = 10_000):
        """Split recording events into clusters at long-gap boundaries.

        Each event is (delta_us, {pin: bool}). Returns list of cluster
        event-lists, where the first event of each cluster has its delta
        reset to 0 so per-cluster timelines start at t=0.
        """
        clusters = []
        current = []
        for delta, states in events:
            if current and delta > gap_us:
                clusters.append(current)
                current = [(0, states)]
            else:
                current.append((delta, states))
        if current:
            clusters.append(current)
        return clusters or [[]]

    def _build_record_canvas(
        self, parent: ttk.Frame, pins: list[int], events: list,
    ) -> None:
        """Draw stacked per-pin waveforms on a horizontally-scrollable canvas."""
        # Geometry
        PIX_PER_US = 0.5             # 1 µs = 0.5 px → 200 µs / 100 px
        ROW_HEIGHT = 60              # vertical span per pin
        ROW_PAD_TOP = 16
        Y_OFFSET = 30
        LEFT_PAD = 60
        RIGHT_PAD = 30
        LABEL_Y_OFFSET = -16

        cum = [0]
        for delta, _ in events:
            cum.append(cum[-1] + delta)
        total_us = cum[-1] if events else 0
        canvas_w = max(800, int(LEFT_PAD + total_us * PIX_PER_US + RIGHT_PAD))
        canvas_h = Y_OFFSET + len(pins) * ROW_HEIGHT + 30

        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        canvas = tk.Canvas(
            wrap, height=canvas_h, background=_COLORS["canvas_bg"],
            scrollregion=(0, 0, canvas_w, canvas_h),
            highlightthickness=0,
        )
        hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(xscrollcommand=hsb.set)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        def _on_wheel(event):
            canvas.xview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        # Draw each pin as a stacked row.
        for row_idx, pin in enumerate(pins):
            y_high = Y_OFFSET + row_idx * ROW_HEIGHT + ROW_PAD_TOP
            y_low = Y_OFFSET + row_idx * ROW_HEIGHT + ROW_HEIGHT - 8
            canvas.create_text(
                LEFT_PAD - 6, (y_high + y_low) // 2,
                text=f"D{pin}", fill="#dddddd",
                anchor="e", font=("Consolas", 10, "bold"),
            )
            # Initial state: from event 0 (which is the first "transition"
            # at t=0 — the first sampled state).
            if not events:
                continue
            state = events[0][1].get(pin, False)
            prev_x = LEFT_PAD
            prev_y = y_high if state else y_low

            for i, (delta, states) in enumerate(events):
                new_state = states.get(pin, state)
                x = LEFT_PAD + cum[i + 1] * PIX_PER_US
                if x > prev_x:
                    canvas.create_line(
                        prev_x, prev_y, x, prev_y, fill=_COLORS["wave_orange"], width=2,
                    )
                    seg_us = cum[i + 1] - cum[i]
                    if seg_us > 0 and (x - prev_x) >= 16 and row_idx == 0:
                        # Pulse-width labels on the top row only — multi-row
                        # gets cluttered fast.
                        canvas.create_text(
                            (prev_x + x) / 2, y_high + LABEL_Y_OFFSET,
                            text=_format_duration_ns(seg_us * 1000.0),
                            fill=_COLORS["wave_label"], font=("Consolas", 8),
                        )
                new_y = y_high if new_state else y_low
                if new_y != prev_y:
                    canvas.create_line(
                        x, prev_y, x, new_y, fill=_COLORS["wave_orange"], width=2,
                    )
                prev_x, prev_y = x, new_y
                state = new_state

            # Tail run.
            tail_x = canvas_w - RIGHT_PAD
            if prev_x < tail_x:
                canvas.create_line(
                    prev_x, prev_y, tail_x, prev_y, fill=_COLORS["wave_orange"], width=2,
                )

        # Bottom time axis.
        axis_y = canvas_h - 14
        if total_us > 0:
            tick_step_us = 50.0 if total_us < 2000 else 200.0
            n_ticks = int(total_us / tick_step_us) + 1
            for k in range(n_ticks + 1):
                t_us = k * tick_step_us
                tx = LEFT_PAD + t_us * PIX_PER_US
                canvas.create_line(tx, axis_y - 4, tx, axis_y, fill=_COLORS["text_secondary"])
                canvas.create_text(
                    tx, axis_y + 6,
                    text=_format_duration_ns(t_us * 1000.0),
                    fill="#888888", font=("Consolas", 8),
                )


# ---------------------------------------------------------------------------
# App: top-level container with shared port entry, notebook, status bar.
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)
        try:
            self.root.iconbitmap(_resource_path("assets/icon.ico"))
        except tk.TclError:
            # Linux Tk's iconbitmap doesn't accept .ico; ignore silently
            # so dev-mode runs on Linux still work. The shipped EXE is
            # Windows-only, so this branch only triggers in dev.
            pass

        # CJK font normalisation. On Windows, Tk's default named fonts
        # (Segoe UI 9pt) fall back to a low-quality CJK glyph set when the
        # clam theme renders Chinese — labels look "扭曲" / pixelated. Pin
        # the named fonts to Microsoft JhengHei UI (繁中,Win 8+,always
        # installed on a stock Windows install) so every ttk widget
        # inherits CJK-clean glyphs without per-widget overrides.
        import tkinter.font as tkfont
        if sys.platform == "win32":
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                         "TkHeadingFont", "TkCaptionFont",
                         "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
                try:
                    tkfont.nametofont(name).configure(
                        family="Microsoft JhengHei UI", size=10)
                except tk.TclError:
                    pass
        # Resolve the REAL family name behind the TkDefaultFont named font.
        # Widgets/styles that need a custom size or weight can't use the
        # named-font string directly — they must pass a (family, size,
        # ...) tuple, and the first tuple element is interpreted as a font
        # FAMILY, not a named font. Passing "TkDefaultFont" there is a bug:
        # there's no family by that name, so Tk falls back to an ugly
        # default and size/weight apply inconsistently (this is why the
        # tabs looked pixelated and the selected tab wasn't visibly larger).
        # Resolve the actual family once and use it everywhere via the
        # module global _UI_FAMILY.
        global _UI_FAMILY
        try:
            _UI_FAMILY = tkfont.nametofont("TkDefaultFont").actual("family")
        except tk.TclError:
            _UI_FAMILY = "TkDefaultFont"

        # Single shared serial connection for the three persistent-session
        # tabs (GPIO / TDBG / RECORD). Opened by the App-level Connect button
        # below the Port row; the three tabs share this one serial + the lock
        # that serialises access to it. Flash uses its own one-shot flow and
        # requires this connection released first (its erase is destructive).
        self._ser = None
        self._serial_lock = threading.Lock()
        self._connected = False
        self._conn_busy = False
        self.gpio_session = None
        self.tdbg_session = None
        self.record_session = None

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

        # Shared connection row — ONE Connect / Disconnect for the GPIO /
        # TDBG / RECORD tabs (Flash stays independent). Sits directly under
        # the Port row so the connection is a top-level, tab-independent
        # state rather than three separate per-tab buttons.
        conn_row = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        conn_row.pack(fill=tk.X)
        ttk.Label(conn_row, text="Connection:").pack(side=tk.LEFT)
        self._conn_status_var = tk.StringVar(value="Disconnected")
        self._conn_status_label = ttk.Label(
            conn_row, textvariable=self._conn_status_var,
            foreground=_COLORS["danger_dark"],
        )
        self._conn_status_label.pack(side=tk.LEFT, padx=(6, 12))
        self._connect_btn = ttk.Button(
            conn_row, text="Connect", command=self._on_app_connect,
            style="Accent.TButton",
        )
        self._connect_btn.pack(side=tk.LEFT)
        self._disconnect_btn = ttk.Button(
            conn_row, text="Disconnect", command=self._on_app_disconnect,
            state=tk.DISABLED,
        )
        self._disconnect_btn.pack(side=tk.LEFT, padx=(6, 0))

        # macOS-inspired Notebook style. Selected tab renders 12pt bold vs
        # unselected 10pt regular, using the resolved _UI_FAMILY (NOT the
        # "TkDefaultFont" named-font string, which as a tuple family name
        # is invalid and falls back to an ugly default — that bug is why
        # the selected tab previously didn't look larger). expand=[0,0,0,0]
        # disables clam's default "lift" of the selected tab so all tabs
        # stay co-planar.
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Font tuples are built HERE (not at module scope) because _UI_FAMILY
        # is only resolved to a real family in __init__ above; at import time
        # it is still the placeholder "TkDefaultFont", which is invalid as a
        # tuple family name and falls back to an ugly default.
        font_base = (_UI_FAMILY, 10)
        font_tab = (_UI_FAMILY, 11)
        font_tab_sel = (_UI_FAMILY, 11, "bold")

        # Global default font for every ttk widget — single source of truth so
        # labels / buttons / entries all share one clean family + size.
        style.configure(".", font=font_base)

        style.configure(
            "TNotebook",
            background=_COLORS["surface_2"],
            borderwidth=0,
            tabmargins=[2, 4, 2, 0],
        )
        # Flat tabs, differentiated by COLOUR + WEIGHT only — no size jump and
        # no white "box" on the selected tab:
        #   unselected -> blue text (regular)
        #   selected   -> black text (bold), same 11pt size, same background
        # The selected/focused tab in clam draws a border + focus rectangle
        # (the "box" the user saw). Matching bordercolor / lightcolor /
        # darkcolor / focuscolor to the fill removes that box entirely, so the
        # tab geometry is identical in every state — only the text colour and
        # weight change.
        style.configure(
            "TNotebook.Tab",
            padding=[16, 8],
            font=font_tab,
            background=_COLORS["surface_2"],
            foreground=_COLORS["accent"],
            borderwidth=0,
            bordercolor=_COLORS["surface_2"],
            lightcolor=_COLORS["surface_2"],
            darkcolor=_COLORS["surface_2"],
            focuscolor=_COLORS["surface_2"],
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", _COLORS["surface_2"]),
                ("active",   _COLORS["surface_2"]),
            ],
            foreground=[
                ("selected", _COLORS["text_primary"]),
                ("active",   _COLORS["accent_dark"]),
            ],
            bordercolor=[("selected", _COLORS["surface_2"]),
                         ("active",   _COLORS["surface_2"])],
            lightcolor=[("selected", _COLORS["surface_2"]),
                        ("active",   _COLORS["surface_2"])],
            darkcolor=[("selected", _COLORS["surface_2"]),
                       ("active",   _COLORS["surface_2"])],
            font=[("selected", font_tab_sel)],
            expand=[("selected", [0, 0, 0, 0])],
        )

        # Flat buttons — strip clam's 3D bevel (lightcolor/darkcolor drive the
        # bevel highlight/shadow; matching them to the fill removes it) and the
        # dotted focus ring (focuscolor). Hover/pressed give subtle feedback.
        style.configure(
            "TButton",
            font=font_base,
            background=_COLORS["surface_2"],
            foreground=_COLORS["text_primary"],
            bordercolor=_COLORS["separator"],
            lightcolor=_COLORS["surface_2"],
            darkcolor=_COLORS["surface_2"],
            focuscolor=_COLORS["surface_2"],
            relief="flat",
            borderwidth=1,
            padding=[12, 6],
        )
        style.map(
            "TButton",
            background=[
                ("pressed",  _COLORS["separator"]),
                ("active",   _COLORS["surface_hover"]),
                ("disabled", _COLORS["surface_2"]),
            ],
            foreground=[("disabled", _COLORS["text_disabled"])],
            lightcolor=[("active", _COLORS["surface_hover"])],
            darkcolor=[("active", _COLORS["surface_hover"])],
        )

        # Primary-action button — solid accent fill, white text, no bevel.
        # Applied to the one main action per tab (Connect / Start Programming /
        # 送出 / 開始).
        style.configure(
            "Accent.TButton",
            font=(_UI_FAMILY, 10, "bold"),
            background=_COLORS["accent"],
            foreground="#ffffff",
            bordercolor=_COLORS["accent"],
            lightcolor=_COLORS["accent"],
            darkcolor=_COLORS["accent"],
            focuscolor=_COLORS["accent"],
            relief="flat",
            borderwidth=0,
            padding=[12, 6],
        )
        style.map(
            "Accent.TButton",
            background=[
                ("pressed",  _COLORS["accent_dark"]),
                ("active",   _COLORS["accent_dark"]),
                ("disabled", _COLORS["text_disabled"]),
            ],
            foreground=[("disabled", "#ffffff")],
            lightcolor=[("active", _COLORS["accent_dark"])],
            darkcolor=[("active", _COLORS["accent_dark"])],
        )

        # Disabled-state foreground — _COLORS["text_disabled"] was defined
        # but never applied. Without this, disabled labels / entries are
        # rendered with the default (full-strength) foreground, making it
        # hard to tell whether a control is interactive. (TButton handles its
        # own disabled state above.)
        for w in ("TLabel", "TEntry", "TCombobox", "TRadiobutton"):
            style.map(w, foreground=[("disabled", _COLORS["text_disabled"])])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 0))

        self.flash_tab = FlashTab(self.notebook, self)
        self.gpio_tab = GpioTab(self.notebook, self)
        self.tdbg_tab = TdbgTab(self.notebook, self)
        self.record_tab = RecordTab(self.notebook, self)
        self.notebook.add(self.flash_tab.frame, text="燒錄 ROM")
        self.notebook.add(self.gpio_tab.frame, text="GPIO 設定")
        self.notebook.add(self.tdbg_tab.frame, text="TDBG")
        self.notebook.add(self.record_tab.frame, text="波形錄製")
        self.notebook.select(self.flash_tab.frame)  # default tab
        self.tabs.extend([self.flash_tab, self.gpio_tab, self.tdbg_tab, self.record_tab])
        # Defer GPIO pin panel construction until that tab is first shown.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Status bar at the bottom (shared across tabs).
        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(bottom, text="Status:").pack(side=tk.LEFT)
        self.status_label = ttk.Label(
            bottom, textvariable=self.status_var, foreground=_COLORS["text_primary"]
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
        self.set_status(f"Port scan failed: {exc}", _COLORS["danger_dark"])
        self._maybe_unlock_refresh()

    def _maybe_unlock_refresh(self) -> None:
        if self.any_tab_busy() or self._connected or self._conn_busy:
            return
        try:
            self.port_refresh_btn.config(state=tk.NORMAL)
        except tk.TclError:
            pass

    def lock_port_entry(self) -> None:
        self.port_combo.config(state=tk.DISABLED)
        self.port_refresh_btn.config(state=tk.DISABLED)

    def unlock_port_entry(self) -> None:
        # Stay locked while the shared connection is open / connecting or any
        # tab is busy — changing the port mid-connection makes no sense.
        if self.any_tab_busy() or self._connected or self._conn_busy:
            return
        self.port_combo.config(state="readonly")
        self.port_refresh_btn.config(state=tk.NORMAL)

    def set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.config(foreground=color)

    def any_tab_busy(self) -> bool:
        return any(t.is_busy() for t in self.tabs)

    # ---- shared connection (GPIO / TDBG / RECORD) ------------------------

    def is_connected(self) -> bool:
        return self._connected

    def _set_conn_status(self, text: str, color: str) -> None:
        self._conn_status_var.set(text)
        self._conn_status_label.config(foreground=color)

    def _on_app_connect(self) -> None:
        if self._connected or self._conn_busy:
            return
        if self.any_tab_busy():
            messagebox.showinfo(
                "Busy", "An operation is in progress. Please wait.")
            return
        port = self.get_port()
        self._conn_busy = True
        self._set_conn_status("Connecting...", _COLORS["warning_dark"])
        self._connect_btn.config(state=tk.DISABLED)
        self.lock_port_entry()
        self.set_status("Connecting...", _COLORS["warning_dark"])

        def work():
            from binFileTransfer_core import open_due_link
            ser = open_due_link(port, self.tdbg_tab._log_callback_threadsafe)
            self.root.after(0, lambda: self._on_app_connect_done(ser))

        threading.Thread(target=work, daemon=True).start()

    def _on_app_connect_done(self, ser) -> None:
        self._conn_busy = False
        if ser is None:
            self._set_conn_status("Disconnected", _COLORS["danger_dark"])
            self._connect_btn.config(state=tk.NORMAL)
            self.unlock_port_entry()
            self.set_status("Connect failed", _COLORS["danger_dark"])
            return
        from binFileTransfer_core import GpioSession, TdbgSession, RecordSession
        self._ser = ser
        # All three sessions BORROW the one serial and share the lock that
        # serialises GPIO/TDBG transactions. Each logs to its own tab.
        self.gpio_session = GpioSession(
            self.gpio_tab._log_callback_threadsafe,
            ser=ser, lock=self._serial_lock)
        self.tdbg_session = TdbgSession(
            self.tdbg_tab._log_callback_threadsafe,
            ser=ser, lock=self._serial_lock)
        self.record_session = RecordSession(
            self.record_tab._log_callback_threadsafe,
            ser=ser, lock=self._serial_lock)
        self._connected = True
        self._set_conn_status("Connected", _COLORS["success_dark"])
        self._disconnect_btn.config(state=tk.NORMAL)
        for t in (self.gpio_tab, self.tdbg_tab, self.record_tab):
            t.set_connected(True)
        self.set_status("Connected", _COLORS["success_dark"])

    def set_recording(self, active: bool) -> None:
        """RECORD capture is exclusive — its live_loop owns the shared serial
        while running. Gate the GPIO and TDBG tabs' controls off during a
        recording so their workers can't write to the port concurrently.
        Re-enables them when recording stops (if still connected)."""
        if not self._connected:
            return
        self.gpio_tab.set_connected(not active)
        self.tdbg_tab.set_connected(not active)

    def _on_app_disconnect(self) -> None:
        self.disconnect_then(None)

    def disconnect_then(self, on_done) -> None:
        """Tear down the shared connection (used by the Disconnect button and
        by FlashTab before a destructive flash), then call on_done() on the
        Tk thread if given. Idempotent — safe to call when not connected."""
        if not self._connected:
            if on_done is not None:
                on_done()
            return
        self._set_conn_status("Disconnecting...", _COLORS["warning_dark"])
        self._disconnect_btn.config(state=tk.DISABLED)
        # Disable the three tabs' controls immediately; close the serial on a
        # transient thread (RECORD's live_loop join may take up to ~2 s).
        for t in (self.gpio_tab, self.tdbg_tab, self.record_tab):
            t.set_connected(False)
        sessions = [s for s in
                    (self.record_session, self.tdbg_session, self.gpio_session)
                    if s is not None]
        ser = self._ser

        def work():
            for s in sessions:
                try:
                    s.close()       # detaches (doesn't own ser)
                except Exception:
                    pass
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self._on_app_disconnect_done(on_done))

        threading.Thread(target=work, daemon=True).start()

    def _on_app_disconnect_done(self, on_done) -> None:
        self._ser = None
        self.gpio_session = None
        self.tdbg_session = None
        self.record_session = None
        self._connected = False
        self._set_conn_status("Disconnected", _COLORS["danger_dark"])
        self._connect_btn.config(state=tk.NORMAL)
        self.unlock_port_entry()
        self.set_status("Disconnected", _COLORS["text_secondary"])
        if on_done is not None:
            on_done()

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
        # Close any open preview Toplevel windows BEFORE destroying the
        # root, otherwise Tk leaves them as orphaned floating windows that
        # outlive the app on some platforms.
        for tab in ("tdbg_tab", "record_tab"):
            t = getattr(self, tab, None)
            if t is not None and getattr(t, "_preview_window", None) is not None:
                try:
                    t._close_preview()
                except Exception:
                    pass
        # Release the shared serial connection (if open) so the OS frees the
        # COM port. Close synchronously here — sessions just detach and the
        # serial close is fast.
        if self._connected:
            for s in (self.record_session, self.tdbg_session, self.gpio_session):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            if self._ser is not None:
                try:
                    self._ser.close()
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
