"""Shared data + pure helpers for the Qt GUI.

Extracted verbatim from the original Tkinter GUI so the serial/protocol-
adjacent constants and the embedded TDBG calibration capture survive the
PySide6 rewrite unchanged. No UI imports here — safe to import anywhere.
"""
from __future__ import annotations


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



def _format_duration_ns(ns: float) -> str:
    """Pretty-print a duration in ns for waveform labels."""
    if ns < 1000:
        return f"{int(round(ns))} ns"
    if ns < 1_000_000:
        return f"{ns / 1000:.2f} µs"   # µs
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.3f} ms"
    return f"{ns / 1_000_000_000:.3f} s"


# Single source of truth for the two time-unit → label conversions the
# waveform views need (was duplicated as _cycles_to_label + an inline
# `lambda us: _format_duration_ns(us*1000)`).
def format_us(us: float) -> str:
    """Microseconds → label."""
    return _format_duration_ns(us * 1000)
