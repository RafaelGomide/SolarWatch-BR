"""Backtesting com janela deslizante (rolling origin).

O erro clássico em séries temporais é validar com embaralhamento aleatório
(`train_test_split`, `KFold`), o que treina com dado posterior ao que se quer
prever e produz métricas boas demais. Aqui:

    |------------------ treino ------------------|-- teste (24h) --|
    |------------------ treino -------------------------|-- teste (24h) --|
    |------------------ treino ----------------------------------|-- teste --|

- a origem avança de `horizonte` em `horizonte` (janelas de teste sem sobreposição);
- o treino é **expansível**: sempre todo o passado até a origem;
- cada modelo é **reajustado do zero** em cada janela;
- antes de cada previsão, um `assert` confirma que o último instante de treino é
  anterior ao primeiro de teste.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from ML.series_temporais.config import HORIZONTE_H, MINIMO_TREINO_H, N_JANELAS
from ML.series_temporais.features import ALVO
from ML.series_temporais.modelos import ModeloPrevisao

log = logging.getLogger(__name__)


def origens(indice: pd.DatetimeIndex, n_janelas: int = N_JANELAS,
            horizonte: int = HORIZONTE_H, minimo_treino: int = MINIMO_TREINO_H) -> list[int]:
    """Posições de corte (fim do treino) de cada janela, da mais antiga à mais recente."""
    ultima = len(indice) - horizonte
    if ultima < minimo_treino:
        raise ValueError(
            f"Série curta demais: {len(indice)} pontos para {minimo_treino} de treino "
            f"+ {horizonte} de teste."
        )
    cortes = [ultima - i * horizonte for i in range(n_janelas)]
    return sorted(c for c in cortes if c >= minimo_treino)


def rodar(serie: pd.DataFrame, modelos: list[ModeloPrevisao], n_janelas: int = N_JANELAS,
          horizonte: int = HORIZONTE_H) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Avalia todos os modelos nas mesmas janelas.

    Retorna (previsões, tempos): o painel longo com uma linha por
    (série, modelo, janela, hora) e os tempos de ajuste por modelo.
    """
    nome_serie = str(serie["serie"].iat[0])
    fonte = str(serie["fonte"].iat[0])
    cortes = origens(serie.index, n_janelas, horizonte)
    log.info("[backtest] %s: %d janelas de %d h (origens de %s a %s)",
             nome_serie, len(cortes), horizonte,
             serie.index[cortes[0]], serie.index[cortes[-1]])

    linhas, tempos = [], []
    for janela, corte in enumerate(cortes, start=1):
        treino = serie.iloc[:corte]
        teste = serie.iloc[corte:corte + horizonte]
        assert treino.index.max() < teste.index.min(), "vazamento: treino alcança o teste"

        exogenas_futuro = teste.drop(columns=[ALVO])
        for modelo in modelos:
            inicio = time.perf_counter()
            try:
                previsto = modelo.ajustar(treino).prever(exogenas_futuro)
            except Exception as erro:  # um modelo com problema não derruba a comparação
                log.warning("[backtest] %s falhou na janela %d: %s", modelo.nome, janela, erro)
                continue
            duracao = time.perf_counter() - inicio
            tempos.append({"serie": nome_serie, "modelo": modelo.nome, "janela": janela,
                           "segundos": duracao})
            linhas.append(pd.DataFrame({
                "serie": nome_serie, "fonte": fonte, "modelo": modelo.nome, "janela": janela,
                "data_hora": teste.index, "y": teste[ALVO].to_numpy(),
                "previsto": previsto.to_numpy(),
            }))
        log.info("[backtest] %s janela %d/%d concluída (treino até %s)",
                 nome_serie, janela, len(cortes), treino.index.max())

    return pd.concat(linhas, ignore_index=True), pd.DataFrame(tempos)
