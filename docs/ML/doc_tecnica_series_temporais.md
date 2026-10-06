# Documentação Técnica — Previsão de Geração (Séries Temporais)

> Escopo: tudo sobre `ML/series_temporais/` — as séries usadas, os quatro modelos comparados, as features, o protocolo de backtesting com janela deslizante, as métricas (e as armadilhas delas), os resultados medidos e os modelos salvos em `ML/modelos/`.
>
> Documentos relacionados:
> - [`docs/DB/doc_tecnica_db.md`](../DB/doc_tecnica_db.md): o banco de onde as séries vêm.
> - [`docs/ETL/doc_tecnica_etl.md`](../ETL/doc_tecnica_etl.md): como geração e clima foram limpos e cruzados.
> - [`docs/ingestao/doc_tecnica_ingestao.md`](../ingestao/doc_tecnica_ingestao.md): origem e latência dos dados de clima.

---

## Sumário

1. [Objetivo e definição do problema](#1-objetivo-e-definição-do-problema)
2. [Estrutura da pasta](#2-estrutura-da-pasta)
3. [Como executar](#3-como-executar)
4. [As séries (`dados.py`)](#4-as-séries-dadospy)
5. [Features (`features.py`)](#5-features-featurespy)
6. [Modelos (`modelos.py`)](#6-modelos-modelospy)
7. [Backtesting com janela deslizante (`backtesting.py`)](#7-backtesting-com-janela-deslizante-backtestingpy)
8. [Métricas (`metricas.py`)](#8-métricas-metricaspy)
9. [Resultados](#9-resultados)
10. [Importância das features](#10-importância-das-features)
11. [Modelos salvos (`ML/modelos/`)](#11-modelos-salvos-mlmodelos)
12. [Como usar um modelo salvo](#12-como-usar-um-modelo-salvo)
13. [Premissas e riscos declarados](#13-premissas-e-riscos-declarados)
14. [Testes automatizados](#14-testes-automatizados)
15. [Limitações conhecidas](#15-limitações-conhecidas)

---

## 1. Objetivo e definição do problema

Prever a **geração horária das próximas 24 horas**, para alimentar o endpoint `/usinas/{id}/previsao` da API (system design §5).

| Elemento | Definição |
|---|---|
| Alvo | `energia_mwh` por hora (`fato_geracao`) |
| Horizonte | **24 h** (o dia seguinte inteiro), previstas de uma vez |
| Granularidade | Horária, em horário de Brasília |
| Séries | Uma por fonte (`solar`, `eolica`) — agregado nacional. Opcionalmente por usina |
| Exógenas | Clima diário da `fato_clima`: irradiância, vento a 50 m e a 10 m, temperatura média, máxima e mínima |
| Validação | Backtesting com janela deslizante, 6 janelas |
| Métrica principal | RMSE (em MWh). MAPE, sMAPE e nRMSE como complemento |

**Por que 24 h de uma vez, sem recursão:** todas as features derivadas do alvo usam defasagem **≥ 24 h**. Assim, a previsão de qualquer hora do horizonte depende apenas de dado conhecido no momento do corte, sem precisar realimentar a previsão no modelo (o que acumula erro) e sem risco de vazamento.

---

## 2. Estrutura da pasta

```
ML/
├── series_temporais/
│   ├── __init__.py
│   ├── config.py        # horizonte, janelas, defasagens, ordens do SARIMA, piso do MAPE
│   ├── dados.py         # carga das séries (DuckDB ou Parquet) + clima alinhado
│   ├── features.py      # calendário + defasagens + clima, com guarda-corpo anti-vazamento
│   ├── modelos.py       # 4 modelos com a mesma interface ajustar/prever
│   ├── backtesting.py   # janela deslizante (rolling origin)
│   ├── metricas.py      # RMSE, MAE, MAPE, sMAPE, nRMSE, viés
│   ├── treinar.py       # orquestra: backtest → comparação → reajuste → pickle
│   ├── tests/           # 32 testes: features anti-vazamento, métricas, baselines, backtesting
│   └── resultados/      # gráficos gerados (backtest_solar.png, backtest_eolica.png)
└── modelos/             # artefatos: pickles + metadados + métricas + previsões
```

---

## 3. Como executar

```bash
python -m ML.series_temporais.treinar                   # 2 séries (solar e eólica), ~7 min
python -m ML.series_temporais.treinar --sem-sarima      # ~15 s (o SARIMA domina o tempo)
python -m ML.series_temporais.treinar --series ambas    # + as 3 maiores usinas
python -m ML.series_temporais.treinar --usinas 12 42 --series usina
python -m ML.series_temporais.treinar --janelas 10 --horizonte 24
python -m ML.series_temporais.treinar --sem-salvar --sem-graficos   # só avaliar
```

| Argumento | Padrão | Efeito |
|---|---|---|
| `--series` | `fonte` | `fonte`, `usina` ou `ambas` |
| `--fontes` | todas | Ex.: `--fontes solar` |
| `--usinas` / `--top-n` | — / 3 | IDs específicos, ou as N usinas de maior geração |
| `--janelas` | 6 | Janelas do backtesting |
| `--horizonte` | 24 | Horas previstas por janela |
| `--sem-sarima` | — | Pula o SARIMA (o modelo caro) |
| `--sem-graficos`, `--sem-salvar` | — | Não gera PNG / não grava o pickle |

Pré-requisito: `DB/solarwatch.duckdb` (ou, na falta dele, os Parquets da camada curated, que o `dados.py` lê automaticamente).

---

## 4. As séries (`dados.py`)

### 4.1 Origem e montagem

```sql
-- geração (por fonte)
SELECT timestamp_utc AS data_hora, sum(energia_mwh) AS energia_mwh
FROM fato_geracao WHERE fonte = ? GROUP BY 1 ORDER BY 1;

-- clima médio das usinas daquela fonte (grão diário)
SELECT c.data, avg(c.irradiancia_kwh_m2), avg(c.vento_ms), avg(c.temperatura_c), ...
FROM fato_clima c JOIN dim_usina u USING (usina_id) WHERE u.fonte = ? GROUP BY 1;
```

Depois:

1. o índice é convertido para **horário de Brasília** e recebe `asfreq("h")`, o que torna a grade horária explícita (o SARIMA exige frequência definida);
2. o clima **diário** é repetido nas 24 horas do dia correspondente, casando pela data local;
3. horas sem geração na grade geram aviso no log.

### 4.2 Perfil das séries (dados de 18/09/2026)

| Série | Pontos | Período | Média | Zeros | Observação |
|---|---|---|---|---|---|
| `solar` | 1.896 h | 01/07 → 17/09/2026 | 12.632 MWh/h | **26,6%** | Ciclo diário forte; zero à noite |
| `eolica` | 1.896 h | 01/07 → 17/09/2026 | 14.938 MWh/h | 0% | Ciclo diário mais fraco, mais ruído |

79 dias de histórico são **pouco** para séries temporais: não há ciclo anual, e a sazonalidade semanal é frágil. Está registrado em [§15](#15-limitações-conhecidas).

---

## 5. Features (`features.py`)

27 colunas, em três blocos:

| Bloco | Features | Observação |
|---|---|---|
| **Calendário** | hora, dia da semana, mês, fim de semana + seno/cosseno de hora, dia da semana e mês | Gerado por `ds_toolkit.criar_features_data(ciclicas=True)`. A codificação cíclica evita que a hora 23 pareça "distante" da hora 0 |
| **Defasagens do alvo** | `lag_24h`, `lag_25h`, `lag_26h`, `lag_48h`, `lag_72h`, `lag_168h` | Mesma hora de 1, 2, 3 e 7 dias atrás |
| **Estatísticas móveis** | `media_*` e `desvio_*` de 24 h, 72 h e 168 h | Calculadas **após** deslocar a série pelo horizonte |
| **Clima (exógenas)** | irradiância, vento 50 m, vento 10 m, temperatura média/máx/mín | Valor diário repetido nas 24 h |

### 5.1 Garantia anti-vazamento

```python
invalidas = [lag for lag in lags if lag < horizonte]
if invalidas:
    raise ValueError(f"Defasagens menores que o horizonte causariam vazamento: {invalidas} < {horizonte}")
```

As médias móveis seguem a mesma regra: `X[ALVO].shift(horizonte).rolling(janela).mean()`. No instante `t`, `media_24h` cobre de `t−47h` a `t−24h`.

**Testes que rodei sobre essa garantia:**

| Teste | Resultado |
|---|---|
| Multiplicar por 10 o alvo do horizonte e reconstruir as features do horizonte | Features idênticas ✅ |
| `lag_24h` em `t` é igual a `y(t − 24h)` | Confere ✅ |
| `media_24h` em `t` é a média de `y` entre `t−47h` e `t−24h` | Confere ✅ |
| Configurar `lags=[1, 24]` | `ValueError` ✅ |

---

## 6. Modelos (`modelos.py`)

Todos implementam a mesma interface, então o backtesting os trata igual e a comparação é honesta:

```python
modelo.ajustar(historico)   # DataFrame com alvo + exógenas
modelo.prever(futuro)       # DataFrame só com exógenas → Series de previsão
```

| Modelo | O que faz | Papel |
|---|---|---|
| `NaiveSazonal` | ŷ(t) = y(t − 24h) | **Baseline**. Em série com ciclo diário forte, é difícil de bater e custa zero |
| `MediaMovelSazonal` | Média da mesma hora nos últimos 7 dias | Baseline que filtra ruído |
| `Sarima` | `SARIMAX(1,0,1)(1,1,1,24)` | **Modelo 1**: estatístico, interpretável, sem exógenas |
| `GradientBoosting` | `HistGradientBoostingRegressor` com as 27 features | **Modelo 2**: múltiplas exógenas, não linearidades |

Detalhes de implementação:

- **SARIMA:** sazonalidade de 24 h. `enforce_stationarity=False` e `enforce_invertibility=False` para o otimizador não travar, `maxiter=50` para limitar o custo. As ordens são pequenas de propósito: `(1,0,1)(1,1,1,24)` já custa ~30 s por ajuste em 1.900 pontos. A previsão é truncada em zero, porque geração não é negativa.
- **Gradient Boosting:** `max_iter=400`, `learning_rate=0.06`, `max_depth=6`, `l2_regularization=1.0`, semente fixa. O `HistGradientBoostingRegressor` lida com `NaN` nativamente, o que importa porque as defasagens longas deixam nulos no início da série. Na previsão, histórico e futuro são concatenados e as features são reconstruídas: como toda defasagem é ≥ horizonte, os valores usados caem dentro do histórico.

---

## 7. Backtesting com janela deslizante (`backtesting.py`)

### 7.1 O protocolo

```
|------------------ treino ------------------|-- teste 24h --|
|------------------ treino --------------------------|-- teste 24h --|
|------------------ treino -----------------------------------|-- teste 24h --|
```

- **Origem expansível:** o treino é sempre todo o passado até o corte; a origem avança de 24 em 24 h.
- **Janelas de teste sem sobreposição:** cada hora avaliada aparece em exatamente uma janela.
- **Reajuste em cada janela:** nenhum modelo "vê" o futuro de janelas anteriores.
- **Mínimo de treino:** 30 dias (`MINIMO_TREINO_H = 720`) antes da primeira previsão.
- **Verificação explícita:** `assert treino.index.max() < teste.index.min()` antes de cada previsão.

Na execução atual, com 6 janelas, as origens vão de 12/09 a 17/09/2026.

### 7.2 O erro que isso evita

`train_test_split` com embaralhamento, ou `KFold` comum, colocam horas futuras no treino e horas passadas no teste. Em série com forte ciclo diário isso produz métricas boas demais, porque o modelo aprende a interpolar dentro do mesmo dia em vez de extrapolar para o dia seguinte. Nenhuma função de divisão aleatória é usada neste módulo.

### 7.3 Robustez

Se um modelo falhar em uma janela (o SARIMA pode não convergir), o erro é registrado no log e os demais continuam. A comparação fica explícita no campo `janelas` da tabela de métricas.

---

## 8. Métricas (`metricas.py`)

| Métrica | Definição | Quando usar |
|---|---|---|
| `rmse` | Raiz do erro quadrático médio (MWh) | **Principal**. Penaliza erro grande, que é o que dói na operação |
| `mae` | Erro absoluto médio (MWh) | Menos sensível a outliers |
| `mape_%` | Erro percentual absoluto médio | Comunicação. **Só acima do piso** (veja abaixo) |
| `smape_%` | MAPE simétrico | Complemento, mesmo piso |
| `nrmse_%` | RMSE / média da série | Compara séries de escalas diferentes, usando **todos** os pontos |
| `vies` | Média do erro (y − ŷ) | Positivo = o modelo subestima |
| `cobertura_mape_smape_%` | Fração de horas acima do piso | Transparência sobre o MAPE |

### 8.1 A armadilha do MAPE em geração solar

26,6% das horas solares têm geração **zero** (noite). Como o MAPE divide pelo observado, esses pontos dariam infinito, e qualquer média seria inútil ou enganosa. Duas saídas comuns e ruins: somar epsilon ao denominador (inventa um número) ou remover a noite sem dizer (esconde metade do dado).

A escolha aqui: **calcular MAPE e sMAPE apenas onde `y > 5% da média da série`** e **reportar a cobertura**. Na série solar a cobertura é de 50%, ou seja, metade das horas não entra nessas métricas. Para avaliação sobre todas as horas, incluindo a noite, a métrica é o **nRMSE**.

O sMAPE sofre do mesmo problema: com `y = 0` e `ŷ > 0`, cada ponto vale 200%. Numa versão anterior deste módulo ele aparecia como 90,6% na série solar por causa disso, e por isso passou a usar o mesmo piso.

---

## 9. Resultados

Execução de 06/10/2026 (retreino com o clima dos 19 pontos): 6 janelas, horizonte de 24 h, 144 horas avaliadas por série.

### 9.1 Tabela comparativa

| Série | Modelo | RMSE | MAE | MAPE | sMAPE | nRMSE | Viés | Ganho vs. baseline | s/ajuste |
|---|---|---|---|---|---|---|---|---|---|
| eólica | **gradient_boosting** | **1.701** | 1.361 | 11,9% | 10,9% | 10,9% | +135 | **+38,1%** | 3,3 |
| eólica | sarima | 2.114 | 1.635 | 17,1% | 14,4% | 13,5% | −75 | +23,0% | 23,9 |
| eólica | media_movel_sazonal | 2.386 | 1.850 | 21,0% | 16,5% | 15,2% | −454 | +13,1% | 0,02 |
| eólica | naive_sazonal | 2.747 | 2.142 | 19,9% | 19,3% | 17,5% | +167 | — | 0,00 |
| solar | **gradient_boosting** | **832** | 519 | 4,8% | 4,7% | 6,2% | −65 | **+59,0%** | 0,8 |
| solar | media_movel_sazonal | 1.414 | 776 | 6,2% | 6,3% | 10,5% | +206 | +30,3% | 0,01 |
| solar | sarima | 1.601 | 891 | 7,1% | 7,1% | 11,9% | −11 | +21,0% | 11,2 |
| solar | naive_sazonal | 2.027 | 1.132 | 8,8% | 8,7% | 15,0% | −161 | — | 0,00 |

Leitura:

1. **O Gradient Boosting vence nas duas fontes**, com 38,1% e 59,0% menos RMSE que o baseline — o resultado que o escopo previa para dados com várias exógenas.
2. **A ordem dos outros modelos muda por fonte.** Na eólica, o SARIMA fica em segundo; na solar, ele perde até para a média móvel. Faz sentido: a curva solar é quase determinística (nasce e põe o sol), então repetir a média dos últimos dias já é forte, enquanto o SARIMA gasta graus de liberdade tentando modelar a dinâmica.
3. **Custo:** o Gradient Boosting treina em ~1–3 s e o SARIMA em ~11–24 s, ou seja, é bem mais rápido **e** mais preciso.
4. **Viés:** todos pequenos perto do RMSE. O Gradient Boosting subestima a eólica em ~135 MWh/h (1% da média).

### 9.1.1 O efeito do clima novo (antes × depois)

Os três baselines **não usam clima**, e por isso saíram com RMSE idêntico ao da execução de 19/09 — o que faz desta comparação um teste controlado: a única coisa que mudou foi o clima que entra no Gradient Boosting ([ETL §8.5](../ETL/doc_tecnica_etl.md#85-fato_clima--grão-usina--dia)).

| Série | GB com clima de 10 pontos | GB com clima de 19 pontos | **Sem nenhuma coluna de clima** |
|---|---|---|---|
| eólica | 1.904 | **1.701** (−10,7%) | 2.235 |
| solar | 1.079 | **832** (−22,9%) | 1.058 |

A terceira coluna é uma ablação: o mesmo backtesting com as colunas de clima removidas da série. Ela mostra o que o número agregado escondia — **o clima dos 10 pontos não valia nada na solar** (1.079 com clima contra 1.058 sem, diferença dentro do ruído de 6 janelas), enquanto o dos 19 pontos vale **21%** de RMSE. Na eólica o clima já ajudava (2.235 → 1.904, 14,8%) e passou a ajudar mais (→ 1.701, 23,9%).

A explicação é espacial: com 10 pontos escolhidos à mão, a média por fonte misturava parques a 800 km do ponto de medição e deixava o subsistema Norte sem clima nenhum; com 19 pontos derivados das coordenadas das usinas, a mediana usina → ponto caiu de 100 km para 65 km e todas as 308 unidades entraram na média.

### 9.2 Estabilidade entre janelas (RMSE por janela)

| Série | Modelo | Média | Desvio | Mín | Máx |
|---|---|---|---|---|---|
| eólica | gradient_boosting | 1.674 | **333** | 1.284 | 2.271 |
| eólica | sarima | 1.975 | 826 | 975 | 3.067 |
| eólica | media_movel_sazonal | 2.210 | 987 | 1.200 | 3.634 |
| eólica | naive_sazonal | 2.542 | 1.141 | 1.237 | 3.901 |
| solar | gradient_boosting | 825 | **122** | 654 | 1.001 |
| solar | media_movel_sazonal | 1.266 | 690 | 580 | 2.261 |
| solar | sarima | 1.468 | 700 | 630 | 2.597 |
| solar | naive_sazonal | 1.884 | 820 | 781 | 3.057 |

Além de ter o menor erro, o Gradient Boosting é o **mais estável**: na solar, o desvio entre janelas é de 122 contra 690–820 dos outros. Em produção isso importa tanto quanto a média, porque erro imprevisível é pior que erro constante. O clima novo melhorou as duas pontas: o desvio da solar caiu de 213 para 122 e o da eólica de 557 para 333, e a pior janela da solar passou de 1.259 para 1.001.

> Atenção: são só 6 janelas. O desvio acima é indicativo, não um intervalo de confiança.

### 9.3 Gráficos

`ML/series_temporais/resultados/backtest_solar.png` e `backtest_eolica.png` mostram a última janela: três dias de contexto, a linha de corte e as quatro previsões contra o observado.

Na série solar as quatro curvas quase se sobrepõem — o ciclo diário é tão dominante que qualquer método acerta a forma. A diferença de RMSE está na **amplitude do pico**, que é o que o Gradient Boosting acerta melhor.

---

## 10. Importância das features

Importância por permutação (`GradientBoosting.importancias`), em % do total:

| Série | 1º | 2º | 3º | Clima (soma) |
|---|---|---|---|---|
| solar | `lag_24h` 82,5% | `hora_cos` 8,7% | `lag_168h` 6,8% | **0,4%** |
| eólica | `hora_cos` 57,3% | `lag_24h` 17,9% | **`vento_ms` 13,4%** | **14,0%** |

- **Na eólica o clima ajuda, e de forma visível:** o vento a 50 m sozinho responde por 13,4% da importância (eram 11,2% com os 10 pontos). É o efeito esperado de uma exógena física relevante.
- **Na solar a importância do clima continua em 0,4%** — e aqui está a lição metodológica deste retreino: **esse número está errado como medida de utilidade.** A ablação da [§9.1.1](#911-o-efeito-do-clima-novo-antes--depois) mostra que remover as colunas de clima piora o RMSE da solar em 21%, ou seja, o clima vale um quinto do erro. A permutação não vê isso porque é medida **dentro da amostra**, num modelo em que `lag_24h` carrega 82% da explicação: embaralhar a irradiância quase não muda o ajuste in-sample, mas treinar sem ela muda a previsão fora da amostra.

> Moral: importância por permutação in-sample responde "de que o modelo já ajustado depende para reproduzir o treino", não "que features valem a pena coletar". A segunda pergunta se responde com ablação no backtesting, e as duas podem discordar por duas ordens de grandeza — como aqui.

Isso reforça o item 1 da [§15](#15-limitações-conhecidas): o ganho da solar veio só de aproximar o ponto de medição (100 km → 65 km de mediana). Clima **horário e por usina** é o próximo degrau, e a granularidade diária continua sendo o teto: a irradiância é idêntica nas 24 horas do dia.

---

## 11. Modelos salvos (`ML/modelos/`)

| Arquivo | Conteúdo |
|---|---|
| `previsao_solar.pkl` | Modelo vencedor da série solar (~950 KB) |
| `previsao_solar.pkl.meta.json` | Metadados: modelo, horizonte, período de treino, métricas do backtesting, exógenas |
| `previsao_eolica.pkl` + `.meta.json` | Idem para a eólica (~1,1 MB) |
| `metricas_backtesting.parquet` | A tabela da [§9.1](#91-tabela-comparativa) |
| `previsoes_backtesting.parquet` | Painel longo: série, modelo, janela, hora, observado, previsto |

O pickle é gravado por `ds_toolkit.salvar_modelo` (joblib), que já anexa o `.meta.json` com versões de biblioteca e data. Exemplo de metadados:

```json
{
  "serie": "solar", "modelo": "gradient_boosting", "horizonte_h": 24,
  "treino_inicio": "2026-07-01 00:00:00-03:00", "treino_fim": "2026-09-17 23:00:00-03:00",
  "n_observacoes": 1896,
  "metricas_backtesting": {"rmse": 832.2, "mae": 519.1, "mape_%": 4.76, "nrmse_%": 6.16, "ganho_vs_baseline_%": 58.95},
  "exogenas": ["irradiancia_kwh_m2", "vento_ms", "temperatura_c", "temperatura_max_c", "temperatura_min_c"],
  "observacao": "Clima do horizonte tratado como conhecido (ver features.py)."
}
```

**O que vai dentro do pickle:** o objeto `GradientBoosting` completo — o estimador treinado, a lista de colunas de feature e o **histórico** da série. O histórico é necessário porque as defasagens da previsão saem dele. Isso explica o tamanho do arquivo (~1 MB) e significa que o modelo precisa ser regravado quando chegar dado novo.

**Escolha do modelo:** o de menor RMSE no backtesting, reajustado em **todo** o histórico antes de ser salvo. Ou seja, a métrica publicada vem da validação, e o modelo entregue foi treinado com todos os dados disponíveis.

---

## 12. Como usar um modelo salvo

```python
import pandas as pd
import ds_toolkit as dst
from ML.series_temporais import dados

modelo = dst.carregar_modelo("ML/modelos/previsao_solar.pkl")

# 24 h à frente, com o clima previsto para o dia (aqui, proxy: clima do dia anterior)
serie = dados.series_por_fonte(["solar"])["solar"]
futuro = pd.DataFrame(index=pd.date_range(serie.index.max() + pd.Timedelta(hours=1),
                                          periods=24, freq="h"))
for coluna in dados.COLUNAS_CLIMA:
    futuro[coluna] = serie[coluna].iloc[-24:].to_numpy()

previsao = modelo.prever(futuro)      # Series de 24 valores em MWh
```

Testado: o modelo carrega, reconhece as 27 features e devolve as 24 horas. Para a API, o caminho é carregar o pickle uma vez na inicialização do processo (system design §8.4) e chamar `prever` por requisição.

---

## 13. Premissas e riscos declarados

1. **O clima do horizonte é tratado como conhecido.** As features de clima usam o valor **observado** do dia previsto, o que equivale a supor previsão meteorológica perfeita. Em produção entraria uma previsão de verdade, com erro próprio, então o desempenho medido aqui é um **limite superior otimista**. Está no código e nos metadados do modelo.
2. **Só 79 dias de histórico** (jul a set/2026), sem ciclo anual. O modelo não viu verão, e a geração solar e eólica tem forte sazonalidade anual.
3. **6 janelas de backtesting** cobrem 12/09 a 17/09/2026, um período curto e possivelmente atípico.
4. **Séries agregadas por fonte** somam usinas de regimes muito diferentes (eólica do NE e do S). O comportamento por usina é mais ruidoso e mais difícil de prever.
5. **O modelo prevê valores pequenos e positivos à noite** na série solar (dezenas de MWh contra zero real). Não há regra impondo zero fora do dia solar; isso entra no erro, mas é pequeno perto da escala de pico (40 GWh/h).
6. **A geração vem do ONS com flags de qualidade**; horas marcadas como `faltante` entram como nulo na série e são interpoladas só dentro do SARIMA. Na escala nacional o efeito é desprezível (96 horas em 575 mil).

---

## 14. Testes automatizados

```bash
pytest ML/series_temporais/tests -q      # 32 testes, ~13 s
```

O risco número um aqui é **vazamento temporal**: uma feature que, na hora de prever, usa um valor que ainda não existia. Quando isso acontece o erro de backtesting fica ótimo, o de produção fica péssimo, e nada no código reclama. Por isso a maior parte dos testes verifica *fronteiras de tempo*, não números de acurácia:

- **defasagem menor que o horizonte é recusada** com `ValueError` (a trava da [§5.1](#51-garantia-anti-vazamento)), e um teste cobra a invariante na própria configuração: `min(LAGS_H) >= HORIZONTE_H`;
- **o lag traz exatamente o valor de 24 h antes**, comparado elemento a elemento com a série original deslocada;
- **a média móvel é deslocada do horizonte** — o valor esperado é recalculado à mão a partir da janela correta, então um `shift` trocado aparece;
- **`origens()` nunca deixa o treino alcançar o teste**: as janelas emendam de 24 em 24 horas, terminam no fim da série, respeitam o mínimo de treino e uma série curta devolve **menos janelas** em vez de treinar com 3 dias.

A série de teste é uma senoide diária limpa, previsível de propósito: sem ruído, `naive_sazonal` acerta com RMSE zero — o que torna qualquer desvio um bug, não ruído. Com ruído, o teste cobra que o Gradient Boosting **bata o baseline**, que nunca preveja geração negativa e que devolva uma previsão por hora do horizonte.

As métricas têm testes próprios porque é onde o número engana: o **MAPE ignora as horas de geração zero** (metade dos pontos do caso de teste, e a `cobertura_mape_smape_%` reporta isso), o piso é relativo à média da série, o `nrmse_%` usa **todos** os pontos, e o sinal de `vies` é fixado — positivo significa que o modelo previu **menos** que o observado. Um modelo que falha numa janela sai do painel sem derrubar a comparação dos outros, e isso também é testado.

---

## 15. Limitações conhecidas

| # | Limitação | Impacto |
|---|---|---|
| 1 | Clima **diário** e regional: 19 pontos, um por (fonte, UF), com mediana de 65 km até a usina | A granularidade diária é o teto — a irradiância é idêntica nas 24 horas do dia. Aproximar o ponto de 100 km para 65 km já valeu 23% de RMSE na solar e 11% na eólica ([§9.1.1](#911-o-efeito-do-clima-novo-antes--depois)); clima horário e por usina é o degrau seguinte |
| 2 | Histórico de 79 dias | Sem sazonalidade anual; o modelo pode degradar fora da janela observada e a sazonalidade semanal é frágil |
| 3 | Hiperparâmetros fixos | O Gradient Boosting não foi otimizado — qualquer otimização precisaria de validação temporal (`TimeSeriesSplit`), nunca `KFold` comum |
| 4 | Sem intervalo de previsão | A API devolve só o valor pontual |
| 5 | Prophet não avaliado | O escopo citava "SARIMA **ou** Prophet"; o SARIMA cobre o papel de modelo estatístico interpretável |
| 6 | Só horizonte de 24 h validado | O código é parametrizado (`--horizonte`), mas 48 h ou 7 dias exigiriam revalidar — as defasagens mínimas acompanham o horizonte |
| 7 | Modelos por fonte, não por usina | `/usinas/{id}/previsao` precisa ratear a previsão da fonte pela participação histórica da usina. `--series usina` existe no treino, mas não é o que está em produção |
| 8 | Os 32 testes ([§14](#14-testes-automatizados)) cobrem features, métricas, baselines e backtesting | Ficam fora `dados.py` (leitura do banco) e `treinar.py` (orquestração), que dependem do banco |
| 9 | Sem monitoramento de deriva | O modelo envelhece silenciosamente: nada compara o RMSE das janelas recentes com o registrado nos metadados |

---

*Documento gerado em 19/09/2026 a partir do código de `ML/series_temporais/`. Revisado em 06/10/2026 sobre o **retreino** com o clima dos 19 pontos da NASA: resultados do backtesting (§9), importâncias (§10), testes automatizados (§14) e limitações.*
