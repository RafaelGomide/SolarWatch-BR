"""Checagens de integridade da camada curated antes de gravar.

Qualquer falha interrompe a pipeline: é melhor não publicar do que publicar
um banco com chave duplicada ou fato órfão.
"""

from __future__ import annotations

import logging

import pandas as pd

import ds_toolkit as dst
from ETL.config import VERBOSE_TOOLKIT

log = logging.getLogger(__name__)

REGRAS = {
    "dim_usina": {
        "pk": ["usina_id"],
        "obrigatorias": ["usina_id", "chave_unidade", "nome", "fonte", "regiao"],
        "faixas": {"potencia_mw": (0, None), "lat": (-34, 5.5), "lon": (-74, -34)},
    },
    "fato_geracao": {
        "pk": ["usina_id", "timestamp_utc"],
        "obrigatorias": ["usina_id", "timestamp_utc", "fonte", "flag_qualidade"],
        "faixas": {"energia_mwh": (0, None)},
    },
    "fato_clima": {
        "pk": ["usina_id", "data"],
        "obrigatorias": ["usina_id", "data", "local_clima"],
        "faixas": {"irradiancia_kwh_m2": (0, 12), "vento_ms": (0, 60), "temperatura_c": (-10, 50)},
    },
    "fato_manutencao": {
        "pk": ["usina_id"],
        "obrigatorias": ["usina_id", "tempo_dias", "evento_ocorreu"],
        "faixas": {"tempo_dias": (1, None)},
    },
}


def validar(tabelas: dict[str, pd.DataFrame]) -> None:
    erros = []
    ids_dim = set(tabelas["dim_usina"]["usina_id"])

    for nome, regra in REGRAS.items():
        df = tabelas[nome]
        dup = int(df.duplicated(regra["pk"]).sum())
        if dup:
            erros.append(f"{nome}: {dup} chaves primárias duplicadas {regra['pk']}")
        for col in regra["obrigatorias"]:
            nulos = int(df[col].isna().sum())
            if nulos:
                erros.append(f"{nome}: {nulos} nulos em '{col}' (obrigatória)")
        for col, (minimo, maximo) in regra["faixas"].items():
            s = df[col].dropna()
            fora = 0
            if minimo is not None:
                fora += int((s < minimo).sum())
            if maximo is not None:
                fora += int((s > maximo).sum())
            if fora:
                erros.append(f"{nome}: {fora} valores de '{col}' fora de [{minimo}, {maximo}]")
        if nome != "dim_usina":
            orfaos = int((~df["usina_id"].isin(ids_dim)).sum())
            if orfaos:
                erros.append(f"{nome}: {orfaos} linhas com usina_id fora da dim_usina")

        log.info("[validacao] %s: %d linhas x %d colunas", nome, len(df), df.shape[1])
        dst.relatorio_qualidade(df, verbose=VERBOSE_TOOLKIT)

    if erros:
        raise ValueError("Validação da camada curated falhou:\n  - " + "\n  - ".join(erros))
    log.info("[validacao] OK: PKs únicas, sem órfãos, obrigatórias preenchidas, faixas válidas")
