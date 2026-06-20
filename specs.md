Markdown
# Especificações Técnicas: Pipeline de Busca e Match de Vagas do LinkedIn

## Objetivo do Projeto
Criar um pipeline automatizado em Python que busca vagas de emprego no LinkedIn com base em termos específicos, analisa a descrição de cada vaga contra o perfil profissional do usuário usando a API do Gemini, calcula uma nota de "match" e gera um relatório consolidado em Excel (ordenado pelas melhores vagas). O objetivo final é permitir que o usuário faça a candidatura manual nas vagas com maior pontuação.

## Arquitetura de Pastas e Arquivos
A aplicação deve seguir rigorosamente a estrutura modular abaixo:
```text
vagas-pipeline/
├── config/
│   └── keywords.yaml          # Termos de busca e perfil do usuário
├── src/
│   ├── __init__.py
│   ├── scraper.py             # Automação/Busca de vagas (Selenium ou Playwright)
│   ├── matcher.py             # Inteligência de match (API do Gemini 2.5)
│   └── reporter.py            # Geração do arquivo Excel de saída
├── main.py                    # Script mestre que executa o pipeline ponta a ponta
├── requirements.txt           # Dependências do projeto
├── specs.md                   # Este arquivo de especificações
└── .env                       # Chaves de API e variáveis de ambiente
Detalhamento dos Módulos
1. Configurações (config/keywords.yaml)
Este arquivo deve conter os parâmetros que guiarão a busca e a IA.

Estrutura desejada:

termos_busca: Lista de cargos para buscar (Ex: "Desenvolvedor Python Pleno", "Data Engineer").

localizacao: Local da busca (Ex: "Brasil", "Remoto").

perfil_candidato: Um texto corrido contendo o resumo profissional, competências técnicas (hard skills), soft skills e nível de experiência do usuário.

2. Coletor de Vagas (src/scraper.py)
Responsável por interagir com o LinkedIn e extrair as vagas.

Abordagem: Utilizar busca pública ou automação via navegador (Playwright ou Selenium em modo headless ou normal com pausas aleatórias para evitar bloqueios).

Entrada: Termos de busca e localização vindos do YAML.

Saída: Uma lista de dicionários Python, onde cada dicionário contém: titulo_vaga, empresa, localizacao, link_vaga, descricao_completa.

3. Analisador de Match (src/matcher.py)
Responsável por comparar cada vaga coletada com o perfil do usuário usando a API do Gemini.

Abordagem: Utilizar a biblioteca oficial google-genai com o modelo gemini-2.5-flash devido ao custo-benefício e velocidade.

Comportamento: Enviar a descrição da vaga + o perfil do usuário em um prompt estruturado.

Retorno Obrigatório (JSON): A IA deve responder estritamente em formato JSON contendo:

JSON
  {
    "nota_match": 85,
    "pontos_fortes": ["Experiência com FastAPI", "Inglês Fluente"],
    "gaps": ["Falta conhecimento em Kubernetes"],
    "veredicto": "Ótima oportunidade, focar em destacar projetos com Docker na entrevista."
  }
4. Gerador de Relatório (src/reporter.py)
Responsável por formatar e salvar os dados coletados e analisados.

Abordagem: Utilizar pandas e openpyxl.

Comportamento: Receber os dados consolidados, transformá-los em um DataFrame, ordenar a tabela da maior nota de match para a menor e salvar em um arquivo chamado vagas_filtradas.xlsx na raiz do projeto.

Colunas do Excel: Nota de Match, Título da Vaga, Empresa, Link da Vaga, Pontos Fortes, Gaps, Veredicto.

5. Executável Principal (main.py)
O ponto de entrada da aplicação. Deve importar os módulos do src/, carregar as variáveis do arquivo .env (usando python-dotenv) e orquestrar a execução sequencial:

Carregar configurações -> 2. Executar Scraper -> 3. Executar Matcher -> 4. Executar Reporter.

Requisitos de Tratamento de Erros e Boas Práticas
Logs: Adicionar mensagens de print limpas indicando o progresso (ex: "Buscando vagas para termo X...", "Analisando vaga 3 de 10...").

Resiliência: Se a API do Gemini falhar em uma vaga específica devido a limite de requisições ou erro de formato, o script deve pular para a próxima vaga sem quebrar a execução inteira.

Segurança: A API Key do Gemini nunca deve estar hardcoded no código. Ela deve ser lida de os.getenv("GEMINI_API_KEY").