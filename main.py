import os
import sys
import yaml
from dotenv import load_dotenv

# Garante que os arquivos do pacote src/ possam ser importados corretamente
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from src.scraper import scrape_linkedin_jobs
from src.matcher import analyze_match
from src.reporter import generate_report

def main():
    print("=" * 60)
    print("      PIPELINE DE BUSCA E MATCH DE VAGAS DO LINKEDIN      ")
    print("=" * 60)
    
    # 1. Carrega variáveis de ambiente (.env)
    load_dotenv()
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "sua_chave_do_gemini_aqui":
        print("[Alerta] GEMINI_API_KEY não configurada ou com valor padrão no arquivo .env.")
        print("Por favor, configure sua chave de API do Gemini para rodar a etapa de match de inteligência artificial.")
        print("O script continuará com a busca de vagas, mas a etapa de match usará uma nota de match padrão (simulada).")
        print("=" * 60)
    
    # 2. Carrega configurações do arquivo YAML
    config_path = "config/keywords.yaml"
    if not os.path.exists(config_path):
        print(f"[Erro] Arquivo de configuração '{config_path}' não encontrado!")
        sys.exit(1)
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"[Erro] Falha ao carregar o arquivo YAML '{config_path}': {e}")
        sys.exit(1)
        
    termos_busca = config.get("termos_busca", [])
    localizacao = config.get("localizacao", "Brasil")
    tipo_contratacao = config.get("tipo_contratacao", "CLT")
    filtros_busca = config.get("filtros_busca") or [{"modelo_trabalho": None, "localizacao": localizacao}]
    perfil_candidato = config.get("perfil_candidato", "")
    
    if not termos_busca:
        print("[Erro] Lista 'termos_busca' vazia no arquivo de configuração.")
        sys.exit(1)
        
    if not perfil_candidato:
        print("[Erro] 'perfil_candidato' não configurado ou vazio no arquivo de configuração.")
        sys.exit(1)
        
    print(f"[Config] Termos de busca encontrados: {termos_busca}")
    print(f"[Config] Tipo de contratação: {tipo_contratacao}")
    print(f"[Config] Filtros de busca: {filtros_busca}")
    print(f"[Config] Perfil do candidato carregado ({len(perfil_candidato)} caracteres).")
    
    # 3. Executa o Scraper para coletar vagas do LinkedIn
    # Limitamos a 3 vagas por termo de busca para ser ágil e evitar bloqueios, mas pode ser expandido
    vagas_coletadas = []
    links_coletados = set()
    max_vagas_por_termo = 3
    
    for termo in termos_busca:
        for filtro in filtros_busca:
            localizacao_filtro = filtro.get("localizacao", localizacao)
            modelo_trabalho = filtro.get("modelo_trabalho")
            try:
                vagas = scrape_linkedin_jobs(
                    termo,
                    location=localizacao_filtro,
                    max_jobs=max_vagas_por_termo,
                    contract_type=tipo_contratacao,
                    workplace_type=modelo_trabalho
                )
                for vaga in vagas:
                    link_vaga = vaga.get("link_vaga")
                    if link_vaga in links_coletados:
                        print(f"[Scraper] Vaga duplicada ignorada: {vaga.get('titulo_vaga')} | {vaga.get('empresa')}")
                        continue
                    links_coletados.add(link_vaga)
                    vagas_coletadas.append(vaga)
            except Exception as e:
                print(f"[Erro Scraper] Erro ao buscar vagas para o termo '{termo}' com filtro {filtro}: {e}")
                continue
            
    total_vagas = len(vagas_coletadas)
    print(f"\n[Scraper] Busca concluída! Total de vagas coletadas de todas as buscas: {total_vagas}")
    
    if total_vagas == 0:
        print("[Fim] Nenhuma vaga pôde ser coletada. Encerrando pipeline.")
        sys.exit(0)
        
    # 4. Executa o Matcher para analisar cada vaga coletada
    vagas_analisadas = []
    
    print("\n" + "=" * 40)
    print("            INICIANDO ANÁLISE DE MATCH            ")
    print("=" * 40)
    
    for idx, vaga in enumerate(vagas_coletadas, start=1):
        print(f"\n[Matcher] Analisando vaga {idx} de {total_vagas}...")
        print(f"  Vaga: {vaga['titulo_vaga']} | Empresa: {vaga['empresa']}")
        
        resultado_analysis = None
        
        # Só executa a análise se a chave de API do Gemini for real/configurada
        if api_key and api_key != "sua_chave_do_gemini_aqui":
            try:
                resultado_analysis = analyze_match(vaga, perfil_candidato)
            except Exception as e:
                print(f"  [Erro Matcher] Falha na chamada da API para esta vaga: {e}")
                
        # Fallback inteligente (Resiliência) caso a API falhe ou a chave não esteja disponível
        if not resultado_analysis:
            if not api_key or api_key == "sua_chave_do_gemini_aqui":
                print("  [Aviso] Usando análise simulada de fallback devido à falta da GEMINI_API_KEY.")
            else:
                print("  [Aviso] Usando análise simulada de fallback devido a uma falha na chamada do Gemini.")
                
            # Cria um resultado amigável de simulação/fallback para que o pipeline não quebre
            # E para que o usuário possa testar o pipeline completo sem a chave se quiser
            resultado_analysis = {
                "nota_match": 50,  # Nota neutra
                "pontos_fortes": ["Vaga coletada com sucesso", "Disponível para avaliação"],
                "gaps": ["Análise do Gemini indisponível (Verifique sua GEMINI_API_KEY no .env)"],
                "veredicto": "Insira uma chave válida em GEMINI_API_KEY no arquivo .env para obter uma análise de inteligência artificial real desta vaga."
            }
            
        # Mescla os dados da vaga com a análise
        vaga_analisada = {**vaga, **resultado_analysis}
        vagas_analisadas.append(vaga_analisada)
        
        print(f"  -> Resultado: Nota {vaga_analisada['nota_match']}/100")
        
    # 5. Executa o Reporter para gerar o arquivo Excel de saída
    vagas_salvas = generate_report(vagas_analisadas, output_path="vagas_filtradas.xlsx")
    
    if vagas_salvas:
        print("\n" + "=" * 60)
        print("   PIPELINE CONCLUÍDO COM SUCESSO!")
        print("   O relatório 'vagas_filtradas.xlsx' está disponível na raiz do projeto.")
        print("=" * 60)
    else:
        print("\n[Erro] Falha ao gerar o relatório final.")
        sys.exit(1)

if __name__ == "__main__":
    main()
