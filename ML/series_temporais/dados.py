"""Carga das séries de geração + clima para a previsão.

Fonte: o banco `DB/solarwatch.duckdb` (ou, se ele não existir, os Parquets da
camada curated). Duas granularidades de série:

- por fonte  : geração horária somada de todas as usinas solares ou eólicas;
- por usina  : geração horária de uma unidade específica.

O clima é diário (grão da `fato_clima`) e é repetido nas 24 horas do dia,
alinhado pela **data em horário de Brasília**.
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from ML.series_temporais.config import BANCO, CURATED, FREQUENCIA, FUSO_BRASIL

log = logging.getLogger(__name__)

COLUNAS_CLIMA = ["irradiancia_kwh_m2", "vento_ms", "temperatura_c",
                 "temperatura_max_c", "temperatura_min_c"]


def _conectar() -> duckdb.DuckDBPyConnection:
    """Conecta no banco; sem banco, registra os Parquets da curated como views."""
    if BANCO.exists():
        return duckdb.connect(str(BANCO), read_only=True)
    con = duckdb.connect()
    for tabela in ["fato_geracao", "fato_clima", "dim_usina"]:
        arquivo = CURATED / f"{tabela}.parquet"
        if not arquivo.exists():
            raise FileNotFoundError(
                f"Nem o banco ({BANCO}) nem {arquivo} existem. "
                "Rode: python -m ETL.pipeline && python -m DB.criar_banco"
            )
        con.execute(f"CREATE VIEW {tabela} AS SELECT * FROM read_parquet('{arquivo.as_posix()}')")
    log.info("[dados] banco ausente; lendo direto dos Parquets da curated")
    return con


def _montar(geracao: pd.DataFrame, clima: pd.DataFrame, nome: str, fonte: str) -> pd.DataFrame:
    serie = geracao.set_index("data_hora").sort_index()
    serie.index = serie.index.tz_convert(FUSO_BRASIL)
    serie = serie.asfreq(FREQUENCIA)  # grade horária explícita (exigida pelo SARIMA)

    serie["data"] = serie.index.date
    clima = clima.set_index("data")
    for coluna in COLUNAS_CLIMA:
        serie[coluna] = serie["data"].map(clima[coluna])
    serie = serie.drop(columns="data")

    serie["serie"] = nome
    serie["fonte"] = fonte
    faltantes = int(serie["energia_mwh"].isna().sum())
    if faltantes:
        log.warning("[dados] %s: %d horas sem geração na grade", nome, faltantes)
    log.info("[dados] %s: %d horas (%s → %s), média %.0f MWh",
             nome, len(serie), serie.index.min(), serie.index.max(),
             serie["energia_mwh"].mean())
    return serie


def series_por_fonte(fontes: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Uma série por fonte: geração nacional somada + clima médio das usinas da fonte."""
    con = _conectar()
    try:
        fontes = fontes or [f[0] for f in con.execute(
            "SELECT DISTINCT fonte FROM fato_geracao ORDER BY 1").fetchall()]
        series = {}
        for fonte in fontes:
            geracao = con.execute("""
                SELECT timestamp_utc AS data_hora, sum(energia_mwh) AS energia_mwh
                FROM fato_geracao WHERE fonte = ? GROUP BY 1 ORDER BY 1
            """, [fonte]).df()
            clima = con.execute(f"""
                SELECT c.data, {", ".join(f"avg(c.{c}) AS {c}" for c in COLUNAS_CLIMA)}
                FROM fato_clima c JOIN dim_usina u USING (usina_id)
                WHERE u.fonte = ? GROUP BY 1 ORDER BY 1
            """, [fonte]).df()
            series[fonte] = _montar(geracao, clima, nome=fonte, fonte=fonte)
        return series
    finally:
        con.close()


def series_por_usina(usina_ids: list[int] | None = None, top_n: int = 3) -> dict[str, pd.DataFrame]:
    """Séries de usinas individuais. Sem ids, usa as `top_n` de maior geração."""
    con = _conectar()
    try:
        if not usina_ids:
            usina_ids = [r[0] for r in con.execute("""
                SELECT usina_id FROM fato_geracao GROUP BY 1
                ORDER BY sum(energia_mwh) DESC LIMIT ?
            """, [top_n]).fetchall()]
        series = {}
        for usina_id in usina_ids:
            geracao = con.execute("""
                SELECT timestamp_utc AS data_hora, energia_mwh
                FROM fato_geracao WHERE usina_id = ? ORDER BY 1
            """, [usina_id]).df()
            clima = con.execute(f"""
                SELECT data, {", ".join(COLUNAS_CLIMA)} FROM fato_clima
                WHERE usina_id = ? ORDER BY 1
            """, [usina_id]).df()
            fonte, nome = con.execute(
                "SELECT fonte, nome FROM dim_usina WHERE usina_id = ?", [usina_id]).fetchone()
            series[f"usina_{usina_id}"] = _montar(
                geracao, clima, nome=f"usina_{usina_id} ({nome})", fonte=fonte)
        return series
    finally:
        con.close()
