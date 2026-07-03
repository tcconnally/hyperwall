"""
Hyperwall — one source of truth for the look.

Pure data + string helpers (no PyQt import at module load) so the palette can
be imported anywhere cheaply, including headless tests. `apply(app)` opts into
a matching Fusion QPalette for native widgets (dialogs, message boxes).

Brand accent stays the Hyperwall blue (#3b8edb) used by the web remote, so all
surfaces read as one product. Video cells themselves stay pure black — this
theme styles the *chrome* (wizard, control bars, overlays), never the frame.
"""

from __future__ import annotations

# ── Palette ───────────────────────────────────────────────────────────────────
# Cool, near-black surfaces layered light→dark by elevation.
SURFACE_0 = "#0e1116"   # dialog / window base
SURFACE_1 = "#151a21"   # group / panel
SURFACE_2 = "#1c222c"   # inputs, list rows
SURFACE_3 = "#252d39"   # hover / elevated
VIDEO_BG  = "#000000"   # behind video frames — always pure black

BORDER        = "#2a323d"
BORDER_STRONG = "#3a4553"

TEXT       = "#e8ebf0"  # primary
TEXT_DIM   = "#9aa4b2"  # secondary / labels
TEXT_MUTED = "#5c6675"  # tertiary / hints

# On-brand accent ramp.
ACCENT        = "#3b8edb"
ACCENT_BRIGHT = "#5aa7f0"
ACCENT_DIM    = "#1e4f78"
ACCENT_DEEP   = "#163a5c"

DANGER     = "#d43535"
DANGER_DIM = "#8b1a1a"

FONT = "'Segoe UI', system-ui, sans-serif"

RADIUS    = 6
RADIUS_SM = 4


def rgba(hex_color: str, alpha: float) -> str:
    """`rgba(r, g, b, a)` from a #rrggbb string and 0.0–1.0 alpha."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"


def dialog_qss() -> str:
    """Cohesive stylesheet for dialogs/wizard-style chrome (QDialog subtree)."""
    return f"""
        QDialog, QWidget#hwRoot {{
            background: {SURFACE_0}; color: {TEXT}; font-family: {FONT};
        }}
        QLabel {{ color: {TEXT_DIM}; font-size: 11px; background: transparent; }}
        QGroupBox {{
            border: 1px solid {BORDER}; border-radius: {RADIUS}px; margin-top: 10px;
            padding-top: 6px; font-weight: 700; font-size: 11px;
            color: {ACCENT}; background: {SURFACE_1};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; left: 10px; padding: 0 5px;
            letter-spacing: 1px;
        }}
        QListWidget {{
            background: {SURFACE_2}; border: 1px solid {BORDER};
            border-radius: {RADIUS_SM}px; color: {TEXT}; outline: none; padding: 3px;
        }}
        QListWidget::item {{ padding: 6px 8px; border-radius: {RADIUS_SM}px; }}
        QListWidget::item:hover {{ background: {SURFACE_3}; }}
        QListWidget::item:selected {{
            background: {ACCENT_DIM}; color: white;
        }}
        QSpinBox {{
            background: {SURFACE_2}; color: {TEXT};
            border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;
            padding: 5px 6px; min-width: 54px;
        }}
        QSpinBox:focus {{ border-color: {ACCENT}; }}
        QPushButton {{
            background: {ACCENT_DIM}; color: white; border: none;
            padding: 10px 22px; font-weight: 700; font-size: 13px;
            border-radius: {RADIUS}px;
        }}
        QPushButton:hover  {{ background: {ACCENT}; }}
        QPushButton:pressed {{ background: {ACCENT_DEEP}; }}
        QToolTip {{
            background: {SURFACE_3}; color: {TEXT};
            border: 1px solid {BORDER_STRONG}; padding: 4px 6px;
        }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
        QScrollBar::handle:vertical {{
            background: {BORDER_STRONG}; border-radius: 5px; min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def apply(app) -> None:
    """Apply Fusion + a matching dark QPalette so native chrome (QMessageBox,
    combo popups, focus rings) blends with the custom stylesheets."""
    from PyQt6.QtGui import QColor, QPalette

    app.setStyle("Fusion")

    def c(hexstr: str) -> "QColor":
        return QColor(hexstr)

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, c(SURFACE_0))
    pal.setColor(QPalette.ColorRole.WindowText, c(TEXT))
    pal.setColor(QPalette.ColorRole.Base, c(SURFACE_2))
    pal.setColor(QPalette.ColorRole.AlternateBase, c(SURFACE_1))
    pal.setColor(QPalette.ColorRole.Text, c(TEXT))
    pal.setColor(QPalette.ColorRole.Button, c(SURFACE_1))
    pal.setColor(QPalette.ColorRole.ButtonText, c(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, c(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, c("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c(SURFACE_3))
    pal.setColor(QPalette.ColorRole.ToolTipText, c(TEXT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c(TEXT_MUTED))
    app.setPalette(pal)
