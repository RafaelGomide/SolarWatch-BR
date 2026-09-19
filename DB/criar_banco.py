"""Cria o banco estático DuckDB do SolarWatch BR a partir da camada curated.

    dados/limpos/curated/*.parquet  ──►  DB/solarwatch.duckdb

O banco é read-only em produção (system design §2.4): a API só lê. Cada execução
reconstrói o arquivo **do zero** em um caminho temporário e só então o substitui
(build-then-swap, §9/§13.3), então a API nunca enxerga um banco pela metade.

O que é criado:
- 4 tabelas do modelo estrela + a ponte de auditoria, com PK, NOT NULL e FK;
- índices declarados em `esquema.INDICES`;
- views de conveniência (`esquema.VIEWS`) para os endpoints mais comuns.

Uso:
    python -m DB.criar_banco
    python -m DB.criar_banco --saida /outro/caminho.duckdb
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import duckdb

from DB.esquema import (BANCO, CURATED, INDICES, TABELAS, VIEWS, ddl_tabela,
                        select_de_parquet)

log = logging.getLogger("DB.criar_banco")


def _conferir_origens() -> None:
    faltando = [t["origem"] for t in TABELAS.values() if not (CURATED / t["origem"]).exists()]
    if faltando:
        raise FileNotFoundError(
            f"Arquivos da camada curated não encontrados: {faltando}. Rode: python -m ETL.pipeline"
        )


def criar(saida: Path = BANCO) -> Path:
    _conferir_origens()
    saida.parent.mkdir(parents=True, exist_ok=True)
    temporario = saida.with_suffix(".duckdb.tmp")
    temporario.unlink(missing_ok=True)

    con = duckdb.connect(str(temporario))
    try:
        # Ordem importa: a dimensão precisa existir antes das FKs das fatos
        for nome, tabela in TABELAS.items():
            con.execute(ddl_tabela(nome))
            con.execute(f"INSERT INTO {nome} {select_de_parquet(nome, CURATED / tabela['origem'])}")
            linhas = con.execute(f"SELECT count(*) FROM {nome}").fetchone()[0]
            log.info("[db] %-18s %8d linhas  (%s)", nome, linhas, tabela["grao"])

        for indice, (tabela, colunas) in INDICES.items():
            con.execute(f"CREATE INDEX {indice} ON {tabela}({', '.join(colunas)})")
        log.info("[db] %d índices criados", len(INDICES))

        for view, sql in VIEWS.items():
            con.execute(f"CREATE VIEW {view} AS {sql}")
        log.info("[db] views: %s", ", ".join(VIEWS))

        con.execute("CHECKPOINT")
    finally:
        con.close()

    os.replace(temporario, saida)  # troca atômica
    log.info("[db] gravado %s (%.1f MB)", saida, saida.stat().st_size / 1024**2)
    return saida


def resumo(banco: Path) -> None:
    """Consultas de fumaça: o banco responde ao que a API vai perguntar?"""
    con = duckdb.connect(str(banco), read_only=True)
    try:
        log.info("[db] usinas por fonte: %s",
                 con.execute("SELECT fonte, count(*) FROM dim_usina GROUP BY 1 ORDER BY 1").fetchall())
        log.info("[db] período da geração: %s",
                 con.execute("SELECT min(timestamp_utc), max(timestamp_utc) FROM fato_geracao").fetchone())
        log.info("[db] geração total por fonte (GWh): %s",
                 con.execute("SELECT fonte, round(sum(energia_mwh)/1000, 1) FROM fato_geracao "
                             "GROUP BY 1 ORDER BY 1").fetchall())
        log.info("[db] fator de capacidade médio (vw): %s",
                 con.execute("SELECT fonte, round(avg(fator_capacidade), 3) "
                             "FROM vw_fator_capacidade_diario GROUP BY 1 ORDER BY 1").fetchall())
        log.info("[db] manutenção — eventos/censuras: %s",
                 con.execute("SELECT evento_ocorreu, count(*) FROM fato_manutencao "
                             "GROUP BY 1 ORDER BY 1").fetchall())
        log.info("[db] exemplo /usinas/{id}/geracao: %s",
                 con.execute("SELECT count(*), round(sum(energia_mwh), 1) FROM fato_geracao "
                             "WHERE usina_id = (SELECT min(usina_id) FROM fato_geracao) "
                             "AND timestamp_utc >= now() - INTERVAL 7 DAY").fetchone())
    finally:
        con.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--saida", type=Path, default=BANCO)
    args = parser.parse_args()

    banco = criar(args.saida)
    resumo(banco)


if __name__ == "__main__":
    main()
