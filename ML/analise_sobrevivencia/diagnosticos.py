"""Diagnóstico de riscos proporcionais, com uma violação de tamanho conhecido.

O conjunto principal satisfaz PH por construção dentro de cada fonte, então
rodar um teste de Schoenfeld nele só responde "não detectei nada" — o que não
diz se o teste **funciona**. Um diagnóstico sem controle positivo é fé.

Este módulo usa a variante `eventos_manutencao_ph_violado.parquet`, em que o
efeito de `log_potencia_mw_c` muda de +0,60 para −0,10 aos 3 anos
(`doc_tecnica_dados_simulados.md` §9.2). Com a verdade conhecida dá para medir
três coisas:

1. **Sensibilidade:** o Schoenfeld acusa a violação que existe?
2. **Especificidade:** ele fica quieto no conjunto principal, onde não há?
3. **Correção:** partindo o tempo no corte, o Cox recupera os dois betas?

O terceiro ponto é o que torna o diagnóstico útil: detectar que a premissa caiu
não serve de nada se não houver o que fazer em seguida.

Há ainda um segundo diagnóstico aqui, de natureza diferente: o **viés de
sobrevivente** do cadastro (`--sobrevivente`). A população de treino são as
usinas que estão em operação **hoje**; as que foram descomissionadas sumiram do
SIGA e nunca entram na conta. Se as descomissionadas forem justamente as que
mais quebravam, o modelo subestima o risco. O que o módulo faz é medir o
tamanho possível desse efeito, em vez de afirmar que ele é pequeno.

Uso:
    python -m ML.analise_sobrevivencia.diagnosticos
    python -m ML.analise_sobrevivencia.diagnosticos --sem-graficos
    python -m ML.analise_sobrevivencia.diagnosticos --sobrevivente
"""

from __future__ import annotations

import argparse
import json
import logging
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ML.analise_sobrevivencia import dados
from ML.analise_sobrevivencia.config import ALPHA, COVARIAVEIS, ESTRATO, RESULTADOS
from ML.analise_sobrevivencia.dados_simulados import SAIDA_PH_VIOLADO

TEMPO, EVENTO = "tempo_anos", "evento"

log = logging.getLogger("ML.analise_sobrevivencia.diagnosticos")


def carregar_variante(caminho: Path = SAIDA_PH_VIOLADO) -> tuple[pd.DataFrame, dict]:
    if not caminho.exists():
        raise FileNotFoundError(
            f"{caminho} não existe. Rode: python -m ML.analise_sobrevivencia.dados_simulados")
    df = pd.read_parquet(caminho)
    meta_json = caminho.with_name(caminho.stem + ".meta.json")
    meta = json.loads(meta_json.read_text(encoding="utf-8")) if meta_json.exists() else {}

    df = dados._covariaveis(df, "potencia_mw", "id_subsistema",
                            df["data_entrada_operacao"].dt.year)
    log.info("[diagnosticos] variante: %d usinas, %d eventos (%d antes do corte)",
             len(df), int(df[EVENTO].sum()),
             int(((df[EVENTO] == 1) & (df["periodo_do_evento"] == "antes")).sum()))
    return df, meta


def _cox(df: pd.DataFrame, covariaveis: list[str], entrada: str | None = None,
         duracao: str = TEMPO):
    from lifelines import CoxPHFitter

    colunas = [duracao, EVENTO, ESTRATO, *covariaveis] + ([entrada] if entrada else [])
    modelo = CoxPHFitter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(df[colunas].dropna(), duration_col=duracao, event_col=EVENTO,
                   entry_col=entrada, strata=[ESTRATO])
    return modelo


def schoenfeld(df: pd.DataFrame, rotulo: str) -> pd.DataFrame:
    """Teste de Schoenfeld por covariável, com o Cox estratificado por fonte."""
    from lifelines.statistics import proportional_hazard_test

    modelo = _cox(df, COVARIAVEIS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        teste = proportional_hazard_test(modelo, df[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna(),
                                         time_transform="rank")
    tabela = (teste.summary.reset_index()
              .rename(columns={"index": "covariavel", "p": "p_valor"})
              .assign(conjunto=rotulo))
    violadas = tabela.loc[tabela["p_valor"] < ALPHA, "covariavel"].tolist()
    log.info("[schoenfeld:%s] %s", rotulo,
             f"acusa {violadas}" if violadas else "nenhuma covariável acusada")
    return tabela[["conjunto", "covariavel", "test_statistic", "p_valor"]]


def partir_no_corte(df: pd.DataFrame, corte: float, covariavel: str) -> pd.DataFrame:
    """Quebra cada usina em dois intervalos no `corte`, em processo de contagem.

    Uma usina que falhou aos 5 anos vira: `(0, 3]` sem evento e `(3, 5]` com
    evento. A interação `<covariavel>_depois` só é diferente de zero no segundo
    trecho, então seu coeficiente é a **diferença** entre o efeito depois e o
    efeito antes — que é exatamente o que se quer estimar.
    """
    primeiro = df[df[TEMPO] > corte].copy()
    primeiro["t_inicio"] = 0.0
    primeiro["t_fim"] = corte
    primeiro[EVENTO] = 0
    primeiro["depois"] = 0.0

    segundo = df[df[TEMPO] > corte].copy()
    segundo["t_inicio"] = corte
    segundo["t_fim"] = segundo[TEMPO]
    segundo["depois"] = 1.0

    curto = df[df[TEMPO] <= corte].copy()
    curto["t_inicio"] = 0.0
    curto["t_fim"] = curto[TEMPO]
    curto["depois"] = 0.0

    painel = pd.concat([primeiro, segundo, curto], ignore_index=True)
    painel[f"{covariavel}_depois"] = painel[covariavel] * painel["depois"]
    return painel


def betas_por_periodo(df: pd.DataFrame, efeito: dict) -> pd.DataFrame:
    """Estima o efeito antes e depois do corte, e compara com a verdade."""
    covariavel, corte = efeito["covariavel"], float(efeito["corte_anos"])
    interacao = f"{covariavel}_depois"
    painel = partir_no_corte(df, corte, covariavel)

    modelo = _cox(painel, [*COVARIAVEIS, interacao], entrada="t_inicio", duracao="t_fim")
    resumo = modelo.summary
    antes = float(resumo.loc[covariavel, "coef"])
    delta = float(resumo.loc[interacao, "coef"])
    erro_antes = float(resumo.loc[covariavel, "se(coef)"])
    erro_delta = float(resumo.loc[interacao, "se(coef)"])

    return pd.DataFrame([
        {"periodo": f"antes de {corte:.0f} anos", "beta_verdadeiro": efeito["beta_antes"],
         "beta_estimado": antes, "erro_padrao": erro_antes,
         "ic_inferior": antes - 1.96 * erro_antes, "ic_superior": antes + 1.96 * erro_antes},
        {"periodo": f"depois de {corte:.0f} anos", "beta_verdadeiro": efeito["beta_depois"],
         "beta_estimado": antes + delta, "erro_padrao": np.hypot(erro_antes, erro_delta),
         "ic_inferior": antes + delta - 1.96 * np.hypot(erro_antes, erro_delta),
         "ic_superior": antes + delta + 1.96 * np.hypot(erro_antes, erro_delta)},
        {"periodo": "diferença (interação)", "beta_verdadeiro": efeito["beta_depois"] - efeito["beta_antes"],
         "beta_estimado": delta, "erro_padrao": erro_delta,
         "ic_inferior": delta - 1.96 * erro_delta, "ic_superior": delta + 1.96 * erro_delta},
    ]).assign(
        ic_cobre=lambda t: (t["ic_inferior"] <= t["beta_verdadeiro"])
        & (t["beta_verdadeiro"] <= t["ic_superior"]),
        p_valor=[float(resumo.loc[covariavel, "p"]), np.nan, float(resumo.loc[interacao, "p"])],
    )


def efeito_ignorando_o_tempo(df: pd.DataFrame, covariavel: str) -> dict:
    """O que um Cox comum estima quando o efeito muda no tempo: uma média
    ponderada dos dois períodos, que não descreve nenhum dos dois."""
    resumo = _cox(df, COVARIAVEIS).summary
    return {"coef": float(resumo.loc[covariavel, "coef"]),
            "p_valor": float(resumo.loc[covariavel, "p"])}


def grafico_residuos(df: pd.DataFrame, covariavel: str, corte: float) -> Path:
    """Resíduos de Schoenfeld escalados no tempo: a inclinação é o efeito mudando."""
    modelo = _cox(df, COVARIAVEIS)
    dados_cox = df[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        residuos = modelo.compute_residuals(dados_cox, "scaled_schoenfeld")

    tempos = dados_cox.loc[residuos.index, TEMPO]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(tempos, residuos[covariavel], s=12, alpha=0.35)
    janela = max(len(residuos) // 20, 5)
    ordenado = pd.DataFrame({"t": tempos.to_numpy(), "r": residuos[covariavel].to_numpy()}).sort_values("t")
    ax.plot(ordenado["t"], ordenado["r"].rolling(janela, center=True, min_periods=1).mean(),
            color="crimson", lw=2, label=f"média móvel ({janela} pontos)")
    ax.axhline(0, color="gray", ls="--", lw=1)
    ax.axvline(corte, color="seagreen", ls=":", lw=2, label=f"corte do gerador ({corte:.0f} anos)")
    ax.set(xlabel="Tempo até o evento (anos)", ylabel=f"Resíduo de Schoenfeld — {covariavel}",
           title="Efeito que muda no tempo deixa inclinação no resíduo (dado simulado)")
    ax.legend()
    fig.tight_layout()
    RESULTADOS.mkdir(parents=True, exist_ok=True)
    caminho = RESULTADOS / "schoenfeld_ph_violado.png"
    fig.savefig(caminho, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return caminho


def curva_de_poder(deltas=(0.0, -0.2, -0.35, -0.5, -0.7), sementes=(42, 7, 2026)) -> pd.DataFrame:
    """Quão grande a violação precisa ser para o teste enxergá-la?

    Regera a variante com diferentes tamanhos de violação (`beta_depois -
    beta_antes`) e conta em quantas sementes o Schoenfeld acusa. É a pergunta
    que ficou em aberto na doc: o teste não pegou a violação embutida no Cox
    ingênuo — era falha do teste ou a violação era pequena demais?
    """
    from ML.analise_sobrevivencia import dados_simulados as gerador

    usinas, data_corte = gerador.carregar_usinas(1.0)
    base = gerador.EFEITO_TEMPO_DEPENDENTE
    linhas = []
    for delta in deltas:
        efeito = {**base, "beta_depois": base["beta_antes"] + delta}
        for semente in sementes:
            bruto = gerador.simular_ph_violado(usinas, data_corte, semente, efeito)
            df = dados._covariaveis(bruto, "potencia_mw", "id_subsistema",
                                    bruto["data_entrada_operacao"].dt.year)
            teste = schoenfeld(df, f"delta={delta:+.2f} seed={semente}")
            p = float(teste.loc[teste["covariavel"] == base["covariavel"], "p_valor"].iloc[0])
            linhas.append({"delta_beta": delta, "seed": semente, "p_valor": p,
                           "acusou": p < ALPHA, "eventos": int(df[EVENTO].sum())})

    tabela = pd.DataFrame(linhas)
    return (tabela.groupby("delta_beta")
            .agg(p_mediano=("p_valor", "median"), acusou=("acusou", "sum"),
                 execucoes=("acusou", "size"), eventos=("eventos", "mean"))
            .reset_index())


# --------------------------------------------------------- viés de sobrevivente
IDADE_COORTE_ANTIGA = 15.0   # anos: só quem já é velho o bastante poderia ter saído


def perfil_do_cadastro() -> pd.DataFrame:
    """Idade das usinas em operação e fases que o SIGA registra.

    O ponto é factual: o cadastro **não tem** estado de "descomissionada" para
    UFV/EOL — a usina simplesmente deixa de aparecer. Então não há como contar
    as que faltam; só dá para limitar quantas poderiam ser.
    """
    from ML.analise_sobrevivencia.dados_simulados import ENTRADA, FONTE

    bruto = pd.read_parquet(ENTRADA, columns=["SigTipoGeracao", "DscFaseUsina",
                                              "DatEntradaOperacao", "DatGeracaoConjuntoDados"])
    fases = (bruto[bruto["SigTipoGeracao"].isin(FONTE)]
             .groupby(["SigTipoGeracao", "DscFaseUsina"]).size()
             .rename("usinas").reset_index())
    log.info("[sobrevivente] fases registradas para UFV/EOL: %s",
             sorted(fases["DscFaseUsina"].unique()))
    return fases


def vies_de_sobrevivente(df: pd.DataFrame, fracoes=(0.05, 0.10, 0.20, 0.50)) -> pd.DataFrame:
    """Quanto as estimativas andariam se o cadastro tivesse perdido usinas.

    Como as descomissionadas não são observáveis, a conta é de **limite
    superior**: supõe-se que a coorte antiga (mais de 15 anos) perdeu uma
    fração `f` de usinas e que **todas elas eram as piores** — falharam cedo,
    no primeiro quartil dos tempos observados da coorte. Qualquer cenário real
    é menos severo que esse.
    """
    from lifelines import CoxPHFitter, KaplanMeierFitter

    # A coorte exposta ao risco de ter sumido é definida pela IDADE da usina
    # (quanto tempo ela existe), não pelo tempo até o evento: uma usina nova que
    # falhou cedo nunca teria tido tempo de ser descomissionada.
    idade = (df["data_corte"] - df["data_entrada_operacao"]).dt.days / 365.25
    antigas = df[idade >= IDADE_COORTE_ANTIGA]
    if antigas.empty:
        raise ValueError(
            f"nenhuma usina com {IDADE_COORTE_ANTIGA:.0f}+ anos; ajuste IDADE_COORTE_ANTIGA")
    tempo_pessimista = float(antigas[TEMPO].quantile(0.25))

    linhas = []
    for fracao in (0.0, *fracoes):
        faltantes = int(round(fracao * len(antigas)))
        extras = antigas.sample(n=faltantes, replace=True, random_state=42).copy() if faltantes else antigas.head(0)
        if faltantes:
            extras[TEMPO] = tempo_pessimista
            extras[EVENTO] = 1
        completo = pd.concat([df, extras], ignore_index=True)

        registro = {"fracao_perdida": fracao, "usinas_adicionadas": faltantes,
                    "n_total": len(completo)}
        for fonte, grupo in completo.groupby(ESTRATO):
            km = KaplanMeierFitter().fit(grupo[TEMPO], grupo[EVENTO])
            registro[f"mediana_{fonte}"] = float(km.median_survival_time_)
            registro[f"s5_{fonte}"] = float(km.predict(5))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cox = CoxPHFitter().fit(completo[[TEMPO, EVENTO, ESTRATO, *COVARIAVEIS]].dropna(),
                                    duration_col=TEMPO, event_col=EVENTO, strata=[ESTRATO])
        registro["beta_potencia"] = float(cox.params_["log_potencia_mw_c"])
        linhas.append(registro)

    tabela = pd.DataFrame(linhas)
    log.info("[sobrevivente] coorte antiga: %d usinas (>= %.0f anos); tempo pessimista = %.1f anos",
             len(antigas), IDADE_COORTE_ANTIGA, tempo_pessimista)
    return tabela


def executar(graficos: bool = True, poder: bool = False, sobrevivente: bool = False) -> dict:
    variante, meta = carregar_variante()
    efeito = meta.get("efeito_tempo_dependente", {})
    covariavel = efeito.get("covariavel", "log_potencia_mw_c")

    principal, _ = dados.carregar_eventos()

    testes = pd.concat([schoenfeld(principal, "principal (PH vale)"),
                        schoenfeld(variante, "variante (PH violado)")], ignore_index=True)
    ingenuo = efeito_ignorando_o_tempo(variante, covariavel)
    periodos = betas_por_periodo(variante, efeito)

    print("\n=== Teste de Schoenfeld: controle negativo x controle positivo ===")
    print(testes.round(4).to_string(index=False))

    print(f"\n=== Cox comum na variante: um coeficiente só para {covariavel} ===")
    print(f"estimado {ingenuo['coef']:+.3f} (p={ingenuo['p_valor']:.4f}) — "
          f"verdadeiros: {efeito.get('beta_antes'):+.2f} antes e {efeito.get('beta_depois'):+.2f} depois")

    print("\n=== Correção: partindo o tempo no corte ===")
    print(periodos.round(3).to_string(index=False))

    curva = None
    if poder:
        curva = curva_de_poder()
        print("\n=== Poder do teste: quanto a violação precisa crescer para aparecer ===")
        print(curva.round(4).to_string(index=False))

    vies = None
    if sobrevivente:
        fases = perfil_do_cadastro()
        vies = vies_de_sobrevivente(principal)
        print("\n=== Fases que o SIGA registra para UFV/EOL ===")
        print(fases.to_string(index=False))
        print("\n=== Viés de sobrevivente: limite superior do efeito ===")
        print(vies.round(3).to_string(index=False))

    caminho = grafico_residuos(variante, covariavel, float(efeito.get("corte_anos", 3))) if graficos else None
    if caminho:
        log.info("[diagnosticos] gráfico: %s", caminho)

    return {"testes": testes, "ingenuo": ingenuo, "periodos": periodos,
            "poder": curva, "sobrevivente": vies, "grafico": caminho}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    warnings.filterwarnings("ignore")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sem-graficos", action="store_true")
    parser.add_argument("--poder", action="store_true",
                        help="regera a variante com violações de vários tamanhos (mais lento)")
    parser.add_argument("--sobrevivente", action="store_true",
                        help="mede o limite superior do viés de sobrevivente do cadastro")
    args = parser.parse_args()

    executar(not args.sem_graficos, args.poder, args.sobrevivente)


if __name__ == "__main__":
    main()
