"""Clean — ONS: geração horária por usina/conjunto (todas as fontes).

- remove duplicatas pela chave natural (instante x unidade);
- textos aparados e marcadores de vazio ('', '-') viram nulo;
- identidade estável da unidade (`chave_unidade`): id_ons quando existe — o ONS
  troca o nome de uma mesma usina ao longo do tempo (ex.: 'LBI_LG BARRO I' ->
  'UFV LG BARRO I', mesmo id_ons PISLB1) — e nome+UF para as agregações
  'Pequenas Usinas', que não têm id_ons;
- nome canônico = o mais recente de cada unidade, mais uma chave normalizada;
- timezone: `din_instante` (horário de Brasília, sem fuso) -> `data_hora_utc`;
- grade horária completa por unidade; gaps de até 3 h interpolados, maiores
  mantidos nulos com flag 'faltante';
- geração negativa (consumo auxiliar) zerada com flag 'negativo_zerado'.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

import ds_toolkit as dst
from ETL.config import FONTE_ONS, FUSO_BRASIL, MAX_GAP_INTERPOLACAO_H, TIPO_UNIDADE_ONS, VERBOSE_TOOLKIT
from ETL.utils import (completar_grade, flag_qualidade, interpolar_gaps_curtos,
                       ler_parquet, limpar_textos, normalizar_nome)

log = logging.getLogger(__name__)

COLUNAS_TEXTO = ["id_subsistema", "nom_subsistema", "id_estado", "nom_estado",
                 "cod_modalidadeoperacao", "nom_tipousina", "nom_tipocombustivel",
                 "nom_usina", "id_ons", "ceg"]
ATRIBUTOS_UNIDADE = ["id_ons", "ceg", "modalidade_ons", "tipo_unidade", "fonte",
                     "combustivel", "id_subsistema", "id_estado"]


def limpar(caminho: Path) -> pd.DataFrame:
    """`caminho` é a pasta mensal do bruto do ONS (um Parquet por mês)."""
    df = ler_parquet(caminho)
    log.info("[clean:ons] %s: %d linhas", caminho.parent.name, len(df))

    df = limpar_textos(df, COLUNAS_TEXTO, vazios=("", "-"))
    df = dst.tratar_duplicados(
        df, subset=["din_instante", "id_estado", "nom_usina", "cod_modalidadeoperacao", "nom_tipousina"],
        manter="last", verbose=VERBOSE_TOOLKIT,
    )

    df["fonte"] = df["nom_tipousina"].map(FONTE_ONS).astype("string")
    desconhecidas = df.loc[df["fonte"].isna(), "nom_tipousina"].unique()
    if len(desconhecidas):
        log.warning("[clean:ons] tipos de usina sem mapeamento: %s", list(desconhecidas))

    df["chave_unidade"] = df["id_ons"].fillna("PQU|" + df["nom_usina"] + "|" + df["id_estado"])
    dup = df.duplicated(["chave_unidade", "din_instante"])
    if dup.any():
        raise ValueError(f"{dup.sum()} linhas com a mesma unidade no mesmo instante")

    df = df.rename(columns={"cod_modalidadeoperacao": "modalidade_ons",
                            "nom_tipocombustivel": "combustivel",
                            "val_geracao": "geracao_mwh"})
    df["tipo_unidade"] = df["modalidade_ons"].map(TIPO_UNIDADE_ONS).fillna("usina").astype("string")

    # Nome canônico: o mais recente de cada unidade (renomeações do ONS)
    df = df.sort_values(["chave_unidade", "din_instante"])
    df["nome_usina"] = df.groupby("chave_unidade")["nom_usina"].transform("last")
    renomeadas = df.groupby("chave_unidade")["nom_usina"].nunique()
    for chave in renomeadas[renomeadas > 1].index:
        nomes = df.loc[df["chave_unidade"] == chave, "nom_usina"].unique().tolist()
        log.info("[clean:ons] unidade %s renomeada no período: %s", chave, nomes)

    # Grade horária completa + gaps
    df, criadas = completar_grade(
        df, "chave_unidade", "din_instante", ATRIBUTOS_UNIDADE + ["nome_usina"]
    )
    if criadas:
        log.info("[clean:ons] %d horas ausentes inseridas na grade", criadas)
    df = df.sort_values(["chave_unidade", "din_instante"]).reset_index(drop=True)

    negativo = df["geracao_mwh"] < 0
    df.loc[negativo, "geracao_mwh"] = 0.0
    df, interpolado, faltante = interpolar_gaps_curtos(
        df, "chave_unidade", ["geracao_mwh"], MAX_GAP_INTERPOLACAO_H
    )
    extra = pd.Series(pd.NA, index=df.index, dtype="string").mask(negativo, "negativo_zerado")
    df["flag_qualidade"] = flag_qualidade(interpolado, faltante, extra)

    # Timezone: horário de Brasília -> UTC (Brasil sem horário de verão desde 2019;
    # para datas antigas o tz database trata as transições)
    local = df["din_instante"].dt.tz_localize(FUSO_BRASIL, ambiguous="NaT", nonexistent="NaT")
    if local.isna().any():
        log.warning("[clean:ons] %d instantes ambíguos/inexistentes no fuso local", local.isna().sum())
    df["data_hora_utc"] = local.dt.tz_convert("UTC")
    df["data_hora_brasilia"] = local

    df = normalizar_nome(df, "nome_usina", "nome_usina_normalizado")

    log.info("[clean:ons] flags: %s", df["flag_qualidade"].value_counts().to_dict())
    return df[[
        "data_hora_utc", "data_hora_brasilia", "chave_unidade", "id_ons", "ceg",
        "nome_usina", "nome_usina_normalizado", "tipo_unidade", "modalidade_ons",
        "fonte", "combustivel", "id_subsistema", "id_estado",
        "geracao_mwh", "flag_qualidade", "arquivo_origem",
    ]]
