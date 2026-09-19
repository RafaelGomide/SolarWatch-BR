"""Métricas de erro de previsão, por série e por modelo.

Cuidado com MAPE em geração solar: 26,6% das horas têm geração zero (noite), e
`|y - ŷ| / y` é infinito nesses pontos. Aqui o MAPE é calculado **apenas** sobre
as horas em que a geração observada passa de um piso (5% da média da série), e a
cobertura desse cálculo é reportada em `cobertura_mape_smape_%`. O sMAPE usa o
mesmo piso, porque com y = 0 e previsão > 0 cada ponto vale 200%.

Para comparar séries de escalas diferentes usando **todos** os pontos, incluindo
as horas de geração zero, a métrica é `nrmse_%` (RMSE / média da série).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ML.series_temporais.config import PISO_MAPE_FRACAO_MEDIA


def calcular(y: pd.Series, previsto: pd.Series, piso_mape: float | None = None) -> dict[str, float]:
    y, previsto = y.align(previsto, join="inner")
    valido = y.notna() & previsto.notna()
    y, previsto = y[valido], previsto[valido]
    erro = y - previsto

    piso = piso_mape if piso_mape is not None else PISO_MAPE_FRACAO_MEDIA * y.mean()
    acima = y > piso
    mape = float((erro[acima] / y[acima]).abs().mean() * 100) if acima.any() else np.nan

    # sMAPE tem o mesmo problema: com y = 0 e previsão > 0, cada ponto vale 200%.
    # Por isso usa o mesmo piso do MAPE.
    denominador = ((y[acima].abs() + previsto[acima].abs()) / 2).replace(0, np.nan)
    smape = float((erro[acima].abs() / denominador).mean() * 100) if acima.any() else np.nan

    rmse = float(np.sqrt((erro**2).mean()))
    return {
        "n": int(len(y)),
        "rmse": rmse,
        "mae": float(erro.abs().mean()),
        "mape_%": mape,
        "smape_%": smape,
        "nrmse_%": float(rmse / y.mean() * 100) if y.mean() else np.nan,
        "vies": float(erro.mean()),
        "cobertura_mape_smape_%": float(acima.mean() * 100),
    }


def resumir(previsoes: pd.DataFrame) -> pd.DataFrame:
    """Métricas por (série, fonte, modelo) a partir do painel de previsões do backtesting."""
    linhas = []
    for (serie, fonte, modelo), grupo in previsoes.groupby(["serie", "fonte", "modelo"], sort=False):
        linhas.append({"serie": serie, "fonte": fonte, "modelo": modelo,
                       "janelas": grupo["janela"].nunique(),
                       **calcular(grupo.set_index("data_hora")["y"],
                                  grupo.set_index("data_hora")["previsto"])})
    resumo = pd.DataFrame(linhas)
    return resumo.sort_values(["serie", "rmse"]).reset_index(drop=True)


def ganho_sobre_baseline(resumo: pd.DataFrame, baseline: str = "naive_sazonal") -> pd.DataFrame:
    """Acrescenta a redução de RMSE de cada modelo em relação ao baseline."""
    referencia = (resumo[resumo["modelo"] == baseline]
                  .set_index("serie")["rmse"].rename("rmse_baseline"))
    saida = resumo.join(referencia, on="serie")
    saida["ganho_vs_baseline_%"] = (1 - saida["rmse"] / saida["rmse_baseline"]) * 100
    return saida.drop(columns="rmse_baseline")
