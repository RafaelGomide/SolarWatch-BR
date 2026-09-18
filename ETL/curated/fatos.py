"""Curated — tabelas-fato (todas referenciam dim_usina.usina_id).

- fato_geracao    (usina x hora): energia gerada, fonte, subsistema, flag de qualidade
- fato_clima      (usina x dia) : irradiância, vento, temperatura do ponto NASA mais próximo
- fato_manutencao (usina)       : tempo até o 1º evento e se ocorreu (SIMULADO)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ETL.config import DISTANCIA_MAXIMA_CLIMA_KM, FONTES_CURATED, VERBOSE_TOOLKIT
from ETL.utils import haversine_km

log = logging.getLogger(__name__)


def fato_geracao(ons: pd.DataFrame, dim: pd.DataFrame) -> pd.DataFrame:
    """Grão usina x hora. Horas sem medição (gap longo) ficam com energia nula e
    flag 'faltante' — explícitas, em vez de sumirem ou serem inventadas."""
    sol_eol = ons.loc[ons["fonte"].isin(FONTES_CURATED),
                      ["chave_unidade", "data_hora_utc", "geracao_mwh", "fonte",
                       "id_subsistema", "flag_qualidade"]]
    fato = dst.mesclar_seguro(sol_eol, dim[["chave_unidade", "usina_id"]], on="chave_unidade",
                              como="inner", validar="many_to_one", verbose=VERBOSE_TOOLKIT)
    fato = fato.rename(columns={"data_hora_utc": "timestamp_utc", "geracao_mwh": "energia_mwh",
                                "id_subsistema": "regiao"})
    fato = fato[["usina_id", "timestamp_utc", "energia_mwh", "fonte", "regiao", "flag_qualidade"]]
    log.info("[curated:fato_geracao] %d linhas | %s", len(fato),
             fato["flag_qualidade"].value_counts().to_dict())
    return fato.sort_values(["usina_id", "timestamp_utc"])


def _ponto_clima_por_usina(dim: pd.DataFrame, locais: pd.DataFrame) -> pd.DataFrame:
    """Associa cada usina ao ponto NASA de referência:
    1) com coordenadas: o ponto mais próximo (haversine);
    2) sem coordenadas: ponto da mesma UF; senão, do mesmo subsistema;
    3) nenhum: fica sem clima.
    """
    linhas = []
    for u in dim.itertuples():
        if pd.notna(u.lat) and pd.notna(u.lon):
            dist = haversine_km(u.lat, u.lon, locais["latitude"].to_numpy(), locais["longitude"].to_numpy())
            i = int(np.argmin(dist))
            metodo = "mais_proximo" if dist[i] <= DISTANCIA_MAXIMA_CLIMA_KM else "mais_proximo_distante"
            linhas.append((u.usina_id, locais["local"].iat[i], float(dist[i]), metodo))
            continue
        mesma_uf = locais[locais["id_estado"] == u.id_estado]
        mesmo_sub = locais[locais["id_subsistema"] == u.regiao]
        if not mesma_uf.empty:
            linhas.append((u.usina_id, mesma_uf["local"].iat[0], np.nan, "mesma_uf"))
        elif not mesmo_sub.empty:
            linhas.append((u.usina_id, mesmo_sub["local"].iat[0], np.nan, "mesmo_subsistema"))
    return pd.DataFrame(linhas, columns=["usina_id", "local_clima", "distancia_km", "metodo_vinculo_clima"])


def fato_clima(nasa_diario: pd.DataFrame, dim: pd.DataFrame) -> pd.DataFrame:
    """Grão usina x dia, a partir da série DIÁRIA da NASA (dia em hora solar
    local ≈ dia de Brasília). A clima vem do ponto de coleta de referência, não
    da coordenada exata da usina: `local_clima`, `distancia_km` e
    `metodo_vinculo_clima` deixam a aproximação explícita."""
    locais = nasa_diario.drop_duplicates("local")[["local", "id_estado", "id_subsistema",
                                                    "latitude", "longitude"]]
    vinculo = _ponto_clima_por_usina(dim, locais)
    sem_clima = len(dim) - len(vinculo)
    if sem_clima:
        log.info("[curated:fato_clima] %d unidades sem ponto NASA de referência", sem_clima)

    diario = nasa_diario.rename(columns={
        "local": "local_clima",
        "irradiancia_kwh_m2_dia": "irradiancia_kwh_m2",
        "vento_50m_ms": "vento_ms",
        "temperatura_2m_c": "temperatura_c",
    })
    fato = dst.mesclar_seguro(vinculo, diario, on="local_clima", como="inner",
                              validar="many_to_many", verbose=VERBOSE_TOOLKIT)
    fato["data"] = fato["data"].dt.date
    fato = fato[["usina_id", "data", "irradiancia_kwh_m2", "vento_ms", "vento_10m_ms",
                 "temperatura_c", "temperatura_max_c", "temperatura_min_c", "flag_qualidade",
                 "local_clima", "distancia_km", "metodo_vinculo_clima"]]
    log.info("[curated:fato_clima] %d linhas | vínculo: %s", len(fato),
             vinculo["metodo_vinculo_clima"].value_counts().to_dict())
    return fato.sort_values(["usina_id", "data"])


def fato_manutencao(manutencao: pd.DataFrame, ponte: pd.DataFrame, dim: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por unidade da dim_usina com usinas ANEEL vinculadas.

    Os eventos simulados são por usina da ANEEL; a unidade (usina ou conjunto)
    herda o PRIMEIRO evento entre os seus membros:
    - início = data de operação mais antiga entre os membros;
    - evento = 1 se algum membro teve evento; tempo = 1º evento - início;
    - senão censurada; tempo = data de corte - início.
    """
    membros = dst.mesclar_seguro(
        manutencao, ponte[["ceg_aneel", "usina_id"]].rename(columns={"ceg_aneel": "ceg"}),
        on="ceg", como="inner", validar="one_to_one", verbose=VERBOSE_TOOLKIT,
    )
    membros = membros.sort_values("data_evento")
    fato = membros.groupby("usina_id").agg(
        inicio=("data_entrada_operacao", "min"),
        data_corte=("data_corte", "max"),
        data_primeiro_evento=("data_evento", "min"),
        tipo_evento=("tipo_evento", "first"),
        n_usinas_consideradas=("ceg", "size"),
    ).reset_index()
    fato["evento_ocorreu"] = fato["data_primeiro_evento"].notna()
    fim = fato["data_primeiro_evento"].where(fato["evento_ocorreu"], fato["data_corte"])
    fato["tempo_dias"] = np.maximum((fim - fato["inicio"]).dt.days, 1).astype("int64")
    fato["data_primeiro_evento"] = fato["data_primeiro_evento"].dt.date
    fato["simulado"] = True

    log.info("[curated:fato_manutencao] %d de %d unidades com dado simulado | eventos: %d",
             len(fato), len(dim), int(fato["evento_ocorreu"].sum()))
    return fato[["usina_id", "tempo_dias", "evento_ocorreu", "data_primeiro_evento",
                 "tipo_evento", "n_usinas_consideradas", "simulado"]]
