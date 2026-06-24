> **Nota histórica:** este é o documento de especificação **original (v1)** do projeto.
> A implementação evoluiu desde então — em especial, a classificação de contratação
> migrou de um classificador local por **embeddings** (`sentence-transformers`) para um
> classificador via **LLM** (`src/matcher.py::LLMContractClassifier`). Para a arquitetura
> atual, consulte o [README](../README.md).

# Especificações Técnicas: Pipeline de Busca e Match de Vagas do LinkedIn

## Objetivo do Projeto
Criar um pipeline automatizado em Python que busca vagas de emprego no LinkedIn com base em termos específicos, analisa a descrição de cada vaga contra o perfil profissional do usuário usando a API do Gemini, calcula uma nota de "match" e gera um relatório consolidado em Markdown (ordenado pelas melhores vagas). O objetivo final é permitir que o usuário faça a candidatura manual nas vagas com maior pontuação, evitando retrabalho por meio de um histórico de vagas já processadas.

## Arquitetura de Pastas e Arquivos
A aplicação deve seguir rigorosamente a estrutura modular abaixo:
```text
vagas-pipeline/
├── config/
│   ├── keywords.yaml          # Termos de busca, filtros e perfil do usuário
│   └── contract_examples.yaml # Protótipos semânticos de CLT e não-CLT
├── src/
│   ├── __init__.py
│   ├── scraper.py             # Automação/Busca de vagas (Selenium ou Playwright)
│   ├── contract_classifier.py # Classificação local de contratação via embeddings
│   ├── matcher.py             # Inteligência de match (API do Gemini 2.5)
│   └── reporter.py            # Geração do arquivo Markdown de saída
├── main.py                    # Script mestre que executa o pipeline ponta a ponta
├── requirements.txt           # Dependências do projeto
├── specs.md                   # Este arquivo de especificações
└── .env                       # Chaves de API e variáveis de ambiente
Detalhamento dos Módulos
1. Configurações (config/keywords.yaml)
Este arquivo deve conter os parâmetros que guiarão a busca e a IA.

Estrutura desejada:

termos_busca: Lista de cargos para buscar (Ex: "Desenvolvedor Python Pleno", "Data Engineer"). O termo de busca não deve receber "CLT" automaticamente, pois a contratação será inferida pela descrição da vaga.

localizacao: Local da busca (Ex: "Brasil", "Remoto").

tipo_contratacao: Tipo desejado, inicialmente "CLT".

filtros_busca: Lista de cenários aceitos de modelo/localidade, por exemplo remoto no Brasil e híbrido em São José-SC ou Florianópolis-SC.

perfil_candidato: Um texto corrido contendo o resumo profissional, competências técnicas (hard skills), soft skills e nível de experiência do usuário.

2. Protótipos de Contratação (config/contract_examples.yaml)
Este arquivo deve conter exemplos curtos representativos de descrições CLT e não-CLT para alimentar o classificador local por embeddings.

Estrutura desejada:

```yaml
modelo_embedding: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
limiar_minimo_clt: 0.55
margem_minima_clt: 0.08
prototipos_clt:
  - "Vaga efetiva CLT com vale-refeição, vale-alimentação, plano de saúde, férias remuneradas e 13º salário."
  - "Contratação CLT, jornada de 40 horas semanais, segunda a sexta, banco de horas e benefícios corporativos."
prototipos_nao_clt:
  - "Contrato PJ, necessário CNPJ ativo, emissão de nota fiscal e remuneração por hora."
  - "Atuação freelancer por projeto, horário totalmente flexível, foco em entregas e sem vínculo empregatício."
```

3. Coletor de Vagas (src/scraper.py)
Responsável por interagir com o LinkedIn e extrair as vagas.

Abordagem: Utilizar busca pública ou automação via navegador (Playwright ou Selenium em modo headless ou normal com pausas aleatórias para evitar bloqueios).

Entrada: Termos de busca e localização vindos do YAML.

Saída: Uma lista de dicionários Python, onde cada dicionário contém: titulo_vaga, empresa, localizacao, link_vaga, descricao_completa, modelo_trabalho, tipo_contratacao_inferido, score_clt, score_nao_clt, margem_contratacao e evidencias_contratacao.

O scraper deve baixar a descrição antes de decidir se a contratação é compatível. Vagas já presentes em vagas_historico.json devem ser ignoradas antes de baixar a descrição, quando o link/ID já estiver disponível.

4. Classificador Local de Contratação (src/contract_classifier.py)
Responsável por inferir se a vaga é CLT, não-CLT ou ambígua usando embeddings locais e similaridade de cosseno.

Abordagem: Usar sentence-transformers com um modelo multilíngue leve e local, preferencialmente "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2". O modelo deve rodar em CPU, carregar uma vez por execução e usar cache local padrão do Hugging Face/Sentence Transformers.

Entrada: descrição completa da vaga e protótipos de contratação carregados de config/contract_examples.yaml.

Comportamento:
- Gerar embedding da descrição da vaga.
- Gerar embeddings dos protótipos CLT e não-CLT.
- Calcular similaridade de cosseno entre a vaga e cada grupo de protótipos.
- Consolidar score_clt como a maior ou média das melhores similaridades com protótipos CLT.
- Consolidar score_nao_clt como a maior ou média das melhores similaridades com protótipos não-CLT.
- Calcular margem_contratacao = score_clt - score_nao_clt.
- Aceitar a vaga somente quando score_clt >= limiar_minimo_clt e margem_contratacao >= margem_minima_clt.
- Descartar vagas ambíguas. Se a margem for insuficiente, se score_clt for baixo, ou se score_nao_clt for maior/igual, a vaga não deve seguir para o matcher.

Regras textuais explícitas podem ser mantidas apenas como apoio de auditoria ou bloqueio de termos inequívocos como PJ, CNPJ e nota fiscal, mas não devem ser o mecanismo principal de classificação CLT.

Saída obrigatória:
```json
{
  "tipo_contratacao_inferido": "CLT | NAO_CLT | AMBIGUA",
  "aceita": true,
  "score_clt": 0.67,
  "score_nao_clt": 0.41,
  "margem_contratacao": 0.26,
  "evidencias_contratacao": "Mais próximo dos protótipos CLT por benefícios, jornada e vínculo."
}
```

5. Analisador de Match (src/matcher.py)
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
6. Gerador de Relatório (src/reporter.py)
Responsável por formatar e salvar os dados coletados e analisados.

Abordagem: Gerar um arquivo Markdown simples, legível em qualquer editor e fácil de versionar.

Comportamento: Receber os dados consolidados, ordenar a lista da maior nota de match para a menor e salvar em um arquivo chamado vagas_filtradas.md na raiz do projeto.

Conteúdo do Markdown: resumo em tabela com Nota de Match, Título da Vaga, Empresa, Tipo de Contratação Inferido, Score CLT, Score Não-CLT, Margem, Modelo de Trabalho, Localização e Link da Vaga; depois uma seção detalhada com evidências de contratação, Pontos Fortes, Gaps e Veredicto.

7. Histórico de Vagas (vagas_historico.json)
Responsável por registrar os links das vagas já processadas para evitar análise repetida em execuções futuras.

Abordagem: Usar JSON na raiz do projeto, com o link da vaga como chave única.

Comportamento: O scraper deve ignorar vagas cujo link já exista no histórico. Ao final de uma execução bem-sucedida, o histórico deve ser atualizado com os metadados principais da vaga, data de processamento, tipo de contratação inferido e scores semânticos.

8. Executável Principal (main.py)
O ponto de entrada da aplicação. Deve importar os módulos do src/, carregar as variáveis do arquivo .env (usando python-dotenv) e orquestrar a execução sequencial:

Carregar configurações -> 2. Carregar protótipos de contratação -> 3. Inicializar classificador local -> 4. Carregar histórico -> 5. Executar Scraper ignorando vagas já conhecidas -> 6. Classificar contratação e descartar ambíguas/não-CLT -> 7. Executar Matcher nas vagas aceitas -> 8. Executar Reporter -> 9. Atualizar histórico.

Requisitos de Tratamento de Erros e Boas Práticas
Logs: Adicionar mensagens de print limpas indicando o progresso (ex: "Buscando vagas para termo X...", "Analisando vaga 3 de 10...").

Resiliência: Se a API do Gemini falhar em uma vaga específica devido a limite de requisições ou erro de formato, o script deve pular para a próxima vaga sem quebrar a execução inteira.

Segurança: A API Key do Gemini nunca deve estar hardcoded no código. Ela deve ser lida de os.getenv("GEMINI_API_KEY").

Recursos locais: O classificador de contratação deve ser leve o suficiente para rodar em CPU. A primeira execução pode baixar o modelo de embeddings, mas execuções futuras devem reutilizar o cache local. Caso o modelo local não esteja disponível, o pipeline deve falhar com mensagem clara explicando como instalar/baixar as dependências, em vez de cair para uma classificação frágil por strings.
