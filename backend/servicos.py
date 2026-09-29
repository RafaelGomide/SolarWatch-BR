"""Consultas ao banco. As rotas ficam finas; o SQL mora aqui."""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd
from fastapi import HTTPException, status

from backend.config import configuracao
from backend.paginacao import codificar, decodificar

CODIGO_VALIDACAO = 422  # ver backend/erros.py

COLUNAS_USINA = """usina_id, nome, fonte, regiao, id_estado, municipio, potencia_mw,
                   lat, lon, data_operacao, tipo_unidade, qualidade_vinculo"""


def _registros(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> lista de dicts com NaN/NaT convertidos em None.

    Sem isso, um `NaN` vira `NaN` no JSON (inválido) e um `NaT` quebra a
    validação do Pydantic: o banco tem colunas legitimamente nulas, como a
    potência de usinas sem vínculo confiável.
    """
    if df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _checar_janela(inicio: date | None, fim: date | None) -> None:
    maximo = configuracao().maximo_dias_serie
    if inicio and fim:
        if inicio > fim:
            raise HTTPException(CODIGO_VALIDACAO,
                                "inicio precisa ser anterior ou igual a fim")
        if (fim - inicio).days > maximo:
            raise HTTPException(
                CODIGO_VALIDACAO,
                f"janela de {(fim - inicio).days} dias excede o máximo de {maximo}")


def listar_usinas(cur: duckdb.DuckDBPyConnection, fonte: str | None, regiao: str | None,
                  limite: int, cursor: str | None) -> dict:
    ultimo_id = decodificar(cursor)
    filtros = ["usina_id > ?"]
    parametros: list = [ultimo_id]
    if fonte:
        filtros.append("fonte = ?")
        parametros.append(fonte)
    if regiao:
        filtros.append("regiao = ?")
        parametros.append(regiao)
    onde = " AND ".join(filtros)

    linhas = cur.execute(
        f"SELECT {COLUNAS_USINA} FROM dim_usina WHERE {onde} ORDER BY usina_id LIMIT ?",
        [*parametros, limite + 1],
    ).df()
    linhas = _registros(linhas)

    total = cur.execute(
        f"SELECT count(*) FROM dim_usina WHERE {onde.replace('usina_id > ?', 'TRUE')}",
        parametros[1:],
    ).fetchone()[0]

    tem_proxima = len(linhas) > limite
    linhas = linhas[:limite]
    proximo = codificar(int(linhas[-1]["usina_id"])) if tem_proxima and linhas else None
    return {"data": linhas, "next_cursor": proximo, "total_estimado": total}


def obter_usina(cur: duckdb.DuckDBPyConnection, usina_id: int) -> dict:
    linha = cur.execute(
        f"""SELECT {COLUNAS_USINA}, n_usinas_aneel, pico_geracao_mw,
                   primeira_medicao_utc, ultima_medicao_utc
            FROM dim_usina WHERE usina_id = ?""", [usina_id]).df()
    if linha.empty:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Nenhuma usina com id={usina_id}")
    return _registros(linha)[0]


def serie_geracao(cur: duckdb.DuckDBPyConnection, usina_id: int,
                  inicio: date | None, fim: date | None) -> dict:
    usina = obter_usina(cur, usina_id)
    _checar_janela(inicio, fim)

    condicoes, parametros = ["usina_id = ?"], [usina_id]
    if inicio:
        condicoes.append("timestamp_utc >= ?")
        parametros.append(datetime.combine(inicio, datetime.min.time()))
    if fim:
        condicoes.append("timestamp_utc < ? + INTERVAL 1 DAY")
        parametros.append(datetime.combine(fim, datetime.min.time()))

    pontos = cur.execute(
        f"""SELECT timestamp_utc AS timestamp, energia_mwh, flag_qualidade
            FROM fato_geracao WHERE {' AND '.join(condicoes)} ORDER BY timestamp_utc""",
        parametros).df()

    return {
        "usina_id": usina_id, "nome": usina["nome"], "fonte": usina["fonte"],
        "inicio": pontos["timestamp"].min() if not pontos.empty else None,
        "fim": pontos["timestamp"].max() if not pontos.empty else None,
        "total_mwh": float(pontos["energia_mwh"].sum()) if not pontos.empty else 0.0,
        "horas": len(pontos),
        "data": _registros(pontos),
    }


def geracao_nacional(cur: duckdb.DuckDBPyConnection, inicio: date | None, fim: date | None,
                     granularidade: str, fonte: str | None) -> dict:
    _checar_janela(inicio, fim)
    periodo = ("timestamp_utc" if granularidade == "hora"
               else "CAST(timestamp_utc AT TIME ZONE 'America/Sao_Paulo' AS DATE)")

    condicoes, parametros = ["TRUE"], []
    if inicio:
        condicoes.append("timestamp_utc >= ?")
        parametros.append(datetime.combine(inicio, datetime.min.time()))
    if fim:
        condicoes.append("timestamp_utc < ? + INTERVAL 1 DAY")
        parametros.append(datetime.combine(fim, datetime.min.time()))
    if fonte:
        condicoes.append("fonte = ?")
        parametros.append(fonte)

    dados = cur.execute(
        f"""SELECT {periodo} AS periodo, fonte, sum(energia_mwh) AS energia_mwh,
                   count(DISTINCT usina_id) AS usinas
            FROM fato_geracao WHERE {' AND '.join(condicoes)}
            GROUP BY 1, 2 ORDER BY 1, 2""", parametros).df()

    return {
        "granularidade": granularidade,
        "inicio": dados["periodo"].min() if not dados.empty else None,
        "fim": dados["periodo"].max() if not dados.empty else None,
        "data": _registros(dados),
    }


def serie_clima(cur: duckdb.DuckDBPyConnection, usina_id: int,
                inicio: date | None, fim: date | None) -> dict:
    obter_usina(cur, usina_id)
    _checar_janela(inicio, fim)

    condicoes, parametros = ["usina_id = ?"], [usina_id]
    if inicio:
        condicoes.append("data >= ?")
        parametros.append(inicio)
    if fim:
        condicoes.append("data <= ?")
        parametros.append(fim)

    dados = cur.execute(
        f"""SELECT data, irradiancia_kwh_m2, vento_ms, temperatura_c,
                   temperatura_max_c, temperatura_min_c, flag_qualidade, medidas_faltantes,
                   local_clima, distancia_km, metodo_vinculo_clima
            FROM fato_clima WHERE {' AND '.join(condicoes)} ORDER BY data""",
        parametros).df()

    if dados.empty:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Sem clima para a usina {usina_id} no período (o subsistema pode não ter ponto de coleta)")

    referencia = dados.iloc[0]
    return {
        "usina_id": usina_id,
        "local_clima": referencia["local_clima"],
        "distancia_km": (None if referencia["distancia_km"] != referencia["distancia_km"]
                         else float(referencia["distancia_km"])),
        "metodo_vinculo_clima": referencia["metodo_vinculo_clima"],
        "aviso": ("Clima do ponto NASA POWER de referência, não da coordenada exata da usina."),
        "data": _registros(dados.drop(columns=["local_clima", "distancia_km",
                                              "metodo_vinculo_clima"])),
    }


def participacao_na_fonte(cur: duckdb.DuckDBPyConnection, usina_id: int, fonte: str,
                          dias: int = 30) -> float:
    """Fração da geração da fonte atribuível à usina nos últimos `dias` observados."""
    linha = cur.execute("""
        WITH limite AS (SELECT max(timestamp_utc) - INTERVAL 1 DAY * ? AS corte FROM fato_geracao)
        SELECT
            sum(CASE WHEN usina_id = ? THEN energia_mwh ELSE 0 END) AS usina,
            sum(energia_mwh) AS total
        FROM fato_geracao, limite
        WHERE fonte = ? AND timestamp_utc >= limite.corte
    """, [dias, usina_id, fonte]).fetchone()
    total = linha[1] or 0
    return float(linha[0] / total) if total else 0.0
