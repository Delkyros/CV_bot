"""Small local web UI to mark jobs as viewed / applied.

Reads the persistent job history (the same vagas_historico.json the pipeline
writes) and lets you set a per-job status (new / viewed / applied) plus free
notes. Changes are written straight back into the history JSON, so they survive
every new search run and live in the same ./data volume as the rest of the data.

Run locally:   python webapp.py      (then open http://localhost:8000)
In Docker:     a dedicated `web` service runs this (see docker-compose.example.yml).

Configuration (env, see .env.example):
  HISTORY_PATH   path to the history JSON (shared with the pipeline)
  WEB_HOST       bind host (default 0.0.0.0)
  WEB_PORT       bind port (default 8000)
"""

import json
import logging
import os
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from src.logging_config import setup_logging
from src.reporter import passes_relevance_filter
from src.run_controller import RunController
from src.settings import env_bool, env_int, env_str

logger = logging.getLogger(__name__)

VALID_STATUSES = ("new", "viewed", "applied", "error")
# Classification options shown when a job is marked as "error". Kept in
# Portuguese on purpose (user-facing labels, persisted verbatim in the history).
ERROR_CLASSES = ("Localidade/Modelo incorreto", "Não é CLT", "Escopo incorreto")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

app = Flask(__name__, static_folder=None)

# Single-run pipeline controller (on-demand trigger + concurrency guard + status).
# Instantiated once per process; boot reconciliation runs in __init__ (FR-013).
controller = RunController()


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _history_path():
    return env_str("HISTORY_PATH", "vagas_historico.json")


def _load_history():
    path = _history_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to read history at %s", path)
        return {}


def _save_history(history):
    """Atomically persist the history (temp file in the same dir, then replace)."""
    path = _history_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vagas_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _job_view(link, entry):
    """Shape a history entry into the JSON the frontend consumes."""
    return {
        "link": link,
        "job_title": entry.get("job_title", "N/A"),
        "company": entry.get("company", "N/A"),
        "location": entry.get("location", "N/A"),
        "workplace_type": entry.get("workplace_type", "N/A"),
        "inferred_contract_type": entry.get(
            "inferred_contract_type", entry.get("contract_type", "N/A")
        ),
        "score_clt": entry.get("score_clt", "N/A"),
        "match_score": entry.get("match_score", 0),
        # Three-scorer comparison + analysis prose for the "Relatório" tab.
        "score_gemini": entry.get("score_gemini"),
        "score_vetor_desc": entry.get("score_vetor_desc"),
        "score_title": entry.get("score_title"),
        "strengths": entry.get("strengths", []),
        "gaps": entry.get("gaps", []),
        "verdict": entry.get("verdict", ""),
        "first_seen_at": entry.get("first_seen_at"),
        "last_processed_at": entry.get("last_processed_at"),
        "status_updated_at": entry.get("status_updated_at"),
        "status": entry.get("status", "new"),
        "error_class": entry.get("error_class", ""),
        "notes": entry.get("notes", ""),
        "relevant": passes_relevance_filter(entry),
    }


def _last_run(history):
    """Most recent pipeline run, inferred from the newest last_processed_at."""
    stamps = [
        e.get("last_processed_at")
        for e in history.values()
        if isinstance(e, dict) and e.get("last_processed_at")
    ]
    return max(stamps) if stamps else None


def _score_of(job):
    try:
        return int(job.get("match_score", 0) or 0)
    except (ValueError, TypeError):
        return 0


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/jobs")
def api_jobs():
    history = _load_history()
    jobs = [_job_view(link, e) for link, e in history.items() if isinstance(e, dict)]
    jobs.sort(key=_score_of, reverse=True)
    return jsonify(
        {
            "jobs": jobs,
            "count": len(jobs),
            "last_run": _last_run(history),
            "error_classes": list(ERROR_CLASSES),
        }
    )


@app.post("/api/status")
def api_status():
    payload = request.get_json(silent=True) or {}
    link = payload.get("link")
    status = payload.get("status")
    notes = payload.get("notes")
    error_class = payload.get("error_class")

    if not link:
        return jsonify({"error": "missing 'link'"}), 400
    if status is None and notes is None and error_class is None:
        return jsonify({"error": "nothing to update (provide 'status', 'notes' and/or 'error_class')"}), 400
    if status is not None and status not in VALID_STATUSES:
        return jsonify({"error": f"invalid status; use one of {list(VALID_STATUSES)}"}), 400
    # Allow clearing with "" / None; otherwise it must be one of the known classes.
    if error_class not in (None, "") and error_class not in ERROR_CLASSES:
        return jsonify({"error": f"invalid error_class; use one of {list(ERROR_CLASSES)}"}), 400

    # Read-modify-write on the freshest on-disk state.
    history = _load_history()
    entry = history.get(link)
    if not isinstance(entry, dict):
        return jsonify({"error": "unknown job link"}), 404

    if status is not None:
        entry["status"] = status
        # Leaving the "error" status drops any stale classification.
        if status != "error":
            entry["error_class"] = ""
    if error_class is not None:
        entry["error_class"] = error_class
    if notes is not None:
        entry["notes"] = str(notes)[:2000]
    entry["status_updated_at"] = _now_iso()
    history[link] = entry
    _save_history(history)

    logger.info(
        "Status updated: %s -> %s%s",
        link,
        entry.get("status"),
        f" ({entry['error_class']})" if entry.get("error_class") else "",
    )
    return jsonify(
        {
            "ok": True,
            "link": link,
            "status": entry.get("status"),
            "error_class": entry.get("error_class"),
            "notes": entry.get("notes"),
        }
    )


@app.post("/api/run")
def api_run():
    """Trigger a pipeline run in the background. 202 if started, 409 if a run is
    already in progress (concurrency guard, FR-002)."""
    try:
        started = controller.start_run("manual")
    except Exception:
        logger.exception("Failed to start pipeline run")
        return jsonify({"ok": False, "error": "failed_to_start"}), 500
    if not started:
        st = controller.status()
        return jsonify({
            "ok": False, "error": "already_running",
            "status": st.get("status"),
            "run_started_at": st.get("run_started_at"),
            "progress": st.get("progress"),
        }), 409
    st = controller.status()
    logger.info("Pipeline run started (manual).")
    return jsonify({
        "ok": True, "status": st.get("status"),
        "trigger": st.get("trigger"),
        "run_started_at": st.get("run_started_at"),
    }), 202


@app.get("/api/run/status")
def api_run_status():
    """Current run/controller state — polled by the front."""
    return jsonify(controller.status())


def main():
    setup_logging()
    load_dotenv()
    host = env_str("WEB_HOST", "0.0.0.0")
    port = env_int("WEB_PORT", 8000)
    # App-owned recurring scheduler (FR-014). RUN_ON_START fires the first run
    # immediately; otherwise it waits one interval.
    controller.start_scheduler(run_on_boot=env_bool("RUN_ON_START", False))
    logger.info("JobMatch web UI on http://%s:%d (history: %s)", host, port, _history_path())
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
