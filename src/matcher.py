import os
import json
import re
import time
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# Espera padrao (segundos) entre retentativas quando a quota do Gemini estoura.
# A API costuma sugerir um retryDelay proprio; quando presente, ele e respeitado
# (limitado por QUOTA_MAX_WAIT). Caso contrario, usamos QUOTA_RETRY_WAIT.
QUOTA_RETRY_WAIT = 60.0
QUOTA_MAX_WAIT = 300.0

# Palavras-chave que indicam erro recuperavel (quota/limite/sobrecarga), no qual
# vale a pena esperar e retentar indefinidamente em vez de cair no fallback.
_QUOTA_KEYWORDS = (
    "resource_exhausted", "resource exhausted", "quota", "rate limit",
    "rate-limit", "ratelimit", "too many requests", "overloaded",
    "unavailable", "try again later",
)

_client = None


def _get_client():
    """Cria o cliente do Google GenAI uma unica vez (reutilizado entre vagas)."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def _is_retryable_quota_error(exc):
    """True para erros transitorios de quota/limite/sobrecarga do Gemini."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 503):
        return True
    text = str(exc).lower()
    return any(keyword in text for keyword in _QUOTA_KEYWORDS)


def _suggested_retry_seconds(exc, default):
    """Extrai o retryDelay sugerido pela API (ex.: 'retryDelay': '37s'),
    limitado por QUOTA_MAX_WAIT. Sem sugestao, retorna o default."""
    match = re.search(r"retry[-_ ]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s?", str(exc), re.IGNORECASE)
    if match:
        try:
            return min(float(match.group(1)), QUOTA_MAX_WAIT)
        except ValueError:
            pass
    return default


def _generate_with_retry(client, prompt):
    """
    Chama o Gemini com retentativa INFINITA em caso de quota/limite/sobrecarga,
    aguardando o tempo sugerido pela API (ou QUOTA_RETRY_WAIT) entre as tentativas.
    Erros nao-recuperaveis (ex.: prompt invalido) sao propagados imediatamente.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
        except Exception as exc:
            if not _is_retryable_quota_error(exc):
                raise
            wait = _suggested_retry_seconds(exc, QUOTA_RETRY_WAIT)
            logger.warning(
                f"Quota/limite do Gemini atingido (tentativa {attempt}). "
                f"Aguardando {wait:.0f}s antes de retentar... [{type(exc).__name__}: {exc}]"
            )
            time.sleep(wait)


def analyze_match(vaga_info, perfil_candidato):
    """
    Compara uma vaga com o perfil do candidato utilizando o Gemini 2.5.
    Retorna um dicionário com: nota_match, pontos_fortes, gaps, veredicto.
    Retorna None ou um dicionário de fallback em caso de erro individual da API.
    """
    # Verifica se a chave de API está definida
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "sua_chave_do_gemini_aqui":
        logger.error("GEMINI_API_KEY não configurada ou inválida no .env")
        return None

    try:
        # Reutiliza o cliente do Google GenAI entre as vagas
        client = _get_client()

        prompt = f"""
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

        # Envia ao Gemini com retentativa infinita em caso de estouro de quota
        response = _generate_with_retry(client, prompt)

        # Limpa o texto de possíveis blocos markdown extras (```json ... ```) se houver
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            # Remove blocos markdown de início e fim
            raw_text = re.sub(r"^```(?:json)?\n", "", raw_text)
            raw_text = re.sub(r"\n```$", "", raw_text)
            raw_text = raw_text.strip()
            
        # Faz o parse da resposta em JSON
        resultado = json.loads(raw_text)
        
        # Garante que os campos obrigatórios existam com tipos corretos
        nota = int(resultado.get("nota_match", 0))
        pontos_fortes = resultado.get("pontos_fortes", [])
        gaps = resultado.get("gaps", [])
        veredicto = resultado.get("veredicto", "Sem veredicto.")
        
        # Garante estrutura padronizada
        return {
            "nota_match": max(0, min(100, nota)), # Garante valor entre 0 e 100
            "pontos_fortes": [str(x) for x in pontos_fortes] if isinstance(pontos_fortes, list) else [],
            "gaps": [str(x) for x in gaps] if isinstance(gaps, list) else [],
            "veredicto": str(veredicto)
        }
        
    except json.JSONDecodeError as jde:
        logger.error(f"Resposta da API não continha um JSON válido: {jde}")
        # Tentativa de consertar ou retornar fallback em vez de crashar o pipeline inteiro
        return None
    except Exception:
        logger.exception(f"Erro ao analisar vaga '{vaga_info.get('titulo_vaga')}'")
        return None
