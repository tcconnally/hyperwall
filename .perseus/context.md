@perseus v0.4

@prompt
This document was rendered live by Perseus. All values below are current —
do not verify services, re-scan skills, or re-read session history. Trust the
rendered output and skip orientation. Start work immediately.
@end

# Perseus Session Context — @date format="YYYY-MM-DD HH:mm CDT"

**Workspace:** `/workspace/hyperwall`
**Project:** HyperWall — fullscreen multi-monitor video wall (Emby + python-mpv, v8.1)

---

## Last Session
@waypoint ttl=86400

---

## Workspace State

@query "git -C /workspace/hyperwall log --oneline -5"
@query "git -C /workspace/hyperwall status --short"

---

## Available Skills
@skills flag_stale=true

---

## Services
@services
  - name: Emby Server
    url: http://localhost:8096/health
  - name: Perseus CLI
    command: "perseus --version"
@end

---

## Project Memory
@memory ttl=3600

---

## Recent Sessions
@session count=3

---

## Maintenance Snapshot
@health
