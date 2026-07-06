"""Tests for the in-app pipeline run controller (src/run_controller.py).

Covers the MVP (User Story 1) behavior: single-run concurrency lock, the
idle->running->finished transitions, failure handling, and boot reconciliation of a
stale 'running' state left by a crash. The pipeline command is injected so tests spawn
a fast dummy subprocess instead of running the real pipeline.
"""

import json
import sys
import time

import pytest

from datetime import datetime

from src.run_controller import RunController, emit_progress, progress_percent

# Fast dummy commands standing in for `python main.py`.
CMD_QUICK_OK = [sys.executable, "-c", "pass"]
CMD_SLOW_OK = [sys.executable, "-c", "import time; time.sleep(1.0)"]
CMD_FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _ctrl(tmp_path, cmd):
    return RunController(cmd=cmd, state_path=str(tmp_path / "run_state.json"))


def test_start_run_transitions_to_running_then_finished_success(tmp_path):
    ctrl = _ctrl(tmp_path, CMD_SLOW_OK)
    assert ctrl.status()["status"] == "idle"

    assert ctrl.start_run("manual") is True
    assert ctrl.is_running() is True
    st = ctrl.status()
    assert st["status"] == "running"
    assert st["trigger"] == "manual"
    assert st["run_started_at"] is not None

    assert _wait(lambda: ctrl.status().get("last_outcome") == "success")
    st = ctrl.status()
    assert st["status"] == "finished"
    assert st["exit_code"] == 0
    assert ctrl.is_running() is False


def test_concurrency_lock_rejects_second_start(tmp_path):
    ctrl = _ctrl(tmp_path, CMD_SLOW_OK)
    assert ctrl.start_run("manual") is True
    # Second trigger while the first run is in progress is rejected (FR-002).
    assert ctrl.start_run("manual") is False
    assert ctrl.is_running() is True
    # After it finishes, the trigger is usable again.
    assert _wait(lambda: not ctrl.is_running())
    assert ctrl.start_run("manual") is True
    assert _wait(lambda: not ctrl.is_running())


def test_failed_run_records_failure_and_releases_lock(tmp_path):
    ctrl = _ctrl(tmp_path, CMD_FAIL)
    assert ctrl.start_run("manual") is True
    assert _wait(lambda: ctrl.status().get("last_outcome") is not None)
    st = ctrl.status()
    assert st["last_outcome"] == "failed"
    assert st["exit_code"] == 3
    assert st["last_error"]
    # Never left permanently disabled (Principle VI / FR-011).
    assert ctrl.is_running() is False
    assert ctrl.start_run("manual") is True
    assert _wait(lambda: not ctrl.is_running())


def test_boot_reconciliation_resets_stale_running(tmp_path):
    # Simulate a crash: a state file left as 'running' with a pid no longer alive.
    state_file = tmp_path / "run_state.json"
    state_file.write_text(json.dumps({
        "status": "running", "pid": 999999999, "trigger": "auto",
        "run_started_at": "2026-07-05T10:00:00",
    }), encoding="utf-8")

    ctrl = RunController(cmd=CMD_QUICK_OK, state_path=str(state_file))
    st = ctrl.status()
    assert st["status"] == "idle"                 # FR-013: never stuck "running"
    assert st["last_outcome"] == "interrupted"
    assert ctrl.is_running() is False


def test_status_projection_includes_server_time(tmp_path):
    ctrl = _ctrl(tmp_path, CMD_QUICK_OK)
    assert "server_time" in ctrl.status()


def test_controller_never_writes_job_history(tmp_path):
    # The controller writes ONLY its own state file (Principle III separation).
    history = tmp_path / "vagas_historico.json"
    history.write_text(json.dumps({"x": {"status": "applied", "notes": "keep me"}}), encoding="utf-8")
    ctrl = _ctrl(tmp_path, CMD_QUICK_OK)
    ctrl.start_run("manual")
    assert _wait(lambda: not ctrl.is_running())
    # History file is untouched by the controller.
    assert json.loads(history.read_text(encoding="utf-8")) == {"x": {"status": "applied", "notes": "keep me"}}


# ---- US2: progress ----

def test_progress_percent_mapping_is_monotonic():
    assert progress_percent("scraping") == 0
    assert progress_percent("dedupe") == 42
    assert progress_percent("matching", 0, 100) == 45
    assert progress_percent("matching", 50, 100) == 70
    assert progress_percent("matching", 100, 100) == 95
    assert progress_percent("done") == 100
    vals = [progress_percent("matching", d, 100) for d in range(0, 101, 10)]
    assert vals == sorted(vals)                       # never goes backwards


def test_emit_progress_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_PROGRESS_PATH", raising=False)
    emit_progress("matching", 1, 10, "x")             # must not raise, must not write
    assert not (tmp_path / "run_state.json").exists()


def test_emit_progress_writes_only_progress_key(tmp_path, monkeypatch):
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"status": "running", "pid": 42}), encoding="utf-8")
    monkeypatch.setenv("PIPELINE_PROGRESS_PATH", str(path))
    emit_progress("matching", 5, 10, "half")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["progress"] == {"stage": "matching", "percent": 70, "detail": "half",
                                "updated_at": data["progress"]["updated_at"]}
    assert data["status"] == "running" and data["pid"] == 42   # other keys preserved


# ---- US3: scheduler ----

def _sched_ctrl(tmp_path, cmd, interval=3600):
    return RunController(cmd=cmd, state_path=str(tmp_path / "run_state.json"),
                         interval_seconds=interval, scheduler_enabled=True)


def test_next_run_at_set_and_reset_on_trigger(tmp_path):
    ctrl = _sched_ctrl(tmp_path, CMD_QUICK_OK, interval=3600)
    ctrl.start_run("manual")
    st = ctrl.status()
    assert st["next_run_at"] is not None
    delta = (datetime.fromisoformat(st["next_run_at"])
             - datetime.fromisoformat(st["run_started_at"])).total_seconds()
    assert abs(delta - 3600) < 5                       # FR-009: cycle = start + interval
    assert _wait(lambda: not ctrl.is_running())


def test_scheduler_off_when_interval_zero(tmp_path):
    ctrl = RunController(cmd=CMD_QUICK_OK, state_path=str(tmp_path / "run_state.json"),
                         interval_seconds=0)
    ctrl.start_scheduler(run_on_boot=False)
    st = ctrl.status()
    assert st["scheduler_enabled"] is False
    assert st["next_run_at"] is None
    assert ctrl._tick() is False                       # nothing fires


def test_scheduler_tick_starts_auto_run_when_due(tmp_path):
    ctrl = _sched_ctrl(tmp_path, CMD_QUICK_OK, interval=3600)
    ctrl._write_lifecycle(next_run_at="2000-01-01T00:00:00")   # overdue
    assert ctrl._tick() is True
    assert _wait(lambda: not ctrl.is_running())
    assert ctrl.status()["trigger"] == "auto"


def test_scheduler_tick_skips_when_running(tmp_path):
    ctrl = _sched_ctrl(tmp_path, CMD_SLOW_OK, interval=3600)
    assert ctrl.start_run("manual") is True
    ctrl._write_lifecycle(next_run_at="2000-01-01T00:00:00")   # overdue, but a run is active
    assert ctrl._tick() is False                       # FR-010: no concurrent auto run
    assert _wait(lambda: not ctrl.is_running())
