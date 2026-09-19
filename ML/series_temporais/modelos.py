"""Modelos de previsão, todos com a mesma interface.

    modelo.ajustar(historico)          # DataFrame com alvo + exógenas, índice horário
    modelo.prever(futuro)              # DataFrame só com exógenas; devolve Series de previsão

Assim o backtesting trata baseline, SARIMA e Gradient Boosting exatamente do
mesmo jeito, e comparar é honesto.

- `NaiveSazonal`      : ŷ(t) = y(t − 24h). Baseline difícil de bater em série com
                        forte ciclo diário; é a referência de "custo zero".
- `MediaMovelSazonal` : média da mesma hora nos últimos k dias (suaviza ruído).
- `Sarima`            : SARIMAX (1,0,1)(1,1,1,24) — estatístico e interpretável.
- `GradientBoosting`  : HistGradientBoostingRegressor com calendário + defasagens
                        + clima (múltiplas exógenas).
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from ML.series_temporais import features
from ML.series_temporais.config import HORIZONTE_H, SARIMA_ORDEM, SARIMA_SAZONAL, SEED

log = logging.getLogger(__name__)
ALVO = features.ALVO


class ModeloPrevisao:
    """Interface comum. `horizonte` é o número de passos previstos de uma vez."""

    nome = "base"

    def __init__(self, horizonte: int = HORIZONTE_H):
        self.horizonte = horizonte
        self.historico_: pd.DataFrame | None = None

    def ajustar(self, historico: pd.DataFrame) -> "ModeloPrevisao":
        self.historico_ = historico
        return self

    def prever(self, futuro: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def _checar_ajustado(self) -> pd.DataFrame:
        if self.historico_ is None:
            raise RuntimeError(f"Modelo '{self.nome}' ainda não foi ajustado.")
        return self.historico_


class NaiveSazonal(ModeloPrevisao):
    """ŷ(t) = y(t − 24h): repete o mesmo horário do dia anterior."""

    nome = "naive_sazonal"

    def prever(self, futuro: pd.DataFrame) -> pd.Series:
        historico = self._checar_ajustado()
        valores = [historico[ALVO].get(t - pd.Timedelta(hours=24), np.nan) for t in futuro.index]
        return pd.Series(valores, index=futuro.index, name=self.nome)


class MediaMovelSazonal(ModeloPrevisao):
    """ŷ(t) = média de y na mesma hora nos últimos `dias` dias."""

    nome = "media_movel_sazonal"

    def __init__(self, dias: int = 7, horizonte: int = HORIZONTE_H):
        super().__init__(horizonte)
        self.dias = dias

    def prever(self, futuro: pd.DataFrame) -> pd.Series:
        historico = self._checar_ajustado()
        valores = []
        for t in futuro.index:
            anteriores = [historico[ALVO].get(t - pd.Timedelta(days=d), np.nan)
                          for d in range(1, self.dias + 1)]
            valores.append(np.nanmean(anteriores) if not np.isnan(anteriores).all() else np.nan)
        return pd.Series(valores, index=futuro.index, name=self.nome)


class Sarima(ModeloPrevisao):
    """SARIMAX com sazonalidade diária. Sem exógenas: é o comparativo estatístico puro."""

    nome = "sarima"

    def __init__(self, ordem=SARIMA_ORDEM, sazonal=SARIMA_SAZONAL, horizonte: int = HORIZONTE_H):
        super().__init__(horizonte)
        self.ordem, self.sazonal = ordem, sazonal
        self.resultado_ = None

    def ajustar(self, historico: pd.DataFrame) -> "Sarima":
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        super().ajustar(historico)
        serie = historico[ALVO].astype(float).interpolate(limit_direction="both")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.resultado_ = SARIMAX(
                serie, order=self.ordem, seasonal_order=self.sazonal,
                enforce_stationarity=False, enforce_invertibility=False,
            ).fit(disp=0, maxiter=50)
        return self

    def prever(self, futuro: pd.DataFrame) -> pd.Series:
        if self.resultado_ is None:
            raise RuntimeError("SARIMA ainda não foi ajustado.")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            previsto = self.resultado_.forecast(steps=len(futuro))
        previsto.index = futuro.index
        return previsto.clip(lower=0).rename(self.nome)   # geração não é negativa


class GradientBoosting(ModeloPrevisao):
    """Gradient Boosting com calendário + defasagens + clima.

    As features do horizonte são construídas sobre `histórico + futuro`: como
    toda defasagem é >= horizonte, os valores usados caem todos dentro do
    histórico — nenhuma informação do futuro entra.
    """

    nome = "gradient_boosting"

    def __init__(self, horizonte: int = HORIZONTE_H, **parametros):
        super().__init__(horizonte)
        self.parametros = {
            "max_iter": 400, "learning_rate": 0.06, "max_depth": 6,
            "min_samples_leaf": 20, "l2_regularization": 1.0,
            "early_stopping": False, "random_state": SEED, **parametros,
        }
        self.modelo_ = HistGradientBoostingRegressor(**self.parametros)
        self.colunas_: list[str] = []

    def ajustar(self, historico: pd.DataFrame) -> "GradientBoosting":
        super().ajustar(historico)
        X = features.construir(historico, horizonte=self.horizonte)
        self.colunas_ = features.colunas_de_feature(X)
        treino = X.dropna(subset=[ALVO])
        # HistGradientBoosting lida com NaN nas features nativamente
        self.modelo_.fit(treino[self.colunas_], treino[ALVO])
        return self

    def prever(self, futuro: pd.DataFrame) -> pd.Series:
        historico = self._checar_ajustado()
        completo = pd.concat([historico, futuro.assign(**{ALVO: np.nan})])
        X = features.construir(completo, horizonte=self.horizonte).loc[futuro.index]
        previsto = self.modelo_.predict(X[self.colunas_])
        return pd.Series(previsto, index=futuro.index, name=self.nome).clip(lower=0)

    def importancias(self, X: pd.DataFrame, y: pd.Series, n_repeticoes: int = 5) -> pd.DataFrame:
        from sklearn.inspection import permutation_importance

        resultado = permutation_importance(self.modelo_, X[self.colunas_], y,
                                           n_repeats=n_repeticoes, random_state=SEED)
        return (pd.DataFrame({"feature": self.colunas_, "importancia": resultado.importances_mean})
                .sort_values("importancia", ascending=False).reset_index(drop=True))


def catalogo(horizonte: int = HORIZONTE_H, com_sarima: bool = True) -> list[ModeloPrevisao]:
    modelos: list[ModeloPrevisao] = [
        NaiveSazonal(horizonte),
        MediaMovelSazonal(dias=7, horizonte=horizonte),
        GradientBoosting(horizonte),
    ]
    if com_sarima:
        modelos.insert(2, Sarima(horizonte=horizonte))
    return modelos
