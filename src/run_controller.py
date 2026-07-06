"""In-app pipeline run controller: on-demand trigger + single-run lock + status.

Owns `run_state.json` (the pipeline<->web channel) and runs `python main.py` as a
**subprocess** so the pipeline's `sys.exit()` calls and any crash stay isolated from the
Flask server (constitution Principle VI, Never-Crash Resilience). Run state is kept in
memory (the live Popen handle is the authority for "is a run active") and mirrored to
`run_state.json` so a freshly booted app can detect a run left "running" by a crash and
never stay permanently blocked (FR-013).

Storage note: `run_state.json` is SEPARATE from `vagas_historico.json`. This controller
NEVER writes the job history (Principle III) — only the pipeline subprocess does, through
`main.save_job_history`.

MVP scope (User Story 1): manual trigger, concurrency guard, status. Progress emission
(US2) and the recurring scheduler (US3) build on this and are added later.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

from src.settings import env_bool, env_int, env_str

logger = logging.getLogger(__name__)

# Lifecycle keys owned by the controller. The pipeline (US2) will own only the
# "progress" key; keeping the writers key-disjoint avoids a lock on the file
# (single-writer-per-key + atomic replace). See contracts/run-state.md.
_LIFECYCLE_KEYS = (
    "status", "pid", "trigger", "run_started_at", "run_finished_at",
    "last_outcome", "last_error", "exit_code",
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env var the controller sets when spawning the pipeline so the subprocess knows
# where to write progress. Unset for direct CLI runs / tests → emit_progress no-ops.
_PROGRESS_ENV = "PIPELINE_PROGRESS_PATH"

# Weighted stage boundaries for the progress bar (research D4): scraping is the
# first ~40%, matching fills 45→95 scaled by jobs done, reporting the tail.
_STAGE_BASE = {"scraping": 0, "dedupe": 42, "matching": 45, "reporting": 95, "done": 100}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _run_state_path():
    return env_str("RUN_STATE_PATH", "run_state.json")


def _idle_progress():
    return {"stage": "idle", "percent": 0, "detail": "", "updated_at": None}


def progress_percent(stage, done=0, total=0):
    """Map a pipeline stage (+ match counter) to an overall 0–100 percent."""
    if stage == "matching" and total > 0:
        return round(45 + 50 * min(done, total) / total)
    return _STAGE_BASE.get(stage, 0)


def emit_progress(stage, done=0, total=0, detail=""):
    """Called from the pipeline subprocess to report progress. Writes ONLY the
    'progress' key of the state file named by $PIPELINE_PROGRESS_PATH. Best-effort:
    a no-op when the env var is unset (direct CLI / tests), and never raises so a
    write failure can't abort the pipeline (Principle VI)."""
    path = os.environ.get(_PROGRESS_ENV)
    if not path:
        return
    try:
        current = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                current = loaded
        current["progress"] = {
            "stage": stage,
            "percent": progress_percent(stage, done, total),
            "detail": detail,
            "updated_at": _now_iso(),
        }
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".runstate_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.debug("progress emit failed (ignored)", exc_info=True)


def _pid_alive(pid):
    """Best-effort liveness check for a pid, cross-platform."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(int(pid)) in out.stdout
        os.kill(int(pid), 0)  # POSIX: signal 0 tests existence
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _idle_state():
    return {
        "status": "idle", "pid": None, "trigger": None,
        "run_started_at": None, "run_finished_at": None,
        "last_outcome": None, "last_error": None, "exit_code": None,
    }


class RunController:
    """Single-run controller. Instantiate once per web-app process."""

    def __init__(self, cmd=None, state_path=None, cwd=None,
                 interval_seconds=None, scheduler_enabled=None, scheduler_tick=5.0):
        # `cmd`/`cwd`/interval/scheduler are injectable so tests can spawn a fast
        # dummy process and control scheduling deterministically.
        self._cmd = cmd or [sys.executable, "main.py"]
        self._cwd = cwd or _REPO_ROOT
        self._state_path = state_path  # None => resolve from env at each I/O
        self._interval = interval_seconds if interval_seconds is not None else env_int("RUN_INTERVAL_SECONDS", 21600)
        self._scheduler_enabled = scheduler_enabled if scheduler_enabled is not None else env_bool("SCHEDULER_ENABLED", True)
        self._scheduler_tick = scheduler_tick
        self._lock = threading.Lock()
        self._proc = None
        self._monitor = None
        self._scheduler = None
        self.reconcile_on_boot()

    def _scheduling_on(self):
        return self._scheduler_enabled and self._interval > 0

    def _next_run_iso(self, start_dt):
        """Next automatic run = start + interval (FR-009), or None if scheduling off."""
        if not self._scheduling_on():
            return None
        return (start_dt + timedelta(seconds=self._interval)).isoformat(timespec="seconds")

    # ---- state file I/O (atomic, single-writer-per-key merge) ----

    def _path(self):
        return self._state_path or _run_state_path()

    def _read_file(self):
        path = self._path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception("Failed to read run state at %s", path)
            return {}

    def _write_lifecycle(self, **fields):
        """Merge lifecycle keys into the state file, preserving other keys
        (e.g. the pipeline's future 'progress'). Atomic temp-file replace."""
        path = self._path()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        current = self._read_file()
        current.update(fields)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".runstate_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- lifecycle ----

    def reconcile_on_boot(self):
        """A fresh process owns no run. If the state file says 'running', the run
        can't be ours, so mark it interrupted and return to idle — never leave the
        trigger permanently blocked (FR-013).

        ponytail: reset any 'running' on boot rather than trying to adopt an orphan
        subprocess. Ceiling: a genuinely-orphaned run could be double-started (rare);
        being stuck 'running' forever is worse. Upgrade path: verify pid + re-attach
        if this ever matters.
        """
        data = self._read_file()
        if data.get("status") == "running":
            logger.warning("Found a stale 'running' run state on boot; marking interrupted.")
            self._write_lifecycle(
                status="idle", pid=None, run_finished_at=_now_iso(),
                last_outcome="interrupted",
                last_error="run was interrupted (app restarted or crashed)",
            )
        elif not data:
            self._write_lifecycle(**_idle_state())

    def is_running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start_run(self, trigger="manual"):
        """Spawn a pipeline run. Returns True if started, False if one is already
        in progress (concurrency guard, FR-002)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False
            # Tell the subprocess where to write progress, sharing our state file.
            env = dict(os.environ)
            env[_PROGRESS_ENV] = os.path.abspath(self._path())
            start_dt = datetime.now()
            # Write the initial run state BEFORE spawning. The controller must not
            # write the 'progress' key again after the subprocess starts (only 'pid'
            # below, a progress-preserving merge), so the pipeline's own progress
            # emissions are never clobbered by a late controller write.
            self._write_lifecycle(
                status="running", pid=None, trigger=trigger,
                run_started_at=start_dt.isoformat(timespec="seconds"), run_finished_at=None,
                last_outcome=None, last_error=None, exit_code=None,
                progress={"stage": "scraping", "percent": 0, "detail": "iniciando…",
                          "updated_at": start_dt.isoformat(timespec="seconds")},
                next_run_at=self._next_run_iso(start_dt),  # reset cycle on every run (FR-009)
                scheduler_enabled=self._scheduling_on(), interval_seconds=self._interval,
            )
            try:
                self._proc = subprocess.Popen(self._cmd, cwd=self._cwd, env=env)
            except Exception as exc:
                # Spawn failed: roll back to idle, record why, keep the web app alive (VI).
                logger.exception("Failed to start pipeline run")
                self._proc = None
                self._write_lifecycle(
                    status="idle", pid=None, last_outcome="failed",
                    last_error=f"failed to start: {exc}",
                )
                raise
            self._write_lifecycle(pid=self._proc.pid)  # merge: preserves 'progress'
            self._monitor = threading.Thread(
                target=self._watch, args=(self._proc,), daemon=True
            )
            self._monitor.start()
            return True

    def _watch(self, proc):
        """Wait for the subprocess (outside the lock) then record the outcome."""
        rc = proc.wait()
        outcome = "success" if rc == 0 else "failed"
        with self._lock:
            self._write_lifecycle(
                status="finished", pid=None, run_finished_at=_now_iso(),
                last_outcome=outcome, exit_code=rc,
                last_error=None if rc == 0 else f"pipeline exited with code {rc}",
            )
            if self._proc is proc:
                self._proc = None
        logger.info("Pipeline run finished (exit code %s, outcome %s).", rc, outcome)

    def status(self):
        """State projection for the web app / front (adds server_time)."""
        data = self._read_file() or _idle_state()
        data.setdefault("scheduler_enabled", self._scheduling_on())
        data.setdefault("interval_seconds", self._interval)
        data.setdefault("next_run_at", None)
        data.setdefault("progress", _idle_progress())
        data["server_time"] = _now_iso()
        return data

    # ---- recurring scheduler (US3) ----

    def _tick(self):
        """One scheduler check: start an auto run if it is due and no run is active.
        Returns True if it started a run. Safe to call from tests directly."""
        if not self._scheduling_on():
            return False
        nra = self._read_file().get("next_run_at")
        if not nra:
            return False
        try:
            due = datetime.fromisoformat(nra) <= datetime.now()
        except (ValueError, TypeError):
            return False
        if due and not self.is_running():  # FR-010: skip if a run is in progress
            return self.start_run("auto")
        return False

    def start_scheduler(self, run_on_boot=False):
        """Start the background daemon that fires automatic runs. No-op (and clears
        next_run_at) when scheduling is disabled or the interval is 0 (FR-006/007)."""
        if not self._scheduling_on():
            self._write_lifecycle(scheduler_enabled=False, next_run_at=None,
                                  interval_seconds=self._interval)
            logger.info("In-app scheduler off (SCHEDULER_ENABLED / RUN_INTERVAL_SECONDS=0).")
            return
        base = datetime.now() if run_on_boot else datetime.now() + timedelta(seconds=self._interval)
        self._write_lifecycle(scheduler_enabled=True, interval_seconds=self._interval,
                              next_run_at=base.isoformat(timespec="seconds"))
        self._scheduler = threading.Thread(target=self._schedule_loop, daemon=True)
        self._scheduler.start()
        logger.info("In-app scheduler on (interval %ss, first run %s).",
                    self._interval, "now" if run_on_boot else "in one interval")

    def _schedule_loop(self):
        while True:
            time.sleep(self._scheduler_tick)
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed (continuing).")
