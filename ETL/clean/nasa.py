"""Clean — NASA POWER: clima horário por ponto de coleta.

- `data_hora_utc` ('AAAAMMDDHH' texto) -> timestamp com fuso UTC (+ horário de Brasília);
- fill value -999 da API -> nulo;
- nomes de parâmetros da NASA -> nomes descritivos com unidade;
- duplicatas por (local, hora) removidas;
- grade horária completa; gaps de até 3 h interpolados, maiores (ex.: latência
  de ~2 dias da NASA no fim da série) mantidos nulos com flag 'faltante'.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ETL.config import FUSO_BRASIL, MAX_GAP_INTERPOLACAO_H, VERBOSE_TOOLKIT
from ETL.utils import completar_grade, flag_qualidade, interpolar_gaps_curtos

log = logging.getLogger(__name__)

FILL_VALUE = -999.0
PARAMETROS = {
    "ALLSKY_SFC_SW_DWN": "irradiancia_wh_m2",
    "WS10M": "vento_10m_ms",
    "WS50M": "vento_50m_ms",
    "T2M": "temperatura_2m_c",
}
ATRIBUTOS_LOCAL = ["municipio", "id_estado", "id_subsistema", "latitude", "longitude"]


def limpar(caminho: Path) -> pd.DataFrame:
    df = pd.read_parquet(caminho)
    log.info("[clean:nasa] %s: %d linhas", caminho.parent.name, len(df))

    df = df.rename(columns=PARAMETROS)
    medidas = list(PARAMETROS.values())
    ausentes = int((df[medidas] == FILL_VALUE).sum().sum())
    df[medidas] = df[medidas].replace(FILL_VALUE, np.nan)
    log.info("[clean:nasa] %d valores -999 convertidos em nulo", ausentes)

    df["instante"] = pd.to_datetime(df["data_hora_utc"], format="%Y%m%d%H")
    df = dst.tratar_duplicados(df, subset=["local", "instante"], manter="last", verbose=VERBOSE_TOOLKIT)

    df, criadas = completar_grade(df, "local", "instante", ATRIBUTOS_LOCAL)
    if criadas:
        log.info("[clean:nasa] %d horas ausentes inseridas na grade", criadas)
    df = df.sort_values(["local", "instante"]).reset_index(drop=True)

    df, interpolado, faltante = interpolar_gaps_curtos(df, "local", medidas, MAX_GAP_INTERPOLACAO_H)
    df["flag_qualidade"] = flag_qualidade(interpolado, faltante)

    utc = df["instante"].dt.tz_localize("UTC")
    df["data_hora_utc"] = utc
    df["data_hora_brasilia"] = utc.dt.tz_convert(FUSO_BRASIL)

    log.info("[clean:nasa] flags: %s", df["flag_qualidade"].value_counts().to_dict())
    return df[["local", "municipio", "id_estado", "id_subsistema", "latitude", "longitude",
               "data_hora_utc", "data_hora_brasilia", *medidas, "flag_qualidade"]]


PARAMETROS_DIARIOS = {
    "ALLSKY_SFC_SW_DWN": "irradiancia_kwh_m2_dia",
    "WS10M": "vento_10m_ms",
    "WS50M": "vento_50m_ms",
    "T2M": "temperatura_2m_c",
    "T2M_MAX": "temperatura_max_c",
    "T2M_MIN": "temperatura_min_c",
}
MAX_GAP_INTERPOLACAO_DIAS = 1


def limpar_diario(caminho: Path) -> pd.DataFrame:
    """Série DIÁRIA da NASA (dia em hora solar local, ~horário de Brasília).

    É a fonte da irradiância recente: a horária é publicada com ~3 meses de atraso.
    Gaps de 1 dia são interpolados; os demais (latência no fim da série) ficam
    nulos com flag 'faltante'.
    """
    df = pd.read_parquet(caminho)
    log.info("[clean:nasa_diario] %s: %d linhas", caminho.parent.name, len(df))

    df = df.rename(columns=PARAMETROS_DIARIOS)
    medidas = list(PARAMETROS_DIARIOS.values())
    ausentes = int((df[medidas] == FILL_VALUE).sum().sum())
    df[medidas] = df[medidas].replace(FILL_VALUE, np.nan)
    log.info("[clean:nasa_diario] %d valores -999 convertidos em nulo", ausentes)

    df["data"] = pd.to_datetime(df["data_lst"], format="%Y%m%d")
    df = dst.tratar_duplicados(df, subset=["local", "data"], manter="last", verbose=VERBOSE_TOOLKIT)
    df, criadas = completar_grade(df, "local", "data", ATRIBUTOS_LOCAL, freq="D")
    if criadas:
        log.info("[clean:nasa_diario] %d dias ausentes inseridos", criadas)
    df = df.sort_values(["local", "data"]).reset_index(drop=True)

    df, interpolado, faltante = interpolar_gaps_curtos(df, "local", medidas, MAX_GAP_INTERPOLACAO_DIAS)
    df["flag_qualidade"] = flag_qualidade(interpolado, faltante)

    log.info("[clean:nasa_diario] flags: %s", df["flag_qualidade"].value_counts().to_dict())
    return df[["local", "municipio", "id_estado", "id_subsistema", "latitude", "longitude",
               "data", *medidas, "flag_qualidade"]]
