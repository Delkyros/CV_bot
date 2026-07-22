"""Ground-truth labels for match-quality evaluation, derived from the user's own
triage marks in the web UI. Single source of truth shared by the evaluation
harness (eval_scores.py), the meta-model (train_meta_model.py), and the few-shot
exemplar selector (main.py) so the positive/negative definition never drifts.

Definition (spec FR-002 / Clarifications 2026-07-22, corrected to the ACTUAL
persisted status vocabulary — webapp.py VALID_STATUSES = new/viewed/applied/error;
the UI labels are Portuguese ("Novo/Visto/Inscrito") but the stored values are
English):
  positive  = status == "applied"                    (the "Inscrito" button)
  negative  = error_class == "Escopo incorreto"       (explicit scope rejection)
  excluded  = everything else → None, dropped from evaluation. In particular:
              - "viewed": looked at, not applied → too noisy to be a positive.
              - "irrelevant": mostly AUTO-assigned by the pipeline for sub-bar
                jobs (passes_relevance_filter), NOT a user judgment. Using it as a
                negative would be circular — it is a threshold on the very scores
                we evaluate — so it is excluded, not counted as negative.
              - other error classes (Não é CLT, Localidade/Modelo incorreto) are
                location/contract errors, not role-fit, so out of match scope.

A scope error wins even over an `applied` status (explicit "wrong role" mark).
"""

SCOPE_ERROR_CLASS = "Escopo incorreto"


def label_of(entry):
    """Return "pos", "neg", or None (excluded) for one history entry."""
    if not isinstance(entry, dict):
        return None
    error_class = (entry.get("error_class") or "").strip()
    if error_class == SCOPE_ERROR_CLASS:
        return "neg"
    status = (entry.get("status") or "").strip().lower()
    if status == "applied":
        return "pos"
    return None


# --- Slicing helpers (used by the harness for per-slice metrics) -------------

# Portuguese-specific characters and a few high-frequency PT words. A coarse
# two-way PT/EN bucket is all the slice needs.
# ponytail: heuristic, not a language detector. If it misclassifies noticeably,
# swap for a real detector (e.g. fasttext lid) — overkill for two buckets today.
_PT_CHARS = set("ãõáàâéêíóôúç")
_PT_WORDS = (
    " de ", " da ", " do ", " para ", " com ", " uma ", " vaga ", " você ",
    " experiência ", " conhecimento ", " atividades ", " requisitos ",
)


def is_probably_pt(text):
    """True if the text looks Portuguese (diacritics or common PT words)."""
    t = (text or "").lower()
    if not t:
        return True  # empty → treat as PT (the profile/config are PT)
    if any(ch in _PT_CHARS for ch in t):
        return True
    padded = f" {t} "
    return sum(1 for w in _PT_WORDS if w in padded) >= 2


def _title_head(title):
    """Role portion of a LinkedIn title (drop the '|'/newline city-model suffix)."""
    head = (str(title or "").split("|")[0].splitlines() or [""])[0]
    return head.strip()


def nearest_term(title, search_terms):
    """Return the search term most similar to the job title's role portion.

    The history does NOT store which termo_busca found a job, so this approximates
    the per-term slice by argmax title↔term cosine (the same signal behind
    score_title, but argmax instead of max). Reuses the local_match embeddings.
    ponytail: approximation; store the true term at collection if this proves too
    coarse (a schema change + backfill this deliberately avoids — research D5).
    """
    if not search_terms:
        return None
    head = _title_head(title)
    if not head:
        return None
    # Imported lazily: the embedding model is heavy and only the slice needs it.
    from src.local_match import _load, _term_vec

    model, _ = _load()
    tv = model.embed(head)
    return max(
        search_terms,
        key=lambda t: float(model.similarity(tv, _term_vec(t))),
    )
