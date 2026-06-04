"""Qt theme for the GUI — palette + QSS stylesheet + helpers.

Design: dark vertical sidebar, light content area with white rounded cards,
blue accent. Hex values are carried over from the original Tkinter palette so
status / log / waveform colours stay consistent across the rewrite.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget


PALETTE = {
    # light content
    "content_bg":     "#eef0f3",
    "card_bg":        "#ffffff",
    "surface_2":      "#f5f5f7",
    "surface_hover":  "#e9eaee",
    "separator":      "#d8dade",
    "text_primary":   "#1d1d1f",
    "text_secondary": "#6e6e73",
    "text_disabled":  "#b8b8be",
    # accent
    "accent":         "#007aff",
    "accent_dark":    "#0050b3",
    # dark sidebar / header strip
    "sidebar_bg":     "#1b1e27",
    "sidebar_text":   "#c4c7cf",
    "sidebar_hover":  "#2a2e3a",
    "sidebar_sub":    "#7e828d",
    # status hues (text on light)
    "success_dark":   "#1f7a1f",
    "danger_dark":    "#c41a1a",
    "warning_dark":   "#a06400",
    # bright indicator hues (on dark canvases / dots)
    "success":        "#34c759",
    "danger":         "#ff3b30",
    "warning":        "#ff9500",
    # waveform canvas (QPainter-drawn, not QSS)
    "canvas_bg":      "#1a1a1a",
    "wave_orange":    "#ff9933",
    "wave_label":     "#ffe680",
    "axis_text":      "#888888",
}

# Log-level → text colour, mirrors the old LEVEL_TAGS.
LOG_COLORS = {
    "info": PALETTE["text_primary"],
    "ok":   PALETTE["success_dark"],
    "warn": PALETTE["warning_dark"],
    "err":  PALETTE["danger_dark"],
    "wait": PALETTE["text_secondary"],
}

UI_FONT_FAMILY = "Microsoft JhengHei UI"   # crisp CJK on Windows
MONO_FONT_FAMILY = "Consolas"


def card_shadow(widget: QWidget, blur: int = 18, dy: int = 2,
                alpha: int = 40) -> None:
    """Attach a soft drop shadow to a card widget (Qt can't do CSS shadows)."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


def build_qss() -> str:
    p = PALETTE
    return f"""
* {{
    font-family: "{UI_FONT_FAMILY}";
    font-size: 13px;
    color: {p['text_primary']};
}}
QMainWindow, QWidget#Root {{ background: {p['content_bg']}; }}

/* ---- sidebar ---- */
QFrame#Sidebar {{ background: {p['sidebar_bg']}; border: none; }}
QLabel#SidebarTitle {{
    color: #ffffff; font-size: 16px; font-weight: 700;
    padding: 18px 18px 4px 18px;
}}
QLabel#SidebarSub {{ color: {p['sidebar_sub']}; font-size: 11px; padding: 0 18px 14px 18px; }}
QPushButton#NavButton {{
    color: {p['sidebar_text']}; background: transparent; border: none;
    text-align: left; padding: 11px 18px; font-size: 14px; border-radius: 0;
}}
QPushButton#NavButton:hover {{ background: {p['sidebar_hover']}; }}
QPushButton#NavButton:checked {{
    color: #ffffff; background: {p['accent']}; font-weight: 700;
    border-left: 3px solid #ffffff;
}}
QLabel#ConnDot {{ font-size: 13px; }}

/* ---- header strip ---- */
QFrame#Header {{
    background: {p['card_bg']};
    border-bottom: 1px solid {p['separator']};
}}
QLabel#PageTitle {{ font-size: 18px; font-weight: 700; }}

/* ---- cards ---- */
QFrame#Card {{
    background: {p['card_bg']};
    border: 1px solid {p['separator']};
    border-radius: 10px;
}}
QLabel#CardTitle {{ font-size: 14px; font-weight: 700; color: {p['text_primary']}; }}
QLabel#Muted {{ color: {p['text_secondary']}; }}

/* ---- buttons ---- */
QPushButton {{
    background: {p['surface_2']};
    color: {p['text_primary']};
    border: 1px solid {p['separator']};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{ background: {p['surface_hover']}; }}
QPushButton:pressed {{ background: {p['separator']}; }}
QPushButton:disabled {{ color: {p['text_disabled']}; background: {p['surface_2']};
                        border-color: {p['separator']}; }}
QPushButton#accent {{
    background: {p['accent']}; color: #ffffff; border: none; font-weight: 700;
}}
QPushButton#accent:hover {{ background: {p['accent_dark']}; }}
QPushButton#accent:pressed {{ background: {p['accent_dark']}; }}
QPushButton#accent:disabled {{ background: {p['text_disabled']}; color: #ffffff; }}

/* compact toggle/secondary buttons inside dense rows */
QPushButton#chip {{ padding: 3px 8px; border-radius: 5px; font-size: 12px; }}

/* segmented [HIGH|LOW] toggle used per GPIO PinRow. Two checkable buttons
   sit flush (sub-layout spacing=0); we round only the outer corners and
   suppress the left button's right border so they read as one control.
   Selected segment fills with the accent colour. */
QPushButton#seg {{
    padding: 3px 10px; font-size: 12px; min-width: 40px;
    background: {p['surface_2']}; color: {p['text_secondary']};
    border: 1px solid {p['separator']}; border-radius: 0;
}}
QPushButton#seg:hover:!checked {{ background: {p['surface_hover']}; }}
QPushButton#seg[side="left"]  {{ border-top-left-radius: 5px;
                                 border-bottom-left-radius: 5px;
                                 border-right: none; }}
QPushButton#seg[side="right"] {{ border-top-right-radius: 5px;
                                 border-bottom-right-radius: 5px; }}
QPushButton#seg:checked {{ background: {p['accent']}; color: #ffffff;
                           border-color: {p['accent']}; }}
QPushButton#seg:disabled {{ color: {p['text_disabled']};
                            background: {p['surface_2']};
                            border-color: {p['separator']}; }}

/* ---- inputs ---- */
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {p['card_bg']}; border: 1px solid {p['separator']};
    border-radius: 6px; padding: 5px 8px; min-height: 18px;
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
    border-color: {p['accent']};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {p['card_bg']}; selection-background-color: {p['accent']};
    selection-color: #ffffff; border: 1px solid {p['separator']};
}}

/* ---- log pane ---- */
QPlainTextEdit#Log {{
    background: {p['card_bg']}; border: 1px solid {p['separator']};
    border-radius: 8px; font-family: "{MONO_FONT_FAMILY}"; font-size: 12px;
}}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c2c4cc; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #a9abb4; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #c2c4cc; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- misc ---- */
QStatusBar {{ background: {p['card_bg']}; border-top: 1px solid {p['separator']};
              color: {p['text_secondary']}; }}
QStatusBar::item {{ border: none; }}
QTabWidget::pane {{ border: 1px solid {p['separator']}; border-radius: 8px; }}
QTabBar::tab {{
    background: {p['surface_2']}; padding: 6px 14px; border: 1px solid {p['separator']};
    border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
    color: {p['text_secondary']};
}}
QTabBar::tab:selected {{ background: {p['card_bg']}; color: {p['text_primary']};
                         font-weight: 700; }}
QCheckBox, QRadioButton {{ spacing: 6px; }}
QToolTip {{ background: #2c2c2e; color: #ffffff; border: none; padding: 4px 8px; }}
"""
