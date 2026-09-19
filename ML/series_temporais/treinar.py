"""Treino e avaliação dos modelos de previsão de geração.

Fluxo:

1. carrega as séries (por fonte e/ou por usina) do banco;
2. roda o backtesting de janela deslizante com todos os modelos;
3. calcula RMSE / MAE / MAPE / sMAPE por série e por modelo, e o ganho sobre o baseline;
4. reajusta o **melhor modelo de cada série** em todo o histórico;
5. salva o modelo em `ML/modelos/previsao_<serie>.pkl` (+ metadados .json),
   as métricas e as previsões do backtesting.

Uso:
    python -m ML.series_temporais.treinar                      # séries por fonte
    python -m ML.series_temporais.treinar --series ambas       # + as 3 maiores usinas
    python -m ML.series_temporais.treinar --sem-sarima         # rápido (SARIMA custa ~35 s/ajuste)
    python -m ML.series_temporais.treinar --janelas 10 --horizonte 24
"""

from __future__ import annotations

import argparse
import logging
import warnings
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import ds_toolkit as dst
from ML.series_temporais import backtesting, dados, metricas, modelos
from ML.series_temporais.config import HORIZONTE_H, MODELOS, N_JANELAS
from ML.series_temporais.features import ALVO

log = logging.getLogger("ML.series_temporais")
RESULTADOS = Path(__file__).resolve().parent / "resultados"


def _grafico(serie: pd.DataFrame, previsoes: pd.DataFrame, nome: str) -> Path:
    """Última janela do backtesting: observado × previsto de cada modelo."""
    ultima = previsoes[previsoes["janela"] == previsoes["janela"].max()]
    inicio = ultima["data_hora"].min() - pd.Timedelta(days=3)

    fig, ax = plt.subplots(figsize=(12, 5))
    for modelo, grupo in ultima.groupby("modelo"):
        ax.plot(grupo["data_hora"], grupo["previsto"], lw=1.8, ls="--", label=modelo, zorder=2)
    # observado por último e acima: é a referência que precisa ficar visível
    contexto = serie.loc[serie.index >= inicio, ALVO]
    ax.plot(contexto.index, contexto.to_numpy(), color="#111111", lw=2.4,
            label="observado", zorder=3)
    ax.axvline(ultima["data_hora"].min(), color="#999999", lw=1, zorder=1)
    ax.set(title=f"Previsão de 24 h — {nome} (última janela do backtesting)",
           xlabel="", ylabel="MWh por hora")
    ax.legend(ncol=3, fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()

    RESULTADOS.mkdir(parents=True, exist_ok=True)
    caminho = RESULTADOS / f"backtest_{nome}.png"
    fig.savefig(caminho, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return caminho


def _salvar_modelo(modelo, serie: pd.DataFrame, nome: str, linha_metricas: dict) -> Path:
    caminho = MODELOS / f"previsao_{nome}.pkl"
    return dst.salvar_modelo(modelo, caminho, metadados={
        "tipo": "previsao_geracao_series_temporais",
        "serie": nome,
        "fonte": str(serie["fonte"].iat[0]),
        "modelo": modelo.nome,
        "horizonte_h": modelo.horizonte,
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "treino_inicio": str(serie.index.min()),
        "treino_fim": str(serie.index.max()),
        "n_observacoes": int(len(serie)),
        "metricas_backtesting": {k: v for k, v in linha_metricas.items()
                                 if k not in ("serie", "fonte", "modelo")},
        "exogenas": dados.COLUNAS_CLIMA,
        "observacao": "Clima do horizonte tratado como conhecido (ver features.py).",
    })


def executar(series: dict[str, pd.DataFrame], n_janelas: int, horizonte: int,
             com_sarima: bool, graficos: bool, salvar: bool) -> pd.DataFrame:
    previsoes, tempos = [], []
    for nome, serie in series.items():
        catalogo = modelos.catalogo(horizonte=horizonte, com_sarima=com_sarima)
        previsto, tempo = backtesting.rodar(serie, catalogo, n_janelas, horizonte)
        previsoes.append(previsto)
        tempos.append(tempo)

    previsoes = pd.concat(previsoes, ignore_index=True)
    tempos = pd.concat(tempos, ignore_index=True)
    resumo = metricas.ganho_sobre_baseline(metricas.resumir(previsoes))
    resumo = resumo.merge(tempos.groupby(["serie", "modelo"])["segundos"].mean()
                          .round(2).rename("seg_por_ajuste"), on=["serie", "modelo"], how="left")

    print("\n=== Backtesting (janela deslizante, horizonte de %d h) ===" % horizonte)
    print(resumo.round(3).to_string(index=False))

    MODELOS.mkdir(parents=True, exist_ok=True)
    resumo.to_parquet(MODELOS / "metricas_backtesting.parquet", index=False)
    previsoes.to_parquet(MODELOS / "previsoes_backtesting.parquet", index=False)

    for nome, serie in series.items():
        do_serie = resumo[resumo["serie"] == str(serie["serie"].iat[0])]
        melhor = do_serie.iloc[0]  # resumir() já ordena por RMSE
        log.info("[treino] %s: melhor modelo = %s (RMSE %.1f, %.1f%% melhor que o baseline)",
                 nome, melhor["modelo"], melhor["rmse"], melhor["ganho_vs_baseline_%"])

        if graficos:
            caminho = _grafico(serie, previsoes[previsoes["serie"] == melhor["serie"]], nome)
            log.info("[treino] gráfico: %s", caminho)

        if salvar:
            catalogo = {m.nome: m for m in modelos.catalogo(horizonte, com_sarima)}
            final = catalogo[melhor["modelo"]].ajustar(serie)   # reajuste no histórico completo
            log.info("[treino] modelo salvo: %s", _salvar_modelo(final, serie, nome, melhor.to_dict()))

    return resumo


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    warnings.filterwarnings("ignore")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--series", choices=["fonte", "usina", "ambas"], default="fonte")
    parser.add_argument("--fontes", nargs="+", default=None, help="ex.: solar eolica")
    parser.add_argument("--usinas", nargs="+", type=int, default=None, help="usina_id específicos")
    parser.add_argument("--top-n", type=int, default=3, help="maiores usinas, se --series usina")
    parser.add_argument("--janelas", type=int, default=N_JANELAS)
    parser.add_argument("--horizonte", type=int, default=HORIZONTE_H)
    parser.add_argument("--sem-sarima", action="store_true")
    parser.add_argument("--sem-graficos", action="store_true")
    parser.add_argument("--sem-salvar", action="store_true")
    args = parser.parse_args()

    series: dict[str, pd.DataFrame] = {}
    if args.series in ("fonte", "ambas"):
        series |= dados.series_por_fonte(args.fontes)
    if args.series in ("usina", "ambas"):
        series |= dados.series_por_usina(args.usinas, args.top_n)

    executar(series, args.janelas, args.horizonte, not args.sem_sarima,
             not args.sem_graficos, not args.sem_salvar)


if __name__ == "__main__":
    main()
