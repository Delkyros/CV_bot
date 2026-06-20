import os
import json
import re
from google import genai
from google.genai import types

def analyze_match(vaga_info, perfil_candidato):
    """
    Compara uma vaga com o perfil do candidato utilizando o Gemini 2.5.
    Retorna um dicionário com: nota_match, pontos_fortes, gaps, veredicto.
    Retorna None ou um dicionário de fallback em caso de erro individual da API.
    """
    # Verifica se a chave de API está definida
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "sua_chave_do_gemini_aqui":
        print("  [Erro Matcher] GEMINI_API_KEY não configurada ou inválida no .env")
        return None

    try:
        # Inicializa o cliente do Google GenAI
        client = genai.Client()
        
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

        # Envia a requisição para o Gemini 2.5 Flash pedindo resposta em JSON
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        
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
        print(f"  [Erro Matcher] Resposta da API não continha um JSON válido: {jde}")
        # Tentativa de consertar ou retornar fallback em vez de crashar o pipeline inteiro
        return None
    except Exception as e:
        print(f"  [Erro Matcher] Erro ao analisar vaga '{vaga_info.get('titulo_vaga')}': {e}")
        return None
