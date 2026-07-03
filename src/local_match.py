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

        _model = Model2VecEmbeddings(env_str("LOCAL_MATCH_MODEL", "minishlab/potion-base-32M"))
        _chunker = SentenceChunker(chunk_size=512)
    return _model, _chunker


def _scale(sim):
    """Map a raw cosine similarity to a 0-100 score.

    ponytail: linear map with a floor/ceil calibration knob — unrelated job vs
    profile lands ~0.30, a strong match ~0.70 with this model (measured), but
    that band shifts with the profile/model, so leave it tunable rather than
    hardcoding sim*100 (which would bunch everything in the 30-70 range).
    """
    floor = env_float("LOCAL_MATCH_SIM_FLOOR", 0.30)
    ceil = env_float("LOCAL_MATCH_SIM_CEIL", 0.70)
    if ceil <= floor:
        return int(max(0, min(100, round(sim * 100))))
    pct = (sim - floor) / (ceil - floor)
    return int(max(0, min(100, round(pct * 100))))


def local_match_analysis(job_info, candidate_profile):
    """Return the same dict shape as matcher.analyze_match, computed locally.

    Score = MEAN chunk-vs-profile cosine similarity, scaled to 0-100. Mean (not
    max): a long posting has many chunks, and taking the single best one lets any
    stray chunk that happens to overlap the profile (generic "tecnologia/dados/
    inovação" boilerplate) saturate the score — that made every job, cook and
    salesperson included, land at 100. The mean reflects overall relevance and
    keeps off-track postings well below the report bar. core_role_compatible is
    always True: this only runs on infrastructure failure, and the deterministic
    title gate already dropped off-track roles upstream — never discard here.
    """
    model, chunker = _load()
    text = " ".join(
        str(job_info.get(k, "")) for k in ("job_title", "company", "full_description")
    ).strip()

    profile_vec = model.embed(candidate_profile)
    chunks = chunker.chunk(text) if text else []
    if chunks:
        sims = [float(model.similarity(profile_vec, model.embed(c.text))) for c in chunks]
        best = sum(sims) / len(sims)
    else:
        best = float(model.similarity(profile_vec, model.embed(text or " ")))

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
    assert _scale(0.50) == 50, "midpoint of [0.30,0.70] band must map to 50"

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
