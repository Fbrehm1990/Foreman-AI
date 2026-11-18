"""Progress reporter for workflow runs; the dashboard reads tasks/status.json.

  python dashboard/progress.py start --run "Queue worker"
  python dashboard/progress.py step  --task "<exact task title>" --type research --step 2 [--note "12 sources so far"]
  python dashboard/progress.py done  --task "<exact task title>" [--blocked]
  python dashboard/progress.py end

Every run keeps its own record under "runs" in status.json, so runs working side by side never reset or hide
each other: one run's `end` only ends that run. A run is recognised by its Claude Code process (the CLAUDE_PID
variable Claude Code gives every command it runs, or the nearest claude.exe parent process); helper agents run
inside that same process, so their steps land in the same record. AIWF_RUN_ID overrides this (used by tests).
The top-level "run", "tasks" and "heartbeat" keys are a summary of all runs still working, in the same shape as
before, so the dashboard, phone sync and dispatcher read it unchanged (see summarize()).
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "tasks" / "status.json"
LOCK = STATUS.with_suffix(".lock")
KEEP_HOURS = 2                                        # runs and tasks with no update for this long are dropped

STEPS = {
    "research": ["Planning", "Searching", "Reading sources", "Verifying", "Writing report"],
    "code": ["Setting up branch", "Reading code", "Implementing", "Testing", "Handing off"],
    "docs": ["Reading inputs", "Transforming", "Checking output", "Writing notes"],
    "brief": ["Gathering news", "Writing brief"],
    "digest": ["Reading today's work", "Writing digest"],
    "web": ["Planning the site", "Building", "Checking quality", "Handing off"],
    "test": ["Setting up", "Running checks", "Investigating failures", "Writing report"],
    "marketing": ["Researching audience", "Drafting", "Reviewing", "Submitting for approval"],
    "plan": ["Reviewing work", "Planning", "Assigning tasks"],
    "other": ["Working"],
}


def now():
    return datetime.now().isoformat(timespec="seconds")


class Locked:
    def __enter__(self):
        for _ in range(100):
            try:
                os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL))
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK.stat().st_mtime > 10:  # stale lock from a crashed writer
                        LOCK.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
                time.sleep(0.1)
        raise SystemExit("progress: could not lock status file")

    def __exit__(self, *a):
        LOCK.unlink(missing_ok=True)


# ---------- which run is calling ----------

def _claude_ancestor():
    """Pid of the nearest claude.exe parent process (Windows), or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(2, 0)        # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        procs = {}
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(e))
        k.CloseHandle(snap)
        pid = procs.get(os.getpid(), (None,))[0]
        for _ in range(12):
            if pid not in procs:
                return None
            parent, name = procs[pid]
            if name.startswith("claude"):
                return pid
            pid = parent
    except Exception:  # noqa: BLE001 - best effort only; falls back to the shared record
        return None
    return None


def run_identity():
    """(run id, pid or None) for the run making this call."""
    rid = os.environ.get("AIWF_RUN_ID", "").strip()
    if rid:
        return rid, None
    pid = os.environ.get("CLAUDE_PID", "").strip()
    pid = int(pid) if pid.isdigit() else _claude_ancestor()
    if pid:
        return f"claude-{pid}", pid
    return "shared", None                              # unknown caller: behaves like the old single record


def alive(pid):
    """Is that process still running? No pid recorded: assume it is."""
    if not pid:
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    import ctypes  # os.kill(pid, 0) would terminate the process on Windows, so ask the OS instead
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259           # STILL_ACTIVE


# ---------- the file ----------

def runs_of(s):
    """The per-run records of a status dict. An older single-record file reads as one run."""
    s = s or {}
    if isinstance(s.get("runs"), dict):
        return s["runs"]
    run = s.get("run")
    if not run:
        return {}
    return {"legacy": dict(run, heartbeat=s.get("heartbeat") or run.get("started"), tasks=s.get("tasks") or {})}


def is_active(r, fresh_minutes=None):
    """A run that hasn't ended, whose process (when known) is alive and, if asked, reported in the last N minutes."""
    if not r or r.get("ended") or not alive(r.get("pid")):
        return False
    if fresh_minutes is None:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(r.get("heartbeat") or "")).total_seconds() < fresh_minutes * 60
    except ValueError:
        return False


def summarize(s):
    """status.json with the top-level run/tasks/heartbeat summed over the runs still working.

    While any run works: run.name lists them, started is the earliest start, done and blocked are summed, and
    tasks holds every working run's tasks. When none works: the most recent run, ended, with no tasks.
    """
    if s is None:
        return None
    runs = runs_of(s)
    out = {"runs": runs, "recent": s.get("recent", [])}
    active = sorted((r for r in runs.values() if is_active(r)), key=lambda r: r.get("started") or "")
    if active:
        names = []
        for r in active:
            if r.get("name") not in names:
                names.append(r.get("name"))
        out["run"] = {"name": names[0] if len(active) == 1 else f"{len(active)} runs: " + ", ".join(names),
                      "started": active[0].get("started"), "ended": None, "runs": len(active),
                      "done": sum(r.get("done", 0) for r in active), "blocked": sum(r.get("blocked", 0) for r in active)}
        out["tasks"] = {k: v for r in active for k, v in (r.get("tasks") or {}).items()}
        out["heartbeat"] = max(r.get("heartbeat") or "" for r in active) or None
    elif runs:
        last = max(runs.values(), key=lambda r: (r.get("ended") or r.get("heartbeat") or "", r.get("started") or ""))
        out["run"] = {k: last.get(k) for k in ("name", "started", "done", "blocked")}
        out["run"]["ended"] = last.get("ended") or last.get("heartbeat")   # its process stopped without `end`
        out["tasks"] = {}
        out["heartbeat"] = last.get("heartbeat")
    else:
        out.update(run=None, tasks={}, heartbeat=None)
    return out


def load():
    try:
        s = json.loads(STATUS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    return {"runs": runs_of(s), "recent": s.get("recent", [])}


def save(s):
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(summarize(s), indent=2), encoding="utf-8")
    os.replace(tmp, STATUS)


def new_run(name, pid, implicit=False):
    r = {"name": name, "started": now(), "ended": None, "done": 0, "blocked": 0, "heartbeat": now(), "tasks": {}}
    if pid:
        r["pid"] = pid
    if implicit:
        r["implicit"] = True                          # made by a step outside start/end; ends when its tasks do
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "step", "done", "end"])
    ap.add_argument("--run", default="Manual run")
    ap.add_argument("--task")
    ap.add_argument("--type", default="other")
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--note", default="")
    ap.add_argument("--blocked", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("step", "done") and not a.task:
        raise SystemExit("progress: --task is required")
    rid, pid = run_identity()

    with Locked():
        s = load()
        cutoff = (datetime.now() - timedelta(hours=KEEP_HOURS)).isoformat()
        runs = {}
        for k, r in s["runs"].items():               # forget old runs and old tasks
            r["tasks"] = {t: v for t, v in (r.get("tasks") or {}).items() if v.get("updated", "") > cutoff}
            if (r.get("heartbeat") or r.get("started") or "") > cutoff or k == rid:
                runs[k] = r
        s["runs"] = runs
        run = runs.get(rid)

        if a.cmd == "start":
            runs[rid] = run = new_run(a.run, pid)
        elif a.cmd == "end":
            if not run and "legacy" in runs:          # a run that started before per-run records existed
                run = runs["legacy"]
            if run and not run.get("ended"):
                run["ended"] = now()
                run["tasks"] = {}
        else:
            if not run or run.get("ended"):           # a step outside start/end still shows up
                runs[rid] = run = new_run(a.run, pid, implicit=True)
            title = " ".join(a.task.split())
            if a.cmd == "step":
                t = run["tasks"].get(title, {"started": now()})
                typ = a.type if a.type in STEPS else t.get("type", "other")
                steps = STEPS[typ]
                n = max(1, min(a.step, len(steps)))
                t.update(type=typ, step=n, total=len(steps), label=steps[n - 1], note=a.note, updated=now())
                run["tasks"][title] = t
            else:
                t = run["tasks"].pop(title, None)
                if t is None:                         # reported by another run (e.g. the queue worker's helper)
                    for other in runs.values():
                        if title in (other.get("tasks") or {}):
                            t = other["tasks"].pop(title)
                            break
                t = t or {}
                run["blocked" if a.blocked else "done"] += 1
                s["recent"] = ([{"title": title, "type": t.get("type", a.type), "finished": now(), "blocked": a.blocked}]
                               + s.get("recent", []))[:5]
                if run.get("implicit") and not run["tasks"]:
                    run["ended"] = now()
        if run:
            run["heartbeat"] = now()
        save(s)


if __name__ == "__main__":
    main()
