"""Serviços que dependem dos modelos de ML.

Se o modelo necessário não carregou, cada função levanta 503 com Problem Details
— os endpoints de dado bruto continuam funcionando (system design §10.5).
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import numpy as np
import pandas as pd
from fastapi import HTTPException, status

from backend.modelos_ml import registro
from backend.servicos import obter_usina, participacao_na_fonte

log = logging.getLogger("backend.servicos_ml")

AVISO_SIMULADO = (
    "Os eventos de manutenção que treinaram este modelo são SINTÉTICOS "
    "(ML/analise_sobrevivencia/dados_simulados.py). O número não representa a "
    "confiabilidade real da usina."
)
PREMISSA_CLIMA = (
    "O clima do horizonte é aproximado pelo último dia observado: em produção "
    "entraria uma previsão meteorológica."
)
POTENCIA_REFERENCIA_MW = 30.0
ANO_REFERENCIA = 2018


def _indisponivel(recurso: str) -> HTTPException:
    motivo = registro.falhas.get(recurso, "modelo não carregado")
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"Modelo '{recurso}' indisponível ({motivo}). Os endpoints de dados históricos seguem ativos.",
    )


# ----------------------------------------------------------------- Previsão
def prever_fonte(fonte: str) -> dict:
    """Previsão das próximas 24 h da geração agregada de uma fonte."""
    if not registro.previsao_disponivel(fonte):
        raise _indisponivel(f"previsao_{fonte}")

    modelo = registro.previsao[fonte]
    historico = modelo.historico_
    colunas_clima = [c for c in ("irradiancia_kwh_m2", "vento_ms", "temperatura_c",
                                 "temperatura_max_c", "temperatura_min_c") if c in historico]

    # date_range a partir da última hora observada, descartando-a: evita
    # aritmética com Timedelta sobre índice com fuso
    grade = pd.date_range(historico.index.max(), periods=modelo.horizonte + 1, freq="h")[1:]
    futuro = pd.DataFrame(index=grade)
    for coluna in colunas_clima:   # proxy: repete o clima do último dia observado
        futuro[coluna] = historico[coluna].iloc[-modelo.horizonte:].to_numpy()

    previsto = modelo.prever(futuro)
    return {
        "fonte": fonte,
        "modelo": modelo.nome,
        "horizonte_h": modelo.horizonte,
        "origem": historico.index.max(),
        "metodo": "modelo_por_fonte",
        "premissa_clima": PREMISSA_CLIMA,
        "metricas_backtesting": registro.metadados.get(f"previsao_{fonte}", {}).get(
            "metricas_backtesting"),
        "data": [{"timestamp": t, "energia_mwh_prevista": float(v)} for t, v in previsto.items()],
    }


def prever_usina(cur: duckdb.DuckDBPyConnection, usina_id: int) -> dict:
    """Previsão por usina, por rateio da previsão da fonte.

    O modelo de séries temporais foi treinado na geração **agregada por fonte**
    (ver docs/ML/doc_tecnica_series_temporais.md). Para uma usina, a previsão da
    fonte é multiplicada pela participação histórica da usina nos últimos 30
    dias. É uma aproximação declarada em `metodo` e `participacao_usina`, não um
    modelo por usina.
    """
    usina = obter_usina(cur, usina_id)
    base = prever_fonte(usina["fonte"])
    participacao = participacao_na_fonte(cur, usina_id, usina["fonte"])

    return {
        **base,
        "usina_id": usina_id,
        "metodo": "rateio_proporcional",
        "participacao_usina": participacao,
        "data": [{"timestamp": ponto["timestamp"],
                  "energia_mwh_prevista": ponto["energia_mwh_prevista"] * participacao}
                 for ponto in base["data"]],
    }


# ------------------------------------------------------------ Sobrevivência
def _covariaveis(usina: dict) -> pd.DataFrame:
    data_operacao = pd.Timestamp(usina["data_operacao"])
    subsistema = usina["regiao"]
    return pd.DataFrame([{
        "fonte": usina["fonte"],
        "log_potencia_mw_c": float(np.log(usina["potencia_mw"]) - np.log(POTENCIA_REFERENCIA_MW)),
        "subsistema_NE": float(subsistema == "NE"),
        "subsistema_S": float(subsistema == "S"),
        "subsistema_N": float(subsistema == "N"),
        "ano_entrada_c": float(data_operacao.year - ANO_REFERENCIA),
        "idade_anos": float((pd.Timestamp.today().normalize() - data_operacao).days / 365.25),
    }])


def sobrevivencia(cur: duckdb.DuckDBPyConnection, usina_id: int,
                  horizontes_meses: list[int] | None = None) -> dict:
    """P(sem manutenção) nos horizontes pedidos, condicional à idade da usina."""
    usina = obter_usina(cur, usina_id)

    # Caminho rápido: tabela pré-calculada pelo treino, quando serve o pedido
    pre = registro.probabilidades
    if pre is not None and usina_id in pre.index and not horizontes_meses:
        linha = pre.loc[usina_id]
        horizontes = [
            {"horizonte_meses": int(coluna.split("_")[-1].rstrip("m")),
             "horizonte_dias": int(round(int(coluna.split("_")[-1].rstrip("m")) * 30.44)),
             "probabilidade_sobrevivencia": float(linha[coluna])}
            for coluna in pre.columns if coluna.startswith("p_sem_manutencao_")
        ]
        return {
            "usina_id": usina_id, "fonte": usina["fonte"],
            "idade_anos": float(linha["idade_anos"]),
            "condicional_na_idade": bool(linha["condicional_na_idade"]),
            "risco_relativo": float(linha["risco_relativo"]),
            "tempo_mediano_anos": float(linha["tempo_mediano_anos"]),
            "metodo_extrapolacao": str(linha["metodo_extrapolacao"]),
            "simulado": True, "aviso": AVISO_SIMULADO,
            "horizontes": sorted(horizontes, key=lambda h: h["horizonte_meses"]),
        }

    # Caminho completo: roda o modelo
    if registro.sobrevivencia is None:
        raise _indisponivel("sobrevivencia")
    if usina["potencia_mw"] is None or usina["data_operacao"] is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"A usina {usina_id} não tem potência ou data de operação confiáveis "
            "(vínculo ONS×ANEEL incompleto), então não é possível estimar a sobrevivência.")

    previsor = registro.sobrevivencia
    entrada = _covariaveis(usina)
    if horizontes_meses:
        previsor = type(previsor)(previsor.cox, previsor.weibull_por_fonte,
                                  previsor.limites_suporte, tuple(horizontes_meses),
                                  previsor.metadados)
    resultado = previsor.prever(entrada, condicional=True).iloc[0]

    return {
        "usina_id": usina_id, "fonte": usina["fonte"],
        "idade_anos": float(entrada["idade_anos"].iat[0]),
        "condicional_na_idade": True,
        "risco_relativo": float(resultado["risco_relativo"]),
        "tempo_mediano_anos": float(previsor.tempo_mediano_anos(entrada)),
        "metodo_extrapolacao": str(resultado["metodo_extrapolacao"]),
        "simulado": True, "aviso": AVISO_SIMULADO,
        "horizontes": [
            {"horizonte_meses": meses,
             "horizonte_dias": int(round(meses * 30.44)),
             "probabilidade_sobrevivencia": float(resultado[f"p_sem_manutencao_{meses}m"])}
            for meses in previsor.horizontes_meses
        ],
    }
