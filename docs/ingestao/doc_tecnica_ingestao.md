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
11. [Limitações conhecidas e próximos passos](#11-limitações-conhecidas-e-próximos-passos)

---

## 1. Visão geral

| Fonte | O que traz | Grão | Forma de acesso | Saída |
|---|---|---|---|---|
| **ONS** — Operador Nacional do Sistema Elétrico | Geração verificada (MWmed) por usina/conjunto, com subsistema, estado e tipo de fonte (hidráulica, térmica, eólica, fotovoltaica, nuclear) | usina × hora | API CKAN do portal (descoberta) + Parquets mensais no S3 | `dados/bruto/dados_ons_bruto.parquet` |
| **NASA POWER** | Irradiância solar, vento a 10 m e 50 m, temperatura a 2 m | coordenada × hora (UTC) | API REST `temporal/hourly/point` | `dados/bruto/dados_nasa_bruto.parquet` |
| **ANEEL — SIGA** | Cadastro de empreendimentos solares (UFV) e eólicos (EOL): nome, CEG, potência, município, fase, data de entrada em operação, coordenadas | empreendimento | API CKAN `datastore_search` (paginada) | `dados/bruto/dados_aneel_bruto.parquet` |

As três fontes são **públicas, gratuitas e sem autenticação**, o que é consistente com a restrição de orçamento zero do projeto (§14 do system design).

O objetivo analítico é cruzar **o que foi gerado** (ONS) com **as condições climáticas que explicam a geração** (NASA) e **a capacidade instalada que poderia ter gerado** (ANEEL). Com isso ficam possíveis o fator de capacidade, a previsão de geração e a análise de sobrevivência de ativos.

### Volume da última execução (18/09/2026)

| Arquivo | Linhas | Tamanho (Parquet) | Tamanho equivalente em CSV | Período |
|---|---|---|---|---|
| `dados_ons_bruto.parquet` | 1.365.096 | ~10,4 MB | ~214 MB | 01/07/2026 00h → 17/09/2026 23h |
| `dados_nasa_bruto.parquet` | 19.200 | ~0,1 MB | ~1,5 MB | 01/07/2026 00h → 18/09/2026 23h (UTC), 10 locais |
| `dados_aneel_bruto.parquet` | 20.511 | ~0,8 MB | ~6 MB | retrato do cadastro em 18/09/2026 |

A camada bruta começou em CSV e foi migrada para Parquet. Veja a [§4.5](#45-formato-de-saída-parquet).

---

## 2. Estrutura de arquivos

```
SolarWatch-BR/
├── .env                        # configuração local (NÃO versionado)
├── .gitignore
├── requirements.txt            # requests, pandas, python-dotenv, pyarrow (+ libs do ds_toolkit)
├── ingestao/
│   ├── __init__.py
│   ├── http.py                 # GET com retry/backoff + sessão HTTP compartilhada
│   ├── armazenamento.py        # gravação atômica em Parquet (camada bruta)
│   ├── ONS/
│   │   ├── __init__.py
│   │   └── ingestao_ons.py
│   ├── nasa_power/
│   │   ├── __init__.py
│   │   ├── ingestao_nasa_power.py
│   │   └── locais.csv          # coordenadas consultadas na NASA POWER
│   └── aneel/
│       ├── __init__.py
│       └── ingestao_aneel.py
├── dados/
│   └── bruto/                  # raw layer (NÃO versionado)
│       ├── dados_ons_bruto.parquet
│       ├── dados_nasa_bruto.parquet
│       └── dados_aneel_bruto.parquet
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

```bash
python -m ingestao.ONS.ingestao_ons                     # últimos 3 meses
python -m ingestao.ONS.ingestao_ons --inicio 2026-01 --fim 2026-08

python -m ingestao.nasa_power.ingestao_nasa_power       # mesmo período do ONS
python -m ingestao.nasa_power.ingestao_nasa_power --inicio 2026-01 --fim 2026-08

python -m ingestao.aneel.ingestao_aneel                 # UFV + EOL
python -m ingestao.aneel.ingestao_aneel --tipos UFV EOL UHE PCH
```

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

Consequência prática: o DataFrame inteiro é montado em memória antes da escrita. Com o ONS lido em Parquet já tipado, isso cai para algumas centenas de MB com 3 meses. Na versão CSV, com tudo como texto, eram cerca de 1–2 GB. Veja [§11](#11-limitações-conhecidas-e-próximos-passos).

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
        if resp.status_code == 404:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            raise requests.HTTPError(...)
        resp.raise_for_status()
        return resp
    except (ConnectionError, Timeout, HTTPError):
        if tentativa == MAX_TENTATIVAS:
            raise
        espera = min(2**tentativa, 60) + random.uniform(0, 1)
        time.sleep(espera)
```

Como cada tipo de resposta é tratado:

| Situação | Comportamento | Por quê |
|---|---|---|
| `2xx` | retorna | sucesso |
| `404` | retorna **sem** retry | "não existe" não é falha transitória. O chamador decide: o ONS usa isso para pular meses ainda não publicados |
| `429` (rate limit) e `5xx` | retry | falhas transitórias típicas de servidor sobrecarregado |
| outros `4xx` (400, 403…) | `raise_for_status()` levanta `HTTPError`, que é capturado e **também gera retry** | ver observação abaixo |
| erro de conexão / timeout | retry | falha de rede transitória |

Espera entre tentativas: **exponential backoff com jitter**. As esperas são 2, 4, 8 e 16 s, mais 0–1 s aleatório, com teto de 60 s. Na pior falha persistente, o total fica em torno de 30 s antes de desistir. O *jitter* evita que várias execuções simultâneas retentem em sincronia. Nesta escala isso é mais boa prática do que necessidade, mas está pedido explicitamente em §11 do system design.

**Por que retry é seguro aqui:** todas as chamadas são `GET` de leitura, idempotentes por definição (§11 do system design).

**Observação:** um erro `4xx` genuíno (por exemplo, parâmetro inválido → `400`) também passa por 5 tentativas antes de falhar, porque `raise_for_status()` levanta o mesmo `HTTPError` capturado pelo `except`. Isso só custa cerca de 30 s num erro que é de programação, e não de rede, e o erro final chega ao usuário do mesmo jeito. Veja [§11](#11-limitações-conhecidas-e-próximos-passos).

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
     │    └─ pd.read_parquet(BytesIO(conteudo))  + coluna arquivo_origem
     ├─ nenhum mês baixado → SystemExit com mensagem
     └─ concat → gravar_parquet → dados/bruto/dados_ons_bruto.parquet
```

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

O mês corrente é publicado de forma parcial e incremental pelo ONS (na execução de 17/09, setembro vinha até o dia 16). Um mês futuro, ou recém-iniciado e ainda não publicado, retorna `404` do S3. `get_com_retry` devolve o 404 sem retry e o script **pula o mês com um aviso** em vez de falhar. Se nenhum mês do período existir, o script termina com `SystemExit("Nenhum arquivo baixado para o período informado.")`, o que evita gravar um arquivo vazio por cima de um bom.

### 6.6 Esquema de `dados_ons_bruto.parquet`

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
- Endpoint: `GET https://power.larc.nasa.gov/api/temporal/hourly/point`
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
- `time-standard=UTC`: pedido explicitamente. O padrão da API é **LST** (*Local Solar Time*), um horário solar que depende da longitude e **não** coincide com o horário de Brasília. Com UTC, a conversão para o fuso do ONS é determinística (−3 h).
- `format=JSON`: mais fácil de parsear de forma robusta do que o CSV da NASA, que vem com um cabeçalho textual de tamanho variável antes dos dados.

### 7.3 Escolha dos locais — `locais.csv`

A NASA POWER trabalha por **coordenada**, mas os dados do ONS não trazem coordenadas. Os locais foram escolhidos como **polos reais de geração**, cada um marcado com UF e subsistema, que são as chaves de ligação com o ONS:

| local | UF | Subsistema | Fonte predominante |
|---|---|---|---|
| pirapora_mg | MG | SE | solar |
| janauba_mg | MG | SE | solar |
| bom_jesus_da_lapa_ba | BA | NE | solar |
| sao_goncalo_do_gurgueia_pi | PI | NE | solar |
| ribeira_do_piaui_pi | PI | NE | solar |
| joao_camara_rn | RN | NE | eólica |
| trairi_ce | CE | NE | eólica |
| parnaiba_pi | PI | NE | eólica |
| caetite_ba | BA | NE | eólica |
| osorio_rs | RS | S | eólica |

Colunas do arquivo: `local, municipio, id_estado, id_subsistema, fonte_predominante, latitude, longitude`.

- As coordenadas são **aproximadas** (centro do município, 2 casas decimais) e foram definidas manualmente. Vale revisá-las, idealmente substituindo-as pelas coordenadas dos empreendimentos do cadastro da ANEEL ([§9](#9-como-as-três-fontes-se-cruzam)).
- Para incluir ou trocar locais, basta editar o CSV. O código não tem nenhum local fixo.
- `fonte_predominante` é só documentação e não é copiada para a saída.

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
| `local` | `pirapora_mg` | Identificador do ponto (de `locais.csv`) |
| `municipio` | `Pirapora` | |
| `id_estado` | `MG` | Mesma codificação de `id_estado` do ONS |
| `id_subsistema` | `SE` | Mesma codificação de `id_subsistema` do ONS |
| `latitude`, `longitude` | `-17.35`, `-44.94` | Coordenada consultada |
| `data_hora_utc` | `2026070100` | `AAAAMMDDHH` em **UTC**, string crua da API |
| `ALLSKY_SFC_SW_DWN` | `0.0` | Wh/m² |
| `WS10M` | `3.82` | m/s |
| `WS50M` | `6.96` | m/s |
| `T2M` | `26.04` | °C |

### 7.8 Observações de qualidade (para o ETL)

- **`-999` = valor ausente** (`fill_value` informado no cabeçalho da resposta). A NASA POWER tem uma **latência de alguns dias** nos dados horários. Na execução de 17/09/2026, as últimas **48 horas** (16 e 17/09) vieram inteiras como `-999`, em todos os locais e parâmetros. O ETL precisa trocar `-999` por nulo **antes** de qualquer média, ou os agregados ficam destruídos.
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

### 8.5 Perfil da última execução

| Tipo | Operação | Construção | Construção não iniciada | Total | Potência outorgada |
|---|---|---|---|---|---|
| UFV (solar) | 17.302 | 32 | 1.628 | 18.962 | ~96,0 GW |
| EOL (eólica) | 1.138 | 61 | 350 | 1.549 | ~51,4 GW |

As usinas não foram filtradas por fase, porque a camada bruta mantém tudo. Para potência instalada efetiva, o ETL deve filtrar `DscFaseUsina = 'Operação'`. Para análise de sobrevivência e pipeline de projetos, as outras fases são justamente o dado de interesse.

### 8.6 Observações de qualidade (para o ETL)

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
| ANEEL ↔ NASA | coordenadas da ANEEL → novas linhas em `locais.csv` | Permite clima **por usina** |

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
- Volume por execução pequeno para NASA (10 chamadas) e ANEEL (5 chamadas). No ONS são 3 downloads de arquivos estáticos em S3.

---

## 11. Limitações conhecidas e próximos passos

| # | Limitação | Impacto | Sugestão |
|---|---|---|---|
| 1 | Os meses do ONS são concatenados em memória antes da escrita | Para anos de histórico (~530 mil linhas/mês), a RAM volta a pesar | Gravar um Parquet por mês (`dados/bruto/ons/AAAA_MM.parquet`) e deixar o DuckDB ler a pasta inteira com `read_parquet('.../*.parquet')` |
| 2 | ~~Bruto em CSV~~ **Resolvido:** migrado para Parquet ([§4.5](#45-formato-de-saída-parquet)) | — | — |
| 3 | `get_com_retry` repete erros `4xx` não-429 | Erro de parâmetro leva cerca de 30 s para ser reportado | Levantar imediatamente em `4xx` (exceto 429) |
| 4 | Coordenadas de `locais.csv` definidas manualmente e aproximadas | Clima representativo do município, não da usina | Gerar `locais.csv` a partir das coordenadas da ANEEL (maiores usinas por UF) |
| 5 | NASA: últimas ~48 h vêm `-999` | Janela recente sem clima | Tratar no ETL; opcionalmente cortar o `fim` padrão para "hoje − 3 dias" |
| 6 | ANEEL é um retrato único, sobrescrito a cada execução | Perde-se o histórico de mudanças de fase (construção → operação) | Se a análise de sobrevivência precisar, arquivar retratos datados (`dados_aneel_bruto_AAAA-MM-DD.parquet`) |
| 7 | Sem testes automatizados | Regressões silenciosas (como o bug da regex do ONS) | Testes unitários de `_meses`, `_janelas_anuais`, `PADRAO_MENSAL` e paginação da ANEEL com respostas simuladas (`pytest` + `responses`), conforme §13 do system design |
| 8 | Três comandos separados | Fácil esquecer uma fonte | Um orquestrador `python -m ingestao` que rode as três fontes em sequência |

---

*Documento gerado em 17/09/2026 a partir do código e das execuções reais da fase de ingestão.*
