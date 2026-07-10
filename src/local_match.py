"""Local, LLM-free match scoring — the fallback used ONLY when every LLM
provider fails (or none is configured). See main._fallback_analysis.

It scores a job by the embedding proximity between the candidate profile and
the closest chunk of the job text, using chonkie chunking + a small offline
model2vec model (~30MB, CPU, no torch). This replaces the old fixed score=50
neutral fallback with a real ranking signal, so a fully rate-limited run still
produces a usable ordering ("o que for mais próximo do perfil, maior score").

It cannot produce the LLM's critical strengths/gaps prose — those slots carry
an honest "estimated locally" note instead.
"""
import logging
import os
from functools import lru_cache

# HF hub tries to symlink into its cache; on stock Windows that raises
# WinError 1314 (no symlink privilege). Force copy mode before anything imports
# huggingface_hub via chonkie/model2vec.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from src.settings import env_float, env_str

logger = logging.getLogger(__name__)

_model = None
_chunker = None


def _load():
    """Lazily load (and process-cache) the embedding model + chunker."""
    global _model, _chunker
    if _model is None:
        from chonkie import Model2VecEmbeddings, SentenceChunker

        # Multilingual: the profile is PT and jobs come in PT+EN. potion-base-32M
        # (English) scored language, not relevance — PT boilerplate (RH, Arte) hit
        # ~0.80 while EN data-science jobs sank to ~0.15. The multilingual model
        # fixes that inversion (measured).
        _model = Model2VecEmbeddings(env_str("LOCAL_MATCH_MODEL", "minishlab/potion-multilingual-128M"))
        _chunker = SentenceChunker(chunk_size=512)
    return _model, _chunker


def _scale(sim):
    """Map a raw cosine similarity to a 0-100 score.

    Linear map over the LOCAL_MATCH_SIM_FLOOR..CEIL band — a calibration knob:
    the band shifts with the embedding model and the profile, so it stays
    tunable. Defaults measured on the labeled history (2026-07,
    potion-multilingual-128M): applied jobs median ~0.49 (p25 0.46), sub-bar
    ones median ~0.41 (p90 0.45), so the 0.35..0.50 band puts the report bar
    (score 70) at cosine ~0.455 — right at the empirical crossover.
    """
    floor = env_float("LOCAL_MATCH_SIM_FLOOR", 0.35)
    ceil = env_float("LOCAL_MATCH_SIM_CEIL", 0.50)
    if ceil <= floor:
        return int(max(0, min(100, round(sim * 100))))
    pct = (sim - floor) / (ceil - floor)
    return int(max(0, min(100, round(pct * 100))))


def description_similarity(job_info, candidate_profile):
    """Raw MEAN chunk-vs-profile cosine (0..1) between the profile and the job
    text. Mean (not max): a long posting has many chunks, and the single best one
    lets any stray chunk that overlaps generic "tecnologia/dados/inovação"
    boilerplate saturate the score. This is the unscaled signal behind
    local_match_analysis, exposed so the pipeline can persist score_vetor_desc.
    """
    model, chunker = _load()
    text = " ".join(
        str(job_info.get(k, "")) for k in ("job_title", "company", "full_description")
    ).strip()
    profile_vec = model.embed(candidate_profile)
    chunks = chunker.chunk(text) if text else []
    if chunks:
        sims = [float(model.similarity(profile_vec, model.embed(c.text))) for c in chunks]
        return sum(sims) / len(sims)
    return float(model.similarity(profile_vec, model.embed(text or " ")))


@lru_cache(maxsize=1024)
def _term_vec(term):
    """Embed a search term once per process — the same ~20 terms repeat for
    every job in a run."""
    model, _ = _load()
    return model.embed(term)


def title_scope_similarity(title, search_terms):
    """Max cosine (0..1) between the job title's role portion and any search term.

    High = the title looks like a role you're hunting (termos_busca); low = the
    title is a different career track. The scope signal behind score_title.
    Drops the city/work-model suffix LinkedIn appends after '|' or a newline.
    """
    if not search_terms:
        return 0.0
    model, _ = _load()
    head = (str(title or "").split("|")[0].splitlines() or [""])[0].strip()
    if not head:
        return 0.0
    tv = model.embed(head)
    return max(float(model.similarity(tv, _term_vec(t))) for t in search_terms)


def local_match_analysis(job_info, candidate_profile):
    """Return the same dict shape as matcher.analyze_match, computed locally.

    Score = description_similarity scaled to 0-100. Keeps off-track postings well
    below the report bar. core_role_compatible is always True: this only runs on
    infrastructure failure, and the deterministic title gate already dropped
    off-track roles upstream — never discard here.
    """
    best = description_similarity(job_info, candidate_profile)
    score = _scale(best)
    return {
        "match_score": score,
        "core_role_compatible": True,
        "strengths": [f"Similaridade local perfil↔vaga: {best:.2f} → score {score}/100"],
        "gaps": [
            "Análise por LLM indisponível — score estimado por embeddings locais, "
            "sem leitura crítica de requisitos."
        ],
        "verdict": (
            "Score local (embeddings) porque todos os provedores LLM falharam. "
            "Revise manualmente antes de decidir."
        ),
    }


def demo():
    """Self-check: a matching job must outscore an unrelated one, and the
    scaler must clamp to [0, 100]."""
    assert _scale(0.05) == 0 and _scale(0.95) == 100, "scaler must clamp to [0,100]"
    assert _scale(0.425) == 50, "midpoint of [0.35,0.50] band must map to 50"

    profile = "Cientista de dados sênior: machine learning, Python, SQL, LLMs, MLOps."
    good = local_match_analysis(
        {"job_title": "Cientista de Dados Sênior", "full_description": "Machine learning, modelos preditivos, Python, MLOps."},
        profile,
    )
    bad = local_match_analysis(
        {"job_title": "Analista de Marketing Digital", "full_description": "Gestão de mídias sociais e campanhas pagas."},
        profile,
    )
    assert good["match_score"] > bad["match_score"], (good["match_score"], bad["match_score"])
    print(f"OK: good={good['match_score']} > bad={bad['match_score']}")


if __name__ == "__main__":
    demo()
