"""Eventos recorrentes de manutenção: Andersen-Gill e PWP.

O modelo de produção (`modelos.py`) trata só o **1º** evento: depois da primeira
manutenção a usina sai do conjunto de risco, como se tivesse deixado de existir.
Na prática ela é reparada e volta a operar — e é justamente a usina que já
quebrou várias vezes que interessa à operação.

Três formulações, todas sobre o mesmo painel `(t_inicio, t_fim]` por episódio:

| Modelo | Escala de tempo | Conjunto de risco | Premissa |
|---|---|---|---|
| **Andersen-Gill (AG)** | total (desde a entrada em operação) | a usina fica em risco o tempo todo | incrementos independentes dado `x`; um só risco de base |
| **PWP tempo total (PWP-TT)** | total | só quem já teve `j-1` eventos entra no estrato `j` | risco de base **próprio por episódio** |
| **PWP gap time (PWP-GT)** | tempo desde o último reparo | idem | o relógio zera a cada reparo |

Qual usar depende do que se acredita sobre o reparo. O AG supõe que o reparo não
muda nada ("as good as before"), o que é forte: uma usina que já falhou cinco
vezes tem o mesmo risco de base de uma que nunca falhou, e toda a diferença
precisa caber nas covariáveis. O PWP relaxa isso com um risco de base por
episódio. Como o gerador destes dados **reinicia o relógio** a cada reparo e
piora o risco a cada episódio (`gamma`), o PWP-GT é o modelo correto aqui — e
os três são ajustados justamente para mostrar o tamanho do erro de usar o outro.

**Erro-padrão robusto é obrigatório.** As linhas de uma mesma usina não são
independentes: uma usina propensa a falhar contribui com vários episódios. Sem
agrupar por usina, o IC sai estreito demais e tudo parece significativo.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

from ML.analise_sobrevivencia.config import COVARIAVEIS, ESTRATO

log = logging.getLogger(__name__)

ID = "id_usina"
INICIO, FIM, GAP = "t_inicio_anos", "t_fim_anos", "gap_anos"
EVENTO = "evento"
EPISODIO = "episodio"
ESTRATO_EPISODIO = "estrato_episodio"
# Episódios acima deste número entram todos no mesmo estrato: sozinhos têm
# poucos eventos. É um compromisso — agrupar demais deixa episódios com riscos
# de base bem diferentes no mesmo estrato e enviesa os coeficientes (com 3, a
# cobertura dos ICs cai de 80% para 60% nestes dados); agrupar de menos deixa
# estratos com um punhado de eventos.
MAX_ESTRATO = 6


def preparar(painel: pd.DataFrame) -> pd.DataFrame:
    """Adiciona as covariáveis e o estrato de episódio ao painel de episódios."""
    from ML.analise_sobrevivencia.dados import _covariaveis

    df = _covariaveis(painel, "potencia_mw", "id_subsistema",
                      painel["data_entrada_operacao"].dt.year)
    df[ESTRATO_EPISODIO] = np.minimum(df[EPISODIO], MAX_ESTRATO)
    # Estratificar por episódio E por fonte de uma vez: as linhas de base das
    # duas fontes já não são proporcionais entre si (ver modelos.ajustar_cox)
    df["estrato"] = df[ESTRATO].astype(str) + "|" + df[ESTRATO_EPISODIO].astype(str)

    invalidos = df[FIM] <= df[INICIO]
    if invalidos.any():
        raise ValueError(f"{int(invalidos.sum())} episódios com intervalo vazio ou negativo")
    return df


def _ajustar_contagem(df: pd.DataFrame, estratos: list[str], rotulo: str,
                      agrupar_por_usina: bool = True):
    """Cox de processo de contagem: cada episódio entra no risco em `t_inicio`
    (entrada tardia) e sai em `t_fim`.

    Usa `CoxPHFitter` com `entry_col`, e não o `CoxTimeVaryingFitter`: este
    ainda não implementa `robust=True` (lifelines 0.30), e sem erro-padrão
    agrupado por usina o AG não se sustenta.
    """
    from lifelines import CoxPHFitter

    colunas = [ID, INICIO, FIM, EVENTO, *COVARIAVEIS, *estratos]
    dados = df[colunas].dropna()
    modelo = CoxPHFitter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(dados.drop(columns=[] if agrupar_por_usina else [ID]),
                   duration_col=FIM, entry_col=INICIO, event_col=EVENTO, strata=estratos,
                   cluster_col=ID if agrupar_por_usina else None)
    log.info("[%s] %d episódios de %d usinas, %d eventos | log-verossimilhança %.1f",
             rotulo, len(dados), dados[ID].nunique(), int(dados[EVENTO].sum()),
             modelo.log_likelihood_)
    return modelo


def ajustar_andersen_gill(df: pd.DataFrame):
    """AG: um risco de base por fonte, tempo total, usina sempre em risco."""
    return _ajustar_contagem(df, [ESTRATO], "andersen-gill")


def ajustar_pwp_tempo_total(df: pd.DataFrame):
    """PWP-TT: risco de base por (fonte, número do episódio), tempo total."""
    return _ajustar_contagem(df, ["estrato"], "pwp-tempo-total")


def ajustar_pwp_gap(df: pd.DataFrame):
    """PWP-GT: risco de base por (fonte, episódio), relógio zerado a cada reparo.

    Como cada episódio entra em risco no instante 0 da sua própria escala, um
    Cox comum sobre o `gap` basta; `cluster_col` dá o SE robusto por usina.
    """
    from lifelines import CoxPHFitter

    dados = df[[GAP, EVENTO, "estrato", ID, *COVARIAVEIS]].dropna()
    modelo = CoxPHFitter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(dados, duration_col=GAP, event_col=EVENTO, strata=["estrato"],
                   cluster_col=ID)
    log.info("[pwp-gap-time] %d episódios, %d eventos | C-index=%.3f",
             len(dados), int(dados[EVENTO].sum()), modelo.concordance_index_)
    return modelo


# ----------------------------------------------------------------------- MCF
def funcao_media_cumulativa(df: pd.DataFrame, por: str = ESTRATO) -> pd.DataFrame:
    """MCF — número médio acumulado de manutenções por usina até o tempo t.

    É o análogo do Kaplan-Meier para eventos recorrentes: em vez de "fração que
    ainda não falhou", responde "quantas manutenções uma usina típica já
    acumulou". Estimador de Nelson-Aalen sobre o processo de contagem:

        MCF(t) = soma_{s <= t} dN(s) / Y(s)

    com `Y(s)` = usinas ainda sob observação em `s` (o acompanhamento de cada
    usina termina no seu último episódio, censurado).
    """
    fim_observacao = df.groupby([por, ID])[FIM].max().rename("fim").reset_index()

    saidas = []
    for grupo, eventos in df[df[EVENTO] == 1].groupby(por):
        limites = fim_observacao.loc[fim_observacao[por] == grupo, "fim"].to_numpy()
        tempos = np.sort(eventos[FIM].unique())
        em_risco = (limites[None, :] >= tempos[:, None]).sum(axis=1)
        ocorridos = eventos.groupby(FIM).size().reindex(tempos).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            incremento = np.where(em_risco > 0, ocorridos / em_risco, 0.0)
        saidas.append(pd.DataFrame({por: grupo, "tempo_anos": tempos, "em_risco": em_risco,
                                    "eventos": ocorridos, "mcf": np.cumsum(incremento)}))
    return pd.concat(saidas, ignore_index=True)


def eventos_esperados(mcf: pd.DataFrame, idade_anos: float, horizonte_anos: float,
                      grupo: str, por: str = ESTRATO) -> float:
    """MCF(idade + horizonte) − MCF(idade): manutenções esperadas no horizonte.

    É a leitura de produto da MCF — "quantas manutenções esperar nos próximos N
    meses" — e a resposta que o modelo de 1º evento não consegue dar.
    """
    curva = mcf[mcf[por] == grupo]
    if curva.empty:
        return float("nan")
    t, y = curva["tempo_anos"].to_numpy(), curva["mcf"].to_numpy()
    return float(np.interp(idade_anos + horizonte_anos, t, y) - np.interp(idade_anos, t, y))


# ---------------------------------------------------------------- comparação
def _coeficientes(modelo, nome: str) -> pd.DataFrame:
    tabela = modelo.summary[["coef", "se(coef)", "coef lower 95%", "coef upper 95%", "p"]].copy()
    tabela.columns = ["coef", "erro_padrao", "ic_inferior", "ic_superior", "p_valor"]
    return tabela.reset_index().rename(columns={"covariate": "covariavel"}).assign(modelo=nome)


def comparar(ajustes: dict[str, object], beta_verdadeiro: dict | None = None) -> pd.DataFrame:
    """Coeficientes lado a lado e, com dado simulado, se o IC cobre o valor real."""
    tabela = pd.concat([_coeficientes(modelo, nome) for nome, modelo in ajustes.items()],
                       ignore_index=True)
    tabela = tabela[tabela["covariavel"].isin(COVARIAVEIS)]

    if beta_verdadeiro:
        tabela["beta_verdadeiro"] = tabela["covariavel"].map(beta_verdadeiro)
        tabela["erro"] = tabela["coef"] - tabela["beta_verdadeiro"]
        tabela["ic_cobre"] = ((tabela["ic_inferior"] <= tabela["beta_verdadeiro"])
                              & (tabela["beta_verdadeiro"] <= tabela["ic_superior"]))
    return tabela.sort_values(["covariavel", "modelo"]).reset_index(drop=True)


def comparar_erros_padrao(df: pd.DataFrame) -> pd.DataFrame:
    """Quanto o SE agrupado por usina difere do ingênuo, no AG.

    Mostra o preço de ignorar que os episódios de uma mesma usina são
    correlacionados: o SE ingênuo trata 4.157 episódios como 4.157 observações
    independentes, quando são 1.854 usinas, e subestima a incerteza.
    """
    ses = {
        "ingenuo": _ajustar_contagem(df, [ESTRATO], "ag-se-ingenuo", agrupar_por_usina=False),
        "agrupado": _ajustar_contagem(df, [ESTRATO], "ag-se-agrupado", agrupar_por_usina=True),
    }
    tabela = pd.DataFrame({rotulo: modelo.summary["se(coef)"] for rotulo, modelo in ses.items()})
    tabela = tabela.loc[COVARIAVEIS]
    tabela["razao"] = tabela["agrupado"] / tabela["ingenuo"]
    return tabela.rename_axis("covariavel").reset_index()


# ------------------------------------------------------------------ Previsor
class PrevisorRecorrencia:
    """Quantas manutenções esperar de cada usina nos próximos N meses.

    É a pergunta que o modelo de 1º evento não responde: ele só diz se a usina
    chega ao fim do horizonte **sem nenhuma** manutenção, e trata como iguais a
    usina nova e a que já foi reparada cinco vezes.

    Estimativa em duas partes:

        E[N(t0, t0+h) | x] ≈ [MCF_fonte(t0 + h) − MCF_fonte(t0)] · exp(beta_AG · x)

    A MCF dá o nível médio da fonte na idade da usina (incorpora a deterioração
    observada, porque é estimada sobre o processo completo) e o Andersen-Gill dá
    o multiplicador da usina — o AG é o modelo de **taxa**, e é dele que sai um
    coeficiente com leitura de "quantas vezes mais eventos por ano".

    É uma aproximação: o multiplicador é aplicado a uma média marginal, não a
    uma MCF ajustada por covariáveis. Serve para ordenar usinas e dar ordem de
    grandeza, não para um número contratual.
    """

    def __init__(self, andersen_gill, mcf: pd.DataFrame, horizontes_meses=(6, 12, 24, 36),
                 metadados: dict | None = None):
        self.andersen_gill = andersen_gill
        self.mcf = mcf
        self.horizontes_meses = tuple(horizontes_meses)
        self.covariaveis = list(COVARIAVEIS)
        self.estrato = ESTRATO
        self.metadados = metadados or {}

    def multiplicador(self, usinas: pd.DataFrame) -> np.ndarray:
        entrada = usinas[[self.estrato, *self.covariaveis]]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return np.exp(self.andersen_gill.predict_log_partial_hazard(entrada).to_numpy())

    def prever(self, usinas: pd.DataFrame) -> pd.DataFrame:
        idade = usinas.get("idade_anos", pd.Series(0.0, index=usinas.index)).to_numpy()
        fatores = self.multiplicador(usinas)
        fontes = usinas[self.estrato].astype(str).to_numpy()

        saida = usinas.copy()
        for meses in self.horizontes_meses:
            base = np.array([eventos_esperados(self.mcf, t0, meses / 12, fonte)
                             for t0, fonte in zip(idade, fontes)])
            saida[f"manutencoes_esperadas_{meses}m"] = np.round(base * fatores, 3)
        saida["taxa_relativa"] = fatores
        return saida


# ------------------------------------------------------------------ frailty
def _exposicao(painel: pd.DataFrame, mcf: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por usina: eventos observados e exposição esperada pela MCF.

    A exposição não é o tempo de observação em anos: o risco de base cresce com
    a idade, então dois anos de uma usina nova não valem dois anos de uma usina
    velha. `MCF_fonte(T_i)` é quantos eventos uma usina *média* daquela fonte
    teria acumulado em `T_i` anos — é a escala certa para comparar usinas de
    idades diferentes.
    """
    por_usina = painel.groupby(ID).agg(
        eventos=(EVENTO, "sum"), observacao_anos=(FIM, "max"),
        **{c: (c, "first") for c in [ESTRATO, *COVARIAVEIS]})
    por_usina["exposicao"] = [
        max(np.interp(t, mcf.loc[mcf[ESTRATO] == fonte, "tempo_anos"],
                      mcf.loc[mcf[ESTRATO] == fonte, "mcf"]), 1e-6)
        for t, fonte in zip(por_usina["observacao_anos"], por_usina[ESTRATO])
    ]
    return por_usina.reset_index()


def _ajustar_nb(sm, y: np.ndarray, X: pd.DataFrame, offset: np.ndarray):
    """Ajusta a binomial negativa pelo otimizador que chegar à melhor verossimilhança.

    A escolha não é preciosismo: com o padrão do `statsmodels`, um dos conjuntos
    testados convergia para `alpha = 0` (llf −2008,4) e o modelo declarava
    "nenhuma heterogeneidade" num dado cuja variância das contagens era 3,1
    vezes a média. O Newton divergia para `alpha` na casa dos milhões sem
    convergir, e o Nelder-Mead achava o ótimo de verdade (llf −1959,4,
    `alpha = 0,25`). Como todos otimizam a mesma função, comparar a
    log-verossimilhança resolve — e um `alpha` estimado no zero passa a
    significar ausência de heterogeneidade, não falha numérica.
    """
    melhor = None
    for metodo in ("nm", "bfgs", "newton"):
        try:
            ajuste = sm.NegativeBinomial(y, X, loglike_method="nb2", offset=offset).fit(
                disp=0, method=metodo, maxiter=2000)
        except Exception as erro:            # otimizador pode estourar sem convergir
            log.debug("[frailty] %s falhou: %s", metodo, erro)
            continue
        if not ajuste.mle_retvals.get("converged") or not np.isfinite(ajuste.llf):
            continue
        if melhor is None or ajuste.llf > melhor.llf:
            melhor = ajuste
    if melhor is None:
        raise RuntimeError("nenhum otimizador convergiu no ajuste da binomial negativa")
    return melhor


def estimar_frailty_gama(painel: pd.DataFrame, mcf: pd.DataFrame) -> dict:
    """Estima a variância da fragilidade gama por usina.

    Com eventos recorrentes, o Andersen-Gill com fragilidade gama tem a mesma
    verossimilhança de uma **binomial negativa** sobre a contagem de eventos por
    usina: o parâmetro de dispersão da NB *é* a variância da fragilidade. Isso
    permite estimá-la com `statsmodels`, sem um ajustador de frailty dedicado
    (que o lifelines não tem).

        N_i ~ NB(media = MCF_fonte(T_i) · exp(beta · x_i),  dispersao = theta)

    O teste contra o Poisson (`theta = 0`) é o teste de que a fragilidade
    existe: rejeitar quer dizer que sobra variação entre usinas depois das
    covariáveis — exatamente o que a linha de base única por fonte ignorava.
    """
    import statsmodels.api as sm

    dados = _exposicao(painel, mcf)
    X = sm.add_constant(pd.get_dummies(dados[[ESTRATO, *COVARIAVEIS]], columns=[ESTRATO],
                                       drop_first=True).astype(float))
    y = dados["eventos"].to_numpy()
    offset = np.log(dados["exposicao"].to_numpy())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        poisson = sm.GLM(y, X, family=sm.families.Poisson(), offset=offset).fit()
        nb = _ajustar_nb(sm, y, X, offset)

    theta = float(nb.params["alpha"])
    ic = nb.conf_int().loc["alpha"]
    razao_verossimilhanca = 2 * (nb.llf - poisson.llf)

    log.info("[frailty] theta estimado = %.3f (IC95 %.3f a %.3f) | LR vs Poisson = %.1f",
             theta, ic[0], ic[1], razao_verossimilhanca)

    dados["frailty_posterior"] = _frailty_posterior(dados, nb, offset, theta)
    return {
        "theta": theta,
        "ic_inferior": float(ic[0]),
        "ic_superior": float(ic[1]),
        "lr_vs_poisson": float(razao_verossimilhanca),
        "por_usina": dados,
        "modelo_nb": nb,
    }


def _frailty_posterior(dados: pd.DataFrame, nb, offset: np.ndarray, theta: float) -> np.ndarray:
    """E[Z_i | dados] — Bayes empírico, conjugado gama-Poisson.

        E[Z_i | N_i] = (1/theta + N_i) / (1/theta + mu_i)

    Lê-se direto: a usina que teve mais eventos do que o esperado para o seu
    perfil (`N_i > mu_i`) sai com Z > 1. O `1/theta` é o peso do encolhimento
    para 1 — com pouca evidência, a estimativa não dispara.
    """
    if theta <= 0:
        return np.ones(len(dados))
    mu = nb.predict(exog=nb.model.exog, offset=offset, which="mean")
    return (1 / theta + dados["eventos"].to_numpy()) / (1 / theta + mu)
