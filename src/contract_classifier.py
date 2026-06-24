import re
import logging
import unicodedata

import numpy as np

logger = logging.getLogger(__name__)


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def explicit_negative_evidence(text):
    normalized = normalize_text(text)
    patterns = [
        ("PJ/legal entity", r"\b(pj|pessoa juridica)\b"),
        ("Service provider", r"prestador(?:a)? de servicos|prestacao de servicos"),
        ("Invoice", r"\b(nf|nota fiscal)\b|emitir nota"),
        ("CNPJ/registered company", r"\bcnpj\b|empresa aberta"),
        ("Freelancer/self-employed/cooperative", r"freelancer|freela|autonomo|cooperado"),
        ("Internship/temporary", r"estagio|temporario"),
        ("Hourly rate/budget/billing", r"valor\s*/?\s*hora|valor hora|budget|faturamento"),
    ]
    return [label for label, pattern in patterns if re.search(pattern, normalized)]


def mean_top_k(values, top_k):
    if values.size == 0:
        return 0.0
    top_k = max(1, min(top_k, values.size))
    return float(np.mean(np.sort(values)[-top_k:]))


class ContractClassifier:
    def __init__(self, config):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency: install sentence-transformers to use the "
                "local contract classifier."
            ) from exc

        # NOTE: config keys come from config/contract_examples.yaml and are kept
        # in Portuguese on purpose (that file is not translated).
        self.model_name = config.get(
            "modelo_embedding",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        self.min_clt_score = float(config.get("limiar_minimo_clt", 0.55))
        self.min_margin = float(config.get("margem_minima_clt", 0.08))
        self.top_k = int(config.get("top_k_prototipos", 3))
        self.clt_examples = config.get("prototipos_clt", [])
        self.non_clt_examples = config.get("prototipos_nao_clt", [])

        if not self.clt_examples or not self.non_clt_examples:
            raise ValueError("Set prototipos_clt and prototipos_nao_clt in config/contract_examples.yaml.")

        logger.info(f"Loading local embedding model: {self.model_name}")
        self.model = SentenceTransformer(self.model_name)
        self.clt_embeddings = self._encode(self.clt_examples)
        self.non_clt_embeddings = self._encode(self.non_clt_examples)

    def _encode(self, texts):
        embeddings = self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        return embeddings

    def classify(self, description_text, title=None, company=None):
        # title/company are accepted for compatibility with the LLM classifier
        # interface; the embedding model uses only the description.
        description = description_text or ""
        negative_evidence = explicit_negative_evidence(description)
        job_embedding = self._encode([description])[0]

        clt_similarities = self.clt_embeddings @ job_embedding
        non_clt_similarities = self.non_clt_embeddings @ job_embedding
        score_clt = mean_top_k(clt_similarities, self.top_k)
        score_non_clt = mean_top_k(non_clt_similarities, self.top_k)
        margin = score_clt - score_non_clt

        if negative_evidence and score_non_clt >= score_clt - 0.02:
            inferred = "NON_CLT"
            accepted = False
            reason = "Explicit signals against CLT: " + ", ".join(negative_evidence)
        elif score_clt >= self.min_clt_score and margin >= self.min_margin:
            inferred = "CLT"
            accepted = True
            reason = "Description semantically closer to the CLT prototypes."
        elif score_non_clt >= score_clt:
            inferred = "NON_CLT"
            accepted = False
            reason = "Description semantically closer to the non-CLT prototypes."
        else:
            inferred = "AMBIGUOUS"
            accepted = False
            reason = "Insufficient margin to confirm CLT; job discarded due to ambiguity."

        if negative_evidence and inferred != "NON_CLT":
            reason += " Observed textual signals: " + ", ".join(negative_evidence)

        return {
            "inferred_contract_type": inferred,
            "accepted": accepted,
            "score_clt": round(score_clt, 4),
            "score_non_clt": round(score_non_clt, 4),
            "contract_margin": round(margin, 4),
            "contract_evidence": reason,
        }
