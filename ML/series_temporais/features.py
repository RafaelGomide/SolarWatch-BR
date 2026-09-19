"""Features para o modelo de Gradient Boosting.

Três blocos:

1. **Calendário** (via `ds_toolkit.criar_features_data`): hora, dia da semana,
   mês, fim de semana + codificação cíclica seno/cosseno. A cíclica evita que o
   modelo veja a hora 23 como "distante" da hora 0.
2. **Defasagens do alvo**: geração na mesma hora de 1, 2, 3 e 7 dias atrás, e
   médias móveis. **Todas com defasagem >= horizonte**, para que a previsão de
   qualquer hora do horizonte use apenas dado conhecido no momento do corte.
3. **Clima** (exógenas): irradiância, vento e temperatura do dia.

> Premissa declarada: o clima do dia previsto é tratado como **conhecido**. Em
> produção isso equivale a usar uma previsão meteorológica; aqui usamos o valor
> observado, então a acurácia do modelo com clima é um limite superior otimista.
"""

from __future__ import annotations

import pandas as pd

import ds_toolkit as dst
from ML.series_temporais.config import HORIZONTE_H, JANELAS_MOVEIS_H, LAGS_H

ALVO = "energia_mwh"
COMPONENTES_CALENDARIO = ("hora", "dia_semana", "mes", "fim_de_semana")


def construir(
    df: pd.DataFrame,
    horizonte: int = HORIZONTE_H,
    lags: list[int] | None = None,
    janelas: list[int] | None = None,
) -> pd.DataFrame:
    """Devolve o DataFrame com as colunas de feature (índice preservado)."""
    lags = lags or LAGS_H
    janelas = janelas or JANELAS_MOVEIS_H
    invalidas = [lag for lag in lags if lag < horizonte]
    if invalidas:
        raise ValueError(
            f"Defasagens menores que o horizonte causariam vazamento: {invalidas} < {horizonte}"
        )

    X = df.copy()
    X = dst.criar_features_data(
        X.reset_index().rename(columns={"index": "data_hora"}),
        "data_hora", componentes=COMPONENTES_CALENDARIO, ciclicas=True, verbose=False,
    ).set_index("data_hora")

    for lag in lags:
        X[f"lag_{lag}h"] = X[ALVO].shift(lag)
    for janela in janelas:
        deslocado = X[ALVO].shift(horizonte)
        X[f"media_{janela}h"] = deslocado.rolling(janela, min_periods=max(2, janela // 4)).mean()
        X[f"desvio_{janela}h"] = deslocado.rolling(janela, min_periods=max(2, janela // 4)).std()

    return X


def colunas_de_feature(X: pd.DataFrame) -> list[str]:
    """Tudo menos o alvo e as colunas de identificação."""
    return [c for c in X.columns if c not in (ALVO, "serie", "fonte")]
