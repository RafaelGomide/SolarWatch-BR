"""Acesso ao banco DuckDB (read-only).

O banco é estático em runtime (system design §2.4, §9): a API só lê. Uma única
conexão read-only é aberta no startup e cada request usa um `cursor()` próprio —
é assim que o DuckDB oferece acesso concorrente seguro a partir de um mesmo
arquivo, sem pagar a abertura repetida.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger("backend.db")

_conexao: duckdb.DuckDBPyConnection | None = None


def abrir(caminho: Path) -> duckdb.DuckDBPyConnection:
    global _conexao
    if not caminho.exists():
        raise FileNotFoundError(
            f"Banco {caminho} não encontrado. Rode: python -m ETL.pipeline && python -m DB.criar_banco"
        )
    _conexao = duckdb.connect(str(caminho), read_only=True)
    tabelas = _conexao.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
    usinas = _conexao.execute("SELECT count(*) FROM dim_usina").fetchone()[0]
    log.info("[db] %s aberto (read-only): %d tabelas, %d usinas", caminho.name, tabelas, usinas)
    return _conexao


def fechar() -> None:
    global _conexao
    if _conexao is not None:
        _conexao.close()
        _conexao = None
        log.info("[db] conexão fechada")


def conexao() -> duckdb.DuckDBPyConnection:
    """Dependência FastAPI: um cursor isolado por request."""
    if _conexao is None:
        raise RuntimeError("Banco não inicializado (lifespan não executou?).")
    return _conexao.cursor()


def esta_disponivel() -> bool:
    return _conexao is not None


def cobertura() -> dict:
    """Período e volume cobertos pelo banco — usado em /health e /info."""
    cur = conexao()
    geracao = cur.execute(
        "SELECT min(timestamp_utc), max(timestamp_utc), count(*) FROM fato_geracao").fetchone()
    clima = cur.execute("SELECT min(data), max(data), count(*) FROM fato_clima").fetchone()
    usinas = cur.execute("SELECT count(*) FROM dim_usina").fetchone()[0]
    return {
        "usinas": usinas,
        "geracao": {"inicio": geracao[0], "fim": geracao[1], "linhas": geracao[2]},
        "clima": {"inicio": clima[0], "fim": clima[1], "linhas": clima[2]},
    }
