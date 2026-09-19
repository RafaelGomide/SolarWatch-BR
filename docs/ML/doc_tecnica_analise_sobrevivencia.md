# Documentação Técnica — Análise de Sobrevivência de Ativos

> Escopo: tudo sobre `ML/analise_sobrevivencia/` — Kaplan-Meier por fonte, ajuste paramétrico, Cox de riscos proporcionais, a saída de produto (probabilidade de a usina passar N meses sem manutenção), a avaliação do modelo e os artefatos salvos em `ML/modelos/`.
>
> ⚠️ **Os eventos de manutenção são SINTÉTICOS.** As usinas, potências, regiões e datas são reais (cadastro ANEEL), mas nenhuma falha aconteceu de verdade. Nada aqui diz respeito à confiabilidade real de usinas, fabricantes ou regiões. A geração desses dados está em [`doc_tecnica_dados_simulados.md`](../ingestao/doc_tecnica_dados_simulados.md).
>
> Documentos relacionados: [ETL](../ETL/doc_tecnica_etl.md) (como a `dim_usina` é construída), [banco](../DB/doc_tecnica_db.md) (de onde vêm as usinas da predição), [séries temporais](doc_tecnica_series_temporais.md) (o outro modelo do projeto).

---

## Sumário

1. [Objetivo](#1-objetivo)
2. [Estrutura da pasta](#2-estrutura-da-pasta)
3. [Como executar](#3-como-executar)
4. [Dados e covariáveis](#4-dados-e-covariáveis)
5. [Kaplan-Meier por fonte](#5-kaplan-meier-por-fonte)
6. [Ajuste paramétrico](#6-ajuste-paramétrico)
7. [Cox de riscos proporcionais](#7-cox-de-riscos-proporcionais)
8. [Avaliação](#8-avaliação)
9. [Saída de produto: probabilidade por usina](#9-saída-de-produto-probabilidade-por-usina)
10. [O bug da extrapolação e como foi resolvido](#10-o-bug-da-extrapolação-e-como-foi-resolvido)
11. [Artefatos salvos](#11-artefatos-salvos)
12. [Como usar o modelo salvo](#12-como-usar-o-modelo-salvo)
13. [Decisões metodológicas](#13-decisões-metodológicas)
14. [Limitações e próximos passos](#14-limitações-e-próximos-passos)

---

## 1. Objetivo

Responder, por usina: **qual a probabilidade de passar os próximos N meses sem precisar de manutenção corretiva?**

Isso alimenta o endpoint `/usinas/{id}/sobrevivencia` (system design §5) e um card do frontend. O caminho até lá tem três etapas, cada uma respondendo a uma pergunta diferente:

| Etapa | Pergunta | Método |
|---|---|---|
| Descrever | Como o tempo até manutenção se distribui em solar e eólica? | **Kaplan-Meier** + log-rank |
| Modelar a forma | Qual família descreve melhor esse tempo? O risco cresce com a idade? | **Weibull / Log-Normal / Log-Logística / Exponencial**, comparadas por AIC |
| Explicar e prever | Que fatores aceleram a manutenção, e qual a probabilidade por usina? | **Cox** estratificado + sobrevivência condicional |

O dado tem **censura à direita**: 45% das usinas terminaram o período de observação sem evento. Só se sabe que elas passaram de um certo tempo. Métodos de sobrevivência existem exatamente para usar essa informação parcial, em vez de descartá-la ou tratá-la como "não falhou nunca".

---

## 2. Estrutura da pasta

```
ML/analise_sobrevivencia/
├── __init__.py
├── dados_simulados.py   # gerador dos eventos (documentado à parte)
├── config.py            # covariáveis, horizontes, centralizações, k-folds
├── dados.py             # carga do treino (simulado) e das usinas da predição (dim_usina)
├── modelos.py           # KM, paramétricos, Cox, Weibull de regressão e o Previsor
├── avaliacao.py         # C-index k-fold, Schoenfeld, recuperação dos betas, calibração
├── treinar.py           # orquestra tudo, salva pickles e tabelas
└── resultados/          # kaplan_meier_por_fonte.png, calibracao.png, probabilidades_12m.png
```

---

## 3. Como executar

```bash
python -m ML.analise_sobrevivencia.dados_simulados    # (se ainda não existir) gera os eventos
python -m ML.analise_sobrevivencia.treinar            # ~20 s
python -m ML.analise_sobrevivencia.treinar --horizontes 3 6 12
python -m ML.analise_sobrevivencia.treinar --sem-graficos --sem-salvar
```

| Argumento | Padrão | Efeito |
|---|---|---|
| `--horizontes` | `6 12 24 36` | Horizontes em meses das probabilidades |
| `--sem-graficos` | — | Não gera os PNG |
| `--sem-salvar` | — | Não grava pickle nem Parquet |

---

## 4. Dados e covariáveis

### 4.1 Base de treino

`dados/simulados/eventos_manutencao_simulados.parquet`: **1.854 usinas** da ANEEL (solar e eólica, em operação, ≥ 1 MW), com:

- `tempo_anos`: tempo até o primeiro evento **ou** até o fim da observação;
- `evento`: 1 = manutenção observada (**1.018**), 0 = censura (**836**, 45%).

A carga passa por `ds_toolkit.preparar_sobrevivencia`, que valida os invariantes: tempo positivo, evento em {0,1} e consistência entre evento e data.

### 4.2 Covariáveis

| Covariável | Definição | Centralização |
|---|---|---|
| `log_potencia_mw_c` | ln(MW) − ln(30) | Usina de 30 MW (mediana do parque) |
| `subsistema_NE`, `subsistema_S`, `subsistema_N` | Indicadores | Referência: **SE** |
| `ano_entrada_c` | ano de operação − 2018 | Coorte de 2018 |
| `fonte` | solar / eólica | **Estrato**, não covariável (ver [§7.1](#71-por-que-estratificar-por-fonte)) |

As centralizações são **idênticas às do gerador**, o que permite comparar diretamente os coeficientes estimados com os verdadeiros ([§8.3](#83-recuperação-dos-parâmetros-verdadeiros)).

### 4.3 Sobre a covariável "idade"

O escopo pedia "idade da usina" entre as covariáveis. Mas a idade **no momento do evento** é o próprio tempo de sobrevivência: usá-la como covariável seria circular (prever o tempo com o tempo).

O que entra no modelo é o **ano de entrada em operação**, que é a *coorte tecnológica* da usina — captura "usinas mais novas têm equipamento melhor". A idade atual aparece em outro papel, igualmente importante: na **sobrevivência condicional** da predição ([§9.1](#91-probabilidade-condicional-à-idade)).

### 4.4 Base de predição

Da `dim_usina` do banco, as unidades com potência confiável e data de operação: **93 usinas** (51 eólicas e 42 solares). As demais ficam de fora porque o vínculo ONS × ANEEL não permitiu determinar potência ou data ([ETL §8.2.4](../ETL/doc_tecnica_etl.md#824-verificação-do-vínculo-contra-a-geração-medida)).

---

## 5. Kaplan-Meier por fonte

Estimador não paramétrico da curva de sobrevivência, via `ds_toolkit.kaplan_meier`, com tabela de risco e teste de log-rank.

![Kaplan-Meier por fonte](../../ML/analise_sobrevivencia/resultados/kaplan_meier_por_fonte.png)

| Fonte | n | Eventos | Mediana | IC 95% |
|---|---|---|---|---|
| eólica | 1.123 | 807 | **3,73 anos** | 3,56 – 3,97 |
| solar | 731 | 211 | **5,35 anos** | 4,97 – 6,13 |

**Log-rank: p = 4,7 × 10⁻¹²** — as curvas diferem de forma clara.

Como ler o gráfico:

- A curva eólica cai mais rápido, o que é coerente com o parque eólico ter mais componentes mecânicos sob fadiga.
- Os **ticks verticais** são censuras: usinas que saíram da observação sem evento.
- A faixa é o IC 95%. Ela **alarga muito no fim** da curva solar porque restam poucas usinas em risco (a tabela abaixo do gráfico mostra: aos 8 anos, 11 usinas solares; aos 10 anos, 3). Conclusões sobre a cauda são frágeis.

---

## 6. Ajuste paramétrico

Quatro famílias ajustadas por fonte e comparadas por AIC (menor = melhor equilíbrio entre ajuste e complexidade):

| Fonte | Modelo | AIC | ΔAIC | Parâmetros | Mediana prevista |
|---|---|---|---|---|---|
| eólica | **weibull** | 3.975,1 | 0,0 | λ=4,84 ρ=**1,59** | 3,85 anos |
| eólica | loglogistico | 4.042,4 | 67,3 | α=3,67 β=2,10 | 3,67 |
| eólica | lognormal | 4.113,0 | 137,9 | μ=1,26 σ=0,89 | 3,53 |
| eólica | exponencial | 4.197,7 | 222,6 | λ=4,95 | 3,43 |
| solar | **weibull** | 1.320,6 | 0,0 | λ=6,92 ρ=**1,49** | 5,41 anos |
| solar | loglogistico | 1.329,2 | 8,6 | α=5,51 β=1,72 | 5,51 |
| solar | lognormal | 1.357,0 | 36,4 | μ=1,80 σ=1,16 | 6,04 |
| solar | exponencial | 1.365,4 | 44,8 | λ=9,31 | 6,45 |

**A Weibull vence nas duas fontes**, e por margem larga na eólica (ΔAIC de 67 para a segunda). Diferença de AIC acima de 10 já é considerada forte; acima de 2, relevante.

Interpretação do parâmetro de forma (ρ): **1,59 na eólica e 1,49 na solar, ambos > 1**, ou seja, **risco crescente com a idade** — desgaste, não mortalidade infantil. A exponencial (risco constante) é a pior colocada nas duas fontes, o que confirma que a taxa de manutenção não é constante no tempo.

> Esse resultado é a validação do método: os dados foram gerados por uma Weibull com formas 1,6 (eólica) e 1,3 (solar), e o AIC escolheu a família certa, com formas estimadas de 1,59 e 1,49. O desvio maior na solar vem dos 71% de censura.

---

## 7. Cox de riscos proporcionais

### 7.1 Por que estratificar por fonte

O modelo de Cox supõe que o efeito de cada covariável **multiplica** o risco por um fator constante no tempo. Solar e eólica têm linhas de base Weibull **com formas diferentes** (1,3 e 1,6), então a razão entre os riscos das duas muda ao longo do tempo — a premissa não vale entre fontes.

Solução: `fonte` entra como **estrato**, não como covariável. Cada fonte tem sua própria linha de base não paramétrica, e os coeficientes das covariáveis são compartilhados e comparáveis.

```python
CoxPHFitter().fit(dados, duration_col="tempo_anos", event_col="evento", strata=["fonte"])
```

### 7.2 Coeficientes

| Covariável | coef | Hazard ratio | IC 95% (coef) | p |
|---|---|---|---|---|
| `log_potencia_mw_c` | **0,178** | **1,19** | 0,079 – 0,277 | 0,0004 |
| `ano_entrada_c` | **−0,043** | **0,96** | −0,062 – −0,024 | < 0,0001 |
| `subsistema_N` | 0,286 | 1,33 | −0,262 – 0,835 | 0,31 |
| `subsistema_NE` | 0,046 | 1,05 | −0,236 – 0,329 | 0,75 |
| `subsistema_S` | 0,017 | 1,02 | −0,339 – 0,374 | 0,92 |

Leitura prática:

- **Potência:** cada unidade de ln(MW) aumenta o risco em 19%. Dobrar a potência (ln 2 = 0,69) multiplica o risco por 1,13, ou seja, **+13%**. Faz sentido: mais equipamento, mais pontos de falha.
- **Coorte:** cada ano mais nova reduz o risco em **4%**. Uma usina de 2024 tem ~22% menos risco que uma de 2018.
- **Região:** nenhum efeito significativo. Isso **não** quer dizer que região não importe — o intervalo é largo demais para concluir qualquer coisa. Ver [§8.3](#83-recuperação-dos-parâmetros-verdadeiros).

### 7.3 O Cox ingênuo e o limite do teste de premissa

O código também ajusta, para comparação, um Cox com `fonte` como covariável comum (`ajustar_cox_ingenuo`), onde a premissa é violada **por construção**. Resultado: HR de 1,40 para eólica, e o **teste de Schoenfeld não acusou nada** (p > 0,05 em todas as covariáveis).

Essa é uma lição que vale registrar: **"o teste passou" não prova que a premissa vale.** As duas formas Weibull (1,3 e 1,6) são próximas o bastante para o teste não ter poder de distinguir na janela observada. A estratificação aqui se justifica pelo que se sabe do processo gerador, não pelo resultado do teste. Com dado real, sem conhecer o gerador, o caminho seria olhar também os resíduos de Schoenfeld no tempo e as curvas log(−log S).

---

## 8. Avaliação

### 8.1 Discriminação (C-index)

| Métrica | Valor |
|---|---|
| C-index no treino | 0,545 |
| **C-index 5-fold (fora da amostra)** | **0,567 ± 0,022** |
| Folds | 0,594 · 0,576 · 0,581 · 0,534 · 0,548 |

O C-index mede se o modelo ordena corretamente quem falha antes (0,5 = aleatório; > 0,7 = bom). **0,567 é baixo**, e isso é esperado: no gerador, os efeitos das covariáveis são modestos e a maior parte da variação do tempo até falha é aleatória (a componente Weibull). Nenhum modelo poderia ir muito além disso nesses dados — o teto é imposto pelo processo, não pelo método.

O C-index fora da amostra ficou **acima** do de treino, o que é possível com efeitos fracos e é sinal de que não há overfitting (são 169 eventos por covariável, muito acima da regra prática de 10).

### 8.2 Premissa de riscos proporcionais (Schoenfeld)

| Covariável | Estatística | p |
|---|---|---|
| `log_potencia_mw_c` | 1,27 | 0,26 |
| `ano_entrada_c` | 0,51 | 0,47 |
| `subsistema_S` | 1,55 | 0,21 |
| `subsistema_N` | 0,004 | 0,95 |
| `subsistema_NE` | 0,003 | 0,96 |

Nenhuma violação detectada no modelo estratificado — o esperado, já que os efeitos das covariáveis são de fato proporcionais por construção. (A ressalva sobre o poder do teste continua valendo: veja [§7.3](#73-o-cox-ingênuo-e-o-limite-do-teste-de-premissa).)

### 8.3 Recuperação dos parâmetros verdadeiros

Por serem dados simulados, dá para perguntar o que nunca se pode perguntar com dado real: **o modelo recupera a verdade?**

| Covariável | Verdadeiro | Estimado | IC 95% | Dentro do IC |
|---|---|---|---|---|
| `log_potencia_mw_c` | 0,20 | 0,178 | 0,079 – 0,277 | ✅ |
| `ano_entrada_c` | −0,04 | −0,043 | −0,062 – −0,024 | ✅ |
| `subsistema_NE` | 0,25 | 0,046 | −0,236 – 0,329 | ✅ |
| `subsistema_S` | 0,10 | 0,017 | −0,339 – 0,374 | ✅ |
| `subsistema_N` | 0,15 | 0,286 | −0,262 – 0,835 | ✅ |

**Os 5 valores verdadeiros caem dentro dos ICs.** Potência e coorte são estimados com precisão; os efeitos regionais têm ICs largos e estimativas pontuais distantes da verdade, porque 74% das usinas estão no Nordeste e só 24 estão no Norte. A geografia real do parque limita o que dá para estimar, e isso apareceria igual em dado real.

### 8.4 Calibração

Discriminação responde "ordena certo?". Calibração responde "**o número está certo?**" — se o modelo diz 80%, acontece em 80% dos casos? Aqui, as usinas foram divididas em tercis de risco e, em cada tempo, a média das probabilidades previstas foi comparada com o Kaplan-Meier observado daquele grupo (que respeita a censura).

| Grupo de risco | n | 1 ano | 2 anos | 3 anos |
|---|---|---|---|---|
| g1 (menor risco) | 620 | 0,950 vs 0,957 | 0,857 vs 0,849 | 0,757 vs 0,766 |
| g2 | 616 | 0,931 vs 0,940 | 0,814 vs 0,832 | 0,682 vs 0,662 |
| g3 (maior risco) | 618 | 0,909 vs 0,893 | 0,766 vs 0,751 | 0,603 vs 0,612 |

*(previsto vs observado)*

**Erro absoluto médio de 0,012, máximo de 0,020.** O modelo está bem calibrado, e os grupos aparecem na ordem certa (g1 sobrevive mais que g3 em todos os tempos). Como a saída do produto é uma probabilidade exibida ao usuário, calibração importa mais que C-index.

![Calibração](../../ML/analise_sobrevivencia/resultados/calibracao.png)

---

## 9. Saída de produto: probabilidade por usina

### 9.1 Probabilidade condicional à idade

Uma usina que **já opera há 5 anos sem manutenção** não parte do zero. A probabilidade servida é condicional:

```math
P(\text{sobreviver mais } N \text{ meses} \mid \text{já sobreviveu } t_0) = \frac{S(t_0 + N)}{S(t_0)}
```

onde `t₀ = idade_anos` (hoje − data de operação) e `S` é a curva prevista pelo Cox para as covariáveis daquela usina. A coluna `condicional_na_idade` registra que foi assim que o número saiu.

### 9.2 Resultado

93 usinas, com horizontes de 6, 12, 24 e 36 meses:

| Fonte | Método | n | Idade média | P(12 meses) média | Risco relativo médio |
|---|---|---|---|---|---|
| eólica | cox | 30 | 5,9 anos | 0,64 | 1,46 |
| eólica | weibull | 21 | 14,1 anos | 0,42 | 1,82 |
| solar | cox | 42 | 4,5 anos | 0,86 | 1,30 |

Distribuição geral das probabilidades:

| | 6 meses | 12 meses | 24 meses | 36 meses |
|---|---|---|---|---|
| média | 0,82 | 0,69 | 0,48 | 0,32 |
| mediana | 0,85 | 0,72 | 0,47 | 0,29 |
| mínimo | 0,35 | 0,12 | 0,01 | 0,00 |
| máximo | 1,00 | 1,00 | 1,00 | 0,91 |

O tempo mediano previsto até manutenção vai de 2,1 a 7,1 anos, com mediana de 3,6.

### 9.3 Colunas do arquivo

`ML/modelos/sobrevivencia_probabilidades_por_usina.parquet` (93 × 18), pronto para o endpoint e para o card:

| Coluna | Descrição |
|---|---|
| `usina_id`, `nome`, `fonte`, `id_subsistema`, `id_estado`, `municipio` | Identificação (vêm da `dim_usina`) |
| `potencia_mw`, `data_operacao`, `idade_anos` | Atributos usados no cálculo |
| `p_sem_manutencao_6m` … `_36m` | **A saída principal** |
| `risco_relativo` | exp(β·x): risco face à usina de referência |
| `tempo_mediano_anos` | Tempo mediano previsto até o evento |
| `condicional_na_idade` | Se a probabilidade é condicional (sempre `true` aqui) |
| `metodo_extrapolacao` | `cox` ou `weibull` ([§10](#10-o-bug-da-extrapolação-e-como-foi-resolvido)) |
| `qualidade_vinculo` | Qualidade do vínculo ONS × ANEEL da unidade |

---

## 10. O bug da extrapolação e como foi resolvido

**Sintoma:** na primeira versão, o `CONJUNTO EOLICO CAETITE NORTE`, com 11,96 anos de operação, saía com `p_sem_manutencao_12m = 1,00` — enquanto seu próprio tempo mediano previsto era de 2,6 anos. Absurdo na cara.

**Causa:** o Cox é **semiparamétrico**. Sua linha de base é estimada a partir dos eventos observados e, depois do **último evento** de cada estrato, ela fica **plana** — o modelo não tem informação ali. Para uma usina com idade além desse ponto, S(t₀) e S(t₀+N) caem na região plana, e a razão S(t₀+N)/S(t₀) vale exatamente 1.

Os limites medidos: **11,49 anos** na eólica e **12,40 anos** na solar.

**Correção:** dentro do suporte, o cálculo usa o Cox; **além dele, usa uma regressão Weibull** (`WeibullAFTFitter`, uma por fonte, com as mesmas covariáveis), que é paramétrica e continua decaindo de forma suave — o comportamento fisicamente esperado de um ativo que envelhece. A coluna `metodo_extrapolacao` marca qual foi usado.

**Efeito:** 21 das 93 usinas (todas eólicas antigas) passaram a usar Weibull. A média de P(12 meses) da eólica caiu de 0,79 para 0,55, e a Taíba (27,8 anos de operação) saiu de 1,00 para **0,12**.

Essa correção também aproveita o modelo paramétrico da [§6](#6-ajuste-paramétrico) no produto, e não só na comparação: a Weibull ganhou por AIC, então é a extrapoladora natural.

---

## 11. Artefatos salvos

| Arquivo (`ML/modelos/`) | Tamanho | Conteúdo |
|---|---|---|
| `sobrevivencia_cox.pkl` | 559 KB | `PrevisorSobrevivencia`: Cox estratificado + Weibull por fonte + limites de suporte + horizontes |
| `sobrevivencia_cox.pkl.meta.json` | 1,2 KB | Metadados: C-index k-fold, betas verdadeiros, limites de suporte, aviso de dado simulado |
| `sobrevivencia_probabilidades_por_usina.parquet` | 21 KB | A tabela do produto (93 usinas) |
| `sobrevivencia_parametricos.parquet` | 6,3 KB | Ranking AIC por fonte |
| `sobrevivencia_diagnosticos.parquet` | 7,1 KB | Calibração + Schoenfeld |
| `sobrevivencia_recuperacao_betas.parquet` | 4,7 KB | Estimado × verdadeiro |

Gráficos em `ML/analise_sobrevivencia/resultados/`: `kaplan_meier_por_fonte.png`, `calibracao.png` e `probabilidades_12m.png`.

O pickle é gravado com `ds_toolkit.salvar_modelo` (joblib), que anexa o `.meta.json` com versões de biblioteca e data de treino.

---

## 12. Como usar o modelo salvo

```python
import pandas as pd
import ds_toolkit as dst

previsor = dst.carregar_modelo("ML/modelos/sobrevivencia_cox.pkl")

usina = pd.DataFrame([{
    "fonte": "solar",
    "log_potencia_mw_c": 0.0,     # 30 MW
    "subsistema_NE": 1.0, "subsistema_S": 0.0, "subsistema_N": 0.0,
    "ano_entrada_c": 4,           # entrou em 2022
    "idade_anos": 2.0,            # já opera há 2 anos sem manutenção
}])

previsao = previsor.prever(usina)
# p_sem_manutencao_6m = 0,947 · 12m = 0,877 · 24m = 0,780 · 36m = 0,649
# risco_relativo = 0,96 · metodo_extrapolacao = "cox"
```

Para a API, o caminho é carregar o pickle na inicialização do processo e chamar `prever` por requisição, **ou** simplesmente ler o Parquet pré-calculado (93 linhas), já que a `dim_usina` é estática entre execuções do ETL. A segunda opção é mais rápida e não carrega o `lifelines` em produção.

---

## 13. Decisões metodológicas

1. **Estrato em vez de covariável para `fonte`** — justificado pelo processo gerador, não pelo teste de premissa ([§7.1](#71-por-que-estratificar-por-fonte) e [§7.3](#73-o-cox-ingênuo-e-o-limite-do-teste-de-premissa)).
2. **Ano de entrada no lugar da idade** — usar a idade como covariável seria circular ([§4.3](#43-sobre-a-covariável-idade)).
3. **Probabilidade condicional à idade** — é a pergunta que o usuário do card realmente faz: "e daqui para frente?".
4. **Weibull para extrapolar** — o Cox não tem informação além do último evento ([§10](#10-o-bug-da-extrapolação-e-como-foi-resolvido)).
5. **Calibração como critério principal** — a saída é uma probabilidade exibida ao usuário; C-index sozinho não garante que o número esteja certo ([§8.4](#84-calibração)).
6. **Treinar por usina, servir por unidade** — o modelo é treinado nas 1.854 usinas da ANEEL e aplicado às 93 unidades da `dim_usina`, porque ambas compartilham o mesmo espaço de covariáveis. A `fato_manutencao` do banco, por sua vez, agrega os eventos por unidade, o que é uma visão diferente e documentada no [ETL §8.6](../ETL/doc_tecnica_etl.md#86-fato_manutencao--grão-usina-simulado).

---

## 14. Limitações e próximos passos

| # | Limitação | Impacto | Próximo passo |
|---|---|---|---|
| 1 | **Eventos sintéticos** | Nenhuma conclusão vale para o mundo real | Substituir pelo histórico real de O&M, se houver acesso; o pipeline não muda |
| 2 | C-index de 0,567 | Discriminação fraca | É o teto deste gerador. Com dado real, avaliar se há sinal mais forte |
| 3 | Só 93 das 308 unidades recebem previsão | Cobertura parcial do frontend | Melhorar o vínculo ONS × ANEEL ([ETL §15](../ETL/doc_tecnica_etl.md#15-limitações-conhecidas-e-próximos-passos)) |
| 4 | Efeitos regionais imprecisos | ICs largos, estimativas distantes | Inerente à geografia do parque; só mais dados resolvem |
| 5 | Um evento por usina | Sem eventos recorrentes | Modelos de recorrência (Andersen-Gill) quando houver dado real |
| 6 | Sem intervalo na probabilidade | O card mostra um ponto | Bootstrap sobre os coeficientes do Cox para banda de confiança |
| 7 | Extrapolação Weibull não validada | 21 usinas dependem dela, fora do suporte observado | Por definição não há dado para validar; sinalizar no frontend quando `metodo_extrapolacao = "weibull"` |
| 8 | Sem testes automatizados | Regressões silenciosas | `pytest`: invariantes da carga, probabilidade em [0,1], monotonicidade em relação ao horizonte, e o caso do [§10](#10-o-bug-da-extrapolação-e-como-foi-resolvido) (usina antiga não pode dar 1,00) |
| 9 | Sem covariáveis operacionais | O modelo só conhece cadastro | Quando houver mais histórico, incluir fator de capacidade, horas de operação e clima acumulado (vento/temperatura) como covariáveis |

---

*Documento gerado em 19/09/2026 a partir do código de `ML/analise_sobrevivencia/` e da execução real (1.854 usinas de treino, 93 unidades de predição).*
