"""Carga e preparação dos dados de sobrevivência.

Duas fontes:

1. **Treino** — `dados/simulados/eventos_manutencao_simulados.parquet`: uma linha
   por usina da ANEEL, com tempo até o 1º evento de manutenção e indicador de
   censura (dado SINTÉTICO, ver `docs/ingestao/doc_tecnica_dados_simulados.md`).
2. **Predição** — `dim_usina` do banco: as unidades que a API serve. Só entram
   as que têm potência confiável e data de operação, porque são as covariáveis
   do modelo.

Sobre a covariável "idade": a idade da usina **no momento do evento** é o próprio
tempo de sobrevivência, então usá-la como covariável seria circular. O que entra
no modelo é o **ano de entrada em operação** (coorte tecnológica), centrado em
2018 — é assim que o gerador dos dados também o define.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ML.analise_sobrevivencia.config import (ANO_REFERENCIA, BANCO, COVARIAVEIS, CURATED,
                                             EVENTOS, META_EVENTOS, POTENCIA_REFERENCIA_MW)

log = logging.getLogger(__name__)


def _covariaveis(df: pd.DataFrame, potencia: str, subsistema: str, ano: pd.Series) -> pd.DataFrame:
    df = df.copy()
    df["log_potencia_mw_c"] = np.log(df[potencia]) - np.log(POTENCIA_REFERENCIA_MW)
    for sigla in ("NE", "S", "N"):   # SE é a categoria de referência
        df[f"subsistema_{sigla}"] = (df[subsistema] == sigla).astype(float)
    df["ano_entrada_c"] = ano - ANO_REFERENCIA
    return df


def carregar_eventos() -> tuple[pd.DataFrame, dict]:
    """Base de treino: tempo, evento e covariáveis por usina da ANEEL."""
    if not EVENTOS.exists():
        raise FileNotFoundError(
            f"{EVENTOS} não existe. Rode: python -m ML.analise_sobrevivencia.dados_simulados"
        )
    df = pd.read_parquet(EVENTOS)
    meta = json.loads(META_EVENTOS.read_text(encoding="utf-8")) if META_EVENTOS.exists() else {}

    # Valida os invariantes de sobrevivência (tempo > 0, evento 0/1, censura)
    df = dst.preparar_sobrevivencia(df, "tempo_anos", "evento", verbose=True)
    df = _covariaveis(df, "potencia_mw", "id_subsistema", df["data_entrada_operacao"].dt.year)
    df["tempo_meses"] = df["tempo_anos"] * 12

    log.info("[dados] treino: %d usinas, %d eventos (%.1f%% censura) | fontes: %s",
             len(df), int(df["evento"].sum()), 100 * (1 - df["evento"].mean()),
             df["fonte"].value_counts().to_dict())
    return df, meta


def carregar_usinas_para_previsao() -> pd.DataFrame:
    """Unidades da `dim_usina` com covariáveis completas, para gerar as probabilidades.

    Inclui `idade_anos` (tempo já em operação), usada na sobrevivência
    **condicional**: quem já operou 5 anos sem manutenção não parte do zero.
    """
    import duckdb

    if BANCO.exists():
        con = duckdb.connect(str(BANCO), read_only=True)
        origem = "dim_usina"
    else:
        con = duckdb.connect()
        arquivo = CURATED / "dim_usina.parquet"
        if not arquivo.exists():
            raise FileNotFoundError(f"Nem {BANCO} nem {arquivo} existem; rode o ETL e o DB.")
        con.execute(f"CREATE VIEW dim_usina AS SELECT * FROM read_parquet('{arquivo.as_posix()}')")
        origem = "dim_usina (parquet)"
    try:
        df = con.execute("""
            SELECT usina_id, nome, fonte, regiao AS id_subsistema, id_estado, municipio,
                   potencia_mw, data_operacao, qualidade_vinculo
            FROM dim_usina
            WHERE potencia_mw IS NOT NULL AND data_operacao IS NOT NULL
            ORDER BY usina_id
        """).df()
    finally:
        con.close()

    df["data_operacao"] = pd.to_datetime(df["data_operacao"])
    hoje = pd.Timestamp.today().normalize()
    df["idade_anos"] = (hoje - df["data_operacao"]).dt.days / 365.25
    df = _covariaveis(df, "potencia_mw", "id_subsistema", df["data_operacao"].dt.year)

    faltando = [c for c in COVARIAVEIS if df[c].isna().any()]
    if faltando:
        raise ValueError(f"Covariáveis com nulo: {faltando}")
    log.info("[dados] predição: %d unidades de %s (idade mediana %.1f anos)",
             len(df), origem, df["idade_anos"].median())
    return df
