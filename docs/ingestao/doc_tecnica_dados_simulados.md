# Documentação Técnica — Dados Simulados de Manutenção/Falha por Usina

> Escopo: por que e como o SolarWatch BR gera um conjunto **sintético** de eventos de manutenção corretiva/falha por usina, qual o modelo estatístico por trás, o que cada coluna significa, como validar e como usar na análise de sobrevivência. Também lista o que os dados **não** representam.
>
> Script: [`ML/analise_sobrevivencia/dados_simulados.py`](../../ML/analise_sobrevivencia/dados_simulados.py)
> Saída: `dados/simulados/eventos_manutencao_simulados.parquet` + `eventos_manutencao_simulados.meta.json`

> ⚠️ **Aviso:** os eventos deste conjunto são **inventados** por um modelo estatístico. As usinas, potências, estados e datas de entrada em operação são reais (cadastro ANEEL), mas **nenhuma falha, data de falha ou tipo de falha aconteceu de verdade**. Não use estes dados para conclusões sobre a confiabilidade real de nenhuma usina, fabricante ou região.

---

## Sumário

1. [Motivação](#1-motivação)
2. [Visão geral do processo](#2-visão-geral-do-processo)
3. [Como executar](#3-como-executar)
4. [População base: usinas reais da ANEEL](#4-população-base-usinas-reais-da-aneel)
5. [Modelo gerador: Weibull de riscos proporcionais](#5-modelo-gerador-weibull-de-riscos-proporcionais)
6. [Parâmetros escolhidos e justificativa](#6-parâmetros-escolhidos-e-justificativa)
7. [Censura à direita](#7-censura-à-direita)
8. [Tipo do evento](#8-tipo-do-evento)
9. [Esquema do arquivo de saída](#9-esquema-do-arquivo-de-saída)
10. [Metadados e reprodutibilidade](#10-metadados-e-reprodutibilidade)
11. [Perfil do conjunto gerado](#11-perfil-do-conjunto-gerado)
12. [Validação: o modelo recupera os parâmetros?](#12-validação-o-modelo-recupera-os-parâmetros)
13. [Como usar na análise de sobrevivência](#13-como-usar-na-análise-de-sobrevivência)
14. [Limitações e premissas](#14-limitações-e-premissas)
15. [Próximos passos](#15-próximos-passos)

---

## 1. Motivação

A API do projeto prevê um endpoint `/usinas/{id}/sobrevivencia` (system design, §5 e §11): a probabilidade de uma usina passar um certo tempo sem precisar de manutenção corretiva grave. Para treinar e demonstrar esse modelo, é preciso um histórico de **tempo até evento por usina**.

Esse histórico **não existe em fonte pública** no Brasil:

- **ONS:** publica geração, restrições e indisponibilidades agregadas, mas não um log de falhas por equipamento ou usina com causa.
- **ANEEL:** o SIGA é cadastro (potência, fase, datas de outorga e operação), não histórico de manutenção.
- **Operadores e fabricantes:** tratam esses registros como informação comercial sigilosa.

A saída adotada é **simular** os eventos de forma controlada e plausível, sobre a população **real** de usinas. Isso traz três vantagens:

1. **Estrutura realista:** o número de usinas, a mistura de fontes, a distribuição geográfica e, principalmente, o **tempo de acompanhamento** de cada usina são reais. É justamente isso que determina a censura.
2. **Verdade conhecida:** como os parâmetros do modelo gerador são conhecidos, dá para **validar** se os métodos de sobrevivência do projeto (Kaplan-Meier, Cox, Weibull) recuperam o que foi colocado. Com dado real isso seria impossível.
3. **Troca transparente:** se um dia houver dado real, basta substituir o arquivo mantendo o esquema ([§9](#9-esquema-do-arquivo-de-saída)). O resto do pipeline não muda.

---

## 2. Visão geral do processo

```
dados/bruto/dados_aneel_bruto.parquet        (cadastro real, 20.511 usinas UFV/EOL)
        │
        ▼  carregar_usinas()
  filtros: fase = Operação, UFV/EOL, potência ≥ 1 MW,
           entrada em operação ≥ 1990 e < data do retrato
  conversões: potência "1.400,00" kW → MW; datas; UF → subsistema
        │                                         (1.854 usinas)
        ▼  simular(seed)
  para cada usina:
    η  = β · x                                     (preditor linear)
    E  ~ Exponencial(1)
    T  = λ_fonte · (E / e^η)^(1/k_fonte)          (tempo até o 1º evento, em anos)
    C  = (data_corte − data_entrada) / 365,25      (tempo observável)
    evento = 1 se T ≤ C, senão 0 (censura)
    tempo_anos = min(T, C)
    tipo_evento ~ Categórica(fonte)                (só se evento = 1)
        │
        ▼  gravar_parquet() + _gravar_metadados()
dados/simulados/eventos_manutencao_simulados.parquet
dados/simulados/eventos_manutencao_simulados.meta.json
```

---

## 3. Como executar

Pré-requisito: a camada bruta da ANEEL já precisa ter sido gerada (`python -m ingestao.aneel.ingestao_aneel`).

```bash
python -m ML.analise_sobrevivencia.dados_simulados                            # padrão: seed 42, ≥ 1 MW
python -m ML.analise_sobrevivencia.dados_simulados --seed 7                   # outra realização aleatória
python -m ML.analise_sobrevivencia.dados_simulados --potencia-minima-mw 5     # só usinas ≥ 5 MW
```

| Argumento | Padrão | Efeito |
|---|---|---|
| `--seed` | `42` | Semente do gerador aleatório (`numpy.random.default_rng`). A mesma semente sobre o mesmo bruto da ANEEL gera exatamente os mesmos dados |
| `--potencia-minima-mw` | `1.0` | Corte mínimo de potência outorgada para a usina entrar na simulação |

Execução típica: menos de 1 segundo. O script reaproveita `ingestao.armazenamento.gravar_parquet`, com a mesma escrita atômica e compressão zstd da camada bruta.

---

## 4. População base: usinas reais da ANEEL

### 4.1 Filtros (`carregar_usinas`)

| Etapa | Critério | Usinas restantes | Motivo |
|---|---|---|---|
| Bruto ANEEL | UFV + EOL, todas as fases | 20.511 | — |
| Fase | `DscFaseUsina == "Operação"` | 18.440 | Usina em construção não acumula tempo de operação |
| Potência | `MdaPotenciaOutorgadaKw ≥ 1000` (≥ 1 MW) | ↓ | Exclui cerca de 16 mil microssistemas de ~1 kW, muitos registrados em nome de **pessoas físicas** (LGPD) e que não são "usinas" no sentido operacional |
| Data de entrada | `DatEntradaOperacao ≥ 1990-01-01` | ↓ | Descarta a data fictícia `1900-01-03` e as datas implausíveis de 1961/1971/1981 (ver a doc de ingestão, §8.6) |
| Data de entrada | `< data do retrato` | **1.854** | O tempo de acompanhamento precisa ser positivo |

A **data de corte** (`data_corte`) é o `DatGeracaoConjuntoDados` do bruto da ANEEL, ou seja, a data do retrato do cadastro (18/09/2026 na última execução). Usar a data do dado, e não `date.today()`, torna o resultado reprodutível: o mesmo bruto e a mesma semente sempre geram o mesmo arquivo.

### 4.2 Conversões

- **Potência:** `"29.700,00"` (kW, formato BR) → remove `.` de milhar, troca `,` por `.` → `29700.0` → `/1000` → **29,7 MW**.
- **Data:** `DatEntradaOperacao` (texto ISO) → `datetime`.
- **Fonte:** `UFV → solar`, `EOL → eolica`.
- **Subsistema:** mapeamento UF → subsistema do SIN, com a mesma codificação do `id_subsistema` do ONS para permitir cruzamento:

| Subsistema | UFs |
|---|---|
| `N` | AM, PA, AP, RR, TO, MA |
| `NE` | BA, RN, PI, CE, PE, PB, SE, AL |
| `SE` (Sudeste/Centro-Oeste) | MG, SP, RJ, ES, GO, DF, MT, MS, RO, AC |
| `S` | RS, SC, PR |

Uma UF fora do mapa gera aviso no log e a usina é descartada. Não aconteceu na execução atual.

### 4.3 Privacidade

O conjunto simulado **não carrega** `NomEmpreendimento` nem `DscPropriRegimePariticipacao`. A usina é identificada só por `id_usina` (sequencial) e `ceg` (código público da ANEEL).

---

## 5. Modelo gerador: Weibull de riscos proporcionais

### 5.1 Função de risco

Para uma usina com fonte $f$ e vetor de covariáveis $x$:

```math
h(t \mid x, f) = h_{0,f}(t)\, e^{\beta^\top x},
\qquad
h_{0,f}(t) = \frac{k_f}{\lambda_f}\left(\frac{t}{\lambda_f}\right)^{k_f - 1}
```

- $h_{0,f}$ é o **risco de base** Weibull, com forma $k_f$ e escala $\lambda_f$ (em anos) **próprias de cada fonte**.
- $e^{\beta^\top x}$ é o **multiplicador de risco** das covariáveis. Dentro de uma mesma fonte, os riscos são **proporcionais** (hipótese de Cox).

As funções derivadas são:

```math
H(t \mid x) = \left(\frac{t}{\lambda_f}\right)^{k_f} e^{\beta^\top x},
\qquad
S(t \mid x) = \exp\!\left[-\left(\frac{t}{\lambda_f}\right)^{k_f} e^{\beta^\top x}\right]
```

### 5.2 Por que Weibull

- É a distribuição clássica de **engenharia de confiabilidade** para tempo até falha de componentes mecânicos e eletrônicos.
- Com um parâmetro só ($k$), representa risco **decrescente** ($k<1$, mortalidade infantil), **constante** ($k=1$, exponencial) ou **crescente** ($k>1$, desgaste).
- É a única família que é **ao mesmo tempo PH e AFT**, o que permite validar os dados tanto com Cox (PH) quanto com regressão Weibull AFT ([§12](#12-validação-o-modelo-recupera-os-parâmetros)).

### 5.3 Amostragem por inversão

Fazendo $S(T \mid x) = U$, com $U \sim \text{Uniforme}(0,1)$, e usando $E = -\ln U \sim \text{Exponencial}(1)$:

```math
\left(\frac{T}{\lambda_f}\right)^{k_f} e^{\eta} = E
\;\;\Longrightarrow\;\;
T = \lambda_f \left(\frac{E}{e^{\eta}}\right)^{1/k_f},
\qquad \eta = \beta^\top x
```

No código:

```python
tempo_ate_evento = escala * (rng.exponential(size=len(df)) / np.exp(eta)) ** (1 / k)
```

É uma operação vetorizada: todas as usinas são sorteadas numa única chamada, sem laço.

### 5.4 Covariáveis e preditor linear

Implementado em `_preditor_linear`:

| Covariável | Definição | Centralização |
|---|---|---|
| `log_potencia_mw_c` | $\ln(\text{MW}) - \ln(30)$ | 30 MW ≈ mediana do parque |
| `subsistema_NE` | 1 se NE, senão 0 | referência: SE |
| `subsistema_S` | 1 se S, senão 0 | referência: SE |
| `subsistema_N` | 1 se N, senão 0 | referência: SE |
| `ano_entrada_c` | ano de entrada − 2018 | 2018 ≈ mediana das eólicas |

A **usina de referência** ($\eta = 0$) tem 30 MW, fica no Sudeste/Centro-Oeste e entrou em operação em 2018. Para ela, o risco é exatamente o risco de base da sua fonte.

Centralizar as covariáveis não muda os $\beta$, mas deixa $\lambda_f$ interpretável: é a escala da usina "típica", e não de uma usina hipotética de 1 MW que entrou no ano zero.

---

## 6. Parâmetros escolhidos e justificativa

> Os valores são **premissas plausíveis de ordem de grandeza**, escolhidas para gerar dados com comportamento realista. Não são estimativas calibradas em estudos publicados.

### 6.1 Linha de base por fonte (`WEIBULL`)

| Fonte | $k$ (forma) | $\lambda$ (escala, anos) | Mediana na referência* | Média na referência* | Racional |
|---|---|---|---|---|---|
| Eólica | **1,6** | **5,5** | 4,4 anos | 4,9 anos | Muitos componentes mecânicos rotativos sob fadiga (caixa multiplicadora, rolamentos, pás). Desgaste marcado ($k$ bem acima de 1) e primeiro evento grave relativamente cedo |
| Solar | **1,3** | **7,0** | 5,3 anos | 6,5 anos | Poucas partes móveis. O ponto fraco são os inversores e a eletrônica de potência, com desgaste mais suave e primeiro evento grave mais tarde |

\* Mediana $= \lambda(\ln 2)^{1/k}$ e média $= \lambda\,\Gamma(1+1/k)$, para a usina de referência ($\eta=0$).

Interpretação de $\lambda$: $S(\lambda)=e^{-1}\approx 0{,}37$. Ou seja, cerca de 63% das usinas de referência teriam o primeiro evento grave até $\lambda$ anos.

### 6.2 Efeitos das covariáveis (`BETA`)

| Covariável | $\beta$ | Hazard ratio $e^\beta$ | Leitura | Racional |
|---|---|---|---|---|
| `log_potencia_mw_c` | **+0,20** | 1,22 por unidade de ln(MW) | Dobrar a potência → risco × $2^{0{,}20}$ = **+15%** | Mais aerogeradores/inversores, mais pontos de falha |
| `subsistema_NE` | **+0,25** | 1,28 | +28% de risco vs. SE | Maresia no litoral, calor e poeira |
| `subsistema_S` | **+0,10** | 1,11 | +11% vs. SE | Rajadas e maior amplitude térmica |
| `subsistema_N` | **+0,15** | 1,16 | +16% vs. SE | Umidade e logística de manutenção difícil |
| `ano_entrada_c` | **−0,04** | 0,96 por ano | Cada ano mais nova → **−3,9%** de risco | Evolução tecnológica e aprendizado de O&M |

**Tradução para o tempo:** num Weibull PH, multiplicar o risco por $e^\beta$ equivale a multiplicar o tempo típico até o evento por $e^{-\beta/k}$. Exemplo: uma eólica no NE tem tempos multiplicados por $e^{-0{,}25/1{,}6} \approx 0{,}86$, ou seja, cerca de 14% mais curtos.

---

## 7. Censura à direita

### 7.1 Mecanismo

Cada usina é acompanhada do dia em que entrou em operação até a data de corte:

```math
C_i = \frac{\text{data\_corte} - \text{data\_entrada}_i}{365{,}25}\ \text{anos}
```

```python
df["evento"]     = (tempo_ate_evento <= tempo_observavel).astype(int)
df["tempo_anos"] = np.where(df["evento"] == 1, tempo_ate_evento, tempo_observavel)
```

| Situação | `evento` | `tempo_anos` | `data_evento` | `tipo_evento` |
|---|---|---|---|---|
| $T_i \le C_i$: falha dentro da janela | 1 | $T_i$ | entrada + $T_i$ (arredondada para o dia) | sorteado |
| $T_i > C_i$: ainda sem falha hoje | 0 | $C_i$ | nulo | nulo |

É **censura administrativa** (tipo I com entradas escalonadas): o estudo "termina" na data do retrato, e as usinas que ainda não falharam só informam que sobreviveram pelo menos $C_i$ anos.

### 7.2 A censura é não informativa?

Os métodos de sobrevivência exigem que a censura seja independente do tempo até o evento, **condicional às covariáveis**. Aqui:

- $C_i$ depende **só** da data de entrada em operação.
- A data de entrada entra no modelo como covariável (`ano_entrada_c`).
- Portanto, condicionalmente a $x$, $T$ e $C$ são independentes, e a hipótese vale **por construção**. Num modelo que omita `ano_entrada_c`, a censura fica parcialmente informativa: usinas novas são ao mesmo tempo menos arriscadas e mais censuradas.

### 7.3 Consequência prática

Como o parque solar é muito mais novo (mediana de entrada em 2023, contra 2018 nas eólicas), as solares ficam **muito mais censuradas**: 71% contra 28%. Isso é realista e é exatamente o cenário em que a análise de sobrevivência é necessária. Uma média ingênua dos tempos observados subestimaria muito o tempo até o evento das solares.

---

## 8. Tipo do evento

Quando `evento = 1`, uma categoria é sorteada conforme a fonte (`TIPOS_EVENTO`):

| Fonte | Tipo | Probabilidade |
|---|---|---|
| Eólica | `caixa_multiplicadora` | 30% |
| Eólica | `sistema_eletrico` | 30% |
| Eólica | `pas` | 20% |
| Eólica | `gerador` | 20% |
| Solar | `inversor` | 55% |
| Solar | `rastreador` | 20% |
| Solar | `modulos` | 15% |
| Solar | `transformador` | 10% |

O tipo é sorteado **depois** do tempo e **independentemente** dele e das covariáveis. Ele serve para descrição e para o frontend, mas **não** é um cenário de riscos competitivos: não há relação entre o tipo e o momento da falha. Veja [§14](#14-limitações-e-premissas).

O sorteio usa o mesmo gerador com semente, então também é reprodutível.

---

## 9. Esquema do arquivo de saída

`dados/simulados/eventos_manutencao_simulados.parquet`: uma linha por usina, 13 colunas.

| Coluna | Tipo | Exemplo | Descrição | Origem |
|---|---|---|---|---|
| `id_usina` | int64 | `1` | Identificador sequencial (ordenado por CEG) | gerado |
| `ceg` | string | `EOL.CV.BA.030283-0.1` | Código CEG da ANEEL: chave para cruzar com o cadastro e, via núcleo do CEG, com o ONS | real |
| `fonte` | string | `eolica` / `solar` | Fonte de geração | real |
| `sig_tipo_geracao` | string | `EOL` / `UFV` | Sigla original da ANEEL | real |
| `id_estado` | string | `BA` | UF principal | real |
| `id_subsistema` | string | `NE` | Subsistema do SIN (N, NE, SE, S), derivado da UF | derivado |
| `potencia_mw` | float64 | `35.07` | Potência outorgada em MW | real |
| `data_entrada_operacao` | datetime | `2012-07-06` | Início da contagem do tempo | real |
| `data_corte` | datetime | `2026-09-18` | Fim da janela de observação (data do retrato ANEEL) | real |
| `tempo_anos` | float64 | `6.87` | $\min(T, C)$ em anos. **Variável de duração** | **simulado** |
| `evento` | int64 | `1` / `0` | 1 = evento observado, 0 = censura à direita | **simulado** |
| `data_evento` | datetime | `2019-05-20` / nulo | Data do 1º evento (nula se censurada) | **simulado** |
| `tipo_evento` | string | `caixa_multiplicadora` / nulo | Componente/sistema afetado (nulo se censurada) | **simulado** |

Invariantes verificados na geração atual:

- `tempo_anos > 0` para todas as linhas;
- `evento = 1` ⟺ `data_evento` não nula ⟺ `tipo_evento` não nulo;
- `data_evento ≤ data_corte`;
- `data_entrada_operacao + tempo_anos ≈ data_evento` (eventos) ou `≈ data_corte` (censuradas).

---

## 10. Metadados e reprodutibilidade

Junto ao parquet é gravado `eventos_manutencao_simulados.meta.json`:

```json
{
  "gerado_em": "2026-09-18T16:08:52",
  "descricao": "Dados SINTÉTICOS de 1º evento de manutenção corretiva/falha por usina.",
  "fonte_usinas": "dados/bruto/dados_aneel_bruto.parquet",
  "seed": 42,
  "potencia_minima_mw": 1.0,
  "data_minima_entrada": "1990-01-01",
  "modelo": "Weibull de riscos proporcionais, linha de base por fonte",
  "weibull_por_fonte": {"eolica": {"k": 1.6, "lambda_anos": 5.5}, "solar": {"k": 1.3, "lambda_anos": 7.0}},
  "beta_verdadeiro": {"log_potencia_mw_c": 0.2, "subsistema_NE": 0.25, "subsistema_S": 0.1, "subsistema_N": 0.15, "ano_entrada_c": -0.04},
  "referencias_centralizacao": {"potencia_mw": 30.0, "ano_entrada": 2018},
  "tipos_evento": { "...": "..." },
  "n_usinas": 1854,
  "n_eventos": 1018
}
```

Esse arquivo é a **"verdade de referência"** ([§12](#12-validação-o-modelo-recupera-os-parâmetros)). Qualquer notebook de validação deve ler os parâmetros daqui, e não copiá-los à mão.

**Reprodutibilidade:** a saída é função determinística de (bruto da ANEEL, `seed`, `potencia_minima_mw`). Ao regenerar o bruto da ANEEL em outro dia, a população muda (usinas novas entram em operação, a data de corte avança) e, portanto, os dados simulados também mudam, mesmo com a mesma semente. Só `gerado_em` muda entre execuções idênticas.

---

## 11. Perfil do conjunto gerado

Execução com `seed = 42`, sobre o bruto ANEEL de 18/09/2026.

### 11.1 Totais

| Fonte | Usinas | Eventos | Censuradas | % censura | Potência mediana |
|---|---|---|---|---|---|
| Eólica | 1.123 | 807 | 316 | 28,1% | 29,7 MW |
| Solar | 731 | 211 | 520 | 71,1% | 32,0 MW |
| **Total** | **1.854** | **1.018** | **836** | **45,1%** | — |

### 11.2 Distribuição por subsistema

| Subsistema | Eólica | Solar | Total |
|---|---|---|---|
| NE | 1.012 | 362 | 1.374 |
| SE | **1** | 324 | 325 |
| S | 95 | 36 | 131 |
| N | 15 | 9 | 24 |

O parque eólico está quase todo no Nordeste. **Há só uma eólica no Sudeste/Centro-Oeste**, que é a categoria de referência. Isso tem consequências importantes para a estimação ([§12.3](#123-por-que-os-efeitos-regionais-são-imprecisos)).

### 11.3 Sobrevivência empírica (Kaplan-Meier)

| Fonte | Mediana KM | $\hat S(5\ \text{anos})$ |
|---|---|---|
| Eólica | 3,7 anos | 0,36 |
| Solar | 5,4 anos | 0,56 |

As medianas empíricas ficam abaixo das medianas da usina de referência ([§6.1](#61-linha-de-base-por-fonte-weibull)) porque a maior parte das usinas está no NE, cujo risco é maior.

### 11.4 Eventos por ano calendário

Os eventos crescem com o tamanho do parque: poucos antes de 2012, cerca de 90 por ano entre 2019 e 2023, e 102 a 132 por ano em 2024–2026 (2026 até setembro).

---

## 12. Validação: o modelo recupera os parâmetros?

### 12.1 Cox estratificado por fonte (recupera $\beta$)

```python
CoxPHFitter().fit(X, "tempo_anos", "evento", strata=["fonte"])
```

A estratificação dá a cada fonte seu próprio risco de base não paramétrico, como no gerador, e estima um $\beta$ comum.

| Covariável | $\beta$ verdadeiro | $\hat\beta$ | IC 95% | Verdade no IC? |
|---|---|---|---|---|
| `log_potencia_mw_c` | 0,20 | 0,178 | [0,079; 0,277] | ✅ |
| `subsistema_NE` | 0,25 | 0,046 | [−0,236; 0,329] | ✅ |
| `subsistema_S` | 0,10 | 0,017 | [−0,339; 0,374] | ✅ |
| `subsistema_N` | 0,15 | 0,286 | [−0,262; 0,835] | ✅ |
| `ano_entrada_c` | −0,04 | −0,042 | [−0,062; −0,023] | ✅ |

C-index = 0,545. É baixo, e isso é esperado: os efeitos simulados são modestos e a maior parte da variação no tempo até a falha é aleatória (a parte Weibull), como na vida real.

**Os 5 valores verdadeiros estão dentro dos ICs de 95%.** Potência e ano de entrada são estimados com boa precisão. Os efeitos regionais têm ICs largos ([§12.3](#123-por-que-os-efeitos-regionais-são-imprecisos)).

### 12.2 Weibull AFT por fonte (recupera $k$)

Ajustando uma regressão Weibull AFT separada para cada fonte, com as mesmas covariáveis:

| Fonte | $k$ verdadeiro | $\hat k$ (AFT) | $\hat k$ (Weibull marginal, sem covariáveis) |
|---|---|---|---|
| Eólica | 1,6 | 1,56 | 1,59 |
| Solar | 1,3 | 1,43 | 1,49 |

A forma é bem recuperada nas eólicas. Nas solares, a estimativa fica um pouco acima com 71% de censura e só 211 eventos, dentro do esperado para essa quantidade de informação.

Relação PH ↔ AFT no Weibull, usada para comparar: $\beta_{PH} = -\,k \cdot \beta_{AFT}$.

### 12.3 Por que os efeitos regionais são imprecisos

- Entre as **eólicas**, a categoria de referência (SE) tem **uma única usina**. Dentro desse estrato, o contraste NE × SE praticamente não é identificável. No AFT só com eólicas, o intercepto ($\hat\lambda = 3{,}35$ anos, contra 5,5 verdadeiros) fica "ancorado" nessa única usina, e os coeficientes regionais compensam. A escala efetiva estimada no NE ($\approx 4{,}9$ anos) bate com a verdadeira ($5{,}5 \cdot e^{-0{,}25/1{,}6} \approx 4{,}7$ anos).
- Na prática, os efeitos regionais são estimados **quase só pelas solares** (324 no SE, 362 no NE), que têm poucos eventos.
- O subsistema N tem só 24 usinas.

Isso **não é defeito da simulação**, e sim reflexo da geografia real do parque. É uma lição útil para a modelagem real: com essa distribuição, não é possível estimar bem efeitos regionais para eólicas.

### 12.4 Checagens de sanidade

Automatizáveis em testes (ver [§15](#15-próximos-passos)):

- invariantes da [§9](#9-esquema-do-arquivo-de-saída);
- a mesma semente gera um parquet idêntico (exceto `gerado_em` no `.meta.json`);
- a proporção de cada `tipo_evento` fica próxima de `TIPOS_EVENTO` (por exemplo, inversor = 119/211 = 56% contra 55% esperado).

---

## 13. Como usar na análise de sobrevivência

### 13.1 Kaplan-Meier por fonte (via `ds_toolkit`)

```python
import pandas as pd
import ds_toolkit as dst

df = pd.read_parquet("dados/simulados/eventos_manutencao_simulados.parquet")
res = dst.kaplan_meier(df, "tempo_anos", "evento", coluna_grupo="fonte", tabela_risco=True)
res["medianas"]      # mediana e IC por fonte
res["logrank"]       # teste de diferença entre curvas
```

### 13.2 Diagnóstico da forma do risco

```python
dst.risco_acumulado(df, "tempo_anos", "evento", coluna_grupo="fonte")
dst.modelos_parametricos_sobrevivencia(df[df.fonte == "eolica"], "tempo_anos", "evento")
```

- O risco acumulado de Nelson-Aalen deve ser **côncavo para cima** (risco crescente, $k>1$).
- Na comparação por AIC, o Weibull deve ganhar ou empatar. A log-normal pode competir no ajuste marginal, porque a mistura de covariáveis distorce a forma.

### 13.3 Cox: use `fonte` como **estrato**

```python
import numpy as np
from lifelines import CoxPHFitter

X = pd.DataFrame({
    "tempo_anos": df.tempo_anos,
    "evento": df.evento,
    "fonte": df.fonte,
    "log_potencia_mw_c": np.log(df.potencia_mw) - np.log(30),
    "subsistema_NE": (df.id_subsistema == "NE").astype(float),
    "subsistema_S": (df.id_subsistema == "S").astype(float),
    "subsistema_N": (df.id_subsistema == "N").astype(float),
    "ano_entrada_c": df.data_entrada_operacao.dt.year - 2018,
})
cph = CoxPHFitter().fit(X, "tempo_anos", "evento", strata=["fonte"])
cph.print_summary()
```

Colocar `fonte` como covariável comum (`dst.cox_ph`, que não estratifica) **viola a hipótese de riscos proporcionais** por construção, porque as fontes têm formas $k$ diferentes. O teste de Schoenfeld deve acusar isso. Esse é um bom exercício didático para mostrar o diagnóstico funcionando.

### 13.4 Predição para o endpoint da API

```python
novas = X.drop(columns=["tempo_anos", "evento"]).head(3)
cph.predict_survival_function(novas, times=[1, 3, 5, 10])   # S(t) por usina
cph.predict_median(novas)                                   # tempo mediano até o 1º evento
```

---

## 14. Limitações e premissas

| # | Limitação / premissa | Impacto | Como mitigar |
|---|---|---|---|
| 1 | **Os eventos são sintéticos** | Nenhuma conclusão sobre confiabilidade real | Sempre rotular como simulado na API/frontend e trocar por dado real se houver |
| 2 | **Só o 1º evento** por usina | Não há eventos recorrentes; após a falha a usina sai do risco | Estender para processo de renovação/recorrente (Andersen-Gill, PWP) |
| 3 | `tipo_evento` independente do tempo | Não serve para riscos competitivos (Fine-Gray, cause-specific) | Gerar um tempo latente por causa e observar o mínimo |
| 4 | Linha de base igual para todas as usinas da mesma fonte | Não há efeito de fabricante/modelo de equipamento, que não está no cadastro público | Adicionar fragilidade (*frailty*) gama por usina ou por grupo |
| 5 | Efeitos das covariáveis constantes no tempo | Hipótese PH vale por construção dentro da fonte | Para testar diagnósticos, gerar uma variante com efeito tempo-dependente |
| 6 | Censura apenas administrativa | Não há perda de acompanhamento nem descomissionamento | Adicionar censura aleatória exponencial independente |
| 7 | População = usinas **hoje** em operação | Usinas já descomissionadas não aparecem (viés de sobrevivente no cadastro) | Pequeno para UFV/EOL, parque jovem; documentado |
| 8 | Parâmetros são premissas de ordem de grandeza | Valores absolutos (medianas de 4–5 anos) podem não refletir o setor | Calibrar com literatura de O&M ou dados de operadores se disponíveis |
| 9 | Subsistema derivado da UF principal | Usinas em mais de um município/UF usam só a principal | Irrelevante nesta escala |
| 10 | Tempo contado da entrada em **operação comercial** | Falhas no comissionamento não entram | Coerente com a definição de "evento de manutenção corretiva em operação" |

---

## 15. Próximos passos

1. **Testes automatizados** (`pytest`): invariantes da [§9](#9-esquema-do-arquivo-de-saída), determinismo por semente e recuperação de $\beta$ dentro do IC em uma amostra grande (por exemplo, replicar a população 10×).
2. **Estudo de Monte Carlo:** gerar K conjuntos com sementes diferentes e medir viés e cobertura dos ICs do Cox. Isso mostra, com números, a imprecisão regional da [§12.3](#123-por-que-os-efeitos-regionais-são-imprecisos).
3. **Variantes do gerador** controladas por flags: *frailty*, eventos recorrentes, riscos competitivos por `tipo_evento` e censura aleatória.
4. **Carga no DuckDB:** tabela `fato_evento_manutencao` com a coluna `simulado = TRUE`, e o endpoint `/usinas/{id}/sobrevivencia` devolvendo esse aviso no payload.
5. **Cruzamento com ONS/NASA:** usar clima (vento a 50 m, temperatura) e fator de capacidade real como covariáveis na próxima versão do gerador, para que o modelo de sobrevivência tenha sinal vindo das outras fontes do projeto.

---

*Documento gerado em 18/09/2026 a partir do código e da execução real de `dados_simulados.py` (seed 42).*
