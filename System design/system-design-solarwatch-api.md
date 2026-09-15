# System Design — API SolarWatch BR

> Documento de design técnico (nível RFC interno). Escopo: API pública de leitura sobre dados de geração de energia solar/eólica no Brasil, backend FastAPI + frontend estático, deploy single-instance no Render free tier, banco DuckDB estático.

---

## 1. Sumário Executivo

O problema é servir dados históricos e derivados (geração real, clima, previsão de série temporal, sobrevivência de ativos) de um domínio de energia renovável, para um público de baixíssimo volume real (recrutadores avaliando um portfólio) mas que precisa **se comportar e se documentar como um sistema de produção real**, não como um script. A restrição dominante do projeto não é escala — é orçamento zero: instância única no Render free tier, que hiberna sem uso, banco estático em arquivo (DuckDB), sem serviços gerenciados pagos.

A solução proposta é um **monolito modular em FastAPI**, servindo tanto a API REST quanto os artefatos estáticos do frontend (JS+HTML+CSS) a partir do mesmo processo, lendo um arquivo DuckDB read-only reconstruído offline por um pipeline ETL batch. Não há banco de escrita em runtime, não há fila, não há cache distribuído — cada uma dessas ausências é uma decisão, não uma omissão, e está justificada seção por seção.

Os três trade-offs principais: (1) **disponibilidade sacrificada por custo** — a instância dorme e o primeiro request após hibernação paga um cold start de segundos, o que é aceitável porque o "SLO" real é "o link funciona quando alguém clica", não uptime contínuo; (2) **consistência forte trivial, ao custo de dados desatualizados por design** — o banco só muda quando o ETL roda manualmente, então não existe race condition de escrita, mas também não existe "tempo real"; (3) **superfície de auth zero** — a API é pública sem autenticação, o que simplifica tudo a jusante (sem gestão de token, sem rate limit por identidade) mas expõe a API a qualquer scraping/abuso, mitigado só por rate limit por IP.

---

## 2. Requisitos

### 2.1 Funcionais

1. `GET /usinas` — lista o cadastro de usinas, com filtros por fonte (solar/eólica) e região.
2. `GET /usinas/{id}/geracao` — série histórica de geração de energia de uma usina.
3. `GET /geracao/nacional` — agregado nacional de geração, filtrável por fonte e período.
4. `GET /usinas/{id}/clima` — série de irradiância solar/velocidade de vento da usina.
5. `GET /usinas/{id}/previsao` — previsão de série temporal de geração futura.
6. `GET /usinas/{id}/sobrevivencia` — probabilidade de sobrevivência (Cox) até o próximo evento de manutenção.
7. Servir os arquivos estáticos do frontend (JS/HTML/CSS) a partir do mesmo backend.

### 2.2 Não-funcionais

| Métrica | Valor |
|---|---|
| Usuários ativos simultâneos (pico otimista) | 20 |
| QPS médio | 30 req/s |
| QPS de pico | 100 req/s |
| Razão leitura/escrita | 100:1 (na prática, ~∞:1 em runtime — ver §2.4) |
| Latência alvo | 50 ms por endpoint crítico (p50, medido no processo, sem contar cold start) |
| Disponibilidade | melhor esforço — instância hiberna sem tráfego; sem SLA |
| Volume de dados | ~1000 registros, sem crescimento em runtime (crescimento só em rebuild do ETL) |
| Consistência | forte, trivialmente — banco é read-only em produção |
| Durabilidade | RPO = até a última execução do ETL local; RTO = tempo de novo deploy (minutos) |
| Picos previsíveis | horário comercial (quando recrutador avalia) |
| Distribuição geográfica | single-region (Render US ou região única disponível no free tier) |
| Compliance | LGPD — sem coleta de dado pessoal no scraping; segredos em `.gitignore` |

### 2.3 Fora de escopo

- Escrita via API (não existe endpoint de mutação; todo dado entra via pipeline ETL offline).
- Autenticação/autorização de qualquer tipo.
- Multi-tenancy.
- Atualização de dados em tempo real (o dado é tão "fresco" quanto a última execução manual do ETL).
- Alta disponibilidade / multi-região.

### 2.4 Suposições declaradas

- **SUPOSIÇÃO**: os números de 30 req/s médio e 100 req/s de pico, para 20 usuários simultâneos avaliando um portfólio, são consideravelmente mais altos do que o padrão de navegação humana (isso daria ~1,5 req/s por usuário sustentado). Trato esses números como **meta de capacidade para fins de exercício de dimensionamento** (é literalmente o objetivo do projeto testar isso), não como projeção realista de tráfego. O design é dimensionado para aguentar esse número caso ocorra (ex.: um scraper de recrutador, um teste de carga que você mesmo rode), mas a infraestrutura real (free tier) não tem como sustentar 100 req/s por muito tempo sem degradar — isso é discutido em §8 e §10.
- **SUPOSIÇÃO**: "razão leitura/escrita 100:1" é interpretada como leitura de API vs. escrita do pipeline ETL (que roda manualmente, fora do runtime da API) — não existe escrita em runtime.
- **SUPOSIÇÃO**: dados de manutenção/falha de ativos (usados no modelo de sobrevivência) são sintéticos, conforme já definido no overview do projeto; a API os serve como se fossem dado real de negócio, mas o README do repositório deixa isso explícito.
- **SUPOSIÇÃO**: "1000 registros" refere-se ao total de linhas nas tabelas-fato relevantes para consulta via API (não ao dataset bruto de ingestão, que pode ser maior antes de agregação).

---

## 3. Estimativa de Capacidade

### 3.1 QPS

- QPS médio dado: **30 req/s**
- QPS de pico dado: **100 req/s**
- Fórmula geral (para referência, caso os números mudem): `QPS_médio = usuários_simultâneos × requests_por_usuário_por_segundo`. Com 20 usuários e 30 req/s médio, isso implica 1,5 req/s por usuário — plausível apenas se o frontend fizer polling ou disparar várias chamas por interação (ex.: um dashboard que atualiza 4 gráficos por vez). Assumo esse padrão de "N chamadas por page view" como explicação do número.

### 3.2 Throughput leitura vs. escrita

- Leitura (runtime, via API): 100% do QPS acima — 30 req/s médio / 100 req/s pico.
- Escrita (ETL, fora do runtime): 1 execução manual a cada atualização de dado — não entra em QPS, é batch.

### 3.3 Storage

- ~1000 registros nas tabelas-fato de consulta. Estimando ~200 bytes/registro (float64 x poucas colunas + strings curtas) → **~200 KB** de dado "quente" de consulta.
- Arquivo DuckDB completo (incluindo dimensões, índices internos, overhead do formato colunar): estimo **1–5 MB** no total, mesmo com folga generosa — trivial para caber em memória inteira do processo.
- Sem crescimento em runtime → sem necessidade de planejar particionamento por tempo/volume.
- Sem replicação (instância única) → esse número não muda com fator de replicação.

### 3.4 Bandwidth

- Payload médio de resposta JSON estimado: 1–5 KB (uma série histórica de usina única, não o dataset inteiro).
- Entrada: desprezível (todos os endpoints são `GET` sem body relevante, só query params).
- Saída no pico: `100 req/s × 5 KB ≈ 500 KB/s ≈ 4 Mbps`. Bem dentro do que uma instância free tier de Render aguenta em rede.

### 3.5 Memória para cache

- Regra 80/20 não se aplica bem aqui porque o dataset inteiro (§3.3) já cabe várias ordens de grandeza abaixo da RAM disponível mesmo no free tier (tipicamente 512 MB). **Decisão: carregar o DuckDB inteiro em memória no startup do processo (via conexão in-memory carregada a partir do arquivo, ou mantendo o arquivo mapeado) em vez de fazer cache seletivo.** Isso elimina a necessidade de qualquer política de invalidação de cache — não há o que invalidar, o processo só recarrega no próximo deploy.

### 3.6 Instâncias

- **1 instância**, Render free tier (tipicamente 512 MB RAM / CPU compartilhada). Suficiente pelos números acima. Sem autoscaling (não existe no free tier, e não seria necessário mesmo se existisse).

### Tabela resumo

| Métrica | Valor estimado |
|---|---|
| QPS médio / pico | 30 / 100 req/s |
| Payload médio | 1–5 KB |
| Bandwidth de pico | ~4 Mbps |
| Storage total (DuckDB) | 1–5 MB |
| Memória necessária | < 50 MB (dado + processo Python), folga enorme nos 512 MB do free tier |
| Instâncias | 1, sem autoscaling |

---

## 4. Modelo de Domínio e Dados

### 4.1 Entidades e relacionamentos

```
dim_usina (1) ──< (N) fato_geracao
dim_usina (1) ──< (N) fato_clima
dim_usina (1) ──< (N) fato_manutencao
```

Todas as tabelas-fato referenciam `dim_usina` por `usina_id`. Não há relacionamento N:N no domínio — é uma estrela simples, de propósito (ver §4.6).

### 4.2 Esquema (DDL DuckDB)

```sql
CREATE TABLE dim_usina (
    usina_id        UBIGINT PRIMARY KEY,
    nome            VARCHAR NOT NULL,
    fonte           VARCHAR NOT NULL,        -- 'solar' | 'eolica'
    regiao          VARCHAR NOT NULL,
    municipio       VARCHAR,
    potencia_mw     DOUBLE,
    lat             DOUBLE,
    lon             DOUBLE,
    data_operacao   DATE
);

CREATE TABLE fato_geracao (
    usina_id        UBIGINT REFERENCES dim_usina(usina_id),
    timestamp       TIMESTAMP NOT NULL,
    energia_mwh     DOUBLE NOT NULL,
    PRIMARY KEY (usina_id, timestamp)
);

CREATE TABLE fato_clima (
    usina_id        UBIGINT REFERENCES dim_usina(usina_id),
    data             DATE NOT NULL,
    irradiancia      DOUBLE,
    vento_ms         DOUBLE,
    temperatura_c    DOUBLE,
    PRIMARY KEY (usina_id, data)
);

CREATE TABLE fato_manutencao (
    usina_id         UBIGINT REFERENCES dim_usina(usina_id),
    tempo_dias       INTEGER NOT NULL,   -- tempo até evento ou censura
    evento_ocorreu   BOOLEAN NOT NULL,   -- 0 = censurado, 1 = evento observado
    PRIMARY KEY (usina_id)
);
```

### 4.3 Estratégia de ID

**Escolha: `UBIGINT` sequencial simples (não UUID/ULID/Snowflake).**

- Alternativas consideradas: UUIDv4 (padrão comum em APIs distribuídas), ULID (ordenável por tempo).
- Por que a escolhida venceu: o dataset é estático, gerado por um único processo ETL, sem geração distribuída de ID e sem necessidade de esconder volume/ordem de criação (não é dado sensível, é cadastro público de usina). UUID adicionaria 16 bytes por linha e nenhuma vantagem real aqui.
- Custo do trade-off: se um dia o projeto evoluir para múltiplas fontes de ETL gerando IDs em paralelo, sequencial simples colide — mas isso está fora do escopo declarado (§2.3).

### 4.4 Índices e queries que servem

| Índice | Query que serve |
|---|---|
| PK `(usina_id, timestamp)` em `fato_geracao` | `GET /usinas/{id}/geracao` (filtro por usina + range de data) |
| PK `(usina_id, data)` em `fato_clima` | `GET /usinas/{id}/clima` |
| Índice secundário em `dim_usina(fonte, regiao)` | `GET /usinas?fonte=solar&regiao=NE` |
| Índice em `fato_geracao(timestamp)` (sem usina) | `GET /geracao/nacional` (agregação por período, todas as usinas) |

Em DuckDB, por ser colunar e o volume ser pequeno (§3.3), índices explícitos importam menos do que em Postgres — a maior parte dessas queries já é rápida por varredura completa da coluna. Ainda assim, declaro os índices para documentar intenção e por disciplina de design.

### 4.5 Access patterns que motivaram a modelagem

- Toda consulta de série temporal é sempre "uma usina, um range de tempo" → grão fato = usina × timestamp, nunca agregado pré-computado (o agregado nacional é calculado em query, dado o volume trivial).
- Não existe consulta "todas as usinas, todos os timestamps" no frontend — sempre há um filtro — então não há necessidade de pré-agregação/materialização.

### 4.6 O que fica desnormalizado e por quê

Nada fica desnormalizado propositalmente — o volume (§3.3) é pequeno o suficiente para que normalização não custe performance, e a leitura (DuckDB colunar) lida bem com joins nesse tamanho. Desnormalizar aqui seria complexidade paga sem retorno (viola a regra do BLOCO 0.4 — "prefira o mais simples que atenda").

---

## 5. Contrato da API

### 5.1 Estilo

**REST**, não GraphQL/gRPC. Justificativa: consumidores são navegadores (frontend estático) e, eventualmente, um recrutador testando via Swagger UI. REST + OpenAPI é o que dá a melhor experiência de "explorar a API sem contexto prévio" — que é literalmente o caso de uso principal deste projeto (alguém desconhecido abrindo o `/docs`).

### 5.2 Convenções

- Recursos no plural (`/usinas`, não `/usina`).
- Verbos: só `GET` (ver §2.3 — sem mutação).
- Status codes: `200` sucesso, `404` recurso não encontrado (ex.: `usina_id` inexistente), `422` erro de validação de query param (padrão FastAPI/Pydantic), `429` rate limit excedido, `500` erro não tratado.

### 5.3 Especificação OpenAPI 3.1 (endpoints principais)

```yaml
openapi: 3.1.0
info:
  title: SolarWatch BR API
  version: "1.0"
paths:
  /usinas:
    get:
      summary: Lista usinas cadastradas
      parameters:
        - name: fonte
          in: query
          schema: { type: string, enum: [solar, eolica] }
        - name: regiao
          in: query
          schema: { type: string }
        - name: limit
          in: query
          schema: { type: integer, default: 50, maximum: 200 }
        - name: cursor
          in: query
          schema: { type: string }
      responses:
        "200":
          description: Lista paginada de usinas
          content:
            application/json:
              schema:
                type: object
                properties:
                  data:
                    type: array
                    items: { $ref: "#/components/schemas/Usina" }
                  next_cursor: { type: string, nullable: true }
        "422": { $ref: "#/components/responses/ValidationError" }

  /usinas/{id}/geracao:
    get:
      summary: Série histórica de geração de uma usina
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: integer }
        - name: inicio
          in: query
          schema: { type: string, format: date }
        - name: fim
          in: query
          schema: { type: string, format: date }
      responses:
        "200":
          description: Série temporal de geração
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  properties:
                    timestamp: { type: string, format: date-time }
                    energia_mwh: { type: number }
        "404": { $ref: "#/components/responses/NotFound" }

  /usinas/{id}/sobrevivencia:
    get:
      summary: Probabilidade de sobrevivência (Cox) até o próximo evento de manutenção
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: integer }
      responses:
        "200":
          description: Curva de sobrevivência estimada
          content:
            application/json:
              schema:
                type: object
                properties:
                  usina_id: { type: integer }
                  horizonte_dias: { type: array, items: { type: integer } }
                  probabilidade_sobrevivencia: { type: array, items: { type: number } }
        "404": { $ref: "#/components/responses/NotFound" }

components:
  schemas:
    Usina:
      type: object
      properties:
        usina_id: { type: integer }
        nome: { type: string }
        fonte: { type: string }
        regiao: { type: string }
        potencia_mw: { type: number }
        lat: { type: number }
        lon: { type: number }
  responses:
    NotFound:
      description: Recurso não encontrado
      content:
        application/json:
          schema: { $ref: "#/components/schemas/ProblemDetails" }
    ValidationError:
      description: Parâmetro inválido
      content:
        application/json:
          schema: { $ref: "#/components/schemas/ProblemDetails" }
```

*(Os demais endpoints — `/geracao/nacional`, `/usinas/{id}/clima`, `/usinas/{id}/previsao` — seguem o mesmo padrão de request/response e foram omitidos aqui por repetição; ficam completos no arquivo `openapi.yaml` do repositório.)*

### 5.4 Formato de erro padronizado

**RFC 9457 (Problem Details)**:

```json
{
  "type": "https://solarwatch.example/errors/not-found",
  "title": "Usina não encontrada",
  "status": 404,
  "detail": "Nenhuma usina com id=9999",
  "instance": "/usinas/9999/geracao"
}
```

Justificativa: é padrão IETF, o FastAPI tem suporte fácil via exception handler customizado, e comunica bem em uma API pública que qualquer terceiro pode consumir sem contexto.

### 5.5 Paginação

**Cursor-based**, não offset. Justificativa formal seguindo a regra (não "porque é melhor prática"): mesmo com volume pequeno (§3.3) que tornaria offset inofensivo hoje, cursor custa a mesma complexidade de implementação em FastAPI e evita reescrever a API se o volume crescer — e é o padrão que qualquer avaliador técnico espera ver em 2026. Cursor = `base64(usina_id_do_último_item)`.

### 5.6 Filtros, ordenação, sparse fieldsets

- Filtros: por campo indexado (`fonte`, `regiao`, range de data) — ver §4.4.
- Ordenação: fixa por `usina_id` ou `timestamp` ascendente (não exponho `sort=` arbitrário — YAGNI dado o volume e o uso real).
- Sparse fieldsets: não implementado — REST simples com payloads já pequenos (§3.4) não justifica a complexidade.

### 5.7 Versionamento

**URL path (`/api/v1/...`)**. Justificativa: mais simples de testar manualmente via browser/curl do que header/content-negotiation — relevante porque um avaliador pode querer abrir a URL direto. Política de deprecação: como é projeto de portfólio sem consumidores reais além de quem está avaliando, não há SLA de deprecação — documentado como tal no README em vez de fingir um processo formal que não existe.

### 5.8 Idempotência

Não aplicável em runtime — todos os endpoints são `GET` (idempotentes por definição do protocolo HTTP). Não existe necessidade de idempotency key.

### 5.9 Rate limiting

**Algoritmo: token bucket, por IP** (não por identidade — não há auth, §2.3).

- Limite: 60 req/min por IP (folga generosa acima do uso humano normal, mas protege contra scraping agressivo/loop de erro no free tier, onde CPU é o recurso mais escasso).
- Implementação: `slowapi` (wrapper de `limits` para FastAPI) — não precisa de Redis externo; usa memória do próprio processo (aceitável com 1 instância única, §3.6).
- Headers de resposta: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- Custo do trade-off: rate limit em memória de processo não sobrevive a redeploy nem escalaria para múltiplas instâncias — irrelevante aqui (§3.6 confirma 1 instância fixa).

### 5.10 Long-running operations

Não existem — todo endpoint responde em uma única chamada síncrona (latência alvo de 50 ms, §2.2). Não há necessidade de 202+polling/webhook/streaming.

### 5.11 Bulk operations

Não aplicável — não há escrita via API (§2.3).

---

## 6. Arquitetura de Alto Nível

### 6.1 Fluxo request → response (Mermaid)

```mermaid
flowchart LR
    subgraph Público["Zona pública (internet)"]
        C[Navegador do avaliador]
    end
    subgraph Render["Render — instância única, free tier"]
        S[Static files: JS/HTML/CSS] 
        A[FastAPI app]
        RL[Rate limiter em memória]
        D[(DuckDB — read-only, em memória)]
    end
    subgraph Offline["Pipeline offline, roda local, não no Render"]
        I[Ingestão: API + scraping]
        E[ETL]
        M[Treino de modelos ML]
    end

    C -- HTTPS/JSON --> A
    C -- HTTPS estático --> S
    A --> RL
    RL -- ok --> A
    A -- query --> D
    I --> E --> D
    M -.artefatos .pkl.-> A
```

### 6.2 Componentes e fronteira de responsabilidade

| Componente | Responsabilidade | NÃO faz |
|---|---|---|
| FastAPI app | Roteamento, validação de request (Pydantic), leitura do DuckDB, serialização de resposta, serve estáticos | Não escreve no banco, não faz ingestão |
| DuckDB (arquivo) | Armazenamento read-only dos dados curados | Não recebe escrita em runtime |
| Pipeline offline (ingestão/ETL/ML) | Roda manualmente na sua máquina, gera o `.duckdb` e os artefatos de modelo (`.pkl`), que são versionados e deployados junto com o código | Não roda no Render — não é um cron job gerenciado |
| Rate limiter | Protege CPU da instância única contra abuso | Não distingue usuários (sem auth) |

### 6.3 Estilo arquitetural

**Monolito modular** (não serviços separados, não event-driven).

Por que NÃO a alternativa distribuída: com 1 instância, 1 desenvolvedor, orçamento zero e 1000 registros, separar em microsserviços adicionaria latência de rede interna, complexidade de deploy (múltiplos processos no mesmo free tier já é apertado) e nenhum ganho — não há domínios com ciclo de vida ou equipe diferentes que justifiquem a fronteira de serviço. É a aplicação direta da regra do BLOCO 0.4: complexidade precisa se pagar, e aqui ela não se paga.

Modularidade interna (dentro do monolito): separação em `routers/`, `services/`, `db.py` — isolamento lógico sem separação física, para não impedir uma extração futura se o projeto crescer de verdade.

### 6.4 Síncrono vs. assíncrono

Tudo síncrono. Não há fila (Kafka/RabbitMQ/SQS) porque não há nenhum processamento em background acionado por request — inferência de modelo já treinado é rápida o bastante (modelo carregado em memória, §9 do overview) para responder dentro do orçamento de latência sem precisar sair do request-response síncrono.

### 6.5 Fluxo detalhado dos 2 casos de uso mais críticos

**Caso 1 — `GET /usinas/{id}/geracao`:**
1. Request chega no FastAPI.
2. Middleware de rate limit verifica bucket do IP.
3. Path/query params validados via Pydantic (`id` int, `inicio`/`fim` date opcionais).
4. Handler consulta DuckDB: `SELECT timestamp, energia_mwh FROM fato_geracao WHERE usina_id = ? AND timestamp BETWEEN ? AND ?`.
5. Se 0 linhas E usina não existe em `dim_usina` → 404. Se existe mas sem dado no range → 200 com lista vazia (distinção importante: "não existe" vs. "existe mas sem dado").
6. Serialização Pydantic → JSON.

**Caso 2 — `GET /usinas/{id}/sobrevivencia`:**
1. Mesma validação de entrada.
2. Handler verifica se `usina_id` existe em `dim_usina` (404 se não).
3. Handler chama o modelo Cox já carregado em memória (carregado 1x no startup do processo, não por request) com as covariáveis da usina (lidas do DuckDB).
4. Modelo retorna curva de sobrevivência para um horizonte fixo de dias.
5. Resposta serializada.

---

## 7. Escolhas de Tecnologia

| Camada | Escolha | Alternativas | Por que essa | Custo do trade-off |
|---|---|---|---|---|
| Compute | Render Web Service, free tier, 1 instância Python | Fly.io, Railway, Vercel (não serve Python bem), VPS próprio | Free tier de fato gratuito, deploy direto do GitHub, suporta Python nativamente | Hiberna sem tráfego (cold start); sem controle fino de recursos |
| Banco primário | DuckDB (arquivo, embarcado) | SQLite, Postgres gerenciado | Já é restrição dada (§1.4); além disso, colunar favorece as agregações analíticas do domínio (séries temporais, agregados nacionais) | Sem suporte nativo a escrita concorrente multi-processo — irrelevante aqui pois é read-only em runtime |
| Cache | Nenhum (dataset inteiro em memória do processo, §3.5) | Redis | Redis é um serviço pago fora do free tier viável e desnecessário dado o volume | Se o volume crescer 1000x, essa decisão precisa ser revisitada |
| Fila/stream | Nenhuma | Celery+Redis, SQS | Não há trabalho assíncrono de background (§6.4) | Nenhum — não é trade-off, é ausência de necessidade |
| Busca | Nenhuma (queries diretas ao DuckDB) | Elasticsearch/Meilisearch | Volume e complexidade de busca (filtro por fonte/região) não justificam motor de busca dedicado | Se precisar de busca full-text (ex.: nome de usina com fuzzy match), precisaria reavaliar |
| Storage de blobs | Nenhum — modelos `.pkl` versionados no próprio repo/deploy | S3/GCS | Artefatos de modelo são pequenos (KB a poucos MB) e não mudam em runtime | Não escala se modelos ficarem grandes (ex.: deep learning) — aceitável para os modelos propostos no overview (Cox, gradient boosting, SARIMA) |
| CDN | Nenhuma dedicada — estáticos servidos pelo próprio FastAPI | Cloudflare Pages, Netlify | Manter tudo em 1 serviço = 1 free tier só, menos peças móveis para operar sozinho | Sem edge caching geográfico — irrelevante para single-region (§2.2) |
| API Gateway | Nenhum — FastAPI é a borda | Kong, AWS API Gateway | Sem custo, sem necessidade de roteamento multi-serviço (monolito, §6.3) | Rate limit e validação ficam acoplados à aplicação em vez de uma camada de borda separada — aceitável na escala do projeto |

---

## 8. Escalabilidade

### 8.1 Vertical vs. horizontal

Nenhuma das duas está no escopo real — 1 instância fixa do free tier (§3.6). Documentado aqui apenas como **plano caso o projeto precise crescer**: primeiro esgotar escala vertical (upgrade de plano Render), só considerar horizontal se CPU-bound persistir após otimizar queries.

### 8.2 Estado

O processo FastAPI é **majoritariamente stateless**, com uma exceção deliberada: o dataset inteiro carregado em memória no startup (§3.5) e o rate limiter em memória (§5.9) são estado local do processo. Isso é aceitável e documentado como **não portável para múltiplas instâncias** sem mudança de design (rate limiter precisaria ir para Redis, dado compartilhado precisaria de um banco externo) — trade-off aceito porque múltiplas instâncias estão fora de escopo.

### 8.3 Load balancing

Não aplicável — 1 instância.

### 8.4 Estratégia de cache

Já coberta em §3.5 e §7: cache = dataset inteiro em memória do processo, sem TTL (porque não há atualização em runtime) e sem invalidação (porque a única forma de o dado mudar é um redeploy, que já reinicia o processo e recarrega tudo). Isso elimina por construção os problemas clássicos de cache (stampede, hot keys, invalidação) — não porque foram resolvidos com uma técnica, mas porque o design os torna inaplicáveis.

### 8.5 Réplicas de leitura / replication lag

Não aplicável — instância única, sem réplicas.

### 8.6 Particionamento/sharding

Não aplicável no volume atual (§3.3). Se o volume crescesse ordens de magnitude, a chave natural de partição seria `usina_id` ou intervalo de tempo — documentado como direção futura, não implementado.

### 8.7 Connection pooling

DuckDB embarcado no mesmo processo não usa pool de conexão de rede como Postgres — é acesso direto in-process. Não há limite de conexão externo a gerenciar.

### 8.8 Backpressure

Quando a CPU da instância satura (ex.: pico real de 100 req/s no free tier compartilhado), o rate limiter (§5.9) já corta requests antes de chegar ao handler pesado, retornando `429` em vez de deixar a fila de requests do Uvicorn crescer sem limite. Não há downstream além do processo local, então não há backpressure entre serviços a coordenar.

---

## 9. Consistência e Coordenação

- **Consistência forte, trivialmente**: como o banco é read-only em runtime (confirmado em §2.4), não existe write-write nem read-after-write a coordenar. Toda leitura reflete exatamente o estado do arquivo `.duckdb` deployado.
- **Transações**: não aplicável em runtime. No pipeline ETL offline, cada rebuild do banco é atômico no sentido de "arquivo novo substitui arquivo antigo" (build-then-swap), evitando estado parcialmente escrito.
- **Sagas/outbox**: não aplicável — não há operação cruzando serviços (monolito único, §6.3).
- **Concorrência (locking)**: não aplicável — sem escrita concorrente.
- **Exactly-once / at-least-once**: não aplicável a requests `GET`. No pipeline de ingestão (fora da API), a idempotência é garantida reprocessando o raw layer do zero a cada rebuild em vez de fazer upsert incremental — mais simples e evita duplicação silenciosa.
- **Ordenação de eventos**: não há eventos — não aplicável.

---

## 10. Confiabilidade e Resiliência

### 10.1 Modos de falha por dependência

| Dependência | O que acontece se falhar | Impacto |
|---|---|---|
| Render (a plataforma) | App fica indisponível até o provedor se recuperar | Total, fora do nosso controle — aceito como risco de free tier |
| Cold start (hibernação) | Primeiro request após inatividade demora alguns segundos | Latência degradada momentânea, não é uma "falha" real |
| Arquivo DuckDB corrompido/ausente no deploy | App falha no startup (fail-fast) | App não sobe — detectável imediatamente no health check do Render, não falha silenciosa em produção |
| Modelo `.pkl` incompatível (versão de lib diferente) | Falha no startup ao carregar modelo | Mesmo tratamento: fail-fast no boot, não em request |

### 10.2 Timeouts

- Timeout de request no Uvicorn/Render: 30s (default da plataforma) — generoso para o orçamento de latência real de 50ms (§2.2); existe só como rede de segurança contra travamento.
- Não há chamada a serviço externo em runtime (tudo é leitura local), então não há orçamento de latência a dividir entre dependências.

### 10.3 Retries

Não aplicável no lado do servidor (não há chamada a downstream). No lado do **pipeline de ingestão** (fora da API, mas vale documentar): retry com exponential backoff + jitter ao chamar as APIs externas (ONS, NASA POWER), respeitando rate limit — retry é permitido ali porque `GET` a essas APIs é idempotente por natureza.

### 10.4 Circuit breaker / bulkheads

Não aplicável — sem dependências externas síncronas no caminho de request.

### 10.5 Graceful degradation

Se o modelo de sobrevivência falhar ao carregar por algum motivo não fatal, a decisão de design é: os endpoints de dado bruto (`/usinas`, `/geracao`) continuam funcionando normalmente; apenas `/usinas/{id}/sobrevivencia` e `/usinas/{id}/previsao` retornam `503` com Problem Details explicando que o modelo está indisponível — em vez de derrubar o processo inteiro por causa de uma feature dependente de ML.

### 10.6 Dead letter queue / reprocessamento

Não aplicável — sem fila (§6.4).

### 10.7 Backup, restore, RPO/RTO reais

- **Backup**: o próprio Git é o backup — o `.duckdb` versionado (ou reconstruível a partir do raw layer, também versionado/documentado) é a fonte de verdade.
- **RPO real**: até a última vez que você rodou o ETL localmente e fez commit/deploy — pode ser dias ou semanas, e isso é aceitável e declarado (§2.4).
- **RTO real**: tempo de um novo deploy no Render a partir do repositório — minutos.

### 10.8 Multi-AZ / multi-região

Nenhum — confirmado como engano de preenchimento (não multi-região). Single-region, sem réplica de disponibilidade. Justificativa de custo: qualquer redundância geográfica está fora do free tier.

---

## 11. Segurança

### 11.1 AuthN

**Nenhuma** — API pública, confirmado. Todo endpoint é acessível sem credencial.

### 11.2 Token

Não aplicável (sem auth).

### 11.3 AuthZ

Não aplicável — não há diferenciação de permissão entre chamadores.

### 11.4 Validação de input e limites

- Todo path/query param validado via Pydantic antes de tocar o handler (tipo, enum, range).
- Limite de `limit` de paginação (`maximum: 200`, §5.3) para impedir que alguém peça um payload gigante de propósito.
- Sem body em nenhum request (`GET` only) — elimina toda a superfície de ataque de payload malicioso de escrita.

### 11.5 OWASP API Security Top 10 — mitigação relevante

| Risco OWASP | Mitigação neste design |
|---|---|
| API1 Broken Object Level Authorization | Não aplicável diretamente (sem auth/ownership) — mas todo `id` é validado contra existência real antes de retornar dado, evitando enumeração silenciosa de erro genérico |
| API4 Unrestricted Resource Consumption | Rate limit por IP (§5.9) + limite de paginação |
| API6 Unrestricted Access to Sensitive Business Flows | Não há fluxo de negócio sensível (é leitura de dado público de energia) |
| API8 Security Misconfiguration | CORS restrito ao domínio do próprio frontend (não `*`), headers de segurança padrão (HSTS via Render, se disponível) |
| API9 Improper Inventory Management | Versionamento explícito por URL (§5.7) evita endpoint "esquecido" sem contrato documentado |

### 11.6 Criptografia

- Em trânsito: HTTPS obrigatório (fornecido pelo próprio Render).
- Em repouso: não há dado sensível a criptografar (dado público de geração de energia) — decisão explícita de não adicionar criptografia de banco, dado que não há PII (§11.7).
- Segredos (se houver, ex.: chave de API do NASA POWER caso exija): variáveis de ambiente do Render, nunca hardcoded, `.gitignore` no repositório (já uma restrição dada em §1.1).

### 11.7 PII

**Nenhum campo pessoal é coletado** — decisão de escopo explícita desde o scraping (ANEEL expõe dado de usina/empresa, não de pessoa física). Isso é o próprio mecanismo de compliance com LGPD citado no briefing: a estratégia não é "mascarar PII", é "nunca ingerir PII".

### 11.8 Auditoria

Logs de request (método, path, status, latência, IP) — retenção limitada pelo próprio free tier de logs do Render (curta, geralmente algumas horas a dias). Não há necessidade de auditoria imutável de longo prazo, dado que não há ação de negócio a auditar (sem escrita, sem usuário autenticado).

### 11.9 Abuso

Rate limit por IP (§5.9) é a única linha de defesa — proporcional ao risco real (projeto de portfólio, não alvo de ataque direcionado). WAF e bot detection dedicados ficam fora de escopo por custo/complexidade desproporcional ao risco.

---

## 12. Observabilidade

### 12.1 Logs estruturados

Formato JSON, campos obrigatórios: `timestamp`, `method`, `path`, `status_code`, `latency_ms`, `client_ip`, `request_id` (correlation id gerado por request, via middleware). **Nunca logar**: nenhum campo aqui é PII, então não há lista de exclusão adicional além de nunca logar headers completos (poderiam conter algo incidental).

### 12.2 Métricas

RED por endpoint (Rate, Errors, Duration) via `prometheus-fastapi-instrumentator` (open-source, sem custo). Métricas de negócio simples: contagem de chamadas por endpoint (qual feature é mais "vista" por avaliadores — dado curioso para o próprio README).

Dado o orçamento zero, não há Prometheus/Grafana gerenciado rodando 24/7 — as métricas ficam expostas em `/metrics` e podem ser inspecionadas manualmente ou raspadas localmente quando você quiser analisar, em vez de um stack de observabilidade always-on.

### 12.3 Tracing distribuído

**Não implementado.** Justificativa honesta: tracing distribuído resolve o problema de rastrear uma requisição através de múltiplos serviços — este é um monolito único (§6.3), então um `request_id` correlacionado no log estruturado (§12.1) já dá 100% da rastreabilidade necessária sem o overhead de OpenTelemetry + backend de trace.

### 12.4 SLIs e SLOs

Dado que não há SLA real (§2.2, §10.8), os SLOs são declarados como **meta de exercício pessoal**, não compromisso operacional:
- SLI: latência p50 dos endpoints de leitura simples (excluindo cold start).
- SLO: 95% dos requests (pós cold-start) abaixo de 50ms.
- Sem error budget formal — não há on-call, não há stakeholder para reportar.

### 12.5 Alertas

Nenhum alerta ativo configurado — não há on-call e o free tier não oferece bom suporte a isso sem custo adicional. Documentado como **limitação conhecida**, não como omissão acidental.

### 12.6 Dashboards

Um único dashboard local (pode ser o próprio Superset do projeto, ou um notebook) consultando `/metrics` esporadicamente — não um painel operacional 24/7.

---

## 13. Deploy e Operação

### 13.1 Pipeline CI/CD

GitHub Actions (gratuito para repositório público): lint (`ruff`), testes (`pytest`), e só então trigger de deploy automático do Render a partir do push na branch principal (Render já integra nativamente com push no GitHub, sem necessidade de step de deploy manual no Actions).

### 13.2 Estratégia de release

**Deploy direto (recreate), não blue-green/canary.** Justificativa: 1 instância única no free tier não suporta rodar duas versões em paralelo (custaria 2x). Critério de rollback: manual, via revert do commit no GitHub (Render redeploya a partir do HEAD) — aceitável dado o baixo risco de uma API read-only sem estado de negócio persistente em runtime.

### 13.3 Migrations de banco sem downtime

Não aplicável no sentido tradicional (não há schema evoluindo com dado de produção em runtime) — o schema do DuckDB é definido no próprio pipeline ETL e o arquivo inteiro é substituído a cada rebuild (build-then-swap, §9), o que é estruturalmente equivalente a uma migration "expand-contract" simplificada ao extremo: não há estado intermediário observável por um client em produção.

### 13.4 Feature flags

Não implementado — escopo pequeno o suficiente para não justificar a infraestrutura de flags; features novas entram via deploy direto.

### 13.5 Estratégia de testes

- Unit: funções de transformação do ETL e de inferência dos modelos.
- Integração: endpoints da API via `TestClient` do FastAPI, contra um DuckDB de fixture pequeno.
- Contrato: validação do schema de resposta contra o OpenAPI gerado automaticamente pelo FastAPI (garante que a doc nunca diverge do código, já que é gerada dele).
- Carga: `locust` ou `k6` rodado manualmente contra a instância local, para validar os números de §3 antes de declarar "suporta 100 req/s pico" — sem isso, o número do briefing seria só uma afirmação não testada.
- Chaos: fora de escopo (desproporcional para 1 instância sem redundância a "caotizar").

### 13.6 Runbook — 3 incidentes mais prováveis

1. **App não sobe após deploy** → causa provável: `.pkl`/`.duckdb` ausente ou corrompido no artefato de deploy. Ação: checar logs de startup (fail-fast, §10.1), redeployar commit anterior.
2. **Cold start lento demais irritando quem avalia** → causa: hibernação natural do free tier. Ação: nenhuma automática (é esperado); mitigação possível é um `README` avisando "primeiro load pode demorar alguns segundos".
3. **429 inesperado em uso normal** → causa provável: rate limit calibrado baixo demais, ou múltiplos usuários atrás do mesmo IP (NAT/proxy corporativo). Ação: revisar threshold de §5.9.

---

## 14. Custos

| Componente | Custo mensal | Conta |
|---|---|---|
| Render Web Service (free tier) | R$ 0 | Dentro do limite de horas gratuitas do plano |
| GitHub (repositório + Actions) | R$ 0 | Repositório público, minutos de Actions gratuitos suficientes para o volume de CI deste projeto |
| DuckDB | R$ 0 | Embarcado, sem serviço gerenciado |
| NASA POWER API | R$ 0 | Gratuita, sem key |
| ONS Open Data | R$ 0 | Gratuita |
| **Total** | **R$ 0** | Confirma a restrição de orçamento do briefing |

Componente que domina o custo: nenhum — o custo total é zero por design. Se algum dia precisasse deixar de ser zero, o primeiro componente a custar seria upgrade do Render (para eliminar cold start), não banco nem ML.

---

## 15. Trade-offs e Alternativas Rejeitadas (ADRs)

**ADR-1 — Banco estático (DuckDB) em vez de Postgres gerenciado**
- Contexto: restrição de orçamento zero e volume de dado trivial.
- Decisão: DuckDB embarcado, reconstruído por pipeline batch.
- Alternativas: Postgres free tier (Supabase/Neon) — daria escrita real e migrations tradicionais.
- Consequências: ganha simplicidade operacional total (sem gestão de conexão, sem migration tool); perde a capacidade de dado "vivo" em runtime e qualquer escrita via API.

**ADR-2 — Sem autenticação**
- Contexto: público-alvo é avaliação de portfólio, não usuários reais com dado privado.
- Decisão: API 100% pública.
- Alternativas: API key simples só para "parecer" produção.
- Consequências: simplifica todo o resto do sistema (sem token, sem RBAC); expõe a API a qualquer scraping — mitigado só por rate limit de IP, que é uma defesa fraca comparada a auth real.

**ADR-3 — Monolito único servindo API + frontend estático**
- Contexto: 1 instância free tier, 1 desenvolvedor.
- Decisão: FastAPI serve tanto os endpoints REST quanto os arquivos estáticos JS/HTML/CSS.
- Alternativas: frontend separado em Vercel/Netlify + backend isolado no Render.
- Consequências: menos peças móveis para operar sozinho e zero custo adicional de hospedagem separada; perde a possibilidade de CDN dedicado para os estáticos e acopla o deploy dos dois (um bug no backend derruba também o frontend).

**ADR-4 — Sem cache distribuído (Redis)**
- Contexto: dataset cabe inteiro em memória do processo (§3.5).
- Decisão: dataset carregado em memória no startup, sem camada de cache externa.
- Alternativas: Redis (mesmo que free tier de terceiro, ex. Upstash).
- Consequências: elimina toda complexidade de invalidação (§8.4); não escalaria se o volume crescesse ordens de magnitude — aceito como limite conhecido do design.

**ADR-5 — Rate limit em memória de processo, não distribuído**
- Contexto: 1 instância fixa, sem plano de multi-instância.
- Decisão: `slowapi` com storage em memória.
- Alternativas: rate limit centralizado em Redis (necessário se houvesse múltiplas instâncias).
- Consequências: zero dependência externa extra; se um dia o projeto escalar horizontalmente, essa decisão precisa ser revisitada primeiro (senão cada instância teria seu próprio limite, multiplicando o limite real).

---

## 16. Riscos e Evolução

### 16.1 Top 5 riscos

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Cold start prejudica primeira impressão de um recrutador | Alta | Médio | Aviso explícito no README/frontend ("primeiro load pode demorar") |
| Dado sintético (sobrevivência) confundido com dado real por quem avalia | Média | Alto (credibilidade) | Disclaimer explícito na resposta da própria API (campo `is_synthetic: true`) e no README |
| Rate limit calibrado errado (muito baixo, barra uso legítimo; muito alto, não protege) | Média | Baixo | Ajustar após teste de carga real (§13.5), não só estimativa |
| Mudança de schema/API externa (ONS/ANEEL) quebra ingestão silenciosamente | Média | Médio | Validação de schema no início do pipeline ETL (fail-fast se o formato mudou) |
| Free tier do Render mudar política/desligar produto | Baixa | Alto | Nenhuma mitigação ativa hoje — aceito como risco de plataforma gratuita; documentar alternativa (Fly.io) como plano B no README |

### 16.2 Onde este design quebra

- Storage: quebra se o volume passar de dezenas de MB sem repensar §3.5/§3.6 (deixaria de caber confortavelmente em memória do free tier).
- Rate limit: quebra (deixa de proteger de verdade) se o projeto ganhar múltiplas instâncias sem migrar para storage centralizado (§ADR-5).
- Consistência trivial: quebra no dia em que alguém pedir uma feature de escrita via API — todo o modelo de "read-only em runtime" precisaria ser reaberto.

### 16.3 Roadmap

- **v1 (MVP, este documento)**: API read-only, dado estático, deploy single-instance gratuito.
- **v2 (se quiser evoluir de verdade)**: dado atualizado por cron real (ex.: GitHub Actions agendado rodando o ETL e fazendo commit do `.duckdb` novo), ainda sem custo.
- **v3 (se "der certo demais")**: banco gerenciado com escrita incremental, auth básica para endpoints de escrita, observabilidade always-on — nesse ponto o projeto deixaria de ser "estudo com restrição de orçamento zero" e viraria produto real, e boa parte das decisões deste documento precisaria ser revisitada uma a uma.

### 16.4 Perguntas em aberto para o time

*(time = você mesmo, mas mantendo o formato do documento)*

- Vale a pena automatizar o rebuild do `.duckdb` via GitHub Actions agendado (v2 do roadmap), ou isso foge do escopo de "projeto de estudo com database estático"?
- O disclaimer de dado sintético (§16.1) deve ir só no README ou também como campo explícito na resposta JSON da API? (Recomendo os dois — é mais honesto e mais visível para quem só olha a resposta da API sem ler o repositório inteiro.)
