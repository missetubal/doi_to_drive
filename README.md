# Baixar artigos por DOI para o computador

Script em Python que lê um CSV com `title`, `abstract` e `doi`, procura versões de **acesso aberto** (Unpaywall, OpenAlex e Semantic Scholar) e salva os PDFs em uma pasta local. Não envia nada ao Google Drive e não contorna paywalls nem autenticações de editoras. O campo `abstract` é aceito como metadado, mas a busca do artigo é feita pelo DOI.

## Preparação

1. Instale Python 3.10 ou superior.
2. Abra um terminal na pasta do projeto e instale as dependências:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Prepare sua lista em CSV, seguindo o formato de `articles.csv` e mantendo as colunas `title`, `abstract` e `doi`.

## Executar

```bash
python doi_to_drive.py --input articles.csv --email seu-email@instituicao.edu
```

Os PDFs e o relatório `results.csv` serão salvos na pasta `artigos_baixados`, criada junto ao projeto. Para escolher outra pasta local:

```bash
python doi_to_drive.py --input articles.csv --email seu-email@instituicao.edu --download-dir "C:/Users/SeuNome/Documents/Artigos"
```

No macOS ou Linux, use um caminho local como `~/Documents/Artigos`. Se não houver uma versão aberta identificada, o script anota isso e continua com os demais artigos.

## Relatório (`results.csv`)

Cada linha traz `title`, `doi`, `doi_found` (DOI descoberto pelo título), `status` (`ok` ou `failed`), `reason`, `file` (nome do PDF na pasta), `source` (fonte que achou o PDF), `source_url` e `message` (detalhe do erro). Valores de `reason`:

| reason | significado |
|---|---|
| `downloaded` | baixado nesta execução |
| `already_downloaded` | o PDF já estava na pasta; não foi baixado de novo |
| `not_open_access` | nenhuma fonte conhece versão aberta |
| `download_failed` | há versão aberta, mas o site bloqueou (ex.: 403) ou não devolveu um PDF |
| `not_found` | DOI não encontrado em nenhuma fonte |
| `invalid_doi` | DOI vazio ou mal formatado no CSV |
| `duplicate` | DOI repetido na lista |
| `api_error` / `error` | falha de rede ou erro inesperado |

O relatório é gravado linha a linha, então fica salvo mesmo se a execução for interrompida. Ao final, o terminal mostra um resumo por motivo.

## Fontes consultadas

Para cada DOI, as fontes são consultadas nesta ordem, parando no primeiro PDF válido:

1. **Unpaywall**: base de versões de acesso aberto (editoras e repositórios).
2. **OpenAlex**: catálogo aberto de publicações, às vezes com links extras.
3. **Semantic Scholar**: campo `openAccessPdf` e cópias no arXiv.

Quando um link aponta para a página do artigo, e não para o PDF, o script tenta o link indicado na meta tag `citation_pdf_url` da página. Para mudar ou limitar as fontes, use `--sources openalex,semanticscholar`. Chaves de API são opcionais e ajudam com limites de requisições: `S2_API_KEY` (Semantic Scholar) e `OPENALEX_API_KEY`, definidas como variáveis de ambiente.

### Elsevier (artigos assinados pela sua instituição)

Artigos da Elsevier (DOI `10.1016/...`) podem ser baixados pela API do ScienceDirect, mesmo sem ser acesso aberto, quando sua instituição assina o periódico (por exemplo, pelo Portal de Periódicos CAPES). A política de mineração de texto da Elsevier permite esse uso para pesquisa não comercial.

1. Crie uma chave gratuita em https://dev.elsevier.com ("I want an API key").
2. Rode o script **na rede da universidade ou conectada à VPN dela**. Fora dessa rede, peça à biblioteca um *insttoken* da Elsevier.
3. Defina a chave e rode normalmente. A fonte `elsevier` é ativada automaticamente:

   ```powershell
   $env:ELSEVIER_API_KEY = "sua-chave"
   # opcional, fora da rede da universidade:
   $env:ELSEVIER_INSTTOKEN = "seu-insttoken"
   nome_do_ambiente\Scripts\python.exe doi_to_drive.py --input articles.csv --email seu-email@...
   ```

Antes de baixar, o script verifica se você tem acesso ao artigo. Sem acesso, o relatório mostra "Elsevier: sem acesso institucional". Um erro HTTP 401 ou 403 indica chave inválida ou que você está fora da rede ou VPN da instituição.

### Artigos sem DOI

Quando o DOI está vazio ou inválido, o script procura o DOI pelo título no OpenAlex e no Crossref. Ele só aceita um resultado quando o título encontrado é praticamente idêntico ao da lista (semelhança de pelo menos 93%). O DOI encontrado aparece na coluna `doi_found` do relatório. Muitos trabalhos de conferência simplesmente não têm DOI e continuam como `invalid_doi`. Para desligar essa busca, use `--no-doi-lookup`.

Nenhuma dessas fontes pesquisa Scopus ou outras bases pagas. O Scopus não guarda PDFs, mas o CSV exportado por ele (colunas `Title`, `Abstract`, `DOI`) pode ser usado diretamente em `--input`. Alguns sites (MDPI, Wiley, ACM) bloqueiam downloads automáticos com erro 403, mesmo de artigos abertos. Nesses casos, abra no navegador um dos links listados na coluna `message`.

## Não baixar de novo

O script identifica um PDF já baixado pelo **DOI no nome do arquivo**, e não pelo título. Por isso ele é reconhecido mesmo que o título mude no CSV. Basta rodar o mesmo comando outra vez: só os artigos que ainda faltam serão buscados, e os já existentes não esperam o `--delay`.

O e-mail informado é enviado como contato às APIs do Unpaywall e do OpenAlex. O programa consulta essas APIs e os endereços das versões abertas; não exige credenciais do Google nem realiza autenticação OAuth.

## Limites de acesso

O script baixa apenas PDFs identificados como acesso aberto. Ele não tenta acessar conteúdo pago, contornar paywalls ou usar autenticações de editoras. A disponibilidade varia conforme o DOI e as fontes abertas registradas.
