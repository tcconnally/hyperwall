"""
hyperwall_remix.py — PyQt6 dialog that drives trio_remix_mv.py on greg.

Public API:
    remix_walls(parent: QWidget | None = None) -> None

When called, this:
  1. Opens a folder picker for the new-clips directory.  Default starting
     location is G:\\media\\etc\\df\\JulieB (the JulieB pool).  If the user
     cancels, no run is started.
  2. Translates the Windows path (G:\\... or \\\\greg\\greg\\...) to greg's
     host path (/mnt/user/greg/...).
  3. Spawns `ssh greg python3 /mnt/user/greg/scripts/trio_remix_mv.py
     --root <ROOT> --new-clips <DIR> --no-prompt` in a QThread, streaming
     stdout/stderr line-by-line into a log dialog.
  4. The dialog is non-modal so HyperWall keeps playing.  Closing it during
     a run prompts for confirmation; a successful exit leaves the close
     button enabled.

Typical wiring inside hyperwall_v8.py
─────────────────────────────────────
Add a button or menu action that imports and calls this:

    from hyperwall_remix import remix_walls

    # ... in your controls strip / menu setup:
    btn_remix = QPushButton("Remix walls", self)
    btn_remix.clicked.connect(lambda: remix_walls(self))

The function is fire-and-forget — it owns its own dialog and thread.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

# ── config ────────────────────────────────────────────────────────────────────
SSH_HOST          = "greg"
GREG_SCRIPT       = "/mnt/user/greg/scripts/trio_remix_mv.py"
GREG_ROOT         = "/mnt/user/greg/media/etc/mv/trios"
DEFAULT_NEW_CLIPS = r"G:\media\etc\vertical"

# Path translations: Windows ↔ greg host
WINDOWS_DRIVE_MAP = {
    "G:": "/mnt/user/greg",       # \\greg\greg
    "g:": "/mnt/user/greg",
}
UNC_PREFIX_GREG = r"\\greg\greg".lower()
# ──────────────────────────────────────────────────────────────────────────────


def windows_to_greg_path(p: str) -> Optional[str]:
    """Translate a Windows-side path (drive-letter or UNC) to greg's host
    path.  Returns None if the path doesn't appear to live on greg."""
    if not p:
        return None
    s = p.replace("\\", "/")
    head = s[:2]

    # Drive-letter case (G:\…)
    if head in WINDOWS_DRIVE_MAP:
        rest = s[2:].lstrip("/")
        return f"{WINDOWS_DRIVE_MAP[head]}/{rest}".rstrip("/")

    # UNC case (\\greg\greg\…)
    low = s.lower()
    if low.startswith("//greg/greg/"):
        rest = s[len("//greg/greg/"):]
        return f"/mnt/user/greg/{rest}".rstrip("/")

    # Already a unix path that starts under /mnt/user/greg — pass through
    if s.startswith("/mnt/user/greg/"):
        return s.rstrip("/")

    return None


# ── worker thread ─────────────────────────────────────────────────────────────
class _RemixWorker(QThread):
    """Streams `ssh greg python3 trio_remix_mv.py …` line-by-line."""
    line     = pyqtSignal(str)
    finished_with = pyqtSignal(int)   # exit code

    def __init__(self, root: str, new_clips_greg: Optional[str],
                 keep_clips: bool = False, parent=None):
        super().__init__(parent)
        self._root = root
        self._new  = new_clips_greg
        self._keep = keep_clips
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def run(self) -> None:
        remote_cmd_parts = [
            "python3", GREG_SCRIPT,
            "--root", self._root,
            "--no-prompt",
        ]
        if self._new:
            remote_cmd_parts += ["--new-clips", self._new]
        if self._keep:
            remote_cmd_parts += ["--keep-clips"]
        # shlex-quote each remote arg so spaces/specials survive ssh's shell
        remote_cmd = " ".join(shlex.quote(a) for a in remote_cmd_parts)
        cmd = ["ssh", SSH_HOST, remote_cmd]
        self.line.emit(f"$ {' '.join(cmd)}\n")

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.line.emit(f"[ERROR] failed to spawn ssh: {e}\n")
            self.finished_with.emit(-1)
            return

        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._cancelled:
                break
            self.line.emit(raw.rstrip("\n"))

        rc = self._proc.wait()
        self.finished_with.emit(rc)

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass


# ── dialog ────────────────────────────────────────────────────────────────────
class _RemixDialog(QDialog):
    def __init__(self, new_clips_greg: Optional[str], keep_clips: bool,
                 parent: Optional[QWidget]):
        super().__init__(parent)
        self.setWindowTitle("Remix trio walls")
        self.setModal(False)
        self.resize(900, 540)

        self._worker: Optional[_RemixWorker] = None
        self._done = False

        clips_line = (new_clips_greg or "— none (remix existing only) —")
        if new_clips_greg:
            clips_line += "  (will be " + ("kept" if keep_clips else "<b>deleted</b>") + " after run)"
        header = QLabel(
            f"<b>Root:</b> {GREG_ROOT}<br>"
            f"<b>New clips:</b> {clips_line}"
        )
        header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Consolas", 10))

        self._btn_close = QPushButton("Close", self)
        self._btn_close.clicked.connect(self.close)

        btnrow = QHBoxLayout()
        btnrow.addStretch(1)
        btnrow.addWidget(self._btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(header)
        lay.addWidget(self._log, 1)
        lay.addLayout(btnrow)

        self._start(new_clips_greg, keep_clips)

    def _start(self, new_clips_greg: Optional[str], keep_clips: bool) -> None:
        self._worker = _RemixWorker(GREG_ROOT, new_clips_greg, keep_clips, self)
        self._worker.line.connect(self._append)
        self._worker.finished_with.connect(self._on_finished)
        self._worker.start()

    def _append(self, s: str) -> None:
        self._log.appendPlainText(s)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_finished(self, rc: int) -> None:
        self._done = True
        if rc == 0:
            self._append("\n[done] remix completed successfully.")
        else:
            self._append(f"\n[done] remix exited with code {rc}.")

    def closeEvent(self, ev) -> None:
        if not self._done and self._worker is not None and self._worker.isRunning():
            choice = QMessageBox.question(
                self, "Cancel remix?",
                "The remix is still running. Cancel it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self._worker.cancel()
            self._worker.wait(2000)
        super().closeEvent(ev)


# ── public entry point ────────────────────────────────────────────────────────
def remix_walls(parent: Optional[QWidget] = None) -> None:
    """Pop a folder picker for the new-clips dir, then spawn the remix on
    greg in a non-modal log dialog. Safe to call from any HyperWall handler.

    By default the script deletes the new-clips files after a successful
    remix; the dialog header shows whether keep-clips is on, and the user
    can toggle it via Shift-click (or by editing DEFAULT_KEEP_CLIPS).
    """
    start_dir = DEFAULT_NEW_CLIPS if os.path.isdir(DEFAULT_NEW_CLIPS) else r"G:\media\etc"

    picked = QFileDialog.getExistingDirectory(
        parent,
        "Pick a directory of new clips to mix into the wall pool "
        "(Cancel = remix existing walls only)",
        start_dir,
    )

    new_clips_greg: Optional[str] = None
    if picked:
        translated = windows_to_greg_path(picked)
        if translated is None:
            QMessageBox.warning(
                parent, "Path not on greg",
                f"That path isn't on the greg share:\n  {picked}\n\n"
                f"Pick something under G:\\ or \\\\greg\\greg\\, or cancel "
                f"to remix existing walls only.",
            )
            return
        new_clips_greg = translated

    # Confirmation dialog with a "keep clips" checkbox so deletion is
    # explicit rather than a surprise.
    msg = QMessageBox(parent)
    msg.setWindowTitle("Remix walls")
    msg.setIcon(QMessageBox.Icon.Question)
    if new_clips_greg:
        msg.setText(
            f"Remix walls in <b>{GREG_ROOT}</b> with new clips from"
            f"<br><b>{new_clips_greg}</b>?"
        )
    else:
        msg.setText(
            f"Remix existing walls in <b>{GREG_ROOT}</b> only "
            f"(no new clips)?"
        )
    keep_box = QCheckBox("Keep new clips after the remix (don't delete them)")
    keep_box.setChecked(False)
    if not new_clips_greg:
        keep_box.setEnabled(False)        # nothing to delete
    msg.setCheckBox(keep_box)
    msg.setStandardButtons(
        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
    )
    if msg.exec() != QMessageBox.StandardButton.Ok:
        return

    dlg = _RemixDialog(new_clips_greg, keep_box.isChecked(), parent)
    dlg.show()           # non-modal — HyperWall keeps playing
    # Keep a reference so it isn't GC'd; attach to parent if available.
    if parent is not None:
        parent._remix_dialog_keepalive = dlg   # type: ignore[attr-defined]
    else:
        _RemixDialog._keepalive = dlg          # type: ignore[attr-defined]


# Stand-alone smoke test
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    remix_walls()
    sys.exit(app.exec())
