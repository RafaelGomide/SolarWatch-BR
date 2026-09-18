"""Clean — eventos de manutenção SIMULADOS (ML/analise_sobrevivencia/dados_simulados.py).

O dado já nasce limpo, então esta etapa só valida os invariantes do modelo de
sobrevivência e deriva `tempo_dias` (a unidade do banco).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ETL.config import VERBOSE_TOOLKIT

log = logging.getLogger(__name__)


def limpar(caminho: Path) -> pd.DataFrame:
    df = pd.read_parquet(caminho)
    log.info("[clean:manutencao] %d usinas simuladas", len(df))

    df = dst.tratar_duplicados(df, subset=["ceg"], manter="last", verbose=VERBOSE_TOOLKIT)

    problemas = {
        "tempo <= 0": int((df["tempo_anos"] <= 0).sum()),
        "evento fora de {0,1}": int((~df["evento"].isin([0, 1])).sum()),
        "evento sem data": int(((df["evento"] == 1) & df["data_evento"].isna()).sum()),
        "censurado com data": int(((df["evento"] == 0) & df["data_evento"].notna()).sum()),
        "evento após o corte": int((df["data_evento"] > df["data_corte"]).sum()),
    }
    if any(problemas.values()):
        raise ValueError(f"Dados simulados inconsistentes: {problemas}")

    df["tempo_dias"] = np.maximum(np.round(df["tempo_anos"] * 365.25), 1).astype("int64")
    df["evento"] = df["evento"].astype("int64")
    df["simulado"] = True
    return df
