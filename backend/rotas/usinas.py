"""Endpoints de usina: cadastro, geração, clima, previsão e sobrevivência."""

from __future__ import annotations

from datetime import date

import duckdb
from fastapi import APIRouter, Depends, Path, Query

from backend import servicos, servicos_ml
from backend.config import configuracao
from backend.db import conexao
from backend.schemas import (PaginaUsinas, Previsao, Recorrencia, SerieClima, SerieGeracao, Sobrevivencia,
                             UsinaDetalhe)

roteador = APIRouter(prefix="/usinas", tags=["usinas"])

ID_USINA = Path(description="Identificador da usina na dim_usina", examples=[12])


@roteador.get("", response_model=PaginaUsinas, summary="Lista usinas cadastradas")
def listar(
    fonte: str | None = Query(None, pattern="^(solar|eolica)$"),
    regiao: str | None = Query(None, pattern="^(N|NE|SE|S)$", description="Subsistema do SIN"),
    tipo_unidade: str | None = Query(
        None, pattern="^(usina|conjunto|pequenas_usinas)$",
        description="Grão da unidade. 'pequenas_usinas' são agregados estaduais de MMGD, "
                    "sem cadastro na ANEEL: filtre por 'usina' ou 'conjunto' para obter só "
                    "as unidades com potência, coordenada e data de operação"),
    limit: int = Query(None, ge=1, le=configuracao().limite_maximo_pagina),
    cursor: str | None = Query(None, description="Cursor devolvido em next_cursor"),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    """Paginação por cursor (§5.5), ordenada por `usina_id`.

    A lista mistura três grãos de medição do ONS; `tipo_unidade` separa os que
    têm cadastro ('usina', 'conjunto') dos agregados estaduais
    ('pequenas_usinas'), que não têm por natureza.
    """
    return servicos.listar_usinas(cur, fonte, regiao,
                                  limit or configuracao().limite_padrao_pagina, cursor,
                                  tipo_unidade)


@roteador.get("/{usina_id}", response_model=UsinaDetalhe, summary="Detalhe de uma usina")
def detalhar(usina_id: int = ID_USINA, cur: duckdb.DuckDBPyConnection = Depends(conexao)):
    return servicos.obter_usina(cur, usina_id)


@roteador.get("/{usina_id}/geracao", response_model=SerieGeracao,
              summary="Série histórica de geração (usina × hora)")
def geracao(
    usina_id: int = ID_USINA,
    inicio: date | None = Query(None, description="Data inicial (inclusive)"),
    fim: date | None = Query(None, description="Data final (inclusive)"),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    """Horas sem medição aparecem com `energia_mwh` nulo e `flag_qualidade='faltante'`."""
    return servicos.serie_geracao(cur, usina_id, inicio, fim)


@roteador.get("/{usina_id}/clima", response_model=SerieClima,
              summary="Série de clima do ponto de referência (usina × dia)")
def clima(
    usina_id: int = ID_USINA,
    inicio: date | None = Query(None),
    fim: date | None = Query(None),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    return servicos.serie_clima(cur, usina_id, inicio, fim)


@roteador.get("/{usina_id}/previsao", response_model=Previsao,
              summary="Previsão de geração das próximas 24 h")
def previsao(usina_id: int = ID_USINA, cur: duckdb.DuckDBPyConnection = Depends(conexao)):
    """Rateio da previsão da fonte pela participação histórica da usina —
    o campo `metodo` deixa a aproximação explícita."""
    return servicos_ml.prever_usina(cur, usina_id)


@roteador.get("/{usina_id}/sobrevivencia", response_model=Sobrevivencia,
              summary="Probabilidade de operar sem manutenção corretiva (DADO SIMULADO)")
def sobrevivencia(
    usina_id: int = ID_USINA,
    horizontes: list[int] | None = Query(None, description="Horizontes em meses (padrão: 6 12 24 36)"),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    return servicos_ml.sobrevivencia(cur, usina_id, horizontes)


@roteador.get("/{usina_id}/recorrencia", response_model=Recorrencia,
              summary="Número esperado de manutenções corretivas (DADO SIMULADO)")
def recorrencia(
    usina_id: int = ID_USINA,
    horizontes: list[int] | None = Query(None, description="Horizontes em meses (padrão: 6 12 24 36)"),
    cur: duckdb.DuckDBPyConnection = Depends(conexao),
):
    """Enquanto `/sobrevivencia` responde "chega ao fim do horizonte sem nenhuma
    manutenção?", este responde **quantas** esperar — a pergunta que o modelo de
    1º evento não consegue responder."""
    return servicos_ml.recorrencia(cur, usina_id, horizontes)
