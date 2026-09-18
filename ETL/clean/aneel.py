"""Clean — ANEEL SIGA: cadastro de usinas solares e eólicas.

- nomes de coluna padronizados (toolkit) e renomeados para nomes descritivos;
- números em formato brasileiro ('1.400,00', '-20,12') -> float (toolkit);
- datas ISO -> datetime; data-sentinela 1900-01-03 e datas < 1990 para
  UFV/EOL viram nulo com `flag_data_operacao`;
- coordenadas (0,0) ou fora do Brasil viram nulo com `flag_coordenada`;
- município: 'Pedra Grande - RN, São Bento do Norte - RN' -> principal
  'Pedra Grande' + lista completa; 'Não Informado' -> nulo;
- chaves normalizadas de nome e município para cruzar grafias com o ONS;
- potências em MW; fonte e subsistema derivados.
- LGPD: a coluna de proprietários (nomes e CNPJs) é descartada aqui.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

import ds_toolkit as dst
from ETL.config import (DATA_MINIMA_PLAUSIVEL_UFV_EOL, DATA_SENTINELA_ANEEL, FONTE_ANEEL,
                        LIMITES_BRASIL, UF_SUBSISTEMA, VERBOSE_TOOLKIT)
from ETL.utils import limpar_textos, normalizar_nome

log = logging.getLogger(__name__)

RENOMEAR = {
    "datgeracaoconjuntodados": "data_retrato",
    "nomempreendimento": "nome",
    "idenucleoceg": "nucleo_ceg",
    "codceg": "ceg",
    "sigufprincipal": "id_estado",
    "sigtipogeracao": "sig_tipo_geracao",
    "dscfaseusina": "fase",
    "dscorigemcombustivel": "origem_combustivel",
    "dscfontecombustivel": "fonte_combustivel",
    "dsctipooutorga": "tipo_outorga",
    "datentradaoperacao": "data_entrada_operacao",
    "mdapotenciaoutorgadakw": "potencia_outorgada_kw",
    "mdapotenciafiscalizadakw": "potencia_fiscalizada_kw",
    "mdagarantiafisicakw": "garantia_fisica_kw",
    "idcgeracaoqualificada": "geracao_qualificada",
    "numcoordnempreendimento": "latitude",
    "numcoordeempreendimento": "longitude",
    "datiniciovigencia": "inicio_vigencia",
    "datfimvigencia": "fim_vigencia",
    "dscmuninicpios": "municipios",
}
DESCARTAR = ["_id", "nomfontecombustivel", "dscpropriregimepariticipacao", "dscsubbacia"]
NUMERICAS = ["potencia_outorgada_kw", "potencia_fiscalizada_kw", "garantia_fisica_kw",
             "latitude", "longitude"]
DATAS = ["data_retrato", "data_entrada_operacao", "inicio_vigencia", "fim_vigencia"]


def limpar(caminho: Path) -> pd.DataFrame:
    df = pd.read_parquet(caminho)
    log.info("[clean:aneel] %s: %d linhas", caminho.parent.name, len(df))

    df = dst.limpar_nomes_colunas(df)
    df = df.drop(columns=[c for c in DESCARTAR if c in df.columns]).rename(columns=RENOMEAR)
    df = limpar_textos(df, [c for c in df.columns if df[c].dtype == object or str(df[c].dtype) == "string"])
    df = dst.tratar_duplicados(df, subset=["ceg"], manter="last", verbose=VERBOSE_TOOLKIT)

    df = dst.converter_tipos(
        df, colunas_numericas=NUMERICAS, decimal_brasileiro=True,
        colunas_data=DATAS, formato_data="%Y-%m-%d", verbose=VERBOSE_TOOLKIT,
    )

    # Datas de entrada em operação inválidas
    flag_data = pd.Series(pd.NA, index=df.index, dtype="string")
    sentinela = df["data_entrada_operacao"] == pd.Timestamp(DATA_SENTINELA_ANEEL)
    implausivel = ~sentinela & (df["data_entrada_operacao"] < pd.Timestamp(DATA_MINIMA_PLAUSIVEL_UFV_EOL))
    flag_data[sentinela] = "sentinela_1900"
    flag_data[implausivel] = "implausivel_pre_1990"
    df.loc[sentinela | implausivel, "data_entrada_operacao"] = pd.NaT
    df["flag_data_operacao"] = flag_data

    # Coordenadas inválidas
    (lat_min, lat_max), (lon_min, lon_max) = LIMITES_BRASIL["lat"], LIMITES_BRASIL["lon"]
    zero = (df["latitude"] == 0) & (df["longitude"] == 0)
    fora = ~zero & ~(df["latitude"].between(lat_min, lat_max) & df["longitude"].between(lon_min, lon_max))
    flag_coord = pd.Series(pd.NA, index=df.index, dtype="string")
    flag_coord[zero] = "zerada"
    flag_coord[fora] = "fora_do_brasil"
    df.loc[zero | fora, ["latitude", "longitude"]] = float("nan")
    df["flag_coordenada"] = flag_coord

    # Município principal ('Nome - UF', o primeiro da lista)
    df["municipios"] = df["municipios"].mask(df["municipios"].str.lower() == "não informado")
    principal = df["municipios"].str.split(",").str[0].str.strip()
    df["municipio"] = principal.str.rsplit(" - ", n=1).str[0].str.strip()
    df["uf_municipio"] = principal.str.rsplit(" - ", n=1).str[1].str.strip()
    df = normalizar_nome(df, "municipio", "municipio_normalizado")
    df = normalizar_nome(df, "nome", "nome_normalizado")

    df["fonte"] = df["sig_tipo_geracao"].map(FONTE_ANEEL).astype("string")
    df["id_subsistema"] = df["id_estado"].map(UF_SUBSISTEMA).astype("string")
    for kw in ["potencia_outorgada_kw", "potencia_fiscalizada_kw", "garantia_fisica_kw"]:
        df[kw.replace("_kw", "_mw")] = df[kw] / 1000

    log.info("[clean:aneel] datas inválidas: %s | coordenadas inválidas: %s",
             df["flag_data_operacao"].value_counts().to_dict(),
             df["flag_coordenada"].value_counts().to_dict())
    return df[[
        "ceg", "nucleo_ceg", "nome", "nome_normalizado", "sig_tipo_geracao", "fonte",
        "fase", "tipo_outorga", "origem_combustivel", "fonte_combustivel",
        "id_estado", "id_subsistema", "municipio", "uf_municipio", "municipio_normalizado",
        "municipios", "potencia_outorgada_mw", "potencia_fiscalizada_mw", "garantia_fisica_mw",
        "latitude", "longitude", "flag_coordenada",
        "data_entrada_operacao", "flag_data_operacao", "inicio_vigencia", "fim_vigencia",
        "geracao_qualificada", "data_retrato",
    ]]
