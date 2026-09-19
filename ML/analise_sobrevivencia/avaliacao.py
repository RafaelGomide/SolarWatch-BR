"""Avaliação do modelo de sobrevivência.

Quatro checagens, cada uma respondendo a uma pergunta diferente:

1. **Discriminação** — C-index por validação cruzada k-fold: o modelo ordena
   corretamente quem falha antes? (0,5 = aleatório.)
2. **Premissa** — teste de Schoenfeld: os riscos são proporcionais?
3. **Recuperação dos parâmetros** — os coeficientes estimados contêm os valores
   verdadeiros do gerador nos seus ICs? Só é possível porque o dado é simulado.
4. **Calibração** — a probabilidade prevista bate com a sobrevivência observada?
   Compara, por grupo de risco, a curva Kaplan-Meier com a média das curvas
   previstas pelo Cox.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

from ML.analise_sobrevivencia.config import ALPHA, COVARIAVEIS, ESTRATO, K_FOLDS, SEED
from ML.analise_sobrevivencia.modelos import EVENTO, TEMPO

log = logging.getLogger(__name__)


def cindex_validacao_cruzada(df: pd.DataFrame, k: int = K_FOLDS) -> dict:
    """C-index fora da amostra (k-fold). Mais honesto que o C-index de treino."""
    from lifelines import CoxPHFitter
    from lifelines.utils import k_fold_cross_validation

    dados = df[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scores = k_fold_cross_validation(
            CoxPHFitter(strata=[ESTRATO]), dados, duration_col=TEMPO, event_col=EVENTO,
            k=k, scoring_method="concordance_index", seed=SEED,
        )
    resultado = {"k": k, "cindex_medio": float(np.mean(scores)),
                 "cindex_desvio": float(np.std(scores)), "scores": [float(s) for s in scores]}
    log.info("[avaliacao] C-index %d-fold = %.3f (+/- %.3f)", k,
             resultado["cindex_medio"], resultado["cindex_desvio"])
    return resultado


def testar_riscos_proporcionais(modelo, df: pd.DataFrame) -> pd.DataFrame:
    """Teste de Schoenfeld por covariável. p < 0,05 indica efeito que muda no tempo."""
    from lifelines.statistics import proportional_hazard_test

    dados = df[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        teste = proportional_hazard_test(modelo, dados, time_transform="rank")
    tabela = teste.summary.reset_index().rename(columns={"index": "covariavel", "p": "p_valor"})
    violadas = tabela.loc[tabela["p_valor"] < ALPHA, "covariavel"].tolist()
    log.info("[avaliacao] Schoenfeld: %s", f"VIOLAM a premissa: {violadas}" if violadas
             else "nenhuma covariável viola a premissa (alpha=0,05)")
    return tabela


def recuperacao_dos_parametros(modelo, meta: dict) -> pd.DataFrame:
    """Compara os coeficientes estimados com os valores verdadeiros do gerador."""
    verdadeiros = meta.get("beta_verdadeiro", {})
    if not verdadeiros:
        return pd.DataFrame()
    resumo = modelo.summary[["coef", "coef lower 95%", "coef upper 95%"]].copy()
    resumo.columns = ["estimado", "ic_inferior", "ic_superior"]
    resumo["verdadeiro"] = pd.Series(verdadeiros)
    resumo["dentro_do_ic"] = resumo["verdadeiro"].between(resumo["ic_inferior"], resumo["ic_superior"])
    resumo["erro"] = resumo["estimado"] - resumo["verdadeiro"]
    dentro = int(resumo["dentro_do_ic"].sum())
    log.info("[avaliacao] recuperação dos betas: %d de %d dentro do IC 95%%", dentro, len(resumo))
    return resumo.reset_index().rename(columns={"covariate": "covariavel"})


def calibracao(previsor, df: pd.DataFrame, tempos_anos=(1, 2, 3), n_grupos: int = 3) -> pd.DataFrame:
    """Previsto × observado por grupo de risco.

    Divide as usinas em grupos pelo risco relativo do Cox e compara, em cada
    tempo, a média das probabilidades previstas com a estimativa Kaplan-Meier
    daquele grupo (que é a sobrevivência observada, respeitando a censura).
    """
    from lifelines import KaplanMeierFitter

    base = df.copy()
    base["risco"] = np.exp(previsor.cox.predict_log_partial_hazard(
        base[[ESTRATO, *COVARIAVEIS]]).to_numpy())
    base["grupo_risco"] = pd.qcut(base["risco"], n_grupos,
                                  labels=[f"g{i+1}" for i in range(n_grupos)])

    curvas = previsor._curvas(base, np.array(tempos_anos))
    linhas = []
    for grupo, subconjunto in base.groupby("grupo_risco", observed=True):
        km = KaplanMeierFitter().fit(subconjunto[TEMPO], subconjunto[EVENTO])
        for t in tempos_anos:
            observado = float(km.predict(t))
            previsto = float(curvas.loc[t, subconjunto.index].mean())
            linhas.append({"grupo_risco": str(grupo), "n": len(subconjunto),
                           "tempo_anos": t, "previsto": previsto, "observado_km": observado,
                           "erro": previsto - observado})
    tabela = pd.DataFrame(linhas)
    log.info("[avaliacao] calibração: erro absoluto médio = %.3f (previsto − KM observado)",
             tabela["erro"].abs().mean())
    return tabela
