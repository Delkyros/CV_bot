import os
import json
import re
import time
import logging

import requests
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# Provedor principal: OpenRouter (modelos gratuitos). Gemini fica como fallback.
# A ordem pode ser ajustada aqui; ambos sao tentados em cada ciclo.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Lista de modelos gratuitos do OpenRouter, tentados em ordem. Os modelos mais
# populares (Llama 70B, Qwen) vivem em 429 ("rate-limited upstream") por
# congestionamento do pool gratuito, entao priorizamos modelos fortes porem
# menos disputados. Em caso de 429 num modelo, passamos para o proximo.
# Pode-se sobrescrever por uma unica escolha via env OPENROUTER_MODEL.
DEFAULT_OPENROUTER_MODELS = (
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
)
PROVIDER_ORDER = ("openrouter", "gemini")

# Quantas vezes percorrer a cadeia inteira de provedores antes de desistir e
# cair na analise simulada do main. Aumente para um valor alto se quiser um
# comportamento praticamente "infinito ate processar". Entre ciclos, espera
# QUOTA_RETRY_WAIT segundos.
MAX_PROVIDER_CYCLES = 3
QUOTA_RETRY_WAIT = 60.0

# Palavras-chave que indicam erro transitorio (quota/limite/sobrecarga). Para
# esses casos vale tentar o proximo provedor / repetir o ciclo em vez de falhar.
_QUOTA_KEYWORDS = (
    "resource_exhausted", "resource exhausted", "quota", "rate limit",
    "rate-limit", "ratelimit", "too many requests", "too many sessions",
    "overloaded", "unavailable", "try again later",
    "http 429", "http 500", "http 502", "http 503", "http 529",
)

_client = None


def _get_client():
    """Cria o cliente do Google GenAI uma unica vez (reutilizado entre vagas)."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def _openrouter_key():
    return os.getenv("OPENROUTER_API_KEY")


def _gemini_key():
    key = os.getenv("GEMINI_API_KEY")
    if key and key != "sua_chave_do_gemini_aqui":
        return key
    return None


def _openrouter_models():
    # Se OPENROUTER_MODEL estiver definido no .env, usa só ele; senão, a lista padrão.
    override = os.getenv("OPENROUTER_MODEL")
    if override:
        return [override]
    return list(DEFAULT_OPENROUTER_MODELS)


def _is_retryable_quota_error(exc):
    """True para erros transitorios de quota/limite/sobrecarga (qualquer provedor)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 529):
        return True
    text = str(exc).lower()
    return any(keyword in text for keyword in _QUOTA_KEYWORDS)


def _build_prompt(vaga_info, perfil_candidato):
    return f"""
Você é um especialista sênior em Recrutamento e Seleção na área de Tecnologia (Tech Recruiter).
Sua missão é analisar de forma crítica e realista se um candidato possui aderência para uma determinada vaga de emprego.

Dados da Vaga:
- Título da Vaga: {vaga_info.get('titulo_vaga', 'N/A')}
- Empresa: {vaga_info.get('empresa', 'N/A')}
- Localização: {vaga_info.get('localizacao', 'N/A')}
- Descrição da Vaga:
{vaga_info.get('descricao_completa', 'Sem descrição.')}

Perfil Profissional do Candidato:
{perfil_candidato}

---

Instruções para Análise:
1. Compare os requisitos técnicos, nível de experiência e competências da vaga com o perfil do candidato.
2. Determine uma nota de match real de 0 a 100 (seja realista e crítico, não dê 100 a menos que todos os requisitos batam perfeitamente).
3. Liste até 4 pontos fortes do candidato que combinam diretamente com os requisitos da vaga.
4. Liste os "gaps", que são os requisitos obrigatórios ou desejáveis da vaga que o candidato não possui ou não mencionou em seu perfil.
5. Escreva um veredicto amigável, sincero e direto (máximo de 3 frases) com conselhos sobre o que focar ou se vale a pena se candidatar.

Você DEVE responder estritamente no formato JSON abaixo, sem blocos explicativos ou markdown fora do JSON.
Estrutura desejada da resposta:
{{
  "nota_match": <número inteiro de 0 a 100>,
  "pontos_fortes": ["Ponto forte 1", "Ponto forte 2", ...],
  "gaps": ["Gap 1", "Gap 2", ...],
  "veredicto": "<Texto do veredicto>"
}}
"""


def _call_openrouter(prompt):
    """Chama o OpenRouter (API compativel com OpenAI), tentando cada modelo da
    lista em ordem e pulando os que estiverem congestionados (429). Retorna o
    texto cru da primeira resposta valida ou levanta a ultima excecao."""
    key = _openrouter_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY ausente")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_error = RuntimeError("Nenhum modelo OpenRouter disponivel")

    for model in _openrouter_models():
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    # Espaco generoso de saida: modelos de reasoning (ex.: gpt-oss)
                    # gastam tokens "pensando" e, sem isso, o JSON sai truncado.
                    "max_tokens": 2000,
                    # Minimiza o reasoning para sobrar orcamento para o JSON final.
                    "reasoning": {"effort": "low"},
                },
                timeout=60,
            )
            if resp.status_code != 200:
                last_error = RuntimeError(f"OpenRouter HTTP {resp.status_code} ({model}): {resp.text[:200]}")
                logger.warning(f"[openrouter:{model}] HTTP {resp.status_code}, tentando próximo modelo...")
                continue
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if model != _openrouter_models()[0]:
                logger.info(f"[openrouter] usando modelo alternativo: {model}")
            return content
        except (KeyError, IndexError, TypeError) as exc:
            last_error = RuntimeError(f"OpenRouter resposta inesperada ({model}): {exc}")
            continue
        except requests.RequestException as exc:
            last_error = RuntimeError(f"OpenRouter erro de rede ({model}): {exc}")
            continue

    raise last_error


def _call_gemini(prompt):
    """Chama o Gemini. Retorna o texto cru da resposta ou levanta excecao."""
    client = _get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return response.text


_PROVIDER_CALLS = {
    "openrouter": (_openrouter_key, _call_openrouter),
    "gemini": (_gemini_key, _call_gemini),
}


def _available_providers():
    """Lista (nome, funcao) dos provedores com chave configurada, na ordem definida."""
    disponiveis = []
    for name in PROVIDER_ORDER:
        key_fn, call_fn = _PROVIDER_CALLS[name]
        if key_fn():
            disponiveis.append((name, call_fn))
    return disponiveis


def has_provider():
    """True se ao menos um provedor de LLM esta configurado."""
    return bool(_available_providers())


def _extract_json_object(text):
    """Extrai o primeiro objeto JSON balanceado {...} de um texto, ignorando
    chaves dentro de strings. Util quando o modelo embrulha o JSON em prosa."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _parse_result(raw_text):
    """Converte o texto cru do LLM no dicionario padronizado, ou None se invalido."""
    if not raw_text:
        return None

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()

    resultado = None
    try:
        resultado = json.loads(text)
    except json.JSONDecodeError:
        # Modelos como gpt-oss as vezes embrulham o JSON em texto/raciocinio;
        # tenta extrair o primeiro objeto {...} balanceado.
        bloco = _extract_json_object(text)
        if bloco:
            try:
                resultado = json.loads(bloco)
            except json.JSONDecodeError:
                resultado = None
    if not isinstance(resultado, dict):
        return None

    try:
        nota = int(resultado.get("nota_match", 0))
    except (ValueError, TypeError):
        nota = 0

    pontos_fortes = resultado.get("pontos_fortes", [])
    gaps = resultado.get("gaps", [])
    veredicto = resultado.get("veredicto", "Sem veredicto.")

    return {
        "nota_match": max(0, min(100, nota)),
        "pontos_fortes": [str(x) for x in pontos_fortes] if isinstance(pontos_fortes, list) else [],
        "gaps": [str(x) for x in gaps] if isinstance(gaps, list) else [],
        "veredicto": str(veredicto),
    }


def analyze_match(vaga_info, perfil_candidato):
    """
    Compara uma vaga com o perfil do candidato usando uma cadeia de provedores
    de LLM (OpenRouter como principal, Gemini como fallback). Em cada ciclo tenta
    cada provedor disponivel; erros de quota/limite fazem cair para o proximo. Se
    todos falharem, espera e repete a cadeia ate MAX_PROVIDER_CYCLES vezes.

    Retorna o dicionario {nota_match, pontos_fortes, gaps, veredicto} ou None
    (para que o main use a analise simulada de fallback).
    """
    providers = _available_providers()
    if not providers:
        logger.error("Nenhum provedor de LLM configurado (defina OPENROUTER_API_KEY e/ou GEMINI_API_KEY no .env).")
        return None

    prompt = _build_prompt(vaga_info, perfil_candidato)
    primario = providers[0][0]

    for cycle in range(1, MAX_PROVIDER_CYCLES + 1):
        for name, call in providers:
            try:
                raw = call(prompt)
            except Exception as exc:
                nivel = "quota/limite" if _is_retryable_quota_error(exc) else "falha não-recuperável"
                logger.warning(f"[{name}] {nivel}: {exc}. Tentando próximo provedor...")
                continue

            resultado = _parse_result(raw)
            if resultado is not None:
                if name != primario or cycle > 1:
                    logger.info(f"Análise obtida via fallback [{name}] (ciclo {cycle}).")
                return resultado

            logger.warning(f"[{name}] retornou JSON inválido. Tentando próximo provedor...")

        if cycle < MAX_PROVIDER_CYCLES:
            logger.warning(
                f"Todos os provedores falharam (ciclo {cycle}/{MAX_PROVIDER_CYCLES}). "
                f"Aguardando {QUOTA_RETRY_WAIT:.0f}s e repetindo a cadeia..."
            )
            time.sleep(QUOTA_RETRY_WAIT)

    logger.error("Todos os provedores de LLM falharam após os ciclos de retentativa.")
    return None
