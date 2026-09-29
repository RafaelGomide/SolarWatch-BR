# Documentação Técnica — Fase de Ingestão de Dados

> Escopo: tudo o que foi construído na fase de ingestão do SolarWatch BR — as três fontes públicas (ONS, NASA POWER e ANEEL/SIGA), o código que as coleta, as decisões de design por trás dele e o que se sabe sobre a qualidade dos dados brutos gerados.
>
> Posição na arquitetura: esta fase corresponde ao bloco **"Ingestão: API + scraping"** do pipeline offline descrito em `System design/system-design-solarwatch-api.md` (§6.1–6.2). Ela roda manualmente na máquina de desenvolvimento, **nunca** no Render, e produz a *raw layer* (`dados/bruto/`) que alimenta o ETL e, por fim, o arquivo DuckDB servido pela API.

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Estrutura de arquivos](#2-estrutura-de-arquivos)
3. [Como executar](#3-como-executar)
4. [Princípios de design comuns](#4-princípios-de-design-comuns)
5. [Módulo compartilhado `ingestao/http.py`](#5-módulo-compartilhado-ingestaohttppy)
6. [Fonte 1 — ONS: geração horária por usina](#6-fonte-1--ons-geração-horária-por-usina)
7. [Fonte 2 — NASA POWER: clima horário por coordenada](#7-fonte-2--nasa-power-clima-horário-por-coordenada)
8. [Fonte 3 — ANEEL SIGA: cadastro de usinas](#8-fonte-3--aneel-siga-cadastro-de-usinas)
9. [Como as três fontes se cruzam](#9-como-as-três-fontes-se-cruzam)
10. [Compliance, segurança e privacidade](#10-compliance-segurança-e-privacidade)
11. [Testes automatizados](#11-testes-automatizados)
12. [Limitações conhecidas e próximos passos](#12-limitações-conhecidas-e-próximos-passos)

---

## 1. Visão geral

| Fonte | O que traz | Grão | Forma de acesso | Saída |
|---|---|---|---|---|
| **ONS** — Operador Nacional do Sistema Elétrico | Geração verificada (MWmed) por usina/conjunto, com subsistema, estado e tipo de fonte (hidráulica, térmica, eólica, fotovoltaica, nuclear) | usina × hora | API CKAN do portal (descoberta) + Parquets mensais no S3 | `dados/bruto/dados_ons_bruto/` (um Parquet por mês) |
| **NASA POWER** | Irradiância solar, vento a 10 m e 50 m, temperatura a 2 m | coordenada × hora (UTC) **e** coordenada × dia (LST) | API REST `temporal/hourly/point` e `temporal/daily/point` | `dados/bruto/dados_nasa_bruto.parquet` + `dados/bruto/dados_nasa_diario_bruto.parquet` |
| **ANEEL — SIGA** | Cadastro de empreendimentos solares (UFV) e eólicos (EOL): nome, CEG, potência, município, fase, data de entrada em operação, coordenadas | empreendimento | API CKAN `datastore_search` (paginada) | `dados/bruto/dados_aneel_bruto.parquet` + retrato datado em `historico_aneel/` |

As três fontes são **públicas, gratuitas e sem autenticação**, o que é consistente com a restrição de orçamento zero do projeto (§14 do system design).

O objetivo analítico é cruzar **o que foi gerado** (ONS) com **as condições climáticas que explicam a geração** (NASA) e **a capacidade instalada que poderia ter gerado** (ANEEL). Com isso ficam possíveis o fator de capacidade, a previsão de geração e a análise de sobrevivência de ativos.

### Volume da última execução (18/09/2026)

| Arquivo | Linhas | Tamanho (Parquet) | Tamanho equivalente em CSV | Período |
|---|---|---|---|---|
| `dados_ons_bruto/` (3 arquivos mensais) | 1.365.096 | ~10,4 MB | ~214 MB | 01/07/2026 00h → 17/09/2026 23h |
| `dados_nasa_bruto.parquet` | 19.200 | ~0,1 MB | ~1,5 MB | 01/07/2026 00h → 18/09/2026 23h (UTC), 10 locais |
| `dados_nasa_diario_bruto.parquet` | 800 | <0,1 MB | — | 01/07/2026 → 18/09/2026 (dias LST), 10 locais |
| `dados_aneel_bruto.parquet` | 20.511 | ~0,8 MB | ~6 MB | retrato do cadastro em 18/09/2026 (arquivado também em `historico_aneel/dados_aneel_bruto_2026-09-18.parquet`) |

A camada bruta começou em CSV e foi migrada para Parquet. Veja a [§4.5](#45-formato-de-saída-parquet).

Os números da NASA são dos 10 locais da versão manual do `locais.csv`. A geração automática a partir da ANEEL ([§7.3](#73-escolha-dos-locais--locaiscsv)) passou para **19 pontos**, então a próxima coleta tem cerca de 1,9x essas linhas — na casa de 36 mil na série horária, o que continua irrelevante em disco.

---

## 2. Estrutura de arquivos

```
SolarWatch-BR/
├── .env                        # configuração local (NÃO versionado)
├── .gitignore
├── requirements.txt            # requests, pandas, python-dotenv, pyarrow (+ libs do ds_toolkit)
├── ingestao/
│   ├── __init__.py
│   ├── __main__.py             # orquestrador: python -m ingestao
│   ├── http.py                 # GET com retry/backoff + sessão HTTP compartilhada
│   ├── armazenamento.py        # gravação atômica em Parquet (camada bruta)
│   ├── ONS/
│   │   ├── __init__.py
│   │   └── ingestao_ons.py
│   ├── nasa_power/
│   │   ├── __init__.py
│   │   ├── ingestao_nasa_power.py
│   │   ├── gerar_locais.py     # gera locais.csv a partir do cadastro da ANEEL
│   │   └── locais.csv          # coordenadas consultadas na NASA POWER (gerado)
│   └── aneel/
│       ├── __init__.py
│       └── ingestao_aneel.py
├── dados/
│   └── bruto/                  # raw layer (NÃO versionado)
│       ├── dados_ons_bruto/            # um Parquet por mês
│       │   ├── dados_ons_bruto_2026_07.parquet
│       │   ├── dados_ons_bruto_2026_08.parquet
│       │   └── dados_ons_bruto_2026_09.parquet
│       ├── dados_nasa_bruto.parquet
│       ├── dados_nasa_diario_bruto.parquet
│       ├── dados_aneel_bruto.parquet    # retrato corrente (lido pelo ETL)
│       └── historico_aneel/             # retratos datados, um por publicação
│           └── dados_aneel_bruto_2026-09-18.parquet
└── docs/ingestao/
    └── doc_tecnica_ingestao.md    # este documento
```

Cada fonte é um **pacote Python** (tem `__init__.py`). Isso permite executá-las com `python -m ingestao.<fonte>.<script>` a partir da raiz e importar os módulos compartilhados `ingestao.http` e `ingestao.armazenamento` sem manipular `sys.path`.

---

## 3. Como executar

### 3.1 Ambiente

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

Versão testada: Python 3.13.5. Bibliotecas:

| Biblioteca | Uso |
|---|---|
| `requests` | Chamadas HTTP (sessão com keep-alive, timeouts, parâmetros de query) |
| `pandas` | Montagem dos DataFrames e leitura/escrita da camada bruta. Fixado em `<3` porque o `lifelines` (usado pelo `ds_toolkit`) ainda não suporta o pandas 3 |
| `pyarrow` | Engine de leitura e escrita Parquet |
| `python-dotenv` | Leitura das variáveis de período a partir do `.env` |

### 3.2 Comandos (sempre a partir da raiz do repositório)

**O caminho normal é um comando só:**

```bash
python -m ingestao                                 # ONS, ANEEL, locais.csv, NASA
python -m ingestao --inicio 2026-01 --fim 2026-08  # período repassado a ONS e NASA
python -m ingestao --fontes ons                    # só uma etapa
python -m ingestao --listar                        # mostra o plano e sai
python -m ingestao --seguir                        # não para na primeira falha
```

Os scripts continuam executáveis individualmente, para depurar uma fonte ou usar uma opção específica:

```bash
python -m ingestao.ONS.ingestao_ons --inicio 2026-01 --fim 2026-08

python -m ingestao.aneel.ingestao_aneel --tipos UFV EOL UHE PCH
python -m ingestao.aneel.ingestao_aneel --listar-retratos    # só lista o histórico
python -m ingestao.aneel.ingestao_aneel --mudancas-de-fase   # compara os 2 últimos retratos

python -m ingestao.nasa_power.gerar_locais              # locais.csv a partir da ANEEL
python -m ingestao.nasa_power.ingestao_nasa_power       # mesmo período do ONS
```

### 3.2.1 O orquestrador — `ingestao/__main__.py`

Eram três comandos em sequência, com uma dependência não óbvia entre eles, e nada impedia rodar na ordem errada ou esquecer uma fonte. O `python -m ingestao` resolve os dois.

**A ordem não é arbitrária:** `gerar_locais` lê o cadastro da ANEEL para montar o `locais.csv`, e a ingestão da NASA consulta exatamente as coordenadas desse arquivo ([§7.3](#73-escolha-dos-locais--locaiscsv)). Rodar a NASA antes da ANEEL usaria os pontos da coleta anterior — sem erro nenhum, só dado desatualizado, que é o pior tipo de falha. Por isso `--fontes nasa ons` executa **ons e depois nasa**: a ordem é a da lista `ETAPAS`, não a que foi digitada.

| Etapa | Módulo | Aceita período |
|---|---|---|
| `ons` | `ingestao.ONS.ingestao_ons` | sim |
| `aneel` | `ingestao.aneel.ingestao_aneel` | não (é um retrato do cadastro) |
| `locais` | `ingestao.nasa_power.gerar_locais` | não |
| `nasa` | `ingestao.nasa_power.ingestao_nasa_power` | sim |

**Cada etapa roda como subprocesso** (`python -m <modulo>`), não por importação. Assim cada script mantém o próprio `argparse`, o próprio logging e o próprio `.env`, e uma falha não deixa estado pela metade no processo das outras. O custo é um interpretador por etapa, irrelevante perto do tempo de rede.

**Falha interrompe por padrão.** Continuar com a ANEEL quebrada só produziria um `locais.csv` desatualizado e um clima dos pontos antigos. O `--seguir` inverte isso quando se quer aproveitar o que der certo. Em qualquer caso, sai um resumo e o código de saída é 1 se alguma etapa não terminou em `ok`:

```
[ingestao] resumo:
  ok      ons        41.2 s
  FALHOU  aneel       3.1 s
  pulada  locais            -
  pulada  nasa              -
```

`python -m ETL.pipeline --ingerir` agora chama este orquestrador em vez de manter a própria lista de módulos: a ordem das fontes passou a ter **uma única definição**, aqui.

### 3.3 Variáveis de ambiente (`.env`)

| Variável | Formato | Usada por | Efeito |
|---|---|---|---|
| `ONS_MES_INICIO` | `AAAA-MM` | ONS, NASA (fallback) | Primeiro mês a baixar |
| `ONS_MES_FIM` | `AAAA-MM` | ONS, NASA (fallback) | Último mês (inclusive). Vazio = mês atual |
| `NASA_MES_INICIO` | `AAAA-MM` | NASA | Sobrescreve `ONS_MES_INICIO` só para a NASA |
| `NASA_MES_FIM` | `AAAA-MM` | NASA | Sobrescreve `ONS_MES_FIM` só para a NASA |

**Precedência:** argumento de linha de comando > variável específica da fonte > variável do ONS (só no caso da NASA) > padrão "últimos 3 meses".

A ANEEL não usa período: o SIGA é um cadastro, um retrato do estado atual, e não uma série temporal.

---

## 4. Princípios de design comuns

Os três scripts seguem as mesmas regras. Elas derivam diretamente das decisões do system design.

### 4.1 Camada bruta sem transformação

Os arquivos em `dados/bruto/` são uma **cópia fiel** do que a fonte entregou:

- **ONS:** o Parquet publicado pelo próprio ONS é lido e regravado **com os tipos que a fonte definiu** (`din_instante` timestamp, `val_geracao` double, o resto texto). O script não faz nenhuma conversão. Um valor ausente na fonte é nulo no Parquet (no CSV do ONS era `""`), e o `"-"` do `ceg` continua `"-"`.
- **NASA:** os valores vêm da API como números JSON e são gravados como vieram, inclusive o `-999` de valor ausente. O timestamp continua como a string `AAAAMMDDHH`.
- **ANEEL:** a API devolve tudo como texto no formato brasileiro (`"1400,00"`, `"-20,12479858"`), e é gravado assim, como colunas de texto no Parquet. Só `_id` é inteiro.

Por que não limpar já na ingestão: se a lógica de limpeza tiver um bug, dá para corrigir e reprocessar a partir do bruto sem voltar às fontes. Além disso, qualquer anomalia observada no dado final pode ser rastreada até o dado original. A limpeza (tipos, fuso horário, sentinelas) é responsabilidade da próxima etapa (ETL).

As únicas adições são **colunas de proveniência ou identificação** que não existem na fonte e não alteram as colunas originais:

- `arquivo_origem` (ONS): nome do arquivo mensal de onde veio cada linha.
- `local`, `municipio`, `id_estado`, `id_subsistema`, `latitude`, `longitude` (NASA): identificam a qual ponto do `locais.csv` a série pertence. A resposta da API só traz a coordenada.

### 4.2 Idempotência por reprocessamento completo

Cada execução **reconstrói o arquivo inteiro** a partir da fonte. Não há *append* nem *upsert* incremental. Rodar duas vezes com os mesmos parâmetros produz o mesmo arquivo, e é impossível duplicar linhas por reexecução. É o que o system design define em §9: "a idempotência é garantida reprocessando o raw layer do zero a cada rebuild". O custo (baixar de novo o que já se tinha) é irrelevante nesta escala.

### 4.3 Escrita atômica (*build-then-swap*)

Implementada uma única vez em `ingestao/armazenamento.py` e usada pelos três scripts:

```python
def gravar_parquet(df, saida):
    temporario = saida.with_suffix(".parquet.tmp")
    df.to_parquet(temporario, index=False, engine="pyarrow", compression="zstd")
    os.replace(temporario, saida)
```

O Parquet é escrito primeiro em `*.parquet.tmp` e só depois substitui o arquivo definitivo com `os.replace`, que é uma operação atômica no mesmo sistema de arquivos, inclusive no Windows. Se o processo cair no meio da escrita (falta de espaço, Ctrl+C, erro de rede antes da escrita), o arquivo anterior continua íntegro. Nunca existe um `dados_*_bruto.parquet` pela metade. Isso espelha o *build-then-swap* que o ETL usa para o `.duckdb` (§9 e §13.3 do system design).

Consequência prática: cada chamada monta um DataFrame inteiro em memória antes de escrever. Por isso o ONS grava **um arquivo por mês** ([§6.2.1](#621-um-parquet-por-mês)): o pico passa a ser o de um mês (~530 mil linhas), independente do tamanho do período. Na versão CSV, com tudo como texto, eram 1–2 GB para os mesmos 3 meses.

### 4.4 Resiliência de rede

Todas as chamadas HTTP passam por `ingestao.http.get_com_retry` (detalhado na [§5](#5-módulo-compartilhado-ingestaohttppy)). Há também uma pausa de cortesia entre chamadas consecutivas à mesma API: 1 s na NASA e 0,5 s na ANEEL. São serviços públicos gratuitos, e não há motivo para martelá-los.

### 4.5 Formato de saída: Parquet

A primeira versão da ingestão gravava CSV. A camada bruta foi migrada para **Apache Parquet** (engine `pyarrow`, compressão **zstd**, sem índice do pandas) pelos motivos abaixo:

| Aspecto | CSV (antes) | Parquet (agora) |
|---|---|---|
| Tamanho em disco (3 meses) | ~221 MB | ~11 MB (**~20× menor**) |
| Download do ONS | CSV de ~70 MB/mês, ~2,5 min para 3 meses | Parquet de ~4,6 MB/mês, **~5 s** para 3 meses |
| Tipos | tudo texto; o ETL precisa reinterpretar | esquema embutido no arquivo (timestamp, double, string) |
| Leitura seletiva | lê o arquivo inteiro | lê só as colunas pedidas (`columns=[...]`), formato colunar |
| Dialeto | separador, aspas e encoding precisam ser combinados | binário, sem ambiguidade de separador ou encoding |
| Integração | — | lido nativamente pelo DuckDB (`read_parquet`), que é o banco do projeto |

**Validação da migração:** antes de apagar os CSVs antigos, os Parquets novos foram comparados com eles:
- **ONS:** nos meses completos (jul e ago/2026), mesmo número de linhas, mesmos nulos de `val_geracao` (63.576 e 63.240) e colunas de texto idênticas. A diferença máxima em `val_geracao` foi de 4,5e-13 (arredondamento de ponto flutuante do CSV).
- **NASA:** as 18.960 linhas que se sobrepõem têm valores idênticos (fora as horas que antes eram `-999` e agora já foram publicadas).
- **ANEEL:** 20.511 registros, mesmas colunas e o mesmo conjunto de CEGs.

Outras regras de saída:
- Os nomes de coluna originais da fonte são **preservados** (`din_instante`, `val_geracao`, `MdaPotenciaOutorgadaKw` etc.). A renomeação para um padrão único fica para o ETL.

### 4.6 Logs

Todos os scripts usam `logging` com nível INFO e formato `data hora NÍVEL mensagem`. Cada arquivo/página/local baixado é registrado com a contagem de linhas, o que funciona como uma auditoria mínima da execução.

---

## 5. Módulo compartilhado `ingestao/http.py`

Criado quando a segunda fonte (NASA) entrou. A lógica de retry, que antes estava no script do ONS, foi extraída para cá para não ser duplicada nas três ingestões.

### 5.1 `get_com_retry(sessao, url, **kwargs)`

```python
for tentativa in range(1, MAX_TENTATIVAS + 1):         # MAX_TENTATIVAS = 5
    try:
        resp = sessao.get(url, timeout=TIMEOUT_S, **kwargs)   # TIMEOUT_S = 120
    except (ConnectionError, Timeout) as erro:                # falha de rede
        _esperar_ou_levantar(tentativa, url, erro)
        continue

    if resp.status_code == 404:
        return resp
    if resp.status_code == 429 or resp.status_code >= 500:     # transitório
        _esperar_ou_levantar(tentativa, url, HTTPError(f"HTTP {resp.status_code}", response=resp))
        continue

    resp.raise_for_status()   # 4xx não-transitório: levanta já na 1ª tentativa
    return resp
```

A decisão de repetir ou não está **antes** do `raise_for_status()`, e não num `except` que capturaria os dois casos. `_esperar_ou_levantar(tentativa, url, erro)` concentra o backoff: dorme e devolve o controle, ou propaga `erro` quando a última tentativa acabou.

Como cada tipo de resposta é tratado:

| Situação | Comportamento | Por quê |
|---|---|---|
| `2xx` | retorna | sucesso |
| `404` | retorna **sem** retry | "não existe" não é falha transitória. O chamador decide: o ONS usa isso para pular meses ainda não publicados |
| `429` (rate limit) e `5xx` | retry | falhas transitórias típicas de servidor sobrecarregado |
| outros `4xx` (400, 401, 403, 409, 422…) | `raise_for_status()` levanta `HTTPError` **na primeira tentativa** | é erro da própria requisição (parâmetro, rota, credencial); repetir devolve exatamente a mesma resposta |
| erro de conexão / timeout | retry | falha de rede transitória |

Espera entre tentativas: **exponential backoff com jitter**. As esperas são 2, 4, 8 e 16 s, mais 0–1 s aleatório, com teto de 60 s. Na pior falha persistente, o total fica em torno de 30 s antes de desistir. O *jitter* evita que várias execuções simultâneas retentem em sincronia. Nesta escala isso é mais boa prática do que necessidade, mas está pedido explicitamente em §11 do system design.

**Por que retry é seguro aqui:** todas as chamadas são `GET` de leitura, idempotentes por definição (§11 do system design).

**Histórico — `4xx` repetido sem motivo:** a primeira versão tinha um único `try` cobrindo a resposta inteira, com `ConnectionError`, `Timeout` e `HTTPError` no mesmo `except`. O `HTTPError` levantado pelo `raise_for_status()` num `400` caía no mesmo caminho de retry dos `5xx`, então um erro de programação — parâmetro errado, rota errada — só era reportado depois de 5 tentativas e ~30 s de espera. Pior, cada tentativa martelava um serviço público com uma requisição que já se sabia inválida.

Agora o `raise_for_status()` está fora do `try`, o que separa **falha transitória** (vale repetir) de **requisição inválida** (não vale). Medido contra a API do ONS, um `409` real (chamada de `package_show` sem o parâmetro `id`) passou de ~30 s para **0,2 s** até o erro chegar. O comportamento dos casos transitórios não mudou: `429`, `5xx`, `ConnectionError` e `Timeout` continuam com as 5 tentativas.

### 5.2 `nova_sessao()`

Cria um `requests.Session` com `User-Agent: SolarWatch-BR/ingestao (dados abertos)`.

- **Sessão:** reaproveita a conexão TCP/TLS entre chamadas (*keep-alive*). Isso faz diferença nas dezenas de chamadas da NASA e da ANEEL.
- **User-Agent identificável:** é boa prática ao consumir APIs públicas. O operador do serviço consegue saber quem está consumindo, em vez de ver um `python-requests/x.y` genérico.

---

## 6. Fonte 1 — ONS: geração horária por usina

Arquivo: [`ingestao/ONS/ingestao_ons.py`](../../../ingestao/ONS/ingestao_ons.py)

### 6.1 A fonte

- Portal: <https://dados.ons.org.br> (CKAN)
- Dataset: **"Geração por Usina em Base Horária"** — id CKAN `geracao-usina-2`
- Arquivos físicos: bucket S3 público do ONS, um arquivo por mês desde 2022, publicado em três formatos (CSV, XLSX e Parquet). **O script usa o Parquet:**
  `https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/geracao_usina_2_ho/GERACAO_USINA-2_AAAA_MM.parquet`
- Cada mês tem cerca de 530 mil linhas: ~4,6 MB em Parquet contra ~70 MB em CSV. Nos testes, as colunas, as linhas e os valores dos dois formatos foram idênticos.

Por que este dataset: o pedido era "geração por fonte, por subsistema, em base horária/diária". Este dataset tem o **grão mais fino disponível** (usina × hora) e traz subsistema, estado e tipo de fonte em cada linha. Qualquer agregação por fonte, subsistema ou dia pode ser derivada dele no ETL, mas o caminho inverso não existe. O grão usina × timestamp também é exatamente o grão da tabela-fato definida no system design (§4, `fato_geracao`).

### 6.2 Fluxo de execução

```
main()
 ├─ load_dotenv(.env)
 ├─ resolve período: --inicio/--fim > ONS_MES_INICIO/ONS_MES_FIM > últimos 3 meses
 └─ baixar(inicio, fim)
     ├─ nova_sessao()
     ├─ _urls_disponiveis()  → consulta CKAN, monta {(ano, mes): url}
     ├─ para cada (ano, mes) em _meses(inicio, fim):
     │    ├─ url = publicados[(ano, mes)]  ou  URL_ARQUIVO montada pelo padrão
     │    ├─ get_com_retry(url)
     │    ├─ 404 → log "ainda não publicado" e pula
     │    ├─ pd.read_parquet(BytesIO(conteudo))  + coluna arquivo_origem
     │    └─ gravar_parquet → dados/bruto/dados_ons_bruto/dados_ons_bruto_AAAA_MM.parquet
     └─ nenhum mês gravado → SystemExit com mensagem
```

### 6.2.1 Um Parquet por mês

A primeira versão acumulava os meses numa lista e gravava um `concat` único em `dados_ons_bruto.parquet`. Agora **cada mês é gravado assim que é baixado**, na pasta `dados/bruto/dados_ons_bruto/`, e a pasta inteira é lida como um só dataset pelo glob `dados_ons_bruto/*.parquet`.

Três razões:

1. **Memória.** O `concat` mantinha todos os meses do período em RAM ao mesmo tempo, mais uma cópia durante a concatenação. Um mês são ~530 mil linhas; três meses cabem, mas baixar anos de histórico não. Gravando por mês, o pico de memória é o de **um** mês, independente do tamanho do período.
2. **Ingestão incremental.** Rodar com `--inicio 2026-10 --fim 2026-10` acrescenta ou regrava só aquele mês, sem tocar nos demais. Antes, o arquivo consolidado era reescrito por inteiro com o período pedido — e um período menor *apagava* os meses anteriores.
3. **Falha parcial não perde tudo.** Se a rede cair no quinto de doze meses, os quatro já gravados continuam lá. Cada arquivo é escrito atomicamente pelo `gravar_parquet` ([§4.5](#45-formato-de-saída-parquet)), então nunca existe um mês pela metade.

O custo é ler uma pasta em vez de um arquivo: quem consome o bruto usa `ETL.utils.ler_parquet`, que faz o glob (detalhado na [doc do ETL](../ETL/doc_tecnica_etl.md)). O DuckDB leria a mesma pasta nativamente com `read_parquet('dados/bruto/dados_ons_bruto/*.parquet')`.

Nomes dos arquivos: `dados_ons_bruto_AAAA_MM.parquet`. A coluna `arquivo_origem` continua guardando o nome do arquivo **do ONS** (`GERACAO_USINA-2_2026_07.parquet`), que é a procedência real do dado.

### 6.3 Descoberta de arquivos: `_urls_disponiveis`

Em vez de montar URLs às cegas, o script primeiro pergunta ao portal quais arquivos existem, com `GET https://dados.ons.org.br/api/3/action/package_show?id=geracao-usina-2`. A resposta lista cerca de 240 recursos: CSV, XLSX e PARQUET de todos os meses e anos.

O filtro dos Parquets mensais usa a regex:

```python
PADRAO_MENSAL = re.compile(r"GERACAO_USINA-2_(\d{4})_(\d{2})\.parquet$")
```

**Histórico — bug encontrado na primeira execução:** a versão inicial extraía ano e mês com `url.rsplit("_", 2)`, supondo que todo arquivo seguisse `..._AAAA_MM.csv`. Os anos mais antigos do dataset, porém, são publicados como **um arquivo anual** (`GERACAO_USINA-2_AAAA.csv`). Para esses, o `rsplit` devolvia `"USINA-2"` como "ano" e o `int()` quebrava com `ValueError`. A regex resolve isso casando só o padrão mensal e ignorando o resto (anuais, CSV, XLSX).

**Fallback:** se a API CKAN estiver fora do ar ou mudar o formato da resposta (`RequestException`, `KeyError`, `ValueError`), a função registra um aviso e devolve `{}`. Nesse caso cada mês usa a URL montada pelo padrão `URL_ARQUIVO`. Ou seja, **o CKAN é uma otimização de robustez, não um ponto único de falha**: o download direto do S3 continua funcionando sem ele.

### 6.4 Geração dos meses: `_meses`

Gera a lista `[(ano, mes), ...]` de `inicio` a `fim`, inclusive, virando o ano corretamente (dez → jan). A comparação `(ano, mes) <= (fim.year, fim.month)` usa a ordenação natural de tuplas do Python.

**Período padrão:** `_meses(date(hoje.year - 1, hoje.month, 1), hoje)[-3]` gera os últimos 13 meses e pega o terceiro a partir do fim. Isso resulta em "mês atual − 2" (em 17/09/2026: julho/2026), com a virada de ano tratada sem aritmética manual.

### 6.5 Mês ainda não publicado

O mês corrente é publicado de forma parcial e incremental pelo ONS (na execução de 17/09, setembro vinha até o dia 16). Um mês futuro, ou recém-iniciado e ainda não publicado, retorna `404` do S3. `get_com_retry` devolve o 404 sem retry e o script **pula o mês com um aviso** em vez de falhar. Se nenhum mês do período existir, o script termina com `SystemExit("Nenhum arquivo baixado para o período informado.")`, o que evita criar uma pasta vazia. Meses gravados em execuções anteriores não são afetados.

### 6.6 Esquema dos Parquets de `dados_ons_bruto/`

| Coluna | Exemplo | Descrição |
|---|---|---|
| `din_instante` | `2026-07-01 00:00:00` | Início da hora de referência, **horário de Brasília** |
| `id_subsistema` | `N` | Sigla do subsistema: N, NE, SE, S |
| `nom_subsistema` | `NORTE` | NORTE, NORDESTE, SUDESTE, SUL |
| `id_estado` | `AM` | UF |
| `nom_estado` | `AMAZONAS` | Nome da UF |
| `cod_modalidadeoperacao` | `TIPO I` | Modalidade de despacho: TIPO I, TIPO II-B, Conjunto de Usinas, Pequenas Usinas (MMGD), Pequenas Usinas (Tipo III)… |
| `nom_tipousina` | `FOTOVOLTAICA` | HIDROELÉTRICA, TÉRMICA, EOLIELÉTRICA, FOTOVOLTAICA, NUCLEAR |
| `nom_tipocombustivel` | `Fotovoltaica` | Detalhamento do combustível (Gás, Hidráulica…) |
| `nom_usina` | `BALBINA` | Nome da usina ou conjunto |
| `id_ons` | `AMBA` | Código ONS da usina (nulo para MMGD) |
| `ceg` | `UHE.PH.AM.000190-2.01` | Código CEG da ANEEL (`-` quando não há) |
| `val_geracao` | `153.459240099589` | Geração verificada na hora, em **MWmed** (double; nulo quando a fonte não informa) |
| `arquivo_origem` | `GERACAO_USINA-2_2026_07.parquet` | *Adicionada pela ingestão* |

Tipos no Parquet: `din_instante` é timestamp (sem fuso), `val_geracao` é double e as demais colunas são texto, exatamente como o ONS publica.

Distribuição na última execução: HIDROELÉTRICA 419 mil linhas, TÉRMICA 356 mil, EOLIELÉTRICA 316 mil, FOTOVOLTAICA 253 mil, NUCLEAR 3,7 mil. SUDESTE 571 mil, NORDESTE 524 mil, SUL 157 mil, NORTE 96 mil.

### 6.7 Observações de qualidade (para o ETL)

- **Fuso:** `din_instante` está em horário de Brasília (UTC−3, sem horário de verão desde 2019). A NASA está em UTC. Veja a [§9](#9-como-as-três-fontes-se-cruzam).
- **Unidade:** MWmed em uma hora equivale numericamente a MWh gerados naquela hora.
- **Linhas de MMGD** (micro e minigeração distribuída), como `PQU MMAM MMGD`: são estimativas agregadas por estado, sem `id_ons` e com `ceg = "-"`.
- **Conjuntos de usinas:** a maior parte da geração solar e eólica aparece agregada em "Conjunto de Usinas", sem CEG individual. Isso tem impacto direto no cruzamento com a ANEEL ([§9](#9-como-as-três-fontes-se-cruzam)).

---

## 7. Fonte 2 — NASA POWER: clima horário por coordenada

Arquivo: [`ingestao/nasa_power/ingestao_nasa_power.py`](../../../ingestao/nasa_power/ingestao_nasa_power.py)

### 7.1 A fonte

- API: <https://power.larc.nasa.gov> (Prediction Of Worldwide Energy Resources), gratuita e sem chave
- Endpoints:
  - `GET https://power.larc.nasa.gov/api/temporal/hourly/point` (série **horária**, UTC)
  - `GET https://power.larc.nasa.gov/api/temporal/daily/point` (série **diária**, LST)
- Dados de reanálise e satélite (MERRA-2 / CERES) interpolados para qualquer coordenada. A resolução nativa é de cerca de 0,5° × 0,625°, então pontos próximos entre si podem devolver valores muito parecidos.

### 7.2 Parâmetros consultados

| Parâmetro | Unidade | Relevância |
|---|---|---|
| `ALLSKY_SFC_SW_DWN` | Wh/m² (na hora) | Irradiância global horizontal com nuvens: principal variável explicativa da geração **solar** |
| `WS10M` | m/s | Vento a 10 m (altura meteorológica padrão) |
| `WS50M` | m/s | Vento a 50 m, mais próximo da altura de cubo de aerogeradores. É a variável mais útil para a geração **eólica** |
| `T2M` | °C | Temperatura a 2 m. Afeta a eficiência dos painéis (perda de rendimento com calor) e a densidade do ar para as turbinas |

Parâmetros fixos da requisição:

- `community=RE` (*Renewable Energy*): define unidades e convenções voltadas a energia renovável.
- `time-standard=UTC` (série horária): pedido explicitamente. O padrão da API é **LST** (*Local Solar Time*), um horário solar que depende da longitude e **não** coincide com o horário de Brasília. Com UTC, a conversão para o fuso do ONS é determinística (−3 h).
- `time-standard=LST` (série diária): o dia é delimitado pela hora solar local. Nas longitudes do Brasil (−35° a −51°) ela fica a menos de 1 h do horário de Brasília, então o "dia LST" corresponde ao dia local. Foi escolhido porque, **com UTC, a API não publica a irradiância diária recente** (testado em 18/09/2026).

**Série diária** (`dados_nasa_diario_bruto.parquet`): parâmetros `ALLSKY_SFC_SW_DWN` (em **kWh/m²/dia**), `WS10M`, `WS50M`, `T2M`, `T2M_MAX` e `T2M_MIN`, com a coluna de tempo `data_lst` (`AAAAMMDD`). Ela foi adicionada depois de descobrir que a **irradiância horária é publicada com ~3 meses de atraso** (ver [§7.8](#78-observações-de-qualidade-para-o-etl)). É a fonte da `fato_clima` (grão usina × dia) no ETL.
- `format=JSON`: mais fácil de parsear de forma robusta do que o CSV da NASA, que vem com um cabeçalho textual de tamanho variável antes dos dados.

### 7.3 Escolha dos locais — `locais.csv`

A NASA POWER trabalha por **coordenada**, mas os dados do ONS não trazem coordenadas. O `locais.csv` é a ponte, e ele é **gerado** por `gerar_locais.py` a partir das coordenadas reais dos empreendimentos do SIGA — não mais escrito à mão.

**Como era:** 10 pontos digitados manualmente, com a coordenada aproximada da *sede do município* (2 casas decimais), escolhidos por conhecimento de mercado ("Caetité é polo eólico"). Funcionava, mas era um número escolhido por uma pessoa, sem critério verificável e sem relação com onde os parques realmente estão — a sede de Caetité fica a dezenas de quilômetros dos aerogeradores.

**Como é:** para cada par (fonte, UF) com capacidade relevante, o script pega a **maior usina em operação** e usa a coordenada dela, com 5 casas decimais, como ponto de clima daquela região. A escolha é reproduzível a partir do bruto da ANEEL e vem com procedência: o CSV guarda `usina_referencia`, `potencia_mw` e `ceg` do empreendimento que originou cada ponto.

Filtros, na ordem (todos com flag de CLI):

| Filtro | Padrão | Por quê |
|---|---|---|
| fase = "Operação", fonte UFV/EOL | — | usina em construção não tem geração para cruzar |
| coordenada dentro do Brasil | `LIMITES_BRASIL` | descarta coordenada zerada ou com sinal trocado |
| potência da usina | `--mw-minimo 1` | o SIGA tem milhares de registros minúsculos (o Pará aparece com 13.105 UFV somando 17 MW) |
| coordenada coerente com a UF | `--max-km-uf 700` | ver o caso abaixo |
| capacidade do par (fonte, UF) | `--mw-minimo-grupo 50` | não gastar chamada de API em UF sem geração relevante |
| distância entre pontos | `--min-km 50` | dois pontos vizinhos pedem a mesma série climática duas vezes |
| pontos por par (fonte, UF) | `--por-grupo 1` | ver o ganho marginal abaixo |
| desempate | potência, depois CEG | dezenas de usinas têm potência idêntica (Castilho 1 a 5, todas 49,999 MW); sem o CEG a escolha oscilaria entre execuções |

**Registro com UF e coordenada de estados diferentes.** As cinco unidades "Fótons de São George" estão cadastradas em **MS** com coordenada no **Piauí** — 2.094 km da mediana das usinas de MS. Como são as maiores "usinas de MS" do cadastro, elas seriam escolhidas como ponto de clima do Mato Grosso do Sul, e todas as usinas do estado passariam a receber o clima do sertão nordestino. O filtro `--max-km-uf` compara cada usina com a **mediana** das coordenadas da própria UF (robusta a alguns pontos errados) e descarta as incoerentes — 9 usinas no retrato de 18/09/2026, todas com troca real de estado. O limite é folgado de propósito: nenhum estado brasileiro tem 700 km da mediana à borda, então o filtro pega troca de UF, não usina legitimamente distante (as eólicas do litoral do Piauí, a 578 km da mediana do estado, continuam valendo).

**Resultado — 19 pontos** (retrato de 18/09/2026), contra os 10 manuais:

| | Manual (10 pontos) | Gerado da ANEEL (19 pontos) |
|---|---|---|
| Distância usina → ponto mais próximo (mediana) | 178 km | **93 km** |
| Idem, p90 | 411 km | **270 km** |
| Capacidade instalada a ≤ 300 km de um ponto | 77,3% | **96,5%** |

Os 300 km são o limiar `DISTANCIA_MAXIMA_CLIMA_KM` do ETL, acima do qual o vínculo usina→clima é marcado como `mais_proximo_distante`. Ou seja: a fração de capacidade com clima de qualidade aceitável saiu de três quartos para quase tudo.

Com `--por-grupo 2` seriam 37 pontos, mediana de 55 km e 97,8% de cobertura: o dobro de chamadas de API por 1,3 ponto percentual. Por isso o padrão é 1 por grupo.

O que sobra fora de alcance são usinas isoladas de 1 a 5 MW em RR e RO (76,7 MW somados, a 1.400–2.200 km do ponto mais próximo). Esses estados não alcançam os 50 MW de grupo e não ganham ponto próprio — em troca, nenhuma requisição é gasta por 1 MW de capacidade. O ETL marca essas usinas como `mais_proximo_distante` e a API devolve a distância em cada resposta de clima.

Colunas do arquivo: `local, municipio, id_estado, id_subsistema, fonte_predominante, latitude, longitude, usina_referencia, potencia_mw, ceg`. A ingestão copia apenas as seis primeiras para a saída (`COLUNAS_LOCAL`); as três últimas existem para auditoria do CSV.

- `local` é o slug `municipio_uf` (`uibai_ba`), com sufixo da fonte quando dois pontos caem no mesmo município.
- Editar o CSV à mão continua funcionando — o código não tem nenhum local fixo —, mas a próxima execução de `gerar_locais` sobrescreve.
- **Trocar o `locais.csv` invalida o clima já ingerido:** os pontos antigos não existem na nova coleta. Rode de novo a ingestão da NASA, e depois o ETL, para o `fato_clima` refletir os pontos novos.

### 7.4 Fluxo de execução

```
main()
 ├─ load_dotenv(.env)
 ├─ resolve período (ver §3.3). "fim" = último dia do mês de --fim, limitado a hoje
 └─ baixar(inicio, fim)
     ├─ lê locais.csv
     ├─ para cada local:
     │    └─ _baixar_local()
     │         ├─ para cada janela em _janelas_anuais(inicio, fim):
     │         │    ├─ get_com_retry(API_URL, params=...)
     │         │    ├─ JSON → properties.parameter → DataFrame
     │         │    └─ sleep(1 s)
     │         └─ concat janelas + colunas de identificação do local
     └─ concat locais → gravar_parquet → dados/bruto/dados_nasa_bruto.parquet
         (o mesmo laço roda uma segunda vez para a série diária
          → dados/bruto/dados_nasa_diario_bruto.parquet)
```

### 7.5 Janelas anuais: `_janelas_anuais`

O período é quebrado em janelas de **no máximo um ano civil**: 15/11/2025 → 31/01/2026 vira `[(15/11/2025, 31/12/2025), (01/01/2026, 31/01/2026)]`.

- Em teste, a API aceitou 20 meses numa única chamada (cerca de 1,1 MB de resposta em 2,2 s), mas não há garantia documentada de que continue aceitando períodos longos para dados horários. Quebrar por ano mantém cada resposta pequena e previsível e custa só uma chamada extra por virada de ano.
- A virada de ano foi testada explicitamente (nov/2025 → jan/2026): 2.208 horas por local, contínuas de `2025110100` a `2026013123`, sem buracos nem duplicatas.

### 7.6 Transformação JSON → tabela (a única "transformação")

A API devolve um objeto **por parâmetro**, indexado pelo timestamp:

```json
"parameter": {
  "T2M":   {"2026091000": 21.3, "2026091001": 20.8, ...},
  "WS50M": {"2026091000": 6.9,  "2026091001": 7.3,  ...}
}
```

`pd.DataFrame(serie)` transforma isso diretamente numa tabela **uma linha por hora, uma coluna por parâmetro**, alinhando os parâmetros pelo timestamp. `rename_axis("data_hora_utc").reset_index()` transforma o índice em coluna. É uma mudança de **forma** (pivot), e não de conteúdo: nenhum valor é alterado.

Em seguida, `df.assign(**{coluna: local[coluna] ...})` acrescenta a identificação do local em todas as linhas, e a seleção final fixa a ordem das colunas: identificação → hora → parâmetros.

### 7.7 Esquema de `dados_nasa_bruto.parquet`

| Coluna | Exemplo | Descrição |
|---|---|---|
| `local` | `uibai_ba` | Identificador do ponto (de `locais.csv`) |
| `municipio` | `Uibaí` | |
| `id_estado` | `BA` | Mesma codificação de `id_estado` do ONS |
| `id_subsistema` | `NE` | Mesma codificação de `id_subsistema` do ONS |
| `latitude`, `longitude` | `-11.43252`, `-42.13743` | Coordenada consultada (a da usina de referência) |
| `data_hora_utc` | `2026070100` | `AAAAMMDDHH` em **UTC**, string crua da API |
| `ALLSKY_SFC_SW_DWN` | `0.0` | Wh/m² |
| `WS10M` | `3.82` | m/s |
| `WS50M` | `6.96` | m/s |
| `T2M` | `26.04` | °C |

### 7.8 Observações de qualidade (para o ETL)

- **`-999` = valor ausente** (`fill_value` informado no cabeçalho da resposta). O ETL precisa trocar `-999` por nulo **antes** de qualquer média, ou os agregados ficam destruídos.
- **Latência, que é diferente por parâmetro e por série:**

  | Série | Parâmetro | Último dado válido (consulta de 18/09/2026) | Atraso |
  |---|---|---|---|
  | horária | vento, temperatura | 16/09 23h UTC | ~2 dias |
  | horária | **irradiância** | **30/06/2026 23h UTC** | **~3 meses** |
  | diária (LST) | vento, temperatura | ~16/09 | ~2 dias |
  | diária (LST) | irradiância | ~06–13/09 (varia por local) | ~1 semana |

  > **Correção:** uma versão anterior deste documento dizia que só as últimas 48 horas vinham `-999`. Isso vale para vento e temperatura. A checagem daquela época olhou só `T2M`, e a irradiância horária do período jul–set/2026 inteiro vem `-999`. Por isso a série diária foi adicionada.
- **Irradiância zero à noite** é valor real, não ausência.
- **Fuso:** para alinhar com o ONS, `hora_brasilia = data_hora_utc − 3 h`.

---

## 8. Fonte 3 — ANEEL SIGA: cadastro de usinas

Arquivo: [`ingestao/aneel/ingestao_aneel.py`](../../../ingestao/aneel/ingestao_aneel.py)

### 8.1 A fonte

- Portal: <https://dadosabertos.aneel.gov.br> (CKAN)
- Dataset: **"SIGA — Sistema de Informações de Geração da ANEEL"** (`siga-sistema-de-informacoes-de-geracao-da-aneel`)
- Recursos do dataset:

| Recurso | Formato | Datastore (API) | Uso |
|---|---|---|---|
| Dicionário de dados | PDF | — | referência dos campos |
| `siga-empreendimentos-geracao.csv` | CSV | sim | retrato mensal (data de geração do conjunto: 01/09/2026) |
| **`siga-empreendimentos-geracao-diario.csv`** | CSV | **sim** | **usado**: retrato diário (17/09/2026) |
| versões `.xml` | XML | não | — |

**Por que o recurso diário:** tem as mesmas colunas do mensal e está mais atualizado. O `resource_id` fica fixo no código (`RESOURCE_ID = "2f65a1b0-19b8-4360-8238-b34ab4693d55"`).

**Por que a API `datastore_search` e não o download do CSV inteiro:** o datastore permite **filtrar no servidor**. Só as cerca de 20 mil linhas solares e eólicas trafegam, e não as 25 mil de todo o parque (hidrelétricas, térmicas etc.). É também a "API pública" que o portal oferece.

### 8.2 A chamada

```
GET https://dadosabertos.aneel.gov.br/api/3/action/datastore_search
    ?resource_id=2f65a1b0-...
    &filters={"SigTipoGeracao": ["UFV", "EOL"]}
    &limit=5000
    &offset=0
    &sort=_id asc
```

| Parâmetro | Detalhe |
|---|---|
| `filters` | JSON serializado com `json.dumps`. No CKAN, uma **lista** como valor significa `IN`: `SigTipoGeracao IN ('UFV','EOL')`. UFV = Usina Fotovoltaica, EOL = Central Geradora Eólica |
| `limit` | Tamanho da página (5.000). É um valor seguro abaixo do teto do servidor CKAN |
| `offset` | Deslocamento da paginação |
| `sort=_id asc` | **Essencial.** Paginar por offset sem ordenação determinística permite que o banco devolva linhas em ordens diferentes entre páginas, o que pode repetir umas e pular outras silenciosamente. `_id` é a chave interna do datastore, única e estável |

A resposta traz `success`, `result.total` (total de linhas que casam com o filtro), `result.fields` (esquema) e `result.records` (a página).

### 8.3 Lógica de paginação e verificações de consistência

```python
while total is None or offset < total:
    resultado = _pagina(sessao, tipos, offset)
    if total is None:                         # 1ª página: fixa total e esquema
        total = resultado["total"]
        colunas = [campo["id"] for campo in resultado["fields"]]
    elif resultado["total"] != total:         # o recurso mudou no meio?
        raise RuntimeError("Total mudou durante a paginação ...")
    lote = resultado["records"]
    if not lote:                              # proteção contra loop infinito
        break
    registros.extend(lote)
    offset += len(lote)
    time.sleep(0.5)

bruto = pd.DataFrame(registros, columns=colunas).drop_duplicates(subset="_id")
if len(bruto) != total:
    raise RuntimeError(f"Esperados {total} registros, recebidos {len(bruto)}.")
```

A ingestão tem quatro camadas de proteção, e cada uma responde a um risco concreto:

1. **`success` falso na resposta** → `RuntimeError` com a mensagem de erro da API (em `_pagina`). O CKAN pode responder HTTP 200 com `success: false`.
2. **Total mudando entre páginas** → aborta. O recurso é republicado diariamente. Se isso acontecer no meio da paginação, as páginas viriam de versões diferentes do cadastro e o arquivo seria uma mistura inconsistente. É melhor falhar e pedir para rodar de novo, já que a execução completa leva cerca de 25 s.
3. **Página vazia antes do total** → `break`, que evita loop infinito se a API passar a devolver menos do que anunciou. Nesse caso a verificação 4 falha e reporta o problema.
4. **Contagem final ≠ total anunciado**, depois de deduplicar por `_id` → aborta **antes** de escrever. Isso garante que o arquivo em disco é sempre um retrato completo.

A ordem das colunas vem de `result.fields`, e não das chaves do primeiro registro. Assim o esquema do arquivo é exatamente o do datastore, mesmo que algum registro venha com chaves faltando.

**CLI:** `--tipos` aceita qualquer sigla de `SigTipoGeracao` (por exemplo `UHE`, `PCH`, `UTE`, `CGH`, `UTN`) e converte para maiúsculas. O padrão é `UFV EOL`, que é o escopo do projeto.

### 8.4 Esquema de `dados_aneel_bruto.parquet`

| Coluna | Exemplo | Descrição |
|---|---|---|
| `_id` | `1` | Chave interna do datastore (adicionada pelo CKAN) |
| `DatGeracaoConjuntoDados` | `2026-09-17` | Data do retrato do cadastro |
| `NomEmpreendimento` | | Nome da usina |
| `IdeNucleoCEG` | `000008` | Núcleo numérico do CEG |
| `CodCEG` | `EOL.CV.CE.002801-0.1` | Código Único de Empreendimento de Geração |
| `SigUFPrincipal` | `CE` | UF principal |
| `SigTipoGeracao` | `UFV` / `EOL` | Tipo de geração |
| `DscFaseUsina` | `Operação` | Operação, Construção, Construção não iniciada |
| `DscOrigemCombustivel` | `Solar` / `Eólica` | |
| `DscFonteCombustivel`, `NomFonteCombustivel` | | Detalhamento da fonte |
| `DscTipoOutorga` | `Autorização` | Autorização, Registro… |
| **`DatEntradaOperacao`** | `2022-01-31` | **Data de entrada em operação** (ver sentinelas abaixo) |
| **`MdaPotenciaOutorgadaKw`** | `1400,00` | **Potência outorgada (kW)**, formato BR |
| **`MdaPotenciaFiscalizadaKw`** | `1400` | Potência fiscalizada (kW) |
| `MdaGarantiaFisicaKw` | `,00` | Garantia física (kW), formato BR |
| `IdcGeracaoQualificada` | `Não` | |
| `NumCoordNEmpreendimento`, `NumCoordEEmpreendimento` | `-20,12479858` | Latitude e longitude, **vírgula decimal** |
| `DatInicioVigencia`, `DatFimVigencia` | | Vigência da outorga |
| `DscPropriRegimePariticipacao` | | Proprietários e participação (nome + CNPJ/regime). Contém dado pessoal, ver [§10](#10-compliance-segurança-e-privacidade) |
| `DscSubBacia` | | Sub-bacia (relevante para hídricas) |
| **`DscMuninicpios`** | `Nova Lima - MG` | **Município(s)**. O nome da coluna tem um erro de digitação na própria fonte, preservado |

### 8.5 Retratos datados — `historico_aneel/`

O recurso da ANEEL é **republicado diariamente e sobrescrito**: não existe versão anterior no portal. Como a ingestão também sobrescrevia o `dados_aneel_bruto.parquet`, cada coleta apagava a anterior e o histórico se perdia — inclusive a informação mais valiosa que esta fonte tem para o projeto: **quando uma usina muda de fase**.

Isso importa porque a fase (`Construção não iniciada` → `Construção` → `Operação`) é um **evento observado com data**, diferente do `DatEntradaOperacao`, que é um campo cadastral podendo ser preenchido retroativamente e que aqui aparece com 2.080 datas-sentinela e 43 datas implausíveis ([§8.7](#87-observações-de-qualidade-para-o-etl)). Uma série de retratos datados permitiria trocar parte dos **dados simulados** da análise de sobrevivência por eventos reais de transição.

Agora cada execução grava dois arquivos:

| Arquivo | Papel |
|---|---|
| `dados/bruto/dados_aneel_bruto.parquet` | retrato corrente, o que o ETL lê (nada mudou para ele) |
| `dados/bruto/historico_aneel/dados_aneel_bruto_AAAA-MM-DD.parquet` | retrato preservado daquela publicação |

**A data vem do dado, não do relógio.** O nome do arquivo usa `DatGeracaoConjuntoDados`, a data em que a ANEEL gerou o conjunto. Usar a data do download criaria dois arquivos idênticos com nomes diferentes só por baixar o mesmo retrato em dois dias. Por consequência, `arquivar` é **idempotente**: se o arquivo daquela data já existe, não é regravado.

**Comparar dois retratos** — `mudancas_de_fase(anterior, atual)` devolve as usinas que mudaram de fase e as que entraram no cadastro, casando por `CodCEG` (único e nunca nulo nos 20.511 registros):

```
CodCEG                NomEmpreendimento   SigUFPrincipal  fase_antes        fase_depois
EOL.CV.RN.032280-6.1  Paraíso Farol II    RN              Construção        Operação
UFV.RS.BA.999999-9.1  Usina Nova Teste    CE              (não cadastrada)  Construção
```

(saída do teste da função, com um retrato sintético — hoje só há um retrato real arquivado, o de 18/09/2026, e a comparação exige dois.)

**Custo:** ~0,8 MB por retrato, e só para UFV + EOL. Uma coleta diária por um ano dá ~290 MB, o que ainda cabe sem política de retenção. Se `--tipos` incluir hídricas e térmicas, vale reavaliar. A pasta fica dentro de `dados/bruto/`, então já está fora do versionamento e coberta pelas mesmas regras de LGPD da [§10](#10-compliance-segurança-e-privacidade) — o retrato bruto inclui `DscPropriRegimePariticipacao`, que é descartada no ETL.

### 8.6 Perfil da última execução

| Tipo | Operação | Construção | Construção não iniciada | Total | Potência outorgada |
|---|---|---|---|---|---|
| UFV (solar) | 17.302 | 32 | 1.628 | 18.962 | ~96,0 GW |
| EOL (eólica) | 1.138 | 61 | 350 | 1.549 | ~51,4 GW |

As usinas não foram filtradas por fase, porque a camada bruta mantém tudo. Para potência instalada efetiva, o ETL deve filtrar `DscFaseUsina = 'Operação'`. Para análise de sobrevivência e pipeline de projetos, as outras fases são justamente o dado de interesse.

### 8.7 Observações de qualidade (para o ETL)

Verificadas nos dados reais da última execução:

1. **Números em formato brasileiro:** `"1400,00"`, `",00"` (= 0), `"-20,12479858"`. Para converter: remover `.` de milhar e trocar `,` por `.`. `",00"` precisa virar `0.00`.
2. **Data-sentinela `1900-01-03`** em `DatEntradaOperacao`: aparece em **2.080** registros, sendo 2.070 de usinas que ainda não operam (Construção / Construção não iniciada) e **10 que constam como "Operação"**. Deve virar nulo no ETL.
3. **Datas implausíveis para solar e eólica:** 43 usinas em operação com `1961-02-21` (5), `1971-02-21` (15) e `1981-02-21` (23). Não havia parque eólico ou solar comercial no Brasil nessas datas. O padrão repetido (sempre 21/02, com saltos exatos de 10 anos) indica erro de digitação ou de migração na fonte. Recomendação: marcar como suspeitas (por exemplo, datas anteriores a 1990 para UFV/EOL) e tratar como nulas na análise de sobrevivência.
4. **Muitas UFVs minúsculas:** **15.992** registros têm potência ≤ 5 kW, e muitos têm nome de pessoa física e 1 kW (concentrados em municípios do PA). São sistemas de pequeno porte registrados individualmente. Apenas **3.927** têm potência ≥ 1 MW. O ETL deve decidir um corte (por exemplo, ≥ 1 MW para "usina") conforme a análise.
5. **Codificação:** os textos ficam em UTF-8 no Parquet e os acentos de `Solar`/`Eólica`/`Operação` estão íntegros. Durante a exploração, um registro **hídrico** do datastore veio com mojibake (`H\xadrica`) na origem. Não afeta UFV/EOL, mas convém lembrar disso se `--tipos` incluir hídricas.

---

## 9. Como as três fontes se cruzam

### 9.1 Chaves disponíveis

| Ligação | Chave | Qualidade |
|---|---|---|
| ONS ↔ NASA | `id_estado` / `id_subsistema` + tempo | Boa, desde que o fuso seja alinhado (ONS em horário de Brasília, NASA em UTC: `hora_brasilia = data_hora_utc − 3 h`) |
| ONS ↔ ANEEL | `ceg` (ONS) ↔ `CodCEG` (ANEEL) | **Precisa, porém rara** (ver abaixo) |
| ONS ↔ ANEEL | `id_estado` ↔ `SigUFPrincipal` + tipo de fonte | Agregada, mas cobre tudo |
| ANEEL ↔ NASA | coordenadas da ANEEL → `locais.csv` (por `gerar_locais.py`) | **Em uso:** os pontos de clima são coordenadas de usinas reais ([§7.3](#73-escolha-dos-locais--locaiscsv)) |

### 9.2 O formato do CEG diverge entre as fontes

- ONS: `UHE.PH.AM.000190-2.01` (sufixo de versão com **2 dígitos**)
- ANEEL: `PCH.PH.MG.000008-6.1` (sufixo com **1 dígito**)

A comparação direta da string falha. O cruzamento correto usa o CEG **sem o sufixo de versão** (tudo antes do último `.`) ou o **núcleo numérico** (`\.(\d{6})-` no ONS ↔ `IdeNucleoCEG` na ANEEL).

### 9.3 Cobertura medida

Nas **309** usinas solares e eólicas distintas do ONS no período baixado:

- apenas **19** têm CEG individual, e **19 de 19 casaram** com a ANEEL (tanto por núcleo quanto por CEG sem sufixo);
- as outras **290** não têm CEG (`"-"`), porque são registradas no ONS como **Conjunto de Usinas** (227), **Pequenas Usinas (MMGD)** (38) e **Pequenas Usinas (Tipo III)** (25).

**Conclusão para o ETL:** para solar e eólica, o cruzamento por CEG cobre só uma fração pequena da geração. O caminho principal para calcular fator de capacidade é **agregar por UF × fonte × tempo**, com a geração do ONS no numerador e a potência em operação da ANEEL no denominador. O cruzamento por CEG serve para o detalhe por usina onde ele existir.

---

## 10. Compliance, segurança e privacidade

### 10.1 `.gitignore`

```gitignore
# Segredos / variáveis de ambiente
.env
.env.*
!.env.example

# Dados brutos (reconstruíveis a partir das fontes públicas)
dados/bruto/

# Python
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.ruff_cache/
```

- **`.env` e variantes** (`.env.local`, `.env.prod`…) nunca são versionados. A exceção `!.env.example` permite versionar um modelo sem valores sensíveis, caso se queira documentar as variáveis no repositório.
- **`dados/bruto/`** não é versionado porque:
  1. **Tamanho:** mesmo em Parquet, a série do ONS cresce ~3,5 MB por mês e o histórico completo desde 2022 passa de 150 MB. Dado versionado no Git inflaria o repositório para sempre.
  2. **Reprodutibilidade:** os dados são reconstruíveis a qualquer momento pelos scripts. O que se versiona é o *código que produz o dado*, não o dado.
  3. **Privacidade:** ver 10.3.
- Os ambientes virtuais e caches do Python também ficam fora do repositório.

### 10.2 Credenciais

Nenhuma das três APIs exige chave ou token. **Não há segredo algum no código.** O `.env` guarda hoje apenas configuração de período. O `.gitignore` já protege qualquer credencial que venha a ser adicionada.

### 10.3 Dados pessoais (LGPD)

O cadastro da ANEEL é **público por lei**, mas contém dados de **pessoas físicas**: o nome do titular aparece em `NomEmpreendimento` para micro-usinas registradas por pessoa física e em `DscPropriRegimePariticipacao`, que também traz CNPJs. Mesmo sendo dado público, a boa prática sob a LGPD (minimização e finalidade) é:

- não redistribuir o bruto (por isso ele não é versionado);
- **não levar esses campos para a camada consumida pela API pública.** O ETL deve descartar ou anonimizar `NomEmpreendimento` de registros de pessoa física e a coluna `DscPropriRegimePariticipacao`, a menos que haja uma finalidade clara para mantê-los.

### 10.4 Uso responsável das APIs

- User-Agent identificável ([§5.2](#52-nova_sessao)).
- Pausas de cortesia entre chamadas e *backoff* exponencial em 429/5xx, que reduzem a carga sobre o serviço quando ele já está degradado.
- Volume por execução pequeno para NASA (38 chamadas: 19 pontos x 2 séries, com 1 s de pausa entre elas) e ANEEL (5 chamadas). No ONS são 3 downloads de arquivos estáticos em S3.

---

## 11. Testes automatizados

```bash
pytest ingestao/tests -q        # 93 testes, ~5 s
pytest -q                       # com os 24 do backend: 117
```

**Nenhum teste toca a rede.** Todas as respostas HTTP são simuladas com [`responses`](https://github.com/getsentry/responses), que intercepta o `requests` na camada do adaptador: a sessão, os cabeçalhos, os parâmetros e o código de status são os reais, só o socket não existe. Isso permite testar coisas que a rede não oferece sob demanda — um `429`, um mês que ainda não foi publicado, o recurso da ANEEL mudando no meio da paginação.

O `conftest.py` anula `time.sleep` para toda a suíte. Sem isso, um teste de retry dormiria os mesmos 2+4+8+16 s da execução real; a fixture `esperas` captura os valores pedidos, o que permite **verificar o backoff sem esperá-lo**.

| Arquivo | Cobre | Testes |
|---|---|---|
| `test_http.py` | política de retry: `4xx` levanta na primeira tentativa, `429`/`5xx`/rede repetem até 5, `404` volta sem exceção, backoff exponencial com jitter, User-Agent | 14 |
| `test_ons.py` | `_meses`, `PADRAO_MENSAL`, `_urls_disponiveis`, `baixar` | 24 |
| `test_nasa_power.py` | `_janelas_anuais`, `_baixar_local` | 12 |
| `test_aneel.py` | paginação e retratos datados | 14 |
| `test_gerar_locais.py` | seleção dos pontos de clima a partir do cadastro | 18 |
| `test_orquestrador.py` | ordem das etapas, repasse de período e comportamento em falha | 11 |

### 11.1 O que cada grupo protege

**`_meses`** ([§6.4](#64-geração-dos-meses-_meses)) — a virada de ano é o caso que a aritmética manual costuma errar: `(2025-11 → 2026-02)` tem que dar quatro meses, e `fim` antes de `inicio` tem que dar lista vazia, não um laço infinito.

**`PADRAO_MENSAL`** ([§6.3](#63-descoberta-de-arquivos-_urls_disponiveis)) — é o teste de regressão do primeiro bug real do projeto: a versão inicial usava `rsplit("_", 2)` e quebrava com `ValueError` nos arquivos **anuais** do ONS, tentando converter `"USINA-2"` em ano. Há um caso explícito para `GERACAO_USINA-2_2015.parquet`, mais CSV, XLSX e `.parquet.tmp`, que também não podem casar.

**`_janelas_anuais`** ([§7.2](#72-a-chamada)) — além dos casos diretos, uma propriedade: para qualquer período, as janelas emendam exatamente (o fim de uma é véspera do início da seguinte), nenhuma cruza o ano e as pontas batem com o período pedido. É o tipo de invariante que um exemplo isolado não garante.

**Paginação da ANEEL** ([§8.3](#83-lógica-de-paginação-e-verificações-de-consistência)) — com `TAMANHO_PAGINA` reduzido para 2 via `monkeypatch`, dá para paginar de verdade sem simular 5.000 linhas. Os testes verificam o avanço do `offset`, o `sort=_id asc` (sem ele a paginação por offset repete ou pula linhas), o filtro `SigTipoGeracao` e as três formas de abortar: total mudando no meio da coleta, contagem final menor que a prometida e `success: false`. Em todos os casos de aborto, verifica-se também que **nada foi gravado** — um retrato pela metade não pode virar camada bruta.

**`baixar` do ONS** — que cada mês vira um arquivo, que um mês `404` é pulado sem derrubar os outros, que a URL do CKAN tem precedência sobre o padrão do S3, que `arquivo_origem` guarda o nome **na fonte** e que reingerir um mês não apaga os demais (a garantia da ingestão incremental).

**Retratos da ANEEL** ([§8.5](#85-retratos-datados--historico_aneel)) — que a data vem do dado e não do relógio, que arquivar duas vezes não regrava (comparando o `mtime` em nanossegundos), que `retratos()` ignora arquivos estranhos na pasta e que `mudancas_de_fase` detecta tanto a transição `Construção → Operação` quanto a usina que entrou no cadastro.

**Orquestrador** ([§3.2.1](#321-o-orquestrador--ingestaomainpy)) — `subprocess.run` é substituído por um dublê que registra o comando pedido, então os testes verificam a ordem, o repasse de `--inicio`/`--fim` só para quem aceita período, a parada na primeira falha (com as seguintes marcadas `pulada`) e o `--seguir`. Nenhum processo é criado.

**`gerar_locais`** ([§7.3](#73-escolha-dos-locais--locaiscsv)) — o formato numérico brasileiro (`"11.832,10"` → `11.8321` MW), os descartes (potência baixa, fase, coordenada fora do Brasil, outra fonte), o caso real das "Fótons de São George" (UF de MS com coordenada no Piauí) e o **determinismo** com potências empatadas: o teste gera o CSV cinco vezes e exige o mesmo ponto.

### 11.2 O que não é testado

- **O contrato das APIs públicas.** Se a ANEEL trocar o nome de uma coluna ou a NASA mudar o formato da resposta, a suíte continua verde — ela testa o nosso código contra o formato **conhecido**. Detectar isso exigiria um teste de integração com rede, rodado em separado e tolerante a indisponibilidade.
- **A ingestão ponta a ponta com dados reais**, pelo mesmo motivo.
- **Cobertura medida.** Não há `pytest-cov` configurado; a escolha dos casos é por risco, não por percentual.

---

## 12. Limitações conhecidas e próximos passos

| # | Limitação | Impacto | Sugestão |
|---|---|---|---|
| 1 | ~~Meses do ONS concatenados em memória~~ **Resolvido:** um Parquet por mês em `dados/bruto/dados_ons_bruto/`, lido por glob ([§6.2.1](#621-um-parquet-por-mês)) | — | — |
| 2 | ~~Bruto em CSV~~ **Resolvido:** migrado para Parquet ([§4.5](#45-formato-de-saída-parquet)) | — | — |
| 3 | ~~`get_com_retry` repete erros `4xx` não-429~~ **Resolvido:** `4xx` (exceto 429) levanta na primeira tentativa ([§5.1](#51-get_com_retrysessao-url-kwargs)) | — | — |
| 4 | ~~Coordenadas de `locais.csv` manuais e aproximadas~~ **Resolvido:** geradas da ANEEL por `gerar_locais.py`; a capacidade com clima a ≤ 300 km subiu de 77,3% para 96,5% ([§7.3](#73-escolha-dos-locais--locaiscsv)) | Resta: um ponto por (fonte, UF) é regional, não por usina | Subir `--por-grupo`, ou consultar a NASA por usina nas maiores (custo linear em chamadas) |
| 5 | ~~NASA: vento/temperatura atrasam ~2 dias; irradiância horária ~3 meses, diária ~1 semana~~ | Janela recente sem parte das variáveis **Tratado:** o valor `-999` vira nulo com flag `faltante`, a coluna `medidas_faltantes` diz **quais** variáveis faltaram (as latências são diferentes por variável) e a `fato_clima` usa a série diária. A API expõe as duas colunas e o frontend escreve a ressalva na tela. Para análises horárias de irradiância, usar períodos com mais de 3 meses |
| 6 | ~~ANEEL é um retrato único, sobrescrito~~ **Resolvido:** cada execução arquiva `historico_aneel/dados_aneel_bruto_AAAA-MM-DD.parquet`, com data lida de `DatGeracaoConjuntoDados`, e `--mudancas-de-fase` compara dois retratos ([§8.5](#85-retratos-datados--historico_aneel)) | Resta: só há um retrato arquivado, então ainda não há série histórica para a análise de sobrevivência usar | Rodar a ingestão periodicamente (o valor aparece com o tempo) |
| 7 | ~~Sem testes automatizados~~ **Resolvido:** 93 testes em `ingestao/tests` com `pytest` + `responses`, sem rede ([§11](#11-testes-automatizados)) | Resta: mudança de contrato nas APIs públicas não é detectada | Um teste de integração com rede, rodado à parte e tolerante a indisponibilidade |
| 8 | ~~Três comandos separados~~ **Resolvido:** `python -m ingestao` roda as etapas na ordem canônica, com `--fontes`, `--listar`, `--seguir` e resumo final ([§3.2.1](#321-o-orquestrador--ingestaomainpy)) | — | — |

---

*Documento gerado em 17/09/2026 a partir do código e das execuções reais da fase de ingestão.*
