"""Endpoints agregados de geração (visão nacional) e previsão por fonte."""

from __future__ import annotations

from datetime import date
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, Query

from backend import servicos, servicos_ml
from backend.db import conexao
from backend.schemas import GeracaoNacional, Previsao

roteador = APIRouter(prefix="/geracao", tags=["geracao"])


@roteador.get("/nacional", response_model=GeracaoNacional,
              summary="Geração agregada do SIN por fonte")
def nacional(
    inicio: date | None = Query(None),
    fim: date | None = Query(None),
    granularidade: Literal["hora", "dia"] = Query("dia"),
    fonte: str | None = Query(None, pattern="^(solar|eolica)$"),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    """Com `granularidade=dia`, o dia é fechado em horário de Brasília."""
    return servicos.geracao_nacional(cur, inicio, fim, granularidade, fonte)


@roteador.get("/previsao", response_model=Previsao,
              summary="Previsão de 24 h da geração agregada de uma fonte")
def previsao(fonte: Literal["solar", "eolica"] = Query(...)):
    """Esta é a previsão que o modelo realmente produz: agregada por fonte.
    A versão por usina (`/usinas/{id}/previsao`) é um rateio desta."""
    return servicos_ml.prever_fonte(fonte)
