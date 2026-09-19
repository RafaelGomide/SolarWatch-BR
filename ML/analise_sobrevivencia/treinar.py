"""Análise de sobrevivência de ativos: treino, avaliação e previsão por usina.

Fluxo:

1. carrega os eventos simulados e prepara as covariáveis;
2. **Kaplan-Meier** por fonte + log-rank (solar × eólica);
3. **Paramétricos** (Weibull, Log-Normal, Log-Logística, Exponencial) por fonte, ranqueados por AIC;
4. **Cox** estratificado por fonte + o Cox ingênuo (comparação: fonte como covariável);
5. avaliação: C-index k-fold, Schoenfeld, recuperação dos betas verdadeiros e calibração;
6. **Saída de produto**: P(sem manutenção em 6, 12, 24 e 36 meses) por usina da `dim_usina`;
7. salva os modelos em pickle (`ML/modelos/`) e os resultados em Parquet.

Uso:
    python -m ML.analise_sobrevivencia.treinar
    python -m ML.analise_sobrevivencia.treinar --sem-graficos --sem-salvar
    python -m ML.analise_sobrevivencia.treinar --horizontes 3 6 12
"""

from __future__ import annotations

import argparse
import logging
import warnings
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import ds_toolkit as dst
from ML.analise_sobrevivencia import avaliacao, dados, modelos
from ML.analise_sobrevivencia.config import (HORIZONTES_MESES, MODELOS, RESULTADOS)

log = logging.getLogger("ML.analise_sobrevivencia")


def _grafico_calibracao(tabela: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(7, 6))
    for tempo, grupo in tabela.groupby("tempo_anos"):
        ax.scatter(grupo["observado_km"], grupo["previsto"], s=70, label=f"{tempo} ano(s)")
        for linha in grupo.itertuples():
            ax.annotate(linha.grupo_risco, (linha.observado_km, linha.previsto),
                        textcoords="offset points", xytext=(6, -3), fontsize=8)
    limites = [tabela[["previsto", "observado_km"]].min().min() - 0.05, 1.0]
    ax.plot(limites, limites, ls="--", color="gray", lw=1)
    ax.set(xlabel="Observado (Kaplan-Meier)", ylabel="Previsto (Cox)",
           title="Calibração por grupo de risco", xlim=limites, ylim=limites)
    ax.legend()
    fig.tight_layout()
    caminho = RESULTADOS / "calibracao.png"
    fig.savefig(caminho, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return caminho


def _grafico_probabilidades(previsoes: pd.DataFrame, meses: int):
    coluna = f"p_sem_manutencao_{meses}m"
    fig, ax = plt.subplots(figsize=(9, 5))
    for fonte, grupo in previsoes.groupby("fonte"):
        ax.hist(grupo[coluna], bins=20, alpha=0.6, label=f"{fonte} (n={len(grupo)})")
    ax.set(xlabel=f"P(sem manutenção nos próximos {meses} meses)", ylabel="usinas",
           title=f"Distribuição da probabilidade prevista — horizonte de {meses} meses")
    ax.legend()
    fig.tight_layout()
    caminho = RESULTADOS / f"probabilidades_{meses}m.png"
    fig.savefig(caminho, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return caminho


def executar(horizontes: tuple[int, ...], graficos: bool, salvar: bool) -> dict:
    RESULTADOS.mkdir(parents=True, exist_ok=True)
    MODELOS.mkdir(parents=True, exist_ok=True)
    eventos, meta = dados.carregar_eventos()

    # 1. Kaplan-Meier por fonte -------------------------------------------------
    km = modelos.kaplan_meier_por_fonte(
        eventos, plotar=graficos, salvar_em=RESULTADOS / "kaplan_meier_por_fonte.png" if graficos else None)
    medianas = pd.DataFrame(km["medianas"]).T.reset_index().rename(columns={"index": "fonte"})

    # 2. Paramétricos -----------------------------------------------------------
    parametricos = modelos.parametricos_por_fonte(eventos, plotar=graficos)
    if graficos:   # a última figura aberta é a comparação paramétrica da 2ª fonte
        plt.close("all")

    # 3. Cox --------------------------------------------------------------------
    log.info("[treino] Cox ingênuo (fonte como covariável), para comparação:")
    modelos.ajustar_cox_ingenuo(eventos)
    cox = modelos.ajustar_cox(eventos)

    # 4. Avaliação --------------------------------------------------------------
    cindex = avaliacao.cindex_validacao_cruzada(eventos)
    schoenfeld = avaliacao.testar_riscos_proporcionais(cox, eventos)
    recuperacao = avaliacao.recuperacao_dos_parametros(cox, meta)

    weibull = modelos.ajustar_weibull_regressao(eventos)
    limites = modelos.limites_de_suporte(eventos)
    previsor = modelos.PrevisorSobrevivencia(cox, weibull, limites, horizontes, metadados={
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "origem_treino": str(dados.EVENTOS.name),
        "dado_simulado": True,
        "n_usinas_treino": int(len(eventos)),
        "n_eventos": int(eventos["evento"].sum()),
        "cindex_cv": cindex,
        "beta_verdadeiro": meta.get("beta_verdadeiro"),
        "weibull_verdadeiro": meta.get("weibull_por_fonte"),
        "horizontes_meses": list(horizontes),
        "limite_suporte_cox_anos": limites,
        "extrapolacao": "regressao Weibull por fonte além do último evento observado",
    })
    calibra = avaliacao.calibracao(previsor, eventos)

    # 5. Produto: probabilidade por usina ---------------------------------------
    usinas = dados.carregar_usinas_para_previsao()
    previsoes = previsor.prever(usinas, condicional=True)
    previsoes["tempo_mediano_anos"] = previsor.tempo_mediano_anos(usinas).to_numpy()

    colunas_saida = ["usina_id", "nome", "fonte", "id_subsistema", "id_estado", "municipio",
                     "potencia_mw", "data_operacao", "idade_anos", "risco_relativo",
                     "tempo_mediano_anos", *[f"p_sem_manutencao_{m}m" for m in horizontes],
                     "condicional_na_idade", "metodo_extrapolacao", "qualidade_vinculo"]
    previsoes = previsoes[colunas_saida]

    print("\n=== Kaplan-Meier: mediana de sobrevivência por fonte ===")
    print(medianas.to_string(index=False))
    print("\n=== Ajuste paramétrico (menor AIC = melhor) ===")
    print(parametricos[["fonte", "modelo", "AIC", "delta_aic", "parametros"]].to_string(index=False))
    print("\n=== Cox estratificado por fonte ===")
    print(previsor.resumo_coeficientes().round(4).to_string(index=False))
    if not recuperacao.empty:
        print("\n=== Recuperação dos parâmetros verdadeiros ===")
        print(recuperacao.round(4).to_string(index=False))
    print("\n=== Calibração (previsto × Kaplan-Meier observado) ===")
    print(calibra.round(3).to_string(index=False))
    print(f"\n=== P(sem manutenção) — {len(previsoes)} usinas ===")
    print(previsoes.groupby("fonte")[[f"p_sem_manutencao_{m}m" for m in horizontes]]
          .mean().round(3).to_string())
    print(previsoes.nsmallest(5, f"p_sem_manutencao_{horizontes[1]}m")
          [["nome", "fonte", "potencia_mw", "idade_anos", f"p_sem_manutencao_{horizontes[1]}m"]]
          .round(3).to_string(index=False))

    if graficos:
        log.info("[treino] gráficos: %s | %s", _grafico_calibracao(calibra),
                 _grafico_probabilidades(previsoes, horizontes[1]))

    if salvar:
        caminho = dst.salvar_modelo(previsor, MODELOS / "sobrevivencia_cox.pkl", metadados={
            "tipo": "sobrevivencia_manutencao",
            "modelo": "cox_ph_estratificado_por_fonte",
            **previsor.metadados,
        })
        log.info("[treino] modelo salvo: %s", caminho)
        parametricos.drop(columns="ajuste").to_parquet(MODELOS / "sobrevivencia_parametricos.parquet",
                                                       index=False)
        previsoes.to_parquet(MODELOS / "sobrevivencia_probabilidades_por_usina.parquet", index=False)
        pd.concat([calibra.assign(tabela="calibracao"),
                   schoenfeld.assign(tabela="schoenfeld")], ignore_index=True) \
          .to_parquet(MODELOS / "sobrevivencia_diagnosticos.parquet", index=False)
        if not recuperacao.empty:
            recuperacao.to_parquet(MODELOS / "sobrevivencia_recuperacao_betas.parquet", index=False)

    return {"km": km, "parametricos": parametricos, "cox": cox, "previsor": previsor,
            "cindex": cindex, "schoenfeld": schoenfeld, "recuperacao": recuperacao,
            "calibracao": calibra, "previsoes": previsoes}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    warnings.filterwarnings("ignore")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--horizontes", nargs="+", type=int, default=list(HORIZONTES_MESES),
                        help="horizontes em meses (padrão: 6 12 24 36)")
    parser.add_argument("--sem-graficos", action="store_true")
    parser.add_argument("--sem-salvar", action="store_true")
    args = parser.parse_args()

    executar(tuple(args.horizontes), not args.sem_graficos, not args.sem_salvar)


if __name__ == "__main__":
    main()
