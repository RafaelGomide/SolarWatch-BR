"""Modelos de sobrevivência: Kaplan-Meier, paramétricos e Cox.

1. **Kaplan-Meier** (não paramétrico): curva agregada por fonte + teste de
   log-rank comparando solar × eólica. É a descrição do dado, sem premissa.
2. **Paramétricos** (Weibull, Log-Normal, Log-Logística, Exponencial): ajustados
   por fonte e comparados por AIC. Dizem qual família descreve melhor o tempo
   até manutenção, e a forma do risco (crescente, constante ou não monotônico).
3. **Cox de riscos proporcionais**: quantifica o efeito das covariáveis
   (potência, região, coorte de entrada) sobre o risco.

Sobre a estratificação do Cox: solar e eólica foram geradas com **linhas de base
Weibull diferentes** (formas 1,3 e 1,6), então o risco relativo entre elas não é
constante no tempo — usar `fonte` como covariável comum viola a premissa de
riscos proporcionais. Por isso o modelo de produção é **estratificado por fonte**:
cada uma tem sua própria linha de base e os coeficientes são compartilhados.

`ajustar_cox_ingenuo` ajusta a versão com `fonte` como covariável comum, para
comparação. Nota medida: nesses dados o teste de Schoenfeld **não** acusa a
violação (p > 0,05 em todas as covariáveis), embora ela exista por construção.
As formas das duas Weibull (1,3 e 1,6) são próximas o bastante para o teste não
ter poder de detectá-las na janela observada — um bom lembrete de que "o teste
passou" não prova que a premissa vale. A estratificação aqui é justificada pelo
que se sabe do processo gerador, não pelo resultado do teste.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ML.analise_sobrevivencia.config import (ALPHA, COVARIAVEIS, DISTRIBUICOES, ESTRATO,
                                             HORIZONTES_MESES, SEED)

log = logging.getLogger(__name__)

TEMPO, EVENTO = "tempo_anos", "evento"


# ---------------------------------------------------------------- Kaplan-Meier
def kaplan_meier_por_fonte(df: pd.DataFrame, plotar: bool = True, salvar_em=None) -> dict:
    """Curvas KM por fonte + log-rank (via ds_toolkit)."""
    return dst.kaplan_meier(df, TEMPO, EVENTO, coluna_grupo="fonte",
                            tabela_risco=True, plotar=plotar,
                            salvar_em=str(salvar_em) if salvar_em else None, verbose=True)


# ------------------------------------------------------------------ Paramétricos
def parametricos_por_fonte(df: pd.DataFrame, plotar: bool = True) -> pd.DataFrame:
    """Ajusta as distribuições candidatas para cada fonte e ranqueia por AIC."""
    tabelas = []
    for fonte, grupo in df.groupby("fonte"):
        ranking = dst.modelos_parametricos_sobrevivencia(
            grupo, TEMPO, EVENTO, modelos=DISTRIBUICOES, plotar=plotar, verbose=True)
        ranking.insert(0, "fonte", fonte)
        ranking["delta_aic"] = ranking["AIC"] - ranking["AIC"].min()
        tabelas.append(ranking)
    return pd.concat(tabelas, ignore_index=True)


# -------------------------------------------------------------------------- Cox
def ajustar_cox_ingenuo(df: pd.DataFrame):
    """Cox com `fonte` como covariável comum — comparação didática.

    A premissa de riscos proporcionais é violada por construção (linhas de base
    diferentes por fonte), mas o teste de Schoenfeld não detecta isso aqui."""
    base = df.assign(fonte_eolica=(df["fonte"] == "eolica").astype(float))
    return dst.cox_ph(base, TEMPO, EVENTO, [*COVARIAVEIS, "fonte_eolica"],
                      verificar_premissas=True, plotar=False, verbose=True)


def ajustar_cox(df: pd.DataFrame, penalizador: float = 0.0):
    """Modelo de produção: Cox estratificado por fonte."""
    from lifelines import CoxPHFitter

    dados = df[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna()
    modelo = CoxPHFitter(penalizer=penalizador)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(dados, duration_col=TEMPO, event_col=EVENTO, strata=[ESTRATO])
    log.info("[cox] n=%d | eventos=%d | C-index (treino)=%.3f",
             len(dados), int(dados[EVENTO].sum()), modelo.concordance_index_)
    return modelo


def ajustar_weibull_regressao(df: pd.DataFrame) -> dict:
    """Regressão Weibull (AFT) com as mesmas covariáveis, uma por fonte.

    Serve para **extrapolar** além do último evento observado: o Cox é
    semiparamétrico e sua linha de base fica plana depois do último evento, o
    que faria a sobrevivência condicional de uma usina muito antiga valer 1,0.
    A Weibull continua decaindo, o que é o comportamento físico esperado.
    """
    from lifelines import WeibullAFTFitter

    ajustes = {}
    for fonte, grupo in df.groupby(ESTRATO):
        dados = grupo[[TEMPO, EVENTO, *COVARIAVEIS]].dropna()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ajustes[str(fonte)] = WeibullAFTFitter().fit(dados, duration_col=TEMPO,
                                                         event_col=EVENTO)
        log.info("[weibull] %s: forma rho=%.2f", fonte,
                 float(np.exp(ajustes[str(fonte)].params_["rho_"]["Intercept"])))
    return ajustes


def limites_de_suporte(df: pd.DataFrame) -> dict:
    """Último tempo COM EVENTO em cada estrato: além disso o Cox extrapola."""
    com_evento = df[df[EVENTO] == 1]
    return com_evento.groupby(ESTRATO)[TEMPO].max().astype(float).to_dict()


# ------------------------------------------------------------------- Previsor
class PrevisorSobrevivencia:
    """Objeto servido pela API: dá a probabilidade de uma usina passar N meses
    sem manutenção corretiva.

    Usa **sobrevivência condicional**: uma usina que já opera há `idade_anos`
    sem evento não parte do zero, então

        P(sobreviver mais N meses | já sobreviveu t0) = S(t0 + N) / S(t0).

    Sem `idade_anos` no DataFrame, devolve a probabilidade incondicional (a
    partir da entrada em operação).
    """

    def __init__(self, cox, weibull_por_fonte: dict | None = None,
                 limites_suporte: dict | None = None,
                 horizontes_meses=HORIZONTES_MESES, metadados: dict | None = None):
        self.cox = cox
        self.weibull_por_fonte = weibull_por_fonte or {}
        self.limites_suporte = limites_suporte or {}
        self.horizontes_meses = tuple(horizontes_meses)
        self.covariaveis = list(COVARIAVEIS)
        self.estrato = ESTRATO
        self.metadados = metadados or {}

    def _curvas(self, usinas: pd.DataFrame, tempos_anos: np.ndarray) -> pd.DataFrame:
        entrada = usinas[[self.estrato, *self.covariaveis]].copy()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return self.cox.predict_survival_function(entrada, times=list(tempos_anos))

    def _sobrevivencia_weibull(self, usinas: pd.DataFrame, tempos: np.ndarray) -> pd.DataFrame:
        """S(t) pela regressão Weibull da fonte de cada usina."""
        partes = []
        for fonte, grupo in usinas.groupby(self.estrato):
            ajuste = self.weibull_por_fonte.get(str(fonte))
            if ajuste is None:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                partes.append(ajuste.predict_survival_function(grupo[self.covariaveis],
                                                               times=list(tempos)))
        return pd.concat(partes, axis=1) if partes else pd.DataFrame(index=tempos)

    def prever(self, usinas: pd.DataFrame, condicional: bool = True) -> pd.DataFrame:
        """Uma linha por usina, com P(sem manutenção) em cada horizonte.

        Usa o Cox dentro do suporte observado e a regressão Weibull além dele
        (usinas mais antigas que o último evento do seu estrato), marcando o
        método em `metodo_extrapolacao`.
        """
        idade = (usinas["idade_anos"].to_numpy() if condicional and "idade_anos" in usinas
                 else np.zeros(len(usinas)))
        horizontes_anos = np.array(self.horizontes_meses) / 12

        # tempos necessários: a idade de cada usina e a idade + cada horizonte
        tempos = np.unique(np.concatenate([idade, (idade[:, None] + horizontes_anos).ravel()]))
        curvas = self._curvas(usinas, tempos)
        limite = usinas[self.estrato].astype(str).map(self.limites_suporte).to_numpy(dtype=float)
        fora = idade > np.nan_to_num(limite, nan=np.inf)
        curvas_weibull = (self._sobrevivencia_weibull(usinas, tempos)
                          if fora.any() and self.weibull_por_fonte else None)

        saida = usinas.copy()
        for meses, horizonte in zip(self.horizontes_meses, horizontes_anos):
            probabilidades = []
            for posicao, (rotulo, coluna) in enumerate(curvas.items()):
                if fora[posicao] and curvas_weibull is not None:
                    coluna = curvas_weibull.iloc[:, list(curvas.columns).index(rotulo)]
                s_inicio = np.interp(idade[posicao], curvas.index.to_numpy(), coluna.to_numpy())
                s_fim = np.interp(idade[posicao] + horizonte, curvas.index.to_numpy(), coluna.to_numpy())
                probabilidades.append(s_fim / s_inicio if s_inicio > 0 else np.nan)
            saida[f"p_sem_manutencao_{meses}m"] = np.clip(probabilidades, 0, 1)
        saida["metodo_extrapolacao"] = np.where(fora, "weibull", "cox")

        saida["risco_relativo"] = np.exp(
            self.cox.predict_log_partial_hazard(usinas[[self.estrato, *self.covariaveis]]).to_numpy()
        )
        saida["condicional_na_idade"] = condicional and "idade_anos" in usinas
        return saida

    def tempo_mediano_anos(self, usinas: pd.DataFrame) -> pd.Series:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return self.cox.predict_median(usinas[[self.estrato, *self.covariaveis]])

    def resumo_coeficientes(self) -> pd.DataFrame:
        tabela = self.cox.summary[["coef", "exp(coef)", "coef lower 95%", "coef upper 95%", "p"]]
        tabela.columns = ["coef", "hazard_ratio", "ic_inferior", "ic_superior", "p_valor"]
        return tabela.reset_index().rename(columns={"covariate": "covariavel"})
