# Documentação Técnica — Backend (FastAPI)

> Escopo: tudo sobre `backend/` — a API pública de leitura do SolarWatch BR. Cobre os endpoints, o contrato de erro, paginação, rate limiting, observabilidade, carga dos modelos de ML, degradação graciosa, testes, desempenho medido e as decisões (inclusive duas em que precisei divergir do system design, com o motivo).
>
> Documentos relacionados: [banco](../DB/doc_tecnica_db.md) (de onde vêm os dados), [séries temporais](../ML/doc_tecnica_series_temporais.md) e [sobrevivência](../ML/doc_tecnica_analise_sobrevivencia.md) (os modelos servidos), [ETL](../ETL/doc_tecnica_etl.md).

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Estrutura da pasta](#2-estrutura-da-pasta)
3. [Como executar](#3-como-executar)
4. [Configuração](#4-configuração)
5. [Ciclo de vida e carga dos modelos](#5-ciclo-de-vida-e-carga-dos-modelos)
6. [Endpoints](#6-endpoints)
7. [Contrato de erro (Problem Details)](#7-contrato-de-erro-problem-details)
8. [Paginação por cursor](#8-paginação-por-cursor)
9. [Rate limiting](#9-rate-limiting)
10. [Observabilidade](#10-observabilidade)
11. [Segurança](#11-segurança)
12. [Degradação graciosa](#12-degradação-graciosa)
13. [Testes](#13-testes)
14. [Desempenho medido](#14-desempenho-medido)
15. [Problemas encontrados e como foram resolvidos](#15-problemas-encontrados-e-como-foram-resolvidos)
16. [Divergências em relação ao system design](#16-divergências-em-relação-ao-system-design)
17. [Limitações conhecidas](#17-limitações-conhecidas)

---

## 1. Visão geral

Monolito modular em FastAPI (system design §6.3): um único processo serve a API REST e, se a pasta `frontend/` existir, também os arquivos estáticos. É o que cabe em uma instância única de free tier.

```
DuckDB read-only ─┐
                  ├─► backend (FastAPI) ─► JSON ─► frontend / Swagger UI / curl
pickles de ML ────┘
```

| Característica | Valor |
|---|---|
| Framework | FastAPI + Uvicorn |
| Métodos | **Somente `GET`** — não há escrita na API (§2.3) |
| Versionamento | Path: `/api/v1/...` (§5.7) |
| Rotas no OpenAPI | 12 |
| Formato de erro | RFC 9457 (Problem Details) |
| Banco | `DB/solarwatch.duckdb`, aberto read-only |
| Modelos | 4 pickles (`previsao_solar`, `previsao_eolica`, `sobrevivencia_cox`, `sobrevivencia_recorrencia`) |
| Testes | 24, todos passando |

---

## 2. Estrutura da pasta

```
backend/
├── main.py             # cria o app, middlewares, lifespan, monta rotas e estáticos
├── config.py           # configuração via env (prefixo SOLARWATCH_)
├── db.py               # conexão DuckDB read-only + cursor por request
├── modelos_ml.py       # carga dos pickles com registro de falhas
├── schemas.py          # modelos Pydantic (o que vira o OpenAPI)
├── servicos.py         # consultas ao banco (o SQL mora aqui)
├── servicos_ml.py      # previsão e sobrevivência (usa os modelos)
├── erros.py            # handlers → Problem Details
├── paginacao.py        # cursor base64
├── limites.py          # rate limiting (token bucket por IP)
├── observabilidade.py  # logs JSON, request_id e /metrics
├── rotas/
│   ├── usinas.py       # /usinas...
│   ├── geracao.py      # /geracao...
│   └── sistema.py      # /health, /metrics
└── tests/
    ├── test_api.py         # 35 testes de contrato (contra o banco real)
    ├── test_unidades.py    # 50 testes das peças: cursor, erros, limites, métricas, logs
    └── test_modelos_ml.py  # 16 testes do registro de modelos e da conexão
```

A separação **rota → serviço → banco/modelo** mantém as rotas finas: cada função de rota valida entrada, chama um serviço e devolve. O SQL fica concentrado em `servicos.py` e o uso dos modelos em `servicos_ml.py`, o que torna os dois testáveis sem subir a aplicação.

---

## 3. Como executar

```bash
uvicorn backend.main:app --reload          # desenvolvimento (a partir da raiz do repositório)
uvicorn backend.main:app --port 8000       # produção local
pytest backend/tests -q                    # testes
```

| URL | O que é |
|---|---|
| `http://localhost:8000/docs` | Swagger UI (a porta de entrada do projeto para quem chega sem contexto) |
| `http://localhost:8000/redoc` | ReDoc |
| `http://localhost:8000/openapi.json` | Contrato OpenAPI 3.1 |
| `http://localhost:8000/health` | Saúde do serviço |
| `http://localhost:8000/metrics` | Métricas em formato Prometheus |

Pré-requisitos: `DB/solarwatch.duckdb` e os pickles em `ML/modelos/`. Sem o banco, o startup falha com instrução explícita (`rode python -m ETL.pipeline && python -m DB.criar_banco`); sem os modelos, a API sobe em modo degradado ([§12](#12-degradação-graciosa)).

> **Importante rodar a partir da raiz do repositório.** O unpickle dos modelos precisa importar `ML.series_temporais.modelos` e `ML.analise_sobrevivencia.modelos` — as classes não são serializadas junto, só a referência a elas.

---

## 4. Configuração

Tudo em `config.py`, lido de variáveis de ambiente com o prefixo `SOLARWATCH_` (ou do `.env` da raiz).

| Variável | Padrão | Para que serve |
|---|---|---|
| `SOLARWATCH_BANCO` | `DB/solarwatch.duckdb` | Caminho do banco |
| `SOLARWATCH_MODELOS` | `ML/modelos` | Pasta dos pickles |
| `SOLARWATCH_FRONTEND` | `frontend/` | Se existir, é servida em `/app` |
| `SOLARWATCH_PREFIXO_API` | `/api/v1` | Prefixo de versão |
| `SOLARWATCH_RATE_LIMIT` | `60/minute` | Limite por IP |
| `SOLARWATCH_LIMITE_PADRAO_PAGINA` | `50` | Itens por página |
| `SOLARWATCH_LIMITE_MAXIMO_PAGINA` | `200` | Teto de `limit` |
| `SOLARWATCH_MAXIMO_DIAS_SERIE` | `120` | Janela máxima de série temporal |
| `SOLARWATCH_ORIGENS_PERMITIDAS` | localhost:5173, :3000 | CORS |
| `SOLARWATCH_AMBIENTE` | `desenvolvimento` | Aparece em `/health` |

O teto de janela (`maximo_dias_serie`) existe para proteger a CPU do free tier: sem ele, um `GET /usinas/1/geracao?inicio=2000-01-01` varreria a base inteira a cada request.

---

## 5. Ciclo de vida e carga dos modelos

```python
@asynccontextmanager
async def ciclo_de_vida(app):
    db.abrir(cfg.banco)                      # falha aqui é fatal: sem banco não há API
    registro.carregar(cfg.modelos, ...)      # falha aqui apenas degrada (§10.5)
    yield
    db.fechar()
```

- **Banco:** uma conexão read-only aberta no startup; cada request recebe um `cursor()` próprio via dependência do FastAPI. É assim que o DuckDB permite concorrência sobre o mesmo arquivo sem reabrir a conexão a cada chamada.
- **Modelos:** carregados uma vez (§8.4 do system design, "dataset e modelo em memória do processo"). Cada falha é registrada em `registro.falhas` com tipo e mensagem, e aparece em `/health`.
- **Metadados dos modelos:** o `.meta.json` gravado ao lado de cada pickle (por `ds_toolkit.salvar_modelo`) também é lido, e as métricas de backtesting viajam na resposta de `/previsao`. Assim quem consome a previsão vê o erro esperado do modelo junto com o número previsto.

---

## 6. Endpoints

Todos sob `/api/v1`, exceto `/health` e `/metrics` (que respondem também sem prefixo).

### 6.1 `GET /usinas`

Lista paginada. Filtros: `fonte` (`solar`|`eolica`), `regiao` (`N`|`NE`|`SE`|`S`), `tipo_unidade` (`usina`|`conjunto`|`pequenas_usinas`). Paginação: `limit` (1–200) e `cursor`.

```json
{
  "data": [{
    "usina_id": 180, "nome": "PQU ALPAL MMGD", "fonte": "solar", "regiao": "NE",
    "id_estado": "AL", "municipio": null, "potencia_mw": null, "lat": null, "lon": null,
    "data_operacao": null, "tipo_unidade": "pequenas_usinas", "qualidade_vinculo": "sem_vinculo"
  }],
  "next_cursor": "MTgw",
  "total_estimado": 78
}
```

> Campos nulos são reais, não erro: 212 das 308 unidades não têm potência confiável. O campo `qualidade_vinculo` permite ao cliente filtrar ([ETL §8.2.4](../ETL/doc_tecnica_etl.md#824-verificação-do-vínculo-contra-a-geração-medida)).

**`tipo_unidade` separa dois nulos de naturezas opostas** ([ETL §8.2.1](../ETL/doc_tecnica_etl.md#821-a-decisão-de-grão)):

- **149 unidades** `conjunto` com vínculo incompleto ou inconsistente: a potência **existe** na ANEEL e não pôde ser atribuída com confiança. Melhorar o vínculo resolve.
- **63 unidades** `pequenas_usinas`: agregados estaduais de MMGD. O ONS publica a soma da geração distribuída de um estado numa linha só; não há usina na ANEEL para vincular, e nunca haverá.

O exemplo acima é justamente um agregado. `GET /usinas?tipo_unidade=conjunto` (ou `usina`) devolve só as unidades com cadastro, e os três filtros se combinam. Por isso os endpoints de estimativa distinguem os dois casos no 404 — ver [§6.6](#66-get-usinasidsobrevivencia).

### 6.2 `GET /usinas/{id}` · `GET /usinas/{id}/geracao` · `GET /usinas/{id}/clima`

- **Detalhe:** o mesmo da lista, mais cobertura da série e dados do vínculo.
- **Geração:** série horária (`inicio`/`fim` opcionais), com `total_mwh`, `horas` e, em cada ponto, `flag_qualidade`. Horas sem medição **aparecem** com `energia_mwh: null` e flag `faltante` — o buraco fica visível em vez de sumir.
- **Clima:** cada ponto traz `flag_qualidade` e `medidas_faltantes`, a lista das variáveis sem valor naquele dia. É o que permite ao cliente dizer "irradiância indisponível nos últimos 5 dias" em vez de desenhar uma lacuna sem explicação — a NASA publica vento e temperatura com ~2 dias de atraso e a irradiância diária com ~1 semana, então o fim da série tem parte das variáveis ausente por construção.
- **Clima:** série diária do ponto NASA de referência, com `local_clima`, `distancia_km`, `metodo_vinculo_clima` e um `aviso` de que o clima não é da coordenada exata da usina. Usina sem ponto de referência (as 10 do subsistema Norte) recebe 404 explicando o motivo.

### 6.3 `GET /geracao/nacional`

Agregado por fonte, com `granularidade=dia` (fechado em horário de Brasília) ou `hora`, filtros de período e fonte.

### 6.4 `GET /geracao/previsao?fonte=solar`

Previsão de 24 h da geração agregada da fonte — **esta é a previsão que o modelo realmente produz**.

```json
{
  "fonte": "solar", "modelo": "gradient_boosting", "horizonte_h": 24,
  "origem": "2026-09-17T23:00:00-03:00", "metodo": "modelo_por_fonte",
  "premissa_clima": "O clima do horizonte é aproximado pelo último dia observado: em produção entraria uma previsão meteorológica.",
  "metricas_backtesting": {"rmse": 1079.2, "mae": 666.0, "mape_%": 5.74, "ganho_vs_baseline_%": 46.77, "janelas": 6},
  "data": [{"timestamp": "2026-09-18T00:00:00-03:00", "energia_mwh_prevista": 60.36}, "..."]
}
```

`origem` é a última hora observada no modelo: a previsão avança a partir dali, não de "agora". Como o banco é estático, isso deixa claro que a previsão envelhece junto com o ETL.

### 6.5 `GET /usinas/{id}/previsao`

Mesma estrutura, com `usina_id`, `metodo: "rateio_proporcional"` e `participacao_usina`.

> **Aproximação declarada:** o modelo é por fonte, não por usina. Aqui a previsão da fonte é multiplicada pela participação da usina na geração daquela fonte nos últimos 30 dias. O campo `metodo` existe justamente para o cliente saber que não é um modelo dedicado.

### 6.6 `GET /usinas/{id}/sobrevivencia`

```json
{
  "usina_id": 12, "fonte": "eolica", "idade_anos": 11.96,
  "condicional_na_idade": true, "risco_relativo": 2.15, "tempo_mediano_anos": 2.56,
  "metodo_extrapolacao": "weibull", "simulado": true,
  "aviso": "Os eventos de manutenção que treinaram este modelo são SINTÉTICOS ...",
  "horizontes": [
    {"horizonte_meses": 6,  "horizonte_dias": 183, "probabilidade_sobrevivencia": 0.633},
    {"horizonte_meses": 12, "horizonte_dias": 365, "probabilidade_sobrevivencia": "..."}
  ]
}
```

Três campos existem para não enganar quem consome:

- `simulado: true` e `aviso`: o dado de manutenção é sintético, e isso viaja no payload, não só na documentação;
- `condicional_na_idade`: a probabilidade é *dado que a usina já operou `idade_anos` sem evento*;
- `metodo_extrapolacao`: `cox` dentro do suporte observado, `weibull` além dele ([sobrevivência §10](../ML/doc_tecnica_analise_sobrevivencia.md#10-o-bug-da-extrapolação-e-como-foi-resolvido)).

O endpoint usa a tabela pré-calculada quando a usina está nela (rápido) e roda o modelo quando há horizontes customizados via `?horizontes=3&horizontes=9`.

Sem potência ou data de operação não há como estimar, e o 404 diz **qual** dos dois motivos é — porque os encaminhamentos são opostos:

| Caso | Mensagem | O que o cliente faz |
|---|---|---|
| `conjunto` com vínculo incompleto | "não tem potência ou data de operação confiáveis (vínculo ONS×ANEEL incompleto)" | nada hoje; pode ganhar cadastro quando o vínculo melhorar |
| `pequenas_usinas` | "é um agregado estadual de pequenas usinas (MMGD / Tipo III)... não existe cadastro na ANEEL" + sugere `tipo_unidade=` | filtra esse grão da lista; não há o que esperar |

O frontend mostra a mensagem da API no lugar do card, então a distinção chega à tela sem código duplicado ([frontend §6.2](../frontend/doc_tecnica_frontend.md)). `/recorrencia` segue a mesma regra.

### 6.7 `GET /usinas/{id}/recorrencia`

```json
{
  "usina_id": 12, "fonte": "eolica", "idade_anos": 11.99,
  "taxa_relativa": 1.65, "modelo": "andersen_gill + MCF",
  "metodo": "Número esperado = [MCF da fonte no horizonte] × exp(β do Andersen-Gill · x) ...",
  "simulado": true, "aviso": "Os eventos de manutenção ... são SINTÉTICOS ...",
  "horizontes": [
    {"horizonte_meses": 6,  "horizonte_dias": 183, "manutencoes_esperadas": 0.237},
    {"horizonte_meses": 12, "horizonte_dias": 365, "manutencoes_esperadas": 0.546}
  ]
}
```

**É a outra metade da pergunta.** O `/sobrevivencia` responde *"a usina chega ao fim do horizonte sem **nenhuma** manutenção?"* e, depois da primeira, não tem mais nada a dizer — trata como iguais a usina nova e a que já foi reparada cinco vezes. Este responde *"**quantas** manutenções esperar?"*, com a usina permanecendo sob risco depois de cada reparo. Vem do modelo de recorrência (Andersen-Gill + função média cumulativa, [sobrevivência §14](../ML/doc_tecnica_analise_sobrevivencia.md#14-eventos-recorrentes-andersen-gill-e-pwp)), não do Cox de 1º evento.

Duas diferenças de contrato que valem atenção de quem consome:

- `manutencoes_esperadas` é uma **contagem**, não uma probabilidade: pode passar de 1 e **cresce** com o horizonte, ao contrário de `probabilidade_sobrevivencia`, que decresce. Há um teste que trava cada um desses sentidos.
- `metodo` carrega a aproximação assumida no próprio payload: o multiplicador da usina é aplicado sobre uma média marginal da fonte, então o número serve para **ordenar** usinas e dar ordem de grandeza, não como compromisso de contagem. O frontend mostra esse texto no *tooltip* de "Como é calculado".

Os dois endpoints são coerentes entre si, e isso é testado: quando a contagem esperada passa de 1,0, a probabilidade de zero manutenções tem que estar abaixo de 0,75. Para a usina 12, 0,55 manutenção esperada em 12 meses convive com 67% de chance de nenhuma — a leitura de Poisson daria e^(−0,55) ≈ 0,58, e a diferença é justamente o que o processo de renovação tem de diferente do Poisson.

Como o `/sobrevivencia`, usa a tabela pré-calculada quando a usina está nela e roda o modelo quando há horizontes customizados.

### 6.8 `GET /health` e `GET /metrics`

`/health` devolve `ok` ou `degradado`, a cobertura do banco e o estado de cada modelo. `/metrics` devolve texto no formato Prometheus. Os dois ficam **fora** do rate limit, para não bloquear monitoramento.

---

## 7. Contrato de erro (Problem Details)

Todo erro responde com `application/problem+json` e o mesmo corpo (RFC 9457, §5.4):

```json
{
  "type": "https://solarwatch.example/errors/not-found",
  "title": "Recurso não encontrado",
  "status": 404,
  "detail": "Nenhuma usina com id=9999",
  "instance": "/api/v1/usinas/9999",
  "request_id": "cf9918ddd5dd"
}
```

| Código | Quando | `type` |
|---|---|---|
| 404 | Usina inexistente; usina sem ponto de clima | `/errors/not-found` |
| 422 | Parâmetro inválido, janela grande demais, cursor corrompido | `/errors/validation-error` |
| 429 | Rate limit estourado | `/errors/rate-limit` |
| 503 | Modelo indisponível | `/errors/service-unavailable` |
| 500 | Erro não tratado | `/errors/internal-error` |

O `request_id` no corpo fecha o ciclo com os logs: um usuário reporta o erro com esse id e a linha exata aparece no log estruturado. No 500, o detalhe técnico fica **apenas** no log — o corpo devolve uma mensagem genérica, para não vazar caminho de arquivo ou consulta SQL.

---

## 8. Paginação por cursor

`cursor = base64(usina_id do último item)`, como manda §5.5. A consulta é `WHERE usina_id > ? ORDER BY usina_id LIMIT ?`, e o backend busca `limit + 1` linhas para saber se existe próxima página sem contar a tabela toda.

Vantagem sobre offset: se a base mudar entre duas páginas (um novo ETL), o cursor continua apontando para a posição correta, enquanto o offset pularia ou repetiria itens. Um teste verifica que duas páginas consecutivas não têm interseção.

Cursor inválido devolve **422** com o valor recebido no `detail`, em vez de silenciosamente devolver a primeira página.

---

## 9. Rate limiting

**60 requisições por minuto por IP**, com token bucket em memória do processo.

| Header | Em toda resposta |
|---|---|
| `X-RateLimit-Limit` | 60 |
| `X-RateLimit-Remaining` | tokens restantes |
| `X-RateLimit-Reset` | segundos até o próximo token |
| `Retry-After` | só no 429 |

Detalhes da implementação (`backend/limites.py`):

- **Token bucket** em vez de janela fixa: a reposição é contínua (`limite/período` por segundo), então um cliente bem-comportado nunca leva bloqueio artificial na virada do minuto.
- **Um balde por IP**, em dicionário de processo. Não sobrevive a redeploy nem escalaria para várias instâncias — aceitável com instância única (§3.6), e declarado.
- **Descarte de baldes inativos** acima de 1.000 entradas, para um scraping com IPs variados não crescer a memória sem limite.
- `/health` e `/metrics` isentos.

Por que não `slowapi`, como o design previa: [§16](#16-divergências-em-relação-ao-system-design).

---

## 10. Observabilidade

### 10.1 Logs estruturados

Uma linha JSON por request, no stdout (§12.1):

```json
{"timestamp": "2026-09-20T19:44:16-0300", "nivel": "INFO", "logger": "backend.acesso",
 "mensagem": "GET /api/v1/usinas -> 200", "method": "GET", "path": "/api/v1/usinas",
 "rota": "/usinas", "status_code": 200, "latency_ms": 43.13, "client_ip": "127.0.0.1",
 "request_id": "72953194bd02"}
```

O campo `rota` guarda o **template** (`/usinas/{usina_id}`), não o path concreto, o que permite agrupar sem cardinalidade explosiva. Headers completos nunca são logados. O access log do Uvicorn é desligado para não duplicar.

### 10.2 Request ID

Gerado por request (ou reaproveitado do header `X-Request-ID`, se o cliente mandar), devolvido no header e incluído no corpo dos erros.

### 10.3 Métricas

`/metrics` expõe, em formato Prometheus:

- `solarwatch_uptime_segundos`
- `solarwatch_requisicoes_total{metodo,rota,status}`
- `solarwatch_latencia_ms_{sum,count,bucket}` por rota

São contadores em memória, sem dependência externa: o orçamento zero não comporta um stack de observabilidade 24/7, então as métricas ficam disponíveis para inspeção sob demanda (§12.2).

---

## 11. Segurança

| Item | Implementação |
|---|---|
| **CORS** | Lista explícita de origens, nunca `*`, e só o método `GET` (§11, API8) |
| **Sem autenticação** | Por decisão de escopo (§2.3): dado 100% público |
| **Sem escrita** | Nenhuma rota além de `GET`; o banco é aberto read-only |
| **Rate limit** | Proteção contra scraping/loop de erro (API4) |
| **Erros opacos** | 500 nunca expõe stack trace ou SQL |
| **Validação** | Pydantic + `pattern` nos enums; janela de série limitada |
| **Dados pessoais** | Nenhum: a `dim_usina` usa nomes do ONS, e os nomes de pessoas físicas da ANEEL ficam na camada clean, fora do banco |

---

## 12. Degradação graciosa

System design §10.5: se um modelo falhar, a API **não** cai — só os endpoints dependentes respondem 503.

| Situação | `/usinas`, `/geracao` | `/previsao` | `/sobrevivencia` | `/recorrencia` | `/health` |
|---|---|---|---|---|---|
| Tudo ok | 200 | 200 | 200 | 200 | `ok` |
| Pickle de previsão ausente | 200 | **503** | 200 | 200 | `degradado` |
| Pickle de sobrevivência ausente | 200 | 200 | **503** | 200 | `degradado` |
| Pickle de recorrência ausente | 200 | 200 | 200 | **503** | `degradado` |
| Banco ausente | startup falha com instrução | — | — | — | — |

O 503 traz no `detail` o motivo real (`FileNotFoundError: ...`), o que encurta o diagnóstico. Há um teste que remove o modelo em tempo de execução e confirma os dois lados: 503 no endpoint do modelo e 200 no endpoint histórico.

---

## 13. Testes

`pytest backend/tests -q` → **101 testes**, ~70 s, em três arquivos:

| Arquivo | Escopo | Testes |
|---|---|---|
| `test_api.py` | contrato da API contra o banco real (read-only, sem efeito colateral); pula com mensagem explicativa se o banco não existir | 35 |
| `test_unidades.py` | as peças que sustentam o contrato, em isolamento: cursor, Problem Details, rate limit, métricas, logs, janela de datas, configuração | 50 |
| `test_modelos_ml.py` | registro de modelos e conexão com o banco — a degradação graciosa do §10.5, com modelos de mentira em `tmp_path` | 16 |

**O que só o teste isolado alcança:** um cursor corrompido, um balde de tokens vazio, um pickle truncado, uma tabela pré-calculada sem a coluna de índice, 1.000 IPs diferentes para ver o descarte de baldes inativos. Nada disso é provocável pelo banco real, e é exatamente onde o código erra.

Três achados dos testes de unidade:

- **o cursor aceitava lixo.** `base64.urlsafe_b64decode` ignora o que vem depois do padding, então `MTI=qualquercoisa` decodificava para `12` em silêncio e a API paginava a partir de um id que ninguém pediu. O `decodificar` passou a exigir a **forma canônica** (o cursor tem de ser exatamente o que `codificar` produziria), e o teste do caso ficou;
- **o log de acesso não vaza header.** Há um teste que manda `Authorization: Bearer segredo-123` e verifica que nem o valor nem o nome do header aparecem no JSON do log (§12.1);
- **o balde repõe de forma contínua e não acumula além da capacidade** — uma hora de inatividade não dá 3.600 tokens de crédito.

| Grupo do `test_api.py` | O que cobre |
|---|---|
| Sistema | `/health`, `/metrics`, `X-Request-ID`, OpenAPI com as rotas versionadas |
| Usinas | limite, **cursor sem interseção entre páginas**, filtros, 404 em Problem Details |
| Validação | `limit=0`, `limit=500`, `fonte=nuclear`, cursor corrompido, janela > 120 dias, `inicio > fim` |
| Séries | geração (contagem e campos), nacional em hora e dia |
| Modelos | previsão por fonte (24 pontos), rateio por usina declarado, sobrevivência com aviso de dado simulado e **probabilidade monotônica no horizonte** |
| Resiliência | modelo removido → 503 só no endpoint dependente |
| Rate limit | unitário do token bucket (esvazia, é por IP, repõe com o tempo) + integração 429 em app isolado |

Dois detalhes de desenho dos testes: o teste de 429 usa um **app separado** com limite de 2/min, para não gastar o orçamento dos demais; e a verificação de modelo disponível é feita **dentro** do teste, porque no momento do import o `lifespan` ainda não rodou.

---

## 14. Desempenho medido

Uvicorn local, melhor de 3 execuções:

| Endpoint | Latência | Orçamento (§2.2) |
|---|---|---|
| `/health` | 11 ms | ✅ |
| `/usinas?limit=50` | 33 ms | ✅ |
| `/usinas/12/geracao` (15 dias) | 62 ms | ⚠️ |
| `/usinas/12/sobrevivencia` | 27 ms | ✅ |
| `/usinas/12/recorrencia` | 25 ms | ✅ |
| `/geracao/previsao?fonte=solar` | 97 ms | ❌ |
| `/geracao/nacional?granularidade=dia` | 170 ms | ❌ |

O alvo do system design é **50 ms (p50)**. As três últimas passam disso, por motivos diferentes:

- `/geracao/nacional` varre as 576 mil linhas a cada chamada;
- `/previsao` reconstrói as features e roda o Gradient Boosting por request;
- a série de 15 dias paga a conversão DuckDB → DataFrame → JSON de 360 pontos.

Como o banco é **estático**, todos os três são resolvíveis por cache: a resposta só muda quando o ETL roda. É o item 1 da [§17](#17-limitações-conhecidas).

---

## 15. Problemas encontrados e como foram resolvidos

### 15.1 O rate limit do `slowapi` não funcionava (silenciosamente)

**Sintoma:** 65 requisições seguidas, todas 200. Nenhum header `X-RateLimit-*`.

**Diagnóstico:** o `SlowAPIMiddleware` descobre a rota com `_find_route_handler`, que procura o atributo `.endpoint` nas rotas do app. Esta versão do FastAPI envolve roteadores incluídos via `include_router` em objetos `_IncludedRouter`, que **não expõem** `.endpoint`. O handler vinha `None`, `_should_exempt` devolvia `True` e o limite nunca era checado — sem erro, sem aviso.

**Correção:** token bucket próprio (`backend/limites.py`, ~60 linhas), que cumpre o que §5.9 especifica. O `slowapi` saiu do `requirements.txt`.

**Lição:** proteção de segurança sem teste é proteção que você *acha* que tem. O teste de 429 entrou junto com a correção.

### 15.2 `NaN`/`NaT` do pandas quebravam a serialização

**Sintoma:** `GET /usinas` devolvia 500 com `TypeError: 'float' object cannot be interpreted as an integer`.

**Causa:** DuckDB → DataFrame → `to_dict()` mantém `NaN` e `NaT`, que o Pydantic não converte para `null` (e `NaN` nem sequer é JSON válido). Isso aparece porque o banco tem nulos legítimos: potência e data de usinas sem vínculo confiável.

**Correção:** um único helper `_registros(df)` em `servicos.py`, usado por todas as consultas, que converte `NaN`/`NaT` em `None`.

### 15.3 `metricas_backtesting` sempre nulo

O campo lia um atributo `metricas_` que o pickle não tem — as métricas ficam no `.meta.json` ao lado. Agora o `RegistroModelos` lê esse arquivo no startup e a resposta de `/previsao` traz RMSE, MAPE e ganho sobre o baseline junto com a previsão.

### 15.4 Teste pulado por avaliação no import

O `skipif` consultava o registro de modelos no momento do import, quando o `lifespan` ainda não rodou — o teste era sempre pulado. Passou a checar o status da resposta dentro do teste.

---

## 16. Divergências em relação ao system design

| Item | Design | Implementado | Motivo |
|---|---|---|---|
| Rate limiting | `slowapi` | Token bucket próprio | O `slowapi` é incompatível com esta versão do FastAPI e desativava o limite sem avisar ([§15.1](#151-o-rate-limit-do-slowapi-não-funcionava-silenciosamente)) |
| `/usinas/{id}/previsao` | Previsão por usina | Rateio da previsão da fonte, declarado em `metodo` | O modelo foi treinado no agregado por fonte ([séries temporais §15](../ML/doc_tecnica_series_temporais.md#15-limitações-conhecidas)) |
| Resposta de `/geracao` | Array puro | Objeto com metadados + `data` | Permite devolver `total_mwh`, cobertura e o nome da usina sem uma segunda chamada |
| Resposta de `/sobrevivencia` | `horizonte_dias[]` + `probabilidade[]` | Lista de objetos + campos de contexto | Dois arrays paralelos são fáceis de desalinhar; e os campos `simulado`, `aviso` e `metodo_extrapolacao` precisavam existir |
| Latência | 50 ms p50 | 11–170 ms conforme o endpoint | Volume real ficou 600× acima do estimado no design ([banco §14](../DB/doc_tecnica_db.md#14-diferenças-em-relação-ao-ddl-do-system-design)) |

---

## 17. Limitações conhecidas

| # | Limitação | Impacto |
|---|---|---|
| 1 | Sem cache de resposta | 3 endpoints ficam acima do orçamento de latência ([§14](#14-desempenho-medido)); o dado só muda no ETL, então a repetição é desperdício puro |
| 2 | Previsão por usina é **rateio** da previsão da fonte | Precisão limitada por usina; o campo `metodo` e `participacao_usina` declaram a aproximação na resposta |
| 3 | Rate limit em memória do processo | Não sobrevive a redeploy nem a múltiplas instâncias (aceitável com instância única, que é o desenho atual) |
| 4 | Sem CI | Os 101 testes rodam só localmente |
| 5 | Sem cabeçalhos de cache HTTP (`Cache-Control`, `ETag`) | O cliente não sabe que o dado é estático e recarrega sempre |
| 6 | `/metrics` aberto | Qualquer um lê as métricas; não há dado sensível ali, mas em produção seria restrito |
| 7 | Sem testes de carga | O número de 100 req/s do design nunca foi verificado |
| 8 | Endpoints de estimativa cobrem 93 das 308 unidades | As demais não têm potência ou data de operação confiáveis, e recebem 404 explicando qual dos dois motivos é ([§6.6](#66-get-usinasidsobrevivencia)) |

---

*Documento gerado em 20/09/2026 a partir do código de `backend/`, da execução real dos testes e das medições de latência no Uvicorn local. Revisado em 06/10/2026: filtro `tipo_unidade`, 101 testes (§13) e situação atual das limitações.*
