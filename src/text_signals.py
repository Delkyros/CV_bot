"""Lightweight text helpers shared across the pipeline.

Contract type (CLT vs PJ/freelancer/internship) is decided entirely here, with
no LLM: `strong_non_clt_evidence` and `internship_evidence` are high-precision
non-CLT signals that override the "assume CLT" default (see
src/matcher.py::classify_contract).
"""

import re
import unicodedata


def normalize_text(text):
    """Lowercase and strip accents/diacritics for accent-insensitive matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


# Companies that are contractor / freelance / staffing marketplaces. A posting
# from one of these is almost never a Brazilian CLT employment bond, so it is
# treated as a strong non-CLT signal on its own.
_CONTRACTOR_PLATFORMS = (
    "turing", "crossing hurdles", "hire feed", "toptal", "andela", "crossover",
    "gun.io", "arc.dev", "braintrust", "lemon.io", "x-team", "x team",
    "upwork", "fiverr", "remotask",
)

# High-precision non-CLT cues (PT + EN). Matching any of these is reliable
# enough to OVERRIDE the default-CLT assumption (see matcher.classify_contract).
# Added after auditing misclassified jobs: USD-hourly pay, "Contract:" titles
# and contractor markers (1099/C2C) were the recurring missed signals.
_STRONG_PATTERNS = [
    ("PJ/legal entity", r"\b(pj|pessoa juridica)\b"),
    ("Service provider", r"prestador(?:a)? de servicos|prestacao de servicos"),
    ("Invoice", r"\b(nf|nota fiscal)\b|emitir nota"),
    ("CNPJ/registered company", r"\bcnpj\b|empresa aberta"),
    (
        "Freelancer/self-employed/cooperative",
        r"freelancer|freela|autonomo|cooperado|\bfreelanc\w*|\bself[\s-]?employed\b",
    ),
    ("Hourly rate (PT)", r"valor\s*/?\s*hora|valor hora"),
    # e.g. "$45/hr", "USD 40 per hour", "$ 50 / h"
    ("USD hourly rate", r"(?:\$|usd)\s*\d{1,4}(?:[.,]\d+)?\s*(?:/|per\s*)?\s*(?:hr|hour|hora|h)\b"),
    ("Contractor/1099/C2C", r"\bcontractor\b|\b1099\b|\bc2c\b|corp[\s-]?to[\s-]?corp|\bw-?2\b"),
    # "Contract:" / "Contract -" title convention, or "contract role/position/basis".
    (
        "Contract role",
        r"\bcontract\s*[:\-]|\bcontract\s+(?:role|position|opportunity|basis|to\s+hire|hire|job|work|assignment|worker)\b",
    ),
]


# Internship is a reliable non-CLT marker on its own (an internship is never a
# CLT employment bond). High-precision: "estagi" stem (estágio/estagiário) and
# the explicit English words, with a word boundary so "intern" does not match
# "internal"/"international". "temporário" is deliberately excluded — fixed-term
# CLT contracts exist, so it is not decisive.
_INTERNSHIP_PATTERN = r"\bestagi\w*|\binternship\b|\bintern\b"


def _match_labels(normalized, patterns):
    return [label for label, pattern in patterns if re.search(pattern, normalized)]


def internship_evidence(text):
    """Return a one-item label list if the text explicitly signals an internship,
    else []. High-precision enough to discard on its own (see classify_contract)."""
    if re.search(_INTERNSHIP_PATTERN, normalize_text(text)):
        return ["Internship/estágio"]
    return []


def contractor_platform(company):
    """Return the marketplace name if `company` is a known contractor platform, else None."""
    normalized = normalize_text(company)
    if not normalized:
        return None
    for name in _CONTRACTOR_PLATFORMS:
        if name in normalized:
            return name
    return None


def strong_non_clt_evidence(text, company=None):
    """High-precision non-CLT signals (USD/hour, contractor, PJ, marketplace company).

    These are reliable enough to justify overriding the default-CLT assumption.
    Returns the matched human-readable labels (empty list when none apply).
    """
    labels = _match_labels(normalize_text(text), _STRONG_PATTERNS)
    platform = contractor_platform(company)
    if platform:
        labels.append(f"Contractor platform: {platform}")
    return labels


# Job-TITLE patterns whose central role is a different career track than a
# senior Data Scientist / ML-AI Engineer. Matched against the accent-stripped,
# lowercased TITLE only: the title is the most reliable scope signal, while the
# description's incidental Python/SQL overlap is what made the LLM keep these.
# Anchored so the candidate's target titles (Cientista de Dados, Data Scientist,
# Engenheiro/Analista de Engenharia de Dados, ML/AI Engineer, "…Software – IA
# Generativa") are NEVER caught, while the flagged off-track ones are. Genuinely
# ambiguous titles that overlap the target (e.g. "Engenheiro de MLOps") are left
# to the LLM + match-score threshold, NOT hard-dropped here.
_OUT_OF_SCOPE_TITLE_PATTERNS = [
    # BI / Qlik / Power BI developer (dashboard/BI tooling, not DS/ML).
    ("desenvolvedor de BI/Qlik/Power BI",
     r"\bqlik\b|power\s*bi|\bbi\b[^.,|]*\b(developer|desenvolvedor)\b|\b(developer|desenvolvedor)\b[^.,|]*\bbi\b"),
    # Market intelligence / market research (pesquisa de mercado), not data science.
    ("inteligencia de mercado/pesquisa",
     r"intelig\w* de mercado|market intelligence|pesquisa de mercado|market research"),
    # Systems analyst — a different track from data science.
    ("analista de sistemas", r"\banalista de sistemas?\b"),
    # Generic / junior / graduate software development (Node.js, trainee, graduate).
    ("dev generico Jr/Node/Graduate", r"\bnode\.?js\b|\bnodejs\b|\bgraduate\b|\btrainees?\b"),
    # Clearly non-technical roles a broad/loose LinkedIn search can surface
    # (cook, waiter, salesperson, driver, nurse, admin...). None overlap with
    # data-science / ML / AI / data-engineering titles, so they are safe to
    # hard-drop here — deterministically, without an LLM call. This is the
    # load-bearing scope defense when the LLM is unavailable/rate-limited and the
    # fallback keeps jobs in-scope by default. Word-boundaried on the stripped,
    # lowercased title. Deliberately avoids ambiguous stems ("analista",
    # "consultor", "seguranca", "operador") that also name tech/DS roles.
    ("função não-técnica (alimentação/atendimento)",
     r"\bcozinheir[oa]\b|\bchef\b|auxiliar de cozinha|\bgarcom\b|\bgarconete\b|\bbarista\b|"
     r"\bbartender\b|\batendente\b|recepcionist[ao]|\bcamareir[oa]\b|\bcopeir[oa]\b"),
    ("função não-técnica (vendas/varejo)",
     r"\bvendedor(?:a)?\b|representante comercial|consultor(?:a)? de vendas|consultor(?:a)? comercial|"
     r"promotor(?:a)? de vendas|executiv[oa] de vendas|operador(?:a)? de caixa|\bbalconist[ao]\b|"
     r"\bestoquist[ao]\b|\brepositor(?:a)?\b"),
    ("função não-técnica (logística/operação/ofícios)",
     r"\bmotorist[ao]\b|\bentregador(?:a)?\b|\bmotoboy\b|\beletricist[ao]\b|\bmecanic[oa]\b|"
     r"\bsoldador\b|\bpedreir[oa]\b|\bpintor\b|\bencanador\b|auxiliar de producao|"
     r"operador(?:a)? de empilhadeira|\bvigilante\b|\bporteir[oa]\b|\bzelador\b"),
    ("função não-técnica (saúde/administrativo)",
     r"\benfermeir[oa]\b|tecnico de enfermagem|\bmedic[oa]\b|\bdentista\b|\bfisioterapeuta\b|"
     r"\bnutricionista\b|\bfarmaceutic[oa]\b|auxiliar administrativ[oa]|assistente administrativ[oa]|"
     r"\btelemarketing\b|\bsecretari[ao]\b|\bcostureir[oa]\b"),

    # --- Off-track TECH/BUSINESS roles the user hand-flagged "Escopo incorreto"
    # in the web UI. The candidate's scope is senior Data Scientist / ML / AI /
    # data engineering — NOT general software engineering, data ANALYST, BI,
    # infosec, or business analysis. These anchor on the discriminating token so
    # they never catch the target roles (validated in test_scope_filtering.py):
    # "backend" (not "AI Engineer"), "analista de dados" (not "engenharia de
    # dados" / "cientista de dados"), etc.

    # Software / backend / mobile / stack developer (not DS/ML). Anchored on
    # backend/frontend/mobile/stack tokens so "AI Engineer", "Engenheiro de
    # Dados" and "Engenheiro de Software – IA Generativa" are NOT caught.
    ("desenvolvedor de software/backend/mobile",
     r"\bback[\s-]?end\b|\bfront[\s-]?end\b|\bfull[\s-]?stack\b|\bmobile\b|\bandroid\b|\bios\b|"
     r"\bflutter\b|react native|\bdelphi\b|\bcamunda\b|\bdynamics\b|\blaravel\b|\bvue\.?js\b|"
     r"\bphp\b|\bjava\b|\brpa\b|\bprogramador\b|desenvolvedor de software|software developer|"
     r"desenvolvedor de sistemas|desenvolvimento de sistemas|analista de desenvolvimento"),

    # Data ANALYST / BI analytics — a different track from data science for this
    # candidate (they flagged "Analista de Dados", "Data Analyst", "Web Analytics").
    # Anchored on "analista de dados"/"data analyst" adjacency so "Cientista de
    # Dados", "Engenheiro de Dados" and "Analista Sênior Engenharia de Dados" survive.
    ("data analyst/analytics (não é ciência de dados)",
     r"analista de dados\b|\bdata analyst\b|web analytics"),

    # HR / people / recruiting.
    ("RH/recrutamento",
     r"business partner|\bhr\b|recursos humanos|atracao e selecao|recrutamento|people analytics"),

    # Sales / commercial / marketing / retail management.
    ("vendas/comercial/marketing",
     r"\bcomercial\b|de vendas\b|vendas externas|pre[\s-]?vendas|customer success|grandes contas|"
     r"executiv[oa] de|gerente de vendas|supervisor de vendas|\bmarketing\b|merchandising|\bbanker\b|"
     r"incorporacao imobiliaria|imobiliari"),

    # QA / testing.
    ("QA/testes", r"\bqa\b|quality assurance|analista de teste"),

    # Finance / controllership / fraud / audit.
    ("finanças/controladoria/fraude",
     r"controladoria|prevencao a fraude|\bauditoria\b|financeir[ao] de ti"),

    # Logistics / supply / operations.
    ("logística/operações", r"\bwms\b|transportes|logistica|gestao de estoque"),

    # Support / franchise / CRM / Salesforce admin.
    ("suporte/CRM/franquias", r"suporte a franquias|\bfranquias\b|salesforce|\bcrm\b"),

    # IT support / systems and generic research/planning/trainee roles.
    ("TI/pesquisa/planejamento genérico",
     r"analista de ti\b|\bpesquisador\b|analista de planejamento\b"),

    # Information security — out of scope for this DS/ML candidate.
    ("segurança da informação", r"seguranca da informacao|information security"),
]


def out_of_scope_title(title):
    """Return a human-readable reason if the job TITLE's central role is a
    different career track than the candidate's (BI/Qlik dev, systems analyst,
    generic/backend/mobile software dev, data analyst/BI, HR, sales/commercial,
    QA, finance, logistics, infosec, non-technical roles), else None.

    Deterministic, title-only scope gate — the load-bearing scope defense when
    the LLM is rate-limited/unavailable (the fallback keeps jobs in-scope by
    default). The LLM still judges the ambiguous titles this leaves through.
    """
    normalized = normalize_text(title)
    if not normalized:
        return None
    for reason, pattern in _OUT_OF_SCOPE_TITLE_PATTERNS:
        if re.search(pattern, normalized):
            return reason
    return None
