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
        ("PJ/pessoa juridica", r"\b(pj|pessoa juridica)\b"),
        ("Prestador de servicos", r"prestador(?:a)? de servicos|prestacao de servicos"),
        ("Nota fiscal", r"\b(nf|nota fiscal)\b|emitir nota"),
        ("CNPJ/empresa aberta", r"\bcnpj\b|empresa aberta"),
        ("Freelancer/autonomo/cooperado", r"freelancer|freela|autonomo|cooperado"),
        ("Estagio/temporario", r"estagio|temporario"),
        ("Valor hora/budget/faturamento", r"valor\s*/?\s*hora|valor hora|budget|faturamento"),
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
                "Dependencia ausente: instale sentence-transformers para usar o "
                "classificador local de contratacao."
            ) from exc

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
            raise ValueError("Configure prototipos_clt e prototipos_nao_clt em config/contract_examples.yaml.")

        logger.info(f"Carregando modelo local de embeddings: {self.model_name}")
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

    def classify(self, description_text):
        description = description_text or ""
        negative_evidence = explicit_negative_evidence(description)
        job_embedding = self._encode([description])[0]

        clt_similarities = self.clt_embeddings @ job_embedding
        non_clt_similarities = self.non_clt_embeddings @ job_embedding
        score_clt = mean_top_k(clt_similarities, self.top_k)
        score_non_clt = mean_top_k(non_clt_similarities, self.top_k)
        margin = score_clt - score_non_clt

        if negative_evidence and score_non_clt >= score_clt - 0.02:
            inferred = "NAO_CLT"
            accepted = False
            reason = "Sinais explicitos contra CLT: " + ", ".join(negative_evidence)
        elif score_clt >= self.min_clt_score and margin >= self.min_margin:
            inferred = "CLT"
            accepted = True
            reason = "Descricao semanticamente mais proxima dos prototipos CLT."
        elif score_non_clt >= score_clt:
            inferred = "NAO_CLT"
            accepted = False
            reason = "Descricao semanticamente mais proxima dos prototipos nao-CLT."
        else:
            inferred = "AMBIGUA"
            accepted = False
            reason = "Margem insuficiente para confirmar CLT; vaga descartada por ambiguidade."

        if negative_evidence and inferred != "NAO_CLT":
            reason += " Sinais textuais observados: " + ", ".join(negative_evidence)

        return {
            "tipo_contratacao_inferido": inferred,
            "aceita": accepted,
            "score_clt": round(score_clt, 4),
            "score_nao_clt": round(score_non_clt, 4),
            "margem_contratacao": round(margin, 4),
            "evidencias_contratacao": reason,
        }
