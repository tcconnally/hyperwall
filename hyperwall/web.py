"""
HyperWall remote — Flask HTTP server for phone/tablet control.

Runs in a daemon thread alongside the Qt main loop. AUTO_DISCOVER: on
startup the URL is logged and printed to stdout so you can scan a QR code
or bookmark it. Port defaults to 8585; override with HYPERWALL_WEB_PORT.

Exposes a JSON API plus a built-in dark-mode HTML control page.
All state reads are thread-safe (snapshot via weakref to WallController).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import weakref
from typing import Any

from flask import Flask, Response, jsonify, request

logger = logging.getLogger("HyperWall")

_PORT = int(os.environ.get("HYPERWALL_WEB_PORT", "8585"))

app = Flask("hyperwall-remote")

_controller_ref: weakref.ReferenceType | None = None


# ── helpers ──────────────────────────────────────────────────────────────
def _ctl():
    if _controller_ref is None:
        return None
    return _controller_ref()


def _cell_snapshot(cell) -> dict[str, Any]:
    """Thread-safe read of cell state."""
    item = cell.current_item
    return {
        "item": (item or {}).get("Name", ""),
        "item_id": (item or {}).get("Id", ""),
        "muted": cell.muted,
        "looping": cell.looping,
        "playing": not bool(cell._mpv["pause"]) if cell._mpv is not None else False,
        "duration_s": round(cell._duration_s, 1),
    }


# ── API ──────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    cells = [_cell_snapshot(c) for c in ctl.cells]
    return jsonify({
        "cells": cells,
        "n_cells": len(cells),
        "filter": "favorites" if (ctl.filtered != ctl.all_items and ctl.filtered) else "all",
        "n_filtered": len(ctl.filtered),
        "n_all": len(ctl.all_items),
        "controls_visible": ctl.controls_visible,
        "uptime_s": time.time() - ctl._start_ts if hasattr(ctl, "_start_ts") else -1,
    })


@app.route("/api/pause", methods=["POST"])
def api_pause():
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    ctl._global_toggle_pause()
    return jsonify({"ok": True})


@app.route("/api/next/<int:cell_idx>", methods=["POST"])
def api_next(cell_idx: int):
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    if cell_idx < 0 or cell_idx >= len(ctl.cells):
        return jsonify({"error": f"cell {cell_idx} out of range (0–{len(ctl.cells)-1})"}), 400
    ctl.next_video(ctl.cells[cell_idx], False)
    return jsonify({"ok": True})


@app.route("/api/prev/<int:cell_idx>", methods=["POST"])
def api_prev(cell_idx: int):
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    if cell_idx < 0 or cell_idx >= len(ctl.cells):
        return jsonify({"error": f"cell {cell_idx} out of range (0–{len(ctl.cells)-1})"}), 400
    ctl.prev_video(ctl.cells[cell_idx])
    return jsonify({"ok": True})


@app.route("/api/loop/<int:cell_idx>", methods=["POST"])
def api_loop(cell_idx: int):
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    if cell_idx < 0 or cell_idx >= len(ctl.cells):
        return jsonify({"error": f"cell {cell_idx} out of range (0–{len(ctl.cells)-1})"}), 400
    cell = ctl.cells[cell_idx]
    cell._toggle_loop()
    return jsonify({"ok": True, "looping": cell.looping})


@app.route("/api/mute/<int:cell_idx>", methods=["POST"])
def api_mute(cell_idx: int):
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    if cell_idx < 0 or cell_idx >= len(ctl.cells):
        return jsonify({"error": f"cell {cell_idx} out of range (0–{len(ctl.cells)-1})"}), 400
    cell = ctl.cells[cell_idx]
    cell._toggle_mute()
    return jsonify({"ok": True, "muted": cell.muted})


@app.route("/api/filter", methods=["POST"])
def api_filter():
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    mode = (request.get_json(silent=True) or {}).get("mode", "all")
    if mode not in ("all", "favorites"):
        return jsonify({"error": "mode must be 'all' or 'favorites'"}), 400
    ctl._set_filter(mode)
    return jsonify({"ok": True, "filter": mode})


@app.route("/api/controls", methods=["POST"])
def api_controls():
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    visible = (request.get_json(silent=True) or {}).get("visible")
    if visible is None:
        ctl._global_toggle_controls()
    elif visible:
        if not ctl.controls_visible:
            ctl._global_toggle_controls()
    else:
        if ctl.controls_visible:
            ctl._global_toggle_controls()
    return jsonify({"ok": True, "visible": ctl.controls_visible})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    ctl = _ctl()
    if ctl is None:
        return jsonify({"error": "no controller"}), 503
    ctl._shutdown()
    return jsonify({"ok": True})


# ── Built-in control page ────────────────────────────────────────────────
_CTRL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>HyperWall Remote</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d0d0d;color:#ccc;padding:16px;max-width:600px;margin:0 auto}
  h1{font-size:18px;font-weight:700;letter-spacing:2px;color:#3b8edb;text-align:center;margin-bottom:4px}
  .sub{text-align:center;color:#555;font-size:11px;margin-bottom:14px}
  .cells{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}
  .cell{background:#141414;border:1px solid #222;border-radius:6px;padding:10px;font-size:12px}
  .cell-idx{color:#3b8edb;font-weight:700;font-size:11px;margin-bottom:2px}
  .cell-title{color:#ddd;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px}
  .cell-info{color:#666;font-size:10px;margin-bottom:6px}
  .cell-btns{display:flex;gap:4px;flex-wrap:wrap}
  .cell-btns button{flex:1;min-width:40px;padding:5px 4px;border:1px solid #333;border-radius:3px;background:#1a1a1a;color:#aaa;font-size:13px;cursor:pointer;transition:all .12s}
  .cell-btns button:active{background:#2563a8;color:white;border-color:#3b8edb}
  .global{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
  .global button{flex:1;min-width:60px;padding:10px 8px;border:1px solid #333;border-radius:5px;background:#1a1a1a;color:#ccc;font-size:13px;font-weight:600;cursor:pointer;transition:all .12s}
  .global button:active{background:#2563a8;color:white;border-color:#3b8edb}
  .global .danger:active{background:#8b1a1a;border-color:#d43535}
  #status{color:#555;text-align:center;font-size:10px;margin-top:10px}
</style>
</head>
<body>
<h1>HYPERWALL</h1>
<div class="sub"><span id="status">connecting…</span></div>
<div id="cells" class="cells"></div>
<div class="global">
  <button onclick="api('pause')">⏯ Pause</button>
  <button onclick="api('filter','favorites')">⭐ Favs</button>
  <button onclick="api('filter','all')">📂 All</button>
  <button onclick="api('controls')">👁 Controls</button>
  <button class="danger" onclick="if(confirm('Shut down the wall?'))api('shutdown')">⏻ Exit</button>
</div>
<script>
const BASE='/api';
let filter='all';
function api(action,arg){
  let url=BASE+'/'+action,opts={method:'POST',headers:{'Content-Type':'application/json'}};
  if(action==='pause'||action==='controls'||action==='shutdown'){}
  else if(action==='filter'){opts.body=JSON.stringify({mode:arg});filter=arg}
  else if(action.startsWith('next')||action.startsWith('prev')||action.startsWith('loop')||action.startsWith('mute')){
    url=BASE+'/'+action
  }
  fetch(url,opts).then(r=>r.json()).then(d=>{if(d.ok)refresh()}).catch(e=>{})
}
function refresh(){
  fetch(BASE+'/status').then(r=>r.json()).then(d=>{
    document.getElementById('status').textContent=d.n_cells+' cells · '+d.filter+' ('+d.n_filtered+'/'+d.n_all+' items)';
    filter=d.filter;
    let h='';
    d.cells.forEach((c,i)=>{
      let icon=c.muted?'🔇':'🔊';
      let dur=c.duration_s>0?c.duration_s+'s':'';
      h+=`<div class="cell">
        <div class="cell-idx">CELL ${i} `+(c.playing?'▶':'⏸')+` ${icon} `+(c.looping?'🔁':'')+`</div>
        <div class="cell-title">${esc(c.item||'—')}</div>
        <div class="cell-info">${dur}</div>
        <div class="cell-btns">
          <button onclick="api('next/${i}')">⏭</button>
          <button onclick="api('prev/${i}')">⏮</button>
          <button onclick="api('loop/${i}')">🔁</button>
          <button onclick="api('mute/${i}')">${icon}</button>
        </div></div>`
    });
    document.getElementById('cells').innerHTML=h;
  }).catch(e=>{document.getElementById('status').textContent='disconnected'})
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
setInterval(refresh,3000);
refresh();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return Response(_CTRL_HTML, mimetype="text/html")


# ── Server lifecycle ─────────────────────────────────────────────────────
def _local_ips() -> list[str]:
    """Discover local network IPs for the startup banner."""
    ips = []
    try:
        # Quick heuristic: connect a UDP socket to get the preferred local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # Also grab all non-loopback interface IPs
    try:
        import netifaces
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface)
            for addr in addrs.get(netifaces.AF_INET, []):
                ip = addr.get("addr", "")
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
    except ImportError:
        pass
    return ips


def start(controller, port: int = _PORT):
    """Start Flask in a daemon thread. Safe to call from Qt main thread.

    The controller is held via weakref so the server never blocks GC
    of the wall during shutdown.
    """
    global _controller_ref
    _controller_ref = weakref.ref(controller)
    controller._start_ts = time.time()

    # Suppress Flask's default startup banner (we print our own).
    import flask.cli
    flask.cli.show_server_banner = lambda *a, **kw: None

    ips = _local_ips()
    url = f"http://{ips[0]}:{port}" if ips else f"http://localhost:{port}"
    logger.info("HyperWall Remote: %s", url)
    print(f"\n{'='*50}\n  HYPERWALL REMOTE\n  {url}\n{'='*50}\n")

    t = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="hyperwall-web",
    )
    t.start()
