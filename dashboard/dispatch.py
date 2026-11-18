"""Start the team when work is waiting.

  python dashboard/dispatch.py         -> prints START or IDLE, plus a reason (check only)
  python dashboard/dispatch.py start   -> if START, launches a headless queue run with the Claude Code CLI and
                                          prints STARTED; prints NO_CLI if the CLI isn't installed or signed in,
                                          so the caller can fall back to the scheduled task

Tasks start right away: the dashboard calls start() when you add a task and checks again every 20 seconds
(see server.auto_start), so a task added while the team is busy gets its own run instead of waiting.
Up to MAX_RUNS runs work side by side. Each run claims tasks by marking them [~], and a new run is only launched
once the previous launch has had GRACE seconds to claim its tasks, so two runs don't grab the same one.
Runs launched here are tracked by process id in tasks/dispatch-runs.json. When none is alive and no other run
(such as a scheduled one) has reported progress for 20 minutes, tasks left [~] by a crashed run go back to [ ].
Headless (-p) runs can't freeze on a permission prompt: a command that isn't pre-approved is refused instead.
team.py also calls start() after the Manager assigns work.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress  # noqa: E402
import server  # noqa: E402

ROOT = server.ROOT
PROMPT = Path(__file__).resolve().parent / "prompts" / "queue.md"
LOCK = ROOT / "tasks" / "dispatch.lock"            # older single-run lock; still honoured if present
RUNS = ROOT / "tasks" / "dispatch-runs.json"
MAX_RUNS = max(1, int(os.environ.get("AIWF_MAX_RUNS", "3")))
GRACE = 150                                          # seconds a new run gets to claim its tasks
RETRIES = ROOT / "tasks" / "dispatch-retries.json"
MAX_RECOVER = 2                                      # a task that stops its run this often is marked BLOCKED
RUN_LOGS = ROOT / "logs" / "cli-runs"


def _runs():
    """Runs launched from here that are still alive (dead ones are dropped from the file)."""
    try:
        runs = json.loads(RUNS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        runs = []
    alive = [r for r in runs if _alive(r.get("pid"))]
    if len(alive) != len(runs):
        try:
            RUNS.write_text(json.dumps(alive), encoding="utf-8")
        except OSError:
            pass
    return alive


def _other_run_active():
    """A run not launched from here (e.g. a manual or scheduled one) that hasn't ended, whose process is still
    alive, and that reported progress in the last 20 minutes. Every run has its own record in status.json, so
    another run calling `progress.py end` no longer hides one that is still working."""
    try:
        prog = json.loads(server.read(server.STATUS) or "null")
    except json.JSONDecodeError:   # mid-write: assume someone is working and look again on the next check
        return True
    return any(progress.is_active(r, fresh_minutes=20) for r in progress.runs_of(prog).values())


def recover():
    """Put tasks a crashed run left marked [~] back in the queue. Returns how many were reset."""
    if _runs() or _other_run_active():
        return 0
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < GRACE and _alive(_lock_pid()):
        return 0
    stuck = [t for t in server.parse_inbox() if t["state"] == "running"]
    if not stuck:
        return 0
    try:
        tries = json.loads(RETRIES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        tries = {}
    inbox_titles = {t["title"] for t in server.parse_inbox()}
    tries = {k: v for k, v in tries.items() if k in inbox_titles}     # forget tasks that have since finished
    lines = server.read(server.INBOX).splitlines()
    blocked = []
    for t in stuck:
        if not lines[t["line"]].startswith("- [~]"):
            continue
        tries[t["title"]] = tries.get(t["title"], 0) + 1
        if tries[t["title"]] > MAX_RECOVER:              # it keeps stopping the run: don't loop on it forever
            blocked.append((t, [t["line"]] + [i for i in range(t["line"] + 1, len(lines)) if lines[i][:1] in (" ", "\t") and lines[i].strip()]))
        else:
            lines[t["line"]] = "- [ ]" + lines[t["line"]][5:]
    drop = {i for _, idx in blocked for i in idx}
    server.write(server.INBOX, "\n".join(l for i, l in enumerate(lines) if i not in drop) + "\n")
    if blocked:
        done = server.read(server.DONE).splitlines()
        at = next((i for i, l in enumerate(done) if l.startswith("- [")), len(done))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        done[at:at] = [f"- [!] {stamp} [{t['type']}] {t['title']} → BLOCKED: the team's run stopped unexpectedly "
                       f"{MAX_RECOVER + 1} times on this task; see logs/cli-runs. Use Retry once it's fixed." for t, _ in blocked]
        server.write(server.DONE, "\n".join(done) + "\n")
        for t, _ in blocked:
            tries.pop(t["title"], None)
    try:
        RETRIES.write_text(json.dumps(tries), encoding="utf-8")
    except OSError:
        pass
    return len(stuck)


def check():
    waiting = [t for t in server.parse_inbox() if t["state"] == "queued"]
    if not waiting:
        return "IDLE", "nothing waiting in the queue"
    runs = _runs()
    if len(runs) >= MAX_RUNS:
        return "IDLE", f"{len(waiting)} waiting; {len(runs)} runs are already working (the most at once)"
    # A run launched moments ago is still claiming the tasks it was started for. Don't launch another for those,
    # but a task added since then gets its own run right away.
    recent = [r for r in runs if time.time() - r.get("at", 0) < GRACE]
    if not runs and LOCK.exists() and time.time() - LOCK.stat().st_mtime < GRACE and _alive(_lock_pid()):
        return "IDLE", f"{len(waiting)} waiting; a run started moments ago and is picking up tasks"
    covered = {raw for r in recent for raw in r.get("for", [])}
    if recent and all(t["raw"] in covered for t in waiting):
        return "IDLE", f"{len(waiting)} waiting; a run started moments ago and is picking up tasks"
    return "START", f"{len(waiting)} task(s) waiting"


def _lock_pid():
    try:
        return int(LOCK.read_text(encoding="utf-8").split()[1])
    except (OSError, IndexError, ValueError):
        return None


_PROCS = {}                                          # runs launched by this process: pid -> Popen


def _alive(pid):
    """Is the launched CLI process still running? (No pid recorded: assume it is.)"""
    if not pid:
        return True
    if pid in _PROCS:                                # our own child: poll() also reaps it once it has exited
        return _PROCS[pid].poll() is None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
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


def cli_path():
    for c in (shutil.which("claude"), str(Path.home() / ".local" / "bin" / "claude.exe")):
        if c and Path(c).exists():
            return c
    return None


def cli_ready(exe):
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30, cwd=ROOT)
        return json.loads(out.stdout or "{}").get("loggedIn") is True
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False


def start():
    state, why = check()
    if state != "START":
        return state, why
    exe = cli_path()
    if not exe or not cli_ready(exe):
        return "NO_CLI", "Claude Code CLI not installed or not signed in"
    RUN_LOGS.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":  # survive the dashboard closing; no console window
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    path = RUN_LOGS / f"{datetime.now():%Y-%m-%d_%H%M%S}.log"
    with open(path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen([exe, "-p", PROMPT.read_text(encoding="utf-8")], cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    _PROCS[proc.pid] = proc
    queued = [t["raw"] for t in server.parse_inbox() if t["state"] == "queued"]
    RUNS.write_text(json.dumps(_runs() + [{"pid": proc.pid, "at": time.time(), "log": path.name, "for": queued}]), encoding="utf-8")
    # A launch that dies within seconds hit a hiccup (e.g. two processes refreshing the sign-in at once).
    # Report it; the dashboard's 20-second check tries again. A run that dies later is caught the same way.
    for _ in range(8):
        time.sleep(1)
        if proc.poll() is not None:
            break
    if proc.poll() is None or proc.returncode == 0:
        return "STARTED", why
    return "FAILED", f"the run stopped right away; see {path.relative_to(ROOT)}"


if __name__ == "__main__":
    state, why = start() if sys.argv[1:] == ["start"] else check()
    print(f"{state}: {why}")
