"""
HyperWall Cyberpunk Theme — v8.2 visual overhaul.
Central QSS stylesheet + colour constants shared across all dialogs and widgets.

Palette
  bg_deep      #0a0a0f  — near-black with blue undertone
  bg_surface   #12121a  — card / group-box background
  bg_raised    #1a1a26  — input / list background
  accent_cyan  #00e5ff  — primary neon cyan (playback, selection)
  accent_magenta #ff007f — secondary neon magenta (alerts, tags)
  accent_amber #ffb300  — warning / attention
  text_primary #e0e0f0  — main text (slightly blue-white)
  text_dim     #707088  — subdued labels
  border_subtle #252535  — faint borders
  border_glow  #00e5ff80 — glowing cyan border (semi-transparent)
"""

# ── Colour tokens ─────────────────────────────────────────────────────────
CYAN       = "#00e5ff"
MAGENTA    = "#ff007f"
AMBER      = "#ffb300"
BG_DEEP    = "#0a0a0f"
BG_SURFACE = "#12121a"
BG_RAISED  = "#1a1a26"
TEXT       = "#e0e0f0"
TEXT_DIM   = "#707088"
BORDER     = "#252535"
BORDER_GLOW = "#00e5ff80"

# ── Global application stylesheet ─────────────────────────────────────────
GLOBAL_QSS = f"""
/* ── Base ───────────────────────────────────────────────────────────── */
QWidget {{
    background-color: {BG_DEEP};
    color: {TEXT};
    font-family: "Segoe UI", "Consolas", "Cascadia Code", monospace;
    font-size: 12px;
}}

/* ── Dialogs ─────────────────────────────────────────────────────────── */
QDialog {{
    background-color: {BG_DEEP};
    border: 1px solid {BORDER};
}}

/* ── Group boxes ─────────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 10px;
    background-color: {BG_SURFACE};
    font-weight: bold;
    font-size: 11px;
    color: {CYAN};
    letter-spacing: 1px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    background-color: {BG_DEEP};
}}

/* ── List widgets ────────────────────────────────────────────────────── */
QListWidget {{
    background-color: {BG_RAISED};
    border: 1px solid {BORDER};
    color: {TEXT};
    outline: none;
    border-radius: 4px;
    padding: 2px;
}}
QListWidget::item {{
    padding: 4px 8px;
    border-radius: 2px;
}}
QListWidget::item:selected {{
    background-color: {CYAN};
    color: {BG_DEEP};
}}
QListWidget::item:hover:!selected {{
    background-color: {BG_SURFACE};
    border: 1px solid {BORDER_GLOW};
}}

/* ── Spin boxes ──────────────────────────────────────────────────────── */
QSpinBox {{
    background-color: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    min-width: 60px;
    border-radius: 4px;
    font-size: 13px;
    font-weight: 600;
}}
QSpinBox:focus {{
    border-color: {CYAN};
}}

/* ── Buttons (primary) ───────────────────────────────────────────────── */
QPushButton {{
    background-color: #0d2847;
    color: {CYAN};
    border: 1px solid {CYAN};
    padding: 8px 18px;
    font-weight: bold;
    font-size: 12px;
    border-radius: 4px;
    letter-spacing: 1px;
    text-transform: uppercase;
}}
QPushButton:hover {{
    background-color: {CYAN};
    color: {BG_DEEP};
    border-color: {CYAN};
}}
QPushButton:pressed {{
    background-color: #009faf;
}}

/* ── Labels ──────────────────────────────────────────────────────────── */
QLabel {{
    color: {TEXT_DIM};
    background: transparent;
}}

/* ── Scroll bars ─────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {BG_DEEP};
    width: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {CYAN};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

/* ── Tooltips ────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {BG_SURFACE};
    color: {CYAN};
    border: 1px solid {BORDER_GLOW};
    padding: 4px 8px;
    border-radius: 3px;
    font-size: 11px;
}}

/* ── Message boxes ───────────────────────────────────────────────────── */
QMessageBox {{
    background-color: {BG_DEEP};
}}
QMessageBox QLabel {{
    color: {TEXT};
    font-size: 12px;
}}
QMessageBox QPushButton {{
    min-width: 80px;
}}
"""
