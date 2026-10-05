# Documentação Técnica — ETL (raw → clean → curated)

> Escopo: tudo sobre a pasta `ETL/` do SolarWatch BR. Cobre a arquitetura em camadas, como rodar, o que cada módulo faz e por quê, os algoritmos de limpeza e de vínculo entre fontes, o modelo estrela gerado, as validações e os resultados da execução real. Também registra onde o resultado diverge do system design e quais são as limitações conhecidas.
>
> Documentos relacionados:
> - [`docs/ingestao/doc_tecnica_ingestao.md`](../ingestao/doc_tecnica_ingestao.md): como os dados brutos são obtidos.
> - [`docs/ingestao/doc_tecnica_dados_simulados.md`](../ingestao/doc_tecnica_dados_simulados.md): como os eventos de manutenção são simulados.
> - `System design/system-design-solarwatch-api.md`: §4 (modelo de dados) e §9 (consistência).

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Estrutura da pasta `ETL/`](#2-estrutura-da-pasta-etl)
3. [Como executar](#3-como-executar)
4. [Configuração central (`config.py`)](#4-configuração-central-configpy)
5. [Raw layer (`raw.py`)](#5-raw-layer-rawpy)
6. [Utilitários (`utils.py`)](#6-utilitários-utilspy)
7. [Clean layer (`clean/`)](#7-clean-layer-clean)
8. [Curated layer (`curated/`)](#8-curated-layer-curated)
9. [Validação (`validacao.py`)](#9-validação-validacaopy)
10. [Uso do `ds_toolkit`](#10-uso-do-ds_toolkit)
11. [Testes automatizados](#11-testes-automatizados)
12. [Resultados da última execução](#12-resultados-da-última-execução)
13. [Diferenças em relação ao DDL do system design](#13-diferenças-em-relação-ao-ddl-do-system-design)
14. [Decisões de design e trade-offs](#14-decisões-de-design-e-trade-offs)
15. [Como consumir a camada curated](#15-como-consumir-a-camada-curated)
16. [Limitações conhecidas e próximos passos](#16-limitações-conhecidas-e-próximos-passos)

---

## 1. Visão geral

O ETL transforma o que a ingestão baixou (ONS, NASA POWER, ANEEL) e os eventos simulados em um **modelo estrela** pronto para o banco estático (DuckDB) da API.

```
            ingestão (ingestao/)                        simulação (ML/)
  ONS ─┐                                                      │
  NASA ├─► dados/bruto/dados_*_bruto.parquet                   ▼
  ANEEL┘            │                       dados/simulados/eventos_manutencao_simulados.parquet
                    ▼  RAW    (raw.py)                          │
     dados/bruto/AAAA-MM-DD/dados_*_bruto.parquet               │
                    │                                           │
                    ▼  CLEAN  (clean/*.py)                      │
     dados/limpos/clean/                                        │
       ons_geracao · nasa_clima_horario · nasa_clima_diario ·   │
       aneel_usinas · manutencao_simulada  ◄────────────────────┘
                    │
                    ▼  CURATED (curated/*.py) + VALIDAÇÃO (validacao.py)
     dados/limpos/curated/
       dim_usina · fato_geracao · fato_clima · fato_manutencao · ponte_usina_aneel
                    │
                    ▼
               DuckDB (próxima etapa)
```

| Camada | Pergunta que responde | Regra de ouro |
|---|---|---|
| **Raw** | "O que a fonte me entregou, e quando?" | Imutável. Nada é alterado, só arquivado por data de coleta |
| **Clean** | "O que esse dado significa, com tipos corretos?" | Mantém o grão e todas as linhas da fonte. Corrige tipos, fuso, nomes e duplicatas, e **marca** problemas em vez de apagá-los |
| **Curated** | "O que a API precisa servir?" | Modelo estrela, escopo solar/eólico, chaves estáveis, integridade validada |

Toda a saída é **Parquet** com compressão zstd e escrita atômica (arquivo temporário + `os.replace`), a mesma de `ingestao/armazenamento.py`. Uma execução interrompida nunca deixa um arquivo pela metade.

---

## 2. Estrutura da pasta `ETL/`

```
ETL/
├── __init__.py
├── pipeline.py        # ponto de entrada: orquestra (ingestão) → raw → clean → curated
├── config.py          # caminhos, mapeamentos (UF→subsistema, fontes) e limiares
├── utils.py           # grade temporal, interpolação de gaps, normalização de nomes, haversine, CEG
├── raw.py             # partição da camada bruta por data de coleta
├── clean/
│   ├── __init__.py
│   ├── ons.py         # geração horária (todas as fontes)
│   ├── nasa.py        # clima horário (limpar) e diário (limpar_diario)
│   ├── aneel.py       # cadastro de usinas solares/eólicas
│   └── manutencao.py  # eventos simulados (validação de invariantes)
├── curated/
│   ├── __init__.py
│   ├── dim_usina.py   # dimensão + vínculo ONS × ANEEL + ponte
│   └── fatos.py       # fato_geracao, fato_clima, fato_manutencao
├── validacao.py       # PK, FK, obrigatórias e faixas antes de gravar
└── tests/             # 91 testes das regras de limpeza, vínculo e validação (§11)
    ├── conftest.py
    ├── test_utils.py
    ├── test_dim_usina.py
    └── test_validacao.py
```

Cada módulo de clean expõe uma função `limpar(caminho) -> DataFrame`, e cada módulo de curated expõe funções puras que recebem DataFrames e devolvem DataFrames. **Nenhum módulo grava arquivo por conta própria**: quem grava é o `pipeline.py`. Isso é o que deixa as funções testáveis isoladamente — e a suíte de `tests/` ([§11](#11-testes-automatizados)) só existe nessa forma por causa disso.

---

## 3. Como executar

Sempre a partir da raiz do repositório, com o ambiente de `requirements.txt` instalado.

```bash
python -m ETL.pipeline                           # raw + clean + curated com o que já foi ingerido (~75 s)
python -m ETL.pipeline --ingerir                 # roda `python -m ingestao` antes (~3 min)
python -m ETL.pipeline --ingerir --simular       # ... e regenera os eventos simulados
python -m ETL.pipeline --etapas clean curated    # pula a etapa raw
python -m ETL.pipeline --etapas curated          # só remonta o modelo estrela a partir do clean (~20 s)
python -m ETL.pipeline --data-coleta 2026-09-18  # reprocessa uma coleta específica

pytest ETL/tests -q                              # 91 testes das regras, sem ler dados/ (~1 s)
```

| Argumento | Efeito |
|---|---|
| `--ingerir` | Chama `python -m ingestao`, o orquestrador da ingestão, que roda as fontes na ordem canônica (ONS → ANEEL → `locais.csv` → NASA) e aborta na primeira falha. A ordem tem uma definição só, lá; a pipeline não mantém cópia dela. Cada ingestão usa seu próprio período (`.env` / padrões) |
| `--simular` | Roda `ML.analise_sobrevivencia.dados_simulados` (semente padrão 42) |
| `--etapas` | Subconjunto de `raw clean curated` (padrão: as três) |
| `--data-coleta AAAA-MM-DD` | Clean lê essa partição raw em vez da mais recente |

A ingestão e a simulação rodam em **processos separados** (`subprocess.run(..., check=True)`): usam seus próprios `argparse`/`.env` sem interferir na pipeline, e qualquer falha interrompe a execução.

### Cenário: "chegou dado novo"

```bash
python -m ETL.pipeline --ingerir
```

1. A ingestão sobrescreve `dados/bruto/dados_*_bruto.parquet`.
2. A **raw** arquiva essa coleta em `dados/bruto/<data de hoje>/`, e as coletas anteriores continuam lá.
3. O **clean** lê a partição mais recente de cada fonte.
4. O **curated** reaproveita os `usina_id` já publicados ([§8.2.5](#825-ids-estáveis)). Uma usina nova ganha um ID novo e as existentes mantêm o seu.
5. A **validação** só deixa gravar se tudo estiver íntegro.

---

## 4. Configuração central (`config.py`)

Todos os parâmetros ajustáveis ficam em um único arquivo.

| Constante | Valor | Uso |
|---|---|---|
| `BRUTO`, `SIMULADOS`, `CLEAN`, `CURATED` | `dados/bruto`, `dados/simulados`, `dados/limpos/clean`, `dados/limpos/curated` | Caminhos das camadas |
| `ARQUIVOS_BRUTOS` | ons, nasa, nasa_diario, aneel | Nome do arquivo bruto de cada fonte — **pasta**, no caso do ONS (um Parquet por mês) |
| `FUSO_BRASIL` | `America/Sao_Paulo` | Fuso de origem do ONS |
| `MAX_GAP_INTERPOLACAO_H` | `3` | Maior buraco horário que é interpolado |
| `POTENCIA_MINIMA_MW_VINCULO` | `1.0` | Usinas ANEEL elegíveis para o vínculo por nome |
| `DATA_SENTINELA_ANEEL` | `1900-01-03` | Data fictícia do cadastro da ANEEL |
| `DATA_MINIMA_PLAUSIVEL_UFV_EOL` | `1990-01-01` | Abaixo disso, a data de operação de solar/eólica é implausível |
| `LIMITES_BRASIL` | lat −34…5,5; lon −74…−34 | Validação de coordenadas |
| `DISTANCIA_MAXIMA_CLIMA_KM` | `300` | Acima disso, o vínculo com o ponto NASA é marcado como distante |
| `FONTE_ONS`, `FONTE_ANEEL` | ver arquivo | Vocabulário único de fonte: `solar`, `eolica`, `hidraulica`, `termica`, `nuclear` |
| `FONTES_CURATED` | `("solar", "eolica")` | Escopo do banco (system design §4.2) |
| `TIPO_UNIDADE_ONS` | ver arquivo | Modalidade ONS → `usina` / `conjunto` / `pequenas_usinas` |
| `UF_SUBSISTEMA` | 27 UFs → N/NE/SE/S | Mesma codificação do `id_subsistema` do ONS |
| `PALAVRAS_GENERICAS_CONJUNTO` | conjunto, eolico, fotovoltaico, ufv… | Removidas do nome do conjunto antes do vínculo por nome |
| `VERBOSE_TOOLKIT` | `True` | Liga os relatórios impressos pelo `ds_toolkit` |

---

## 5. Raw layer (`raw.py`)

### 5.1 Objetivo

A ingestão sempre sobrescreve `dados/bruto/dados_<fonte>_bruto.parquet`, então sem esta etapa cada nova coleta apagaria a anterior. A raw layer cria **partições imutáveis por data de coleta**, como pede a arquitetura:

```
dados/bruto/
├── dados_ons_bruto/                   ← última saída da ingestão (área de trabalho)
│   ├── dados_ons_bruto_2026_07.parquet    um Parquet por mês
│   ├── dados_ons_bruto_2026_08.parquet
│   └── dados_ons_bruto_2026_09.parquet
├── dados_nasa_bruto.parquet
├── dados_nasa_diario_bruto.parquet
├── dados_aneel_bruto.parquet
└── 2026-09-18/                        ← partição da coleta de 18/09/2026
    ├── dados_ons_bruto/
    │   ├── dados_ons_bruto_2026_07.parquet
    │   ├── dados_ons_bruto_2026_08.parquet
    │   └── dados_ons_bruto_2026_09.parquet
    ├── dados_nasa_bruto.parquet
    ├── dados_nasa_diario_bruto.parquet
    └── dados_aneel_bruto.parquet
```

O ONS é uma **pasta** porque a ingestão grava um Parquet por mês (ver [§6.2.1 da doc de ingestão](../ingestao/doc_tecnica_ingestao.md#621-um-parquet-por-mês)). A raw trata os dois casos com o mesmo código: `_arquivos(origem)` devolve `[o próprio arquivo]` ou o `glob("*.parquet")` da pasta, e cada arquivo é copiado individualmente por `_copiar`.

A convenção do repositório é `dados/`, em português. O overview citava `data/raw/`. O conceito é o mesmo.

### 5.2 `particionar(data_coleta=None)`

- Para cada arquivo de `ARQUIVOS_BRUTOS`, a **data de coleta** é a data de modificação do arquivo (`st_mtime`), ou seja, o dia em que a ingestão o gravou. Numa pasta mensal, é o mtime **mais recente** entre os meses: a partição registra quando aquela coleta terminou, e um mês antigo que não foi rebaixado não puxa a data para trás.
- Copia com `shutil.copy2`, que **preserva o mtime**. Assim a cópia carrega a própria data de coleta.
- **Idempotente:** se a partição já tem o arquivo com o mesmo tamanho e o mesmo mtime, nada é copiado. Na pasta do ONS a checagem é por arquivo, então reingerir só outubro copia só aquele mês. Rodar a pipeline dez vezes no mesmo dia não duplica nada. Se a ingestão rodar de novo no mesmo dia, o mtime muda e a cópia daquele dia é atualizada.
- Fonte ausente → aviso no log, sem erro. Isso permite rodar o ETL mesmo sem ter ingerido todas as fontes, e o clean acusa a falta depois.

### 5.3 `localizar(fonte, data_coleta=None)`

Devolve o caminho do bruto de uma fonte na **partição mais recente que contém aquele arquivo** (ou pasta não vazia, no caso do ONS), ou na partição pedida. As fontes podem ter sido coletadas em dias diferentes (por exemplo, a ANEEL ontem e o ONS hoje), e cada uma é resolvida separadamente. Se não houver partição, levanta `FileNotFoundError` com instrução de como resolver.

---

## 6. Utilitários (`utils.py`)

### 6.1 `ler_parquet(caminho)`

Lê o bruto de uma fonte sem o consumidor precisar saber se é arquivo ou pasta:

```python
if caminho.is_file():
    return pd.read_parquet(caminho)

arquivos = sorted(caminho.glob("*.parquet"))      # dados_ons_bruto/*.parquet
if not arquivos:
    raise FileNotFoundError(f"Nenhum .parquet em {caminho}")
return pd.concat((pd.read_parquet(a) for a in arquivos), ignore_index=True)
```

Dois detalhes:

- O **glob explícito** em vez de entregar a pasta ao pyarrow: o `pd.read_parquet` de um diretório tenta ler *todo* arquivo que encontrar, e uma sobra de escrita interrompida (`.parquet.tmp`) quebraria a leitura. O `*.parquet` ignora essas sobras. É também o mesmo padrão que o DuckDB aceita nativamente em `read_parquet('.../*.parquet')`, caso a leitura passe para SQL.
- O `sorted` torna a ordem das linhas determinística (meses em ordem cronológica, porque o nome termina em `AAAA_MM`), o que faz a saída do clean ser reproduzível byte a byte.

Verificação: depois da mudança, `ons_geracao` e as quatro tabelas curated saíram **idênticas** às geradas a partir do Parquet único — mesmas 1.365.096 linhas, mesmos dtypes, mesmo hash de conteúdo.

### 6.2 `completar_grade(df, chave, tempo, colunas_fixas, freq="h")`

Garante **uma linha por período** (`h` = hora, `D` = dia) entre a primeira e a última observação de cada série (`chave`).

1. Calcula min, max e contagem por série. Se a contagem já bate com o esperado em todas, retorna sem mexer (caminho rápido, que é o caso atual).
2. Senão, monta a grade completa com `pd.date_range`, faz merge à esquerda com os dados e preenche os atributos fixos da série (UF, fonte etc.) com `ffill().bfill()` dentro de cada chave.
3. As medições das linhas criadas ficam **nulas** e são tratadas como gap no passo seguinte.

Motivação: um buraco pode aparecer como **valor nulo** ou como **linha ausente**. Depois da grade, os dois viram a mesma coisa (nulo) e recebem o mesmo tratamento.

### 6.3 `interpolar_gaps_curtos(df, chave, colunas, max_gap)`

Interpola linearmente **apenas** os buracos de até `max_gap` valores consecutivos, dentro de cada série.

O `interpolate(limit=N)` do pandas **não** faz isso: num buraco de 24 horas com `limit=3`, ele preenche as 3 primeiras horas e deixa 21 nulas. Aqui cada sequência de nulos é medida antes:

```python
nulo = df[col].isna()
sequencia = (~nulo).groupby(df[chave]).cumsum()          # id do trecho: nº de válidos até ali
tamanho = nulo.groupby([df[chave], sequencia]).transform("sum")   # tamanho do buraco
estimado = df.groupby(chave)[col].transform(lambda s: s.interpolate(limit_area="inside"))
preencher = nulo & (tamanho <= max_gap) & estimado.notna()
```

- `limit_area="inside"`: só interpola entre dois valores válidos, nunca extrapola as pontas. Um buraco no fim da série, como a latência da NASA, nunca é inventado.
- Devolve duas máscaras por linha: `interpolado` (algum valor foi preenchido) e `faltante` (algum valor continua nulo).

### 6.4 `flag_qualidade(interpolado, faltante, extra=None)`

Gera a coluna textual `flag_qualidade`, com precedência **`faltante` > `interpolado` > flag extra > `original`**. A flag extra é específica da fonte, por exemplo `negativo_zerado` no ONS.

### 6.5 `listar_faltantes(df, colunas)`

Coluna textual com os **nomes** das medidas que continuaram nulas em cada linha, separados por vírgula (nulo quando não falta nada).

O `flag_qualidade` responde *se* falta algo; esta responde *o quê*. A distinção existe porque a latência da NASA POWER é **desigual por variável**: vento e temperatura atrasam ~2 dias, a irradiância diária ~1 semana e a irradiância horária ~3 meses. Sem esta coluna, um dia em que só a irradiância não saiu fica marcado exatamente como um dia sem nenhuma medição — e, na série horária, isso significava marcar **as 19.200 linhas** como `faltante` por causa de uma única variável, com vento e temperatura perfeitamente utilizáveis ao lado.

A implementação é vetorizada, sem `apply` linha a linha: em `numpy`, `True * "nome"` devolve `"nome"` e `False * "nome"` devolve `""`, então o produto de matrizes da máscara de nulos pelos nomes já concatena o resultado.

```python
nulos.dot(np.array([f"{c}," for c in colunas], dtype=object)).str.rstrip(",")
```

### 6.6 `normalizar_nome(df, coluna, destino)`

Chave de comparação de nomes: minúsculas, sem acento, sem pontuação e sem espaços repetidos (`"Caetité  2"`, `"CAETITE 2"` e `"caetite-2"` viram `"caetite 2"`). Usa `ds_toolkit.padronizar_texto`.

**Otimização:** normaliza só os **valores distintos** e mapeia de volta. O ONS tem 1,36 milhão de linhas e cerca de 1.200 nomes distintos, então isso é ~1.000× menos trabalho de string.

### 6.7 Outros

| Função | O que faz |
|---|---|
| `limpar_textos(df, colunas, vazios)` | `strip()` + converte marcadores de vazio (`""`, `"-"`) em nulo |
| `haversine_km(lat1, lon1, lat2, lon2)` | Distância na esfera (raio de 6.371 km), vetorizada |
| `base_ceg(ceg)` | CEG sem o sufixo de versão: `UHE.PH.AM.000190-2.01` → `UHE.PH.AM.000190-2`. O ONS usa sufixo de 2 dígitos e a ANEEL de 1, então só a base é comparável |
| `gravar(df, pasta, nome)` | `ingestao.armazenamento.gravar_parquet` (atômico, zstd) |

---

## 7. Clean layer (`clean/`)

Princípios comuns:

- **Mantém o grão e todas as linhas** da fonte. Não filtra por escopo (o ONS clean tem todas as fontes, a ANEEL clean tem todas as fases).
- **Marca em vez de apagar:** dado suspeito vira nulo **com flag** explicando o motivo.
- **Tempo sempre em UTC com fuso explícito**, mais uma coluna em horário de Brasília por conveniência.

### 7.1 ONS — `clean/ons.py` → `ons_geracao.parquet`

Entrada: 1.365.096 linhas (usina/conjunto × hora, jul a set/2026, todas as fontes).

| # | Passo | Detalhe |
|---|---|---|
| 1 | Textos | `strip` em todas as colunas de texto. `""` e `"-"` → nulo (`ceg = "-"` significava "sem CEG") |
| 2 | Duplicatas | `dst.tratar_duplicados` pela chave natural (instante, UF, nome, modalidade, tipo), mantendo a última. **0 encontradas** |
| 3 | Fonte | `nom_tipousina` → vocabulário único (`FOTOVOLTAICA` → `solar` etc.). Tipo desconhecido gera aviso |
| 4 | Identidade da unidade | `chave_unidade` = `id_ons`. Nas agregações "Pequenas Usinas", que não têm `id_ons`: `PQU|<nome>|<UF>`. Aborta se houver duas linhas da mesma unidade no mesmo instante |
| 5 | Grafias | **Nome canônico = o mais recente** de cada unidade. O ONS renomeia usinas: `LBI_LG BARRO I` → `UFV LG BARRO I` (mesmo `id_ons` PISLB1, em agosto). Cada renomeação é registrada no log |
| 6 | Tipo de unidade | Modalidade → `usina` (TIPO I/II), `conjunto` ou `pequenas_usinas` |
| 7 | Grade horária | `completar_grade` por unidade. **0 horas ausentes** na coleta atual |
| 8 | Negativos | 19 valores entre −1,4 e 0 MWh (consumo auxiliar da usina) → `0` com flag `negativo_zerado` |
| 9 | Gaps | Até 3 h consecutivas: interpolação linear. Mais que isso: nulo com flag `faltante` |
| 10 | Fuso | `din_instante` (horário de Brasília, sem fuso) → `tz_localize("America/Sao_Paulo")` → `data_hora_utc`. O tz database trata o horário de verão histórico (extinto em 2019). Instantes ambíguos ou inexistentes viram `NaT` com aviso |
| 11 | Nome normalizado | `nome_usina_normalizado` para comparar com a ANEEL |

**Checagem do fuso:** a geração solar agregada tem pico às **11h de Brasília = 14h UTC**, que é o esperado para o rótulo "início da hora" (11:00–12:00). Isso confirma que `din_instante` está mesmo em horário de Brasília.

**Gaps encontrados:** nas fontes solar e eólica há só 96 horas faltantes (4 unidades com um dia inteiro sem dado cada). Um buraco de 24 h **não** é interpolado: inventar um dia de curva solar distorceria qualquer análise. Nas hídricas e térmicas os buracos são grandes (161 mil horas), porque são unidades que não reportam em parte do período.

| Coluna | Tipo | Descrição |
|---|---|---|
| `data_hora_utc` | timestamp UTC | Início da hora |
| `data_hora_brasilia` | timestamp America/Sao_Paulo | Mesmo instante no fuso local |
| `chave_unidade` | string | Identidade estável da unidade |
| `id_ons`, `ceg` | string | Códigos da fonte (nulos quando não há) |
| `nome_usina`, `nome_usina_normalizado` | string | Nome canônico + chave de comparação |
| `tipo_unidade`, `modalidade_ons` | string | `usina`/`conjunto`/`pequenas_usinas` + modalidade original |
| `fonte`, `combustivel` | string | Fonte padronizada + detalhe do ONS |
| `id_subsistema`, `id_estado` | string | N/NE/SE/S e UF |
| `geracao_mwh` | double | Geração na hora (MWmed ≡ MWh em 1 h) |
| `flag_qualidade` | string | `original` / `interpolado` / `negativo_zerado` / `faltante` |
| `arquivo_origem` | string | Arquivo mensal do ONS (proveniência) |

### 7.2 NASA horária — `clean/nasa.py::limpar` → `nasa_clima_horario.parquet`

| Passo | Detalhe |
|---|---|
| Parâmetros | `ALLSKY_SFC_SW_DWN` → `irradiancia_wh_m2`, `WS10M` → `vento_10m_ms`, `WS50M` → `vento_50m_ms`, `T2M` → `temperatura_2m_c` |
| Fill value | `-999` → nulo (20.640 valores na coleta atual) |
| Tempo | `"AAAAMMDDHH"` → timestamp → `tz_localize("UTC")` + `data_hora_brasilia` |
| Duplicatas | por (local, hora), via toolkit |
| Grade + gaps | Igual ao ONS (≤ 3 h interpola; mais que isso, `faltante`) |

**Alerta:** na coleta atual, as 19.200 linhas saem como `faltante`. A **irradiância horária** da NASA é publicada com cerca de **3 meses de atraso** (último valor válido em 30/06/2026), então o período jul a set/2026 inteiro veio `-999` nessa coluna. Vento e temperatura horários estão completos, exceto nas últimas ~48 h. Por isso esta tabela serve para análises horárias de vento e temperatura, e a `fato_clima` usa a **série diária** (7.3).

É justamente aqui que o `flag_qualidade` sozinho engana: 100% das linhas marcadas `faltante`, sendo que em 18.720 delas **só** a irradiância está ausente. A coluna `medidas_faltantes` ([§6.5](#65-listar_faltantesdf-colunas)) separa os dois casos, e o log da etapa passou a imprimir os nulos por medida:

```
[clean:nasa] nulos por medida: {'irradiancia_wh_m2': 19200, 'vento_10m_ms': 480,
                                'vento_50m_ms': 480, 'temperatura_2m_c': 480}
```

### 7.3 NASA diária — `clean/nasa.py::limpar_diario` → `nasa_clima_diario.parquet`

Série criada na ingestão justamente para contornar a latência da irradiância horária. Detalhes em `doc_tecnica_ingestao.md` §7.

| Passo | Detalhe |
|---|---|
| Parâmetros | `irradiancia_kwh_m2_dia`, `vento_10m_ms`, `vento_50m_ms`, `temperatura_2m_c`, `temperatura_max_c`, `temperatura_min_c` |
| Tempo | `data_lst` (`AAAAMMDD`) → `data`. O dia é em **hora solar local**, a menos de 1 h do horário de Brasília nas longitudes do país |
| Grade + gaps | Grade diária (`freq="D"`). Buracos de **1 dia** são interpolados (`MAX_GAP_INTERPOLACAO_DIAS = 1`); o resto (latência no fim da série) fica `faltante` |

Resultado: 800 linhas (10 locais × 80 dias). 740 `original`, 10 `interpolado` e 50 `faltante`, todas a partir de 14/09 (latência). Das 50, **20 têm só a irradiância ausente** e 30 não têm nenhuma medida — as duas latências diferentes aparecendo lado a lado, e agora distinguíveis por `medidas_faltantes`.

### 7.4 ANEEL — `clean/aneel.py` → `aneel_usinas.parquet`

Entrada: 20.511 empreendimentos UFV/EOL, todas as fases.

| # | Passo | Detalhe |
|---|---|---|
| 1 | Nomes de coluna | `dst.limpar_nomes_colunas` (snake_case, sem acento) e depois renomeação para nomes descritivos (`MdaPotenciaOutorgadaKw` → `potencia_outorgada_kw`) |
| 2 | **LGPD** | A coluna de proprietários (`DscPropriRegimePariticipacao`, com nomes de pessoas físicas e CNPJs) é **descartada**, junto com colunas irrelevantes (sub-bacia, `_id`) |
| 3 | Textos | `strip` + vazio → nulo |
| 4 | Duplicatas | por `ceg` (0 encontradas) |
| 5 | Números BR | `dst.converter_tipos(decimal_brasileiro=True)`: `"1.400,00"` → 1400.0, `",00"` → 0.0, `"-20,12479858"` → −20.12479858 |
| 6 | Datas | `dst.converter_tipos(formato_data="%Y-%m-%d")` |
| 7 | Data de operação | `1900-01-03` → nulo com flag `sentinela_1900` (**2.080**). Antes de 1990 → nulo com flag `implausivel_pre_1990` (**43**) |
| 8 | Coordenadas | (0,0) → nulo com flag `zerada` (**415**). Fora do Brasil → `fora_do_brasil` (0) |
| 9 | Município | `"Pedra Grande - RN, São Bento do Norte - RN"` → `municipio = "Pedra Grande"`, `uf_municipio = "RN"`, com a lista completa em `municipios` (124 usinas em mais de um município). `"Não Informado"` → nulo |
| 10 | Chaves normalizadas | `nome_normalizado`, `municipio_normalizado`. O toolkit reporta 3 grafias duplicadas unificadas nos nomes |
| 11 | Derivados | `fonte`, `id_subsistema` (via UF), potências em **MW** |

Principais colunas: `ceg`, `nucleo_ceg`, `nome`, `nome_normalizado`, `sig_tipo_geracao`, `fonte`, `fase`, `tipo_outorga`, `id_estado`, `id_subsistema`, `municipio`, `uf_municipio`, `municipio_normalizado`, `municipios`, `potencia_outorgada_mw`, `potencia_fiscalizada_mw`, `garantia_fisica_mw`, `latitude`, `longitude`, `flag_coordenada`, `data_entrada_operacao`, `flag_data_operacao`, `inicio_vigencia`, `fim_vigencia`, `data_retrato`.

> O campo `nome` ainda contém nomes de pessoas físicas (microssistemas registrados em nome próprio). Ele fica no clean porque é necessário para o vínculo por nome, mas **não chega à camada curated**: a `dim_usina` usa os nomes do ONS.

### 7.5 Manutenção simulada — `clean/manutencao.py` → `manutencao_simulada.parquet`

O dado já nasce limpo, então a etapa **valida os invariantes** de sobrevivência e aborta se algum falhar:

- `tempo_anos > 0`;
- `evento ∈ {0, 1}`;
- evento ⟺ `data_evento` preenchida;
- `data_evento ≤ data_corte`.

Também deriva `tempo_dias = max(round(tempo_anos × 365,25), 1)` (a unidade do banco) e a coluna `simulado = True`.

---

## 8. Curated layer (`curated/`)

### 8.1 Modelo estrela

```
                      ┌──────────────────┐
                      │    dim_usina     │  308 unidades solar/eólica
                      │  PK usina_id     │
                      └────────┬─────────┘
          ┌────────────────────┼──────────────────────┐
          │                    │                      │
┌─────────▼─────────┐ ┌────────▼────────┐ ┌───────────▼──────────┐
│   fato_geracao    │ │   fato_clima    │ │   fato_manutencao    │
│ usina × hora      │ │ usina × dia     │ │ usina (SIMULADO)     │
│ 575.904 linhas    │ │ 23.840 linhas   │ │ 159 linhas           │
└───────────────────┘ └─────────────────┘ └──────────────────────┘

        ponte_usina_aneel (790): usina da ANEEL → usina_id (auditoria + manutenção)
```

### 8.2 `dim_usina` — `curated/dim_usina.py`

#### 8.2.1 A decisão de grão

O system design pede uma `dim_usina` com potência, localização, município e data de operação, referenciada por uma `fato_geracao` de grão **usina × hora**. O problema é que as duas fontes não enxergam "usina" do mesmo jeito:

- a **ANEEL** cadastra cada usina individualmente (com CEG, potência e coordenadas), mas não tem geração;
- o **ONS** mede a geração, mas, para solar e eólica, quase sempre por **conjunto de usinas** (227 das 308 unidades) ou por agregado estadual de pequenas usinas (63). Só 18 são usinas individuais, e o ONS não publica quais usinas compõem cada conjunto.

**Escolha:** o grão da `dim_usina` é a **unidade geradora do ONS** (usina individual, conjunto ou agregado), porque é o único nível em que existe geração medida. Os atributos de cadastro vêm da ANEEL por um **vínculo explícito e auditável**. A alternativa, dividir a geração de um conjunto entre suas usinas (por exemplo, proporcionalmente à potência), fabricaria dado e foi descartada.

**Consequência:** a dimensão passa a conter três grãos diferentes, e `tipo_unidade` é o que os separa. A distinção não é cosmética — ela decide o que existe para cada linha:

| `tipo_unidade` | Unidades | Com potência | Com coordenada | Com manutenção | Energia medida |
|---|---|---|---|---|---|
| `conjunto` | 227 | 78 | 144 | 144 | 35.272 GWh |
| `pequenas_usinas` | 63 | **0** | **0** | **0** | 16.640 GWh |
| `usina` | 18 | 18 | 18 | 15 | 361 GWh |

As 63 linhas `pequenas_usinas` são as modalidades "Pequenas Usinas (MMGD)" e "(Tipo III)": o ONS publica numa linha só a **soma** da geração distribuída de um estado. Não existe usina correspondente na ANEEL para vincular, e isso não é falha do vínculo — é o grão em que a medição é publicada. Elas carregam **32% da energia medida** no período, então descartá-las na ingestão perderia um terço do dado; o que se faz é marcá-las e deixar quem precisa de cadastro filtrar por `tipo_unidade` ([backend §6.1](../backend/doc_tecnica_backend.md#61-get-usinas)).

#### 8.2.2 Unidades (`_unidades_ons`)

Para cada `chave_unidade` solar/eólica do ONS clean, os atributos mais recentes: `id_ons`, `ceg_ons`, `nome`, `fonte`, `tipo_unidade`, `modalidade_ons`, `id_estado`, `regiao` (= subsistema), além de primeira e última medição e **`pico_geracao_mw`** (maior geração horária observada).

#### 8.2.3 Vínculo ONS × ANEEL (`_vincular`)

**1. Por CEG (usinas individuais)**

- `base_ceg(ceg_ons) == base_ceg(ceg_aneel)`, contra **todas** as usinas UFV/EOL da ANEEL, em qualquer fase.
- Motivo de aceitar qualquer fase: 3 usinas já geram no ONS mas ainda constam como "Construção" no cadastro (Passagem, Solar Lagoa do Barro I, Solar Mundo Novo). O CEG é um identificador exato, então a fase não importa.
- Resultado: **18 de 18** usinas individuais vinculadas.

**2. Por nome (conjuntos)**

1. Candidatas: usinas ANEEL em **Operação**, com **≥ 1 MW**, da **mesma UF** e **mesma fonte** do conjunto, e ainda não usadas.
2. **Núcleo** do nome do conjunto: o nome normalizado sem palavras genéricas (`conjunto`, `eolico`, `fotovoltaico`…) e sem tensão de subestação (`230 kv`). Exemplo: `CONJUNTO EOLICO MORRO DO CHAPEU SUL II 230 KV` → `morro do chapeu sul ii`.
3. Casa as usinas cujo `nome_normalizado` contém o núcleo como **sequência de palavras inteiras** (regex com fronteira de espaço). `santa eugenia` casa `ventos de santa eugenia 01`, mas `sul` não casa `sulamerica`.
4. Se o núcleo inteiro não casar, **remove palavras do fim** e tenta de novo (`caetite 123` → `caetite`), sem descer abaixo de 4 caracteres.
5. **Ordem de escolha:** os conjuntos com núcleo **mais longo** (mais específico) escolhem primeiro, e cada usina da ANEEL entra em **um único** conjunto. Assim, `serra da babilonia` pega as suas usinas antes que `babilonia` possa pegá-las.

O vínculo por nome é **aproximado por natureza**, porque muitos conjuntos têm nome de subestação que não aparece na ANEEL. Por isso ele é verificado.

#### 8.2.4 Verificação do vínculo contra a geração medida

Uma unidade não gera mais do que sua potência instalada, e num período de ~80 dias costuma atingir de 40% a 100% dela. Então:

```math
\text{razao\_pico\_potencia} = \frac{\text{pico\_geracao\_mw (ONS)}}{\text{potência vinculada (soma ANEEL)}}
```

| `qualidade_vinculo` | Regra | Unidades | Razão mediana |
|---|---|---|---|
| `exata` | vínculo por CEG | 18 | 0,86 |
| `consistente` | por nome e razão ∈ [0,40; 1,15] | 78 | 0,92 |
| `inconsistente` | por nome e razão fora da faixa | 66 | 2,74 |
| `sem_vinculo` | nenhum correspondente | 146 (83 conjuntos + 63 agregados) | — |

- Razão ≫ 1 significa que o vínculo pegou **só parte** do conjunto. Exemplo: `CONJUNTO FOTOVOLTAICO PARACATU 4 PILOTO` gera até 939 MW, mas só uma usina de 33 MW foi vinculada (razão 28,5).
- Razão ≪ 1 significa vínculo excessivo, ou unidade ainda sem geração no período.

**Consequência:** `potencia_mw` só é publicada quando o vínculo é `exata` ou `consistente` (96 unidades, **20,3 GW**). Nas demais fica **nula**, e o valor vinculado continua auditável em `potencia_aneel_vinculada_mw`. `lat`, `lon`, `municipio` e `data_operacao` são mantidos também nos vínculos inconsistentes, porque as usinas de um mesmo complexo ficam na mesma região. A `qualidade_vinculo` permite filtrar.

#### 8.2.5 IDs estáveis

`usina_id` é `UBIGINT` sequencial (system design §4.3), mas **estável entre execuções**:

1. Se existe `dados/limpos/curated/dim_usina.parquet` de uma execução anterior, lê o mapa `chave_unidade → usina_id` e o reaproveita.
2. Unidades novas recebem `max(id) + 1`, na ordem (fonte, região, UF, nome).

Isso evita que uma usina nova "empurre" os IDs das outras, o que quebraria URLs da API como `/usinas/42`. Verificado: rodar a pipeline duas vezes gera exatamente os mesmos IDs.

#### 8.2.6 Esquema

| Coluna | Tipo | Descrição |
|---|---|---|
| `usina_id` | uint64 | **PK** |
| `chave_unidade` | string | Chave natural (`id_ons` ou `PQU|nome|UF`) |
| `id_ons`, `ceg_ons` | string | Códigos do ONS |
| `nome`, `nome_normalizado` | string | Nome canônico do ONS |
| `fonte` | string | `solar` \| `eolica` |
| `tipo_unidade`, `modalidade_ons` | string | `usina`/`conjunto`/`pequenas_usinas` |
| `regiao` | string | Subsistema N/NE/SE/S (o `regiao` do DDL) |
| `id_estado` | string | UF |
| `municipio` | string | Município mais frequente entre as usinas vinculadas |
| `potencia_mw` | double | Soma das potências vinculadas (**só se o vínculo for confiável**) |
| `lat`, `lon` | double | Centroide das usinas vinculadas |
| `data_operacao` | date | Entrada em operação mais antiga entre as vinculadas |
| `n_usinas_aneel` | int | Nº de usinas da ANEEL vinculadas |
| `metodo_vinculo` | string | `ceg` / `nome` / `sem_vinculo` |
| `qualidade_vinculo` | string | `exata` / `consistente` / `inconsistente` / `sem_vinculo` |
| `potencia_aneel_vinculada_mw` | double | Soma vinculada, sempre (auditoria) |
| `pico_geracao_mw` | double | Maior geração horária medida no período |
| `razao_pico_potencia` | double | pico / potência vinculada |
| `primeira_medicao_utc`, `ultima_medicao_utc` | timestamp | Cobertura da série de geração |

### 8.3 `ponte_usina_aneel`

Uma linha por usina da ANEEL vinculada (790): `ceg_aneel`, `usina_id`, `chave_unidade`, `metodo_vinculo`, `nucleo_usado` (o texto que casou, no vínculo por nome), `potencia_outorgada_mw`, `municipio`, `data_entrada_operacao`. Serve para **auditar** o vínculo ("quais usinas foram parar nesse conjunto, e por qual núcleo?") e alimenta a `fato_manutencao`.

### 8.4 `fato_geracao` — grão usina × hora

- Filtra o ONS clean em `solar`/`eolica` e junta `usina_id` via `dst.mesclar_seguro(validar="many_to_one")`. O pandas **aborta** se uma unidade aparecer duas vezes na dimensão.
- Colunas: `usina_id`, `timestamp_utc`, `energia_mwh`, `fonte`, `regiao`, `flag_qualidade`.
- `fonte` e `regiao` são repetidas da dimensão porque o overview pede ("energia gerada, fonte, subsistema") e porque isso acelera o endpoint `/geracao/nacional`.
- Horas sem medição **ficam na tabela** com `energia_mwh` nula e flag `faltante` (96 linhas), em vez de sumirem. Assim quem consome enxerga o buraco.

### 8.5 `fato_clima` — grão usina × dia

A NASA foi coletada em **10 pontos** (`ingestao/nasa_power/locais.csv`), não em cada usina. Cada unidade é associada a um ponto de referência (`_ponto_clima_por_usina`):

| Situação da unidade | Regra | `metodo_vinculo_clima` | Unidades |
|---|---|---|---|
| Tem `lat`/`lon` | Ponto mais próximo por haversine, até 300 km | `mais_proximo` | 133 |
| Tem `lat`/`lon`, ponto a mais de 300 km | Mesmo ponto, marcado | `mais_proximo_distante` | 29 |
| Sem coordenadas | Ponto da mesma UF | `mesma_uf` | 90 |
| Sem coordenadas nem ponto na UF | Ponto do mesmo subsistema | `mesmo_subsistema` | 46 |
| Nada disso | Fica sem clima | — | 10 (subsistema Norte, sem ponto NASA) |

Distância usina → ponto (com coordenadas): mediana de 100 km, máxima de 834 km.

A série é a **diária** da NASA (clean 7.3) — e não a horária, precisamente por causa da latência de 3 meses da irradiância horária. Ela é renomeada para o vocabulário do DDL: `irradiancia_kwh_m2`, `vento_ms` (**a 50 m**, altura mais próxima do cubo dos aerogeradores), `temperatura_c` (média), mais `vento_10m_ms`, `temperatura_max_c`, `temperatura_min_c`, `flag_qualidade`, `medidas_faltantes`, `local_clima`, `distancia_km` e `metodo_vinculo_clima`. As três últimas deixam a **aproximação espacial** explícita; `medidas_faltantes` deixa explícita a **temporal**.

`medidas_faltantes` é **recalculado** aqui, depois do rename, em vez de ser copiado do clean: quem consome a fato (API, frontend) não deve precisar saber que na camada clean a coluna se chamava `irradiancia_kwh_m2_dia`.

Resultado: 23.840 linhas (298 unidades × 80 dias, de 01/07 a 18/09/2026). 22.052 `original`, 298 `interpolado` e 1.490 `faltante` (latência NASA no fim da série). Dessas 1.490, **596 têm só a irradiância ausente** e 894 não têm nenhuma medida.

### 8.6 `fato_manutencao` — grão usina (SIMULADO)

Os eventos simulados são por usina da ANEEL. A unidade herda o **primeiro evento entre suas usinas vinculadas** (via `ponte_usina_aneel`):

- `inicio` = data de operação mais antiga entre os membros;
- se algum membro teve evento: `evento_ocorreu = True` e `tempo_dias` = primeiro evento − início;
- senão: censurada, com `tempo_dias` = data de corte − início.

Isso é o "tempo até o primeiro evento de manutenção no complexo", uma definição coerente para um conjunto de usinas.

Colunas: `usina_id`, `tempo_dias`, `evento_ocorreu` (bool), `data_primeiro_evento`, `tipo_evento`, `n_usinas_consideradas`, `simulado = True`.

Resultado: **159** unidades (as que têm usinas da ANEEL vinculadas, de qualquer qualidade), 134 com evento e 25 censuradas. Mediana de `tempo_dias` = 692, e mediana de 3 usinas por unidade (máximo de 24).

> Com o "primeiro evento entre N usinas", conjuntos grandes têm eventos mais cedo e menos censura. Para **treinar e validar** o modelo de sobrevivência, use o arquivo simulado por usina (`dados/simulados/`), cujos parâmetros verdadeiros são conhecidos. A `fato_manutencao` é a visão que a API serve.

---

## 9. Validação (`validacao.py`)

Roda **antes** de gravar a curated. Qualquer violação levanta `ValueError` com a lista de problemas, e nada é gravado: é melhor não publicar do que publicar um banco com chave duplicada.

| Tabela | PK | Obrigatórias | Faixas |
|---|---|---|---|
| `dim_usina` | `usina_id` | `usina_id`, `chave_unidade`, `nome`, `fonte`, `regiao` | `potencia_mw ≥ 0`, lat −34…5,5, lon −74…−34 |
| `fato_geracao` | (`usina_id`, `timestamp_utc`) | `usina_id`, `timestamp_utc`, `fonte`, `flag_qualidade` | `energia_mwh ≥ 0` |
| `fato_clima` | (`usina_id`, `data`) | `usina_id`, `data`, `local_clima` | irradiância 0…12 kWh/m², vento 0…60 m/s, temperatura −10…50 °C |
| `fato_manutencao` | `usina_id` | `usina_id`, `tempo_dias`, `evento_ocorreu` | `tempo_dias ≥ 1` |

Checagens adicionais:

- **Integridade referencial:** todo `usina_id` das fatos existe na `dim_usina` (nenhum fato órfão).
- **Relatório de qualidade** (`dst.relatorio_qualidade`) de cada tabela no log: duplicatas, nulos, cardinalidade e colunas quase constantes.

Os nulos permitidos por desenho (como `energia_mwh` com flag `faltante`) não entram nas faixas, que são avaliadas só sobre valores presentes.

---

## 10. Uso do `ds_toolkit`

| Função do toolkit | Onde | Para quê |
|---|---|---|
| `tratar_duplicados` | clean ONS, NASA (horária e diária), ANEEL, manutenção | Remoção de duplicatas pela chave natural, com relatório |
| `padronizar_texto` | `utils.normalizar_nome` | Chaves de nome e município (acentos, pontuação, caixa), com contagem de grafias unificadas |
| `limpar_nomes_colunas` | clean ANEEL | `MdaPotenciaOutorgadaKw` → `mdapotenciaoutorgadakw` antes da renomeação |
| `converter_tipos` | clean ANEEL | Números BR (`1.400,00`) e datas ISO, reportando o que virou nulo |
| `mesclar_seguro` | curated (dim, geração, clima, manutenção) | Joins com `validate` (aborta em cardinalidade errada) e relatório de match/explosão de linhas |
| `relatorio_qualidade` | `validacao.py` | Diagnóstico de cada tabela curated |

Os relatórios são impressos quando `VERBOSE_TOOLKIT = True` (`config.py`).

---

## 11. Testes automatizados

```bash
pytest ETL/tests -q      # 91 testes, ~1 s
pytest -q                # suíte do projeto (ingestão, ETL, simulação, backend): 238
```

**Nenhum teste lê `dados/`.** Cada caso constrói em memória a menor tabela que exibe a regra — uma série de cinco horas com um buraco de três, uma `dim_usina` de duas linhas — ou escreve Parquets de brinquedo em `tmp_path`. Isso é o que permite testar o que o dado real não oferece sob demanda: um buraco de exatamente 4 horas, uma coordenada no meridiano de Greenwich, uma unidade que desapareceu do ONS entre duas execuções.

O `conftest.py` desliga os relatórios do `ds_toolkit` (`VERBOSE_TOOLKIT = False`). A função continua sendo chamada — o caminho de código é o da pipeline —, só não despeja uma tabela por tabela validada na saída do pytest.

| Arquivo | Cobre | Testes |
|---|---|---|
| `test_utils.py` | `interpolar_gaps_curtos`, `completar_grade`, `base_ceg`, `listar_faltantes`, `flag_qualidade`, `ler_parquet` | 35 |
| `test_dim_usina.py` | `_nucleo` e `_ids_estaveis` | 19 |
| `test_validacao.py` | PK, obrigatórias, faixas, integridade referencial e o relatório de erros | 37 |

### 11.1 O que cada grupo protege

**`interpolar_gaps_curtos`** ([§6.3](#63-interpolar_gaps_curtosdf-chave-colunas-max_gap)) — a regra do projeto é "3 horas interpola, 4 não", e os testes a verificam nos dois sentidos: buracos de 1, 2 e 3 horas saem preenchidos; de 4, 5, 12 e 48 saem **inteiros nulos**. O caso central é `test_buraco_longo_nao_e_preenchido_pela_metade`, que mede também o comportamento que estamos evitando — `interpolate(limit=3)` deixa 21 dos 24 nulos, preenchendo as 3 primeiras horas — para o teste não ser uma tautologia sobre a própria implementação.

Os extremos das séries sintéticas estão sobre a reta `y = 10x`, então o valor interpolado de cada hora é conhecido exatamente e o teste compara números, não só a ausência de nulos. Os outros casos cobrem as fronteiras que a implementação precisa respeitar: não extrapolar as pontas (a latência da NASA nunca é inventada), não fechar o buraco do fim de uma usina com o começo da próxima, medir cada buraco na sua própria série (um passa, o outro não na mesma chamada) e marcar `interpolado` e `faltante` na mesma linha quando uma medida foi preenchida e outra não.

**`completar_grade`** ([§6.2](#62-completar_gradedf-chave-tempo-colunas_fixas-freqh)) — que o caminho rápido devolve o **mesmo objeto** (`df is entrada`) quando a grade já está completa, que cada série é completada entre o seu próprio primeiro e último instante (uma usina que entrou em operação depois não recebe horas anteriores à primeira medição dela) e que os atributos fixos são herdados nas linhas criadas. O teste `test_linha_ausente_e_valor_nulo_recebem_o_mesmo_tratamento` é o que justifica a função existir: com uma hora *ausente* e outra *nula*, as duas saem da grade como nulos e recebem o mesmo valor da reta.

**`base_ceg`** ([§6.7](#67-outros)) — o ponto é um só: o CEG do ONS (`...-2.01`) e o da ANEEL (`...-2.1`) têm que colapsar na mesma base, porque é essa igualdade que sustenta o vínculo por CEG. Os testes registram também o que a função pressupõe: sem sufixo de versão, o último trecho removido é o número da usina.

**`_nucleo`** ([§8.2.3](#823-vínculo-ons--aneel-_vincular)) — os casos reais do cadastro: `conjunto eolico morro do chapeu sul ii 230 kv` → `morro do chapeu sul ii`, com o nível de tensão removido colado (`230kv`) ou separado, e `caetite 2` mantendo o `2`, que é parte do nome e não tensão. Mais a idempotência e o caso-limite: um conjunto nomeado só com palavras genéricas devolve núcleo vazio, abaixo de `TAMANHO_MINIMO_NUCLEO` — o que faz o `_vincular` não vincular, em vez de casar com qualquer usina da UF.

**`_ids_estaveis`** ([§8.2.5](#825-ids-estáveis)) — a promessa de que `usina_id` não muda entre execuções, testada contra uma `dim_usina.parquet` anterior escrita em `tmp_path`. Os IDs seguem a **chave**, não a posição (a dim é reordenada por fonte/região/nome antes de receber o ID); unidades novas recebem `max + 1`; e o ID de uma unidade que saiu do ONS **não é reciclado** — reciclá-lo faria uma usina nova herdar o histórico da antiga em qualquer coisa que tenha guardado o número: gráfico salvo, modelo treinado, link compartilhado.

**`validacao`** ([§9](#9-validação-validacaopy)) — um conjunto curated mínimo e válido (duas usinas, dois fatos de cada) é sabotado uma regra por vez. Cada violação é verificada pela mensagem, não só pela exceção: PK duplicada na dimensão e na PK **composta** do fato horário (repetir o `usina_id` em horas diferentes é o normal e tem que passar), nulo em cada obrigatória, cada faixa estourada por cima e por baixo, e fato órfão em cada uma das três fatos.

Dois grupos registram o que **não** é erro, que é a parte fácil de quebrar sem perceber: `energia_mwh` nula com flag `faltante` é dado ausente declarado (o resultado normal de um gap longo demais), `potencia_mw` nula é o vínculo inconsistente se recusando a publicar número, as bordas das faixas são fechadas (lat `-34,0` e `5,5` passam) e uma usina sem fato de clima não é violação — a integridade referencial vale só na direção fato → dimensão.

Por fim, dois testes sobre o **relatório**: que todos os problemas saem numa lista só (não é preciso rodar a pipeline quatro vezes para descobrir quatro erros) e que a contagem aparece na mensagem — saber que são 3 linhas e não 3.000 muda o diagnóstico. Um deles documenta a cascata: sobrescrever um `usina_id` na dimensão produz a PK duplicada **e** os fatos daquela usina virando órfãos, e ver os quatro sintomas juntos aponta para a causa única.

### 11.2 O que não é testado

- **O vínculo ONS × ANEEL ponta a ponta.** `_nucleo` é testado isoladamente; `_vincular` (a ordem por especificidade, a remoção de palavras do fim, a exclusividade das usinas já usadas) exigiria um cadastro de brinquedo grande o bastante para ser representativo. A verificação que vale hoje é a empírica, contra a geração medida: `razao_pico_potencia` ([§8.2.4](#824-verificação-do-vínculo-contra-a-geração-medida)).
- **A pipeline ponta a ponta.** Não há teste que rode `raw → clean → curated` com um dataset de brinquedo. Hoje a garantia é a execução real, que valida a curated antes de gravar.
- **O vínculo usina → ponto de clima** (`haversine_km` e a escolha do ponto mais próximo), coberto indiretamente pelos testes de `gerar_locais` na ingestão.
- **Cobertura medida.** Não há `pytest-cov`; a escolha dos casos é por risco, não por percentual.

---

## 12. Resultados da última execução

Execução de 18/09/2026, partição raw `2026-09-18`.

### 12.1 Arquivos gerados

| Camada | Arquivo | Linhas | Colunas | Tamanho |
|---|---|---|---|---|
| clean | `ons_geracao.parquet` | 1.365.096 | 16 | 5,5 MB |
| clean | `nasa_clima_horario.parquet` | 19.200 | 13 | 0,1 MB |
| clean | `nasa_clima_diario.parquet` | 800 | 14 | <0,1 MB |
| clean | `aneel_usinas.parquet` | 20.511 | 28 | 1,0 MB |
| clean | `manutencao_simulada.parquet` | 1.854 | 15 | 0,1 MB |
| curated | `dim_usina.parquet` | 308 | 24 | <0,1 MB |
| curated | `fato_geracao.parquet` | 575.904 | 6 | 2,4 MB |
| curated | `fato_clima.parquet` | 23.840 | 12 | 0,1 MB |
| curated | `fato_manutencao.parquet` | 159 | 7 | <0,1 MB |
| curated | `ponte_usina_aneel.parquet` | 790 | 8 | <0,1 MB |

### 12.2 Flags de qualidade do ONS clean por fonte

| Fonte | original | faltante | negativo_zerado |
|---|---|---|---|
| eólica | 319.573 | 48 | 11 |
| solar | 256.216 | 48 | 8 |
| hidráulica | 369.528 | 55.176 | 0 |
| térmica | 254.472 | 106.224 | 0 |
| nuclear | 3.792 | 0 | 0 |

### 12.3 `dim_usina` por fonte e região

| Fonte | N | NE | S | SE | Total |
|---|---|---|---|---|---|
| eólica | 1 | 150 | 17 | 2 | 170 |
| solar | 9 | 78 | 4 | 47 | 138 |
| **Total** | 10 | 228 | 21 | 49 | **308** |

### 12.4 Tempo de execução

| Execução | Tempo |
|---|---|
| raw + clean + curated | ~75 s (a limpeza do ONS responde por ~40 s) |
| só curated | ~20 s |
| com `--ingerir` | ~3 min |

---

## 13. Diferenças em relação ao DDL do system design

| DDL (§4.2) | Implementado | Motivo |
|---|---|---|
| `fato_geracao.timestamp` | `timestamp_utc` (com fuso) | Deixa o fuso explícito no nome e no tipo. O ONS está em horário de Brasília, e confundir isso gera erro de 3 h |
| `fato_geracao.energia_mwh NOT NULL` | Pode ser nula, sempre com `flag_qualidade = 'faltante'` | Pedido do overview: "flag explícita onde não fizer sentido interpolar". Apagar a linha esconderia o buraco |
| `fato_geracao` só com energia | + `fonte`, `regiao`, `flag_qualidade` | Overview pede fonte e subsistema no fato. A flag carrega a qualidade |
| `dim_usina.nome NOT NULL`, `regiao NOT NULL` | Cumprido | — |
| `dim_usina.potencia_mw` | Nula quando o vínculo não é confiável | Não publicar potência errada ([§8.2.4](#824-verificação-do-vínculo-contra-a-geração-medida)) |
| `dim_usina` com 9 colunas | 24 colunas | Colunas de auditoria (vínculo, pico, razão, cobertura) |
| `fato_clima.irradiancia` | `irradiancia_kwh_m2` (+ vento 10 m, temp. máx/mín, flag, ponto de referência) | Unidade no nome e aproximação explícita |
| `fato_clima.vento_ms` | Vento a **50 m** | Altura mais representativa para eólica |
| `fato_manutencao.tempo_dias`, `evento_ocorreu` | Cumprido, + `data_primeiro_evento`, `tipo_evento`, `n_usinas_consideradas`, `simulado` | Contexto e aviso de dado sintético |
| Grão de "usina" | Unidade geradora do ONS | Único nível com geração medida ([§8.2.1](#821-a-decisão-de-grão)) |

---

## 14. Decisões de design e trade-offs

1. **Marcar em vez de apagar.** Toda correção deixa rastro (`flag_qualidade`, `flag_data_operacao`, `flag_coordenada`, `qualidade_vinculo`, `metodo_vinculo_clima`). Quem consome decide o que filtrar. É mais trabalho no esquema, mas nenhum problema fica escondido.
2. **Interpolar só buracos curtos (≤ 3 h, ≤ 1 dia).** Buraco curto em série física contínua é bem aproximado por reta. Buraco longo é ausência de informação, e preenchê-lo inventaria dado.
3. **UTC como tempo canônico.** Horário local tem ambiguidade (horário de verão histórico) e muda conforme o fuso de quem lê. UTC não.
4. **Grão da dimensão = unidade do ONS, com vínculo verificado.** Veja [§8.2](#82-dim_usina--curateddim_usinapy). A verificação pico/potência transforma um vínculo heurístico em um vínculo com medida de confiança.
5. **Clean mantém tudo e o curated recorta.** O clean do ONS mantém hidráulica, térmica e nuclear, e o da ANEEL mantém todas as fases. Se o escopo do produto crescer, basta mudar `FONTES_CURATED`, sem refazer a limpeza.
6. **Reprocessamento completo a cada execução.** Assim como na ingestão (system design §9), nada é incremental: cada execução refaz clean e curated do zero a partir da raw. Isso é simples, idempotente e rápido nesta escala. A única memória entre execuções é o mapa de `usina_id`, que é deliberado.
7. **Validação como portão.** A curated só é gravada se passar em todas as checagens, o que espelha o *build-then-swap* do banco.

---

## 15. Como consumir a camada curated

### 15.1 pandas / ds_toolkit

```python
import pandas as pd
import ds_toolkit as dst

dim = pd.read_parquet("dados/limpos/curated/dim_usina.parquet")
ger = pd.read_parquet("dados/limpos/curated/fato_geracao.parquet",
                      filters=[("usina_id", "==", 42)])          # leitura seletiva

confiaveis = dim[dim["qualidade_vinculo"].isin(["exata", "consistente"])]
dst.plot_serie_temporal(ger, "timestamp_utc", "energia_mwh", frequencia="D", agregacao="sum")
```

### 15.2 DuckDB (próxima etapa do projeto)

```sql
CREATE TABLE dim_usina       AS SELECT * FROM read_parquet('dados/limpos/curated/dim_usina.parquet');
CREATE TABLE fato_geracao    AS SELECT * FROM read_parquet('dados/limpos/curated/fato_geracao.parquet');
CREATE TABLE fato_clima      AS SELECT * FROM read_parquet('dados/limpos/curated/fato_clima.parquet');
CREATE TABLE fato_manutencao AS SELECT * FROM read_parquet('dados/limpos/curated/fato_manutencao.parquet');

-- fator de capacidade diário das unidades com potência confiável
SELECT u.usina_id, u.nome, CAST(g.timestamp_utc AS DATE) AS dia,
       SUM(g.energia_mwh) / (u.potencia_mw * 24) AS fator_capacidade
FROM fato_geracao g JOIN dim_usina u USING (usina_id)
WHERE u.potencia_mw IS NOT NULL AND g.flag_qualidade <> 'faltante'
GROUP BY ALL;
```

> Observação: `DuckDB` ainda não está no `requirements.txt`. Ele entra quando a etapa de carga no banco for implementada.

---

## 16. Limitações conhecidas e próximos passos

| # | Limitação | Impacto | Próximo passo |
|---|---|---|---|
| 1 | Vínculo por nome é heurístico: 66 conjuntos inconsistentes e 83 sem vínculo | ~52% das unidades sem potência publicada | Tabela manual de correspondência conjunto → CEGs (`ETL/referencias/`), com prioridade sobre a heurística; ou usar a lista oficial de usinas por conjunto, se o ONS publicar |
| 2 | Clima de 10 pontos para 298 unidades (mediana de 100 km, máximo de 834 km) | Clima aproximado, fraco no Norte (sem ponto) | Gerar `locais.csv` a partir dos centroides da `dim_usina` (um ponto por unidade com coordenada) |
| 3 | Irradiância horária da NASA com ~3 meses de atraso | `nasa_clima_horario` sem irradiância no período recente | Para modelos horários, usar janelas com mais de 3 meses ou outra fonte (por exemplo, INMET) |
| 4 | Dia da NASA diária em hora solar local (≈ Brasília, com até ~1 h de diferença) | Desalinhamento mínimo na borda do dia | Aceitável. Documentado |
| 5 | Agregados "Pequenas Usinas" (63) sem cadastro | Sem potência, localização nem manutenção — mas **32% da energia medida** | **Aceito por natureza:** são somatórios estaduais de MMGD, não usinas, e nenhum vínculo os resolveria ([§8.2.1](#821-a-decisão-de-grão)). Separáveis por `tipo_unidade`: filtro na API (`GET /usinas?tipo_unidade=`), no painel ("Tipo de unidade") e aviso próprio na página da unidade; os endpoints de estimativa respondem 404 dizendo que o grão é agregado, não que o vínculo falhou |
| 6 | `fato_manutencao` herda o 1º evento de N usinas | Conjuntos grandes parecem "falhar antes" | Usar o arquivo simulado por usina para modelagem. Documentado |
| 7 | Limpeza do ONS ~40 s (transformações com `groupby` + `lambda`) | Aceitável hoje; cresce com o histórico | Particionar o clean do ONS por mês e processar só as partições novas |
| 8 | ~~Sem testes automatizados~~ **Resolvido:** 91 testes em `ETL/tests`, sem ler `dados/` ([§11](#11-testes-automatizados)) — `interpolar_gaps_curtos` (3 h interpola, 4 h não, e não pela metade), `completar_grade`, `base_ceg`, `_nucleo`, `_ids_estaveis` e as quatro regras da `validacao` | Resta: o `_vincular` e a pipeline ponta a ponta não têm teste ([§11.2](#112-o-que-não-é-testado)) | Um teste de integração com um cadastro de brinquedo, cobrindo `raw → clean → curated` |
| 9 | Carga no DuckDB ainda não implementada | O banco estático ainda não existe | Etapa `load` na pipeline: gerar `solarwatch.duckdb` a partir da curated, com *build-then-swap* |

---

*Documento gerado em 18/09/2026 a partir do código de `ETL/` e da execução real da pipeline (partição raw 2026-09-18).*
