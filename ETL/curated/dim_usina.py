"""Curated — dim_usina: uma linha por UNIDADE GERADORA solar/eólica do ONS.

Grão: a unidade em que o ONS mede a geração — usina individual (TIPO I/II),
conjunto de usinas ou agregado de pequenas usinas de um estado. É o único
grão em que existe geração medida, então é ele que ancora a estrela.

Enriquecimento com o cadastro da ANEEL (potência, município, coordenadas,
data de operação), com o método registrado em `metodo_vinculo`:

- 'ceg'           : usina individual; CEG do ONS == CEG da ANEEL (sem sufixo de
                    versão), em qualquer fase — há usinas que já geram no ONS e
                    ainda constam como 'Construção' no cadastro.
- 'nome'          : conjunto; usinas da ANEEL da mesma UF e fonte cujo nome
                    normalizado contém o "núcleo" do nome do conjunto
                    ('CONJUNTO EOLICO SANTA EUGENIA' -> 'santa eugenia' casa
                    'Ventos de Santa Eugenia 01'...). Conjuntos com núcleo mais
                    específico escolhem primeiro e cada usina da ANEEL entra em
                    um único conjunto. É um vínculo APROXIMADO.
- 'sem_vinculo'   : nenhum correspondente (ex.: agregados de pequenas usinas,
                    conjuntos com nome de subestação).

Como o vínculo por nome pode pegar só parte das usinas de um conjunto, ele é
verificado contra a geração medida: `razao_pico_potencia` = maior geração
horária observada / potência vinculada. Uma unidade não gera mais que sua
potência, e no período costuma chegar a 40–100% dela. Daí `qualidade_vinculo`:

- 'exata'         : vínculo por CEG;
- 'consistente'   : por nome, com razão pico/potência em [0,40; 1,15];
- 'inconsistente' : por nome, fora dessa faixa (vínculo parcial ou excessivo);
- 'sem_vinculo'.

`potencia_mw` só é publicada para 'exata' e 'consistente'; nas demais fica nula
e o valor vinculado continua auditável em `potencia_aneel_vinculada_mw`.

Também produz `ponte_usina_aneel` (usina ANEEL -> usina_id), usada pela
fato_manutencao e útil para auditar o vínculo.

`usina_id` é estável entre execuções: IDs de uma dim_usina anterior são
reaproveitados pela `chave_unidade`, e unidades novas recebem max(id) + 1.
"""

from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ETL.config import (CURATED, FONTES_CURATED, PALAVRAS_GENERICAS_CONJUNTO,
                        POTENCIA_MINIMA_MW_VINCULO, VERBOSE_TOOLKIT)
from ETL.utils import base_ceg

log = logging.getLogger(__name__)

TAMANHO_MINIMO_NUCLEO = 4  # evita casar núcleos genéricos como 'sul', 'ii'
FAIXA_RAZAO_PICO_POTENCIA = (0.40, 1.15)


def _unidades_ons(ons: pd.DataFrame) -> pd.DataFrame:
    sol_eol = ons[ons["fonte"].isin(FONTES_CURATED)].sort_values("data_hora_utc")
    return sol_eol.groupby("chave_unidade", as_index=False).agg(
        id_ons=("id_ons", "last"),
        ceg_ons=("ceg", "last"),
        nome=("nome_usina", "last"),
        nome_normalizado=("nome_usina_normalizado", "last"),
        fonte=("fonte", "last"),
        tipo_unidade=("tipo_unidade", "last"),
        modalidade_ons=("modalidade_ons", "last"),
        id_estado=("id_estado", "last"),
        regiao=("id_subsistema", "last"),
        primeira_medicao_utc=("data_hora_utc", "min"),
        ultima_medicao_utc=("data_hora_utc", "max"),
        pico_geracao_mw=("geracao_mwh", "max"),
    )


def _nucleo(nome_normalizado: str) -> str:
    """'conjunto eolico morro do chapeu sul ii 230 kv' -> 'morro do chapeu sul ii'"""
    sem_tensao = re.sub(r"\b\d+\s*kv\b", " ", nome_normalizado)
    palavras = [p for p in sem_tensao.split() if p not in PALAVRAS_GENERICAS_CONJUNTO]
    return " ".join(palavras)


def _vincular(unidades: pd.DataFrame, aneel: pd.DataFrame) -> pd.DataFrame:
    """Retorna a ponte (ceg_aneel, chave_unidade, metodo_vinculo, nucleo_usado)."""
    solar_eolica = aneel[aneel["fonte"].isin(FONTES_CURATED)].copy()
    solar_eolica["ceg_base"] = base_ceg(solar_eolica["ceg"])
    elegiveis = solar_eolica[
        (solar_eolica["fase"] == "Operação")
        & (solar_eolica["potencia_outorgada_mw"] >= POTENCIA_MINIMA_MW_VINCULO)
    ]
    ligacoes = []

    # 1) Usinas individuais: CEG exato, em qualquer fase do cadastro
    individuais = unidades[unidades["ceg_ons"].notna()].copy()
    individuais["ceg_base"] = base_ceg(individuais["ceg_ons"])
    por_ceg = individuais.merge(solar_eolica[["ceg", "ceg_base"]], on="ceg_base", how="inner")
    ligacoes += [{"ceg_aneel": r.ceg, "chave_unidade": r.chave_unidade,
                  "metodo_vinculo": "ceg", "nucleo_usado": None} for r in por_ceg.itertuples()]
    usados = set(por_ceg["ceg"])

    # 2) Conjuntos: nome (mais específico primeiro; cada usina ANEEL em um só conjunto)
    conjuntos = unidades[unidades["tipo_unidade"] == "conjunto"].copy()
    conjuntos["nucleo"] = conjuntos["nome_normalizado"].map(_nucleo)
    conjuntos = conjuntos.sort_values("nucleo", key=lambda s: s.str.len(), ascending=False)
    for c in conjuntos.itertuples():
        palavras = c.nucleo.split()
        candidatos = elegiveis[
            (elegiveis["id_estado"] == c.id_estado) & (elegiveis["fonte"] == c.fonte)
            & ~elegiveis["ceg"].isin(usados)
        ]
        # tenta o núcleo inteiro e, sem sucesso, vai removendo palavras do fim
        # ('caetite 123' -> 'caetite'), sem descer abaixo de 4 caracteres
        for n in range(len(palavras), 0, -1):
            nucleo = " ".join(palavras[:n])
            if len(nucleo) < TAMANHO_MINIMO_NUCLEO:
                break
            padrao = rf"(?:^|\s){re.escape(nucleo)}(?:\s|$)"
            achados = candidatos[candidatos["nome_normalizado"].str.contains(padrao, regex=True, na=False)]
            if not achados.empty:
                ligacoes += [{"ceg_aneel": ceg, "chave_unidade": c.chave_unidade,
                              "metodo_vinculo": "nome", "nucleo_usado": nucleo}
                             for ceg in achados["ceg"]]
                usados |= set(achados["ceg"])
                break

    ponte = pd.DataFrame(ligacoes, columns=["ceg_aneel", "chave_unidade", "metodo_vinculo", "nucleo_usado"])
    return ponte.merge(
        solar_eolica[["ceg", "potencia_outorgada_mw", "latitude", "longitude", "municipio",
                   "data_entrada_operacao"]].rename(columns={"ceg": "ceg_aneel"}),
        on="ceg_aneel", how="left",
    )


def _ids_estaveis(dim: pd.DataFrame) -> pd.Series:
    anterior = CURATED / "dim_usina.parquet"
    mapa: dict[str, int] = {}
    if anterior.exists():
        antigo = pd.read_parquet(anterior, columns=["usina_id", "chave_unidade"])
        mapa = dict(zip(antigo["chave_unidade"], antigo["usina_id"]))
    proximo = max(mapa.values(), default=0) + 1
    ids = []
    for chave in dim["chave_unidade"]:
        if chave not in mapa:
            mapa[chave] = proximo
            proximo += 1
        ids.append(mapa[chave])
    return pd.Series(ids, index=dim.index, dtype="uint64")


def construir(ons: pd.DataFrame, aneel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    unidades = _unidades_ons(ons)
    ponte = _vincular(unidades, aneel)

    membros = ponte.groupby("chave_unidade").agg(
        potencia_mw=("potencia_outorgada_mw", "sum"),
        lat=("latitude", "mean"),
        lon=("longitude", "mean"),
        municipio=("municipio", lambda s: s.mode().iat[0] if s.notna().any() else pd.NA),
        data_operacao=("data_entrada_operacao", "min"),
        n_usinas_aneel=("ceg_aneel", "size"),
        metodo_vinculo=("metodo_vinculo", "first"),
    ).reset_index()

    dim = dst.mesclar_seguro(unidades, membros, on="chave_unidade", como="left",
                             validar="one_to_one", verbose=VERBOSE_TOOLKIT)
    dim["metodo_vinculo"] = dim["metodo_vinculo"].fillna("sem_vinculo")
    dim["n_usinas_aneel"] = dim["n_usinas_aneel"].fillna(0).astype("int64")

    # Verificação do vínculo contra a geração medida
    dim["potencia_aneel_vinculada_mw"] = dim["potencia_mw"]
    dim["razao_pico_potencia"] = dim["pico_geracao_mw"] / dim["potencia_mw"]
    minimo, maximo = FAIXA_RAZAO_PICO_POTENCIA
    coerente = dim["razao_pico_potencia"].between(minimo, maximo)
    dim["qualidade_vinculo"] = np.select(
        [dim["metodo_vinculo"] == "ceg", (dim["metodo_vinculo"] == "nome") & coerente,
         dim["metodo_vinculo"] == "nome"],
        ["exata", "consistente", "inconsistente"], default="sem_vinculo",
    )
    dim.loc[~dim["qualidade_vinculo"].isin(["exata", "consistente"]), "potencia_mw"] = np.nan
    dim["data_operacao"] = pd.to_datetime(dim["data_operacao"]).dt.date

    dim = dim.sort_values(["fonte", "regiao", "id_estado", "nome"]).reset_index(drop=True)
    dim.insert(0, "usina_id", _ids_estaveis(dim))
    dim = dim.sort_values("usina_id").reset_index(drop=True)

    ponte = ponte.merge(dim[["chave_unidade", "usina_id"]], on="chave_unidade", how="left")

    log.info("[curated:dim_usina] %d unidades | qualidade do vínculo: %s | potência publicada: %.1f GW",
             len(dim), dim["qualidade_vinculo"].value_counts().to_dict(), dim["potencia_mw"].sum() / 1000)
    dim = dim[[
        "usina_id", "chave_unidade", "id_ons", "ceg_ons", "nome", "nome_normalizado",
        "fonte", "tipo_unidade", "modalidade_ons", "regiao", "id_estado", "municipio",
        "potencia_mw", "lat", "lon", "data_operacao", "n_usinas_aneel", "metodo_vinculo",
        "qualidade_vinculo", "potencia_aneel_vinculada_mw", "pico_geracao_mw", "razao_pico_potencia",
        "primeira_medicao_utc", "ultima_medicao_utc",
    ]]
    ponte = ponte[["ceg_aneel", "usina_id", "chave_unidade", "metodo_vinculo", "nucleo_usado",
                   "potencia_outorgada_mw", "municipio", "data_entrada_operacao"]]
    return dim, ponte
