"""Pipeline completa do SolarWatch BR: (ingestão) -> raw -> clean -> curated.

    raw      dados/bruto/AAAA-MM-DD/           cópia imutável do dump da ingestão
    clean    dados/limpos/clean/*.parquet      tipos, UTC, duplicatas, nomes, gaps
    curated  dados/limpos/curated/*.parquet    dim_usina + fatos (vai para o DuckDB)

Uso (a partir da raiz do repositório):
    python -m ETL.pipeline                        # raw + clean + curated com o que já foi ingerido
    python -m ETL.pipeline --ingerir              # roda `python -m ingestao` antes (ONS, ANEEL, NASA)
    python -m ETL.pipeline --ingerir --simular    # ... e regenera os eventos simulados
    python -m ETL.pipeline --etapas clean curated # pula a etapa raw
    python -m ETL.pipeline --data-coleta 2026-09-18  # usa uma partição raw específica

Quando um dado novo chega, basta rodar com --ingerir: a raw arquiva a nova
coleta em uma partição datada, o clean lê a partição mais recente e a curated
preserva os usina_id já publicados.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import date

import pandas as pd

from ETL import raw, validacao
from ETL.clean import aneel, manutencao, nasa, ons
from ETL.config import ARQUIVO_SIMULADO, CLEAN, CURATED, RAIZ
from ETL.curated import dim_usina, fatos
from ETL.utils import gravar

log = logging.getLogger("ETL.pipeline")

# A ordem das fontes (e a dependência ANEEL -> locais.csv -> NASA) mora no
# orquestrador da ingestão, não aqui: `python -m ingestao` é a única definição.
ORQUESTRADOR_INGESTAO = "ingestao"
SIMULACAO = "ML.analise_sobrevivencia.dados_simulados"
ETAPAS = ("raw", "clean", "curated")


def _rodar_modulo(modulo: str) -> None:
    log.info("[pipeline] executando %s", modulo)
    subprocess.run([sys.executable, "-m", modulo], cwd=RAIZ, check=True)


def etapa_clean(data_coleta: date | None) -> dict[str, pd.DataFrame]:
    tabelas = {
        "ons_geracao": ons.limpar(raw.localizar("ons", data_coleta)),
        "nasa_clima_horario": nasa.limpar(raw.localizar("nasa", data_coleta)),
        "nasa_clima_diario": nasa.limpar_diario(raw.localizar("nasa_diario", data_coleta)),
        "aneel_usinas": aneel.limpar(raw.localizar("aneel", data_coleta)),
        "manutencao_simulada": manutencao.limpar(ARQUIVO_SIMULADO),
    }
    for nome, df in tabelas.items():
        gravar(df, CLEAN, nome)
    return tabelas


def _ler_clean() -> dict[str, pd.DataFrame]:
    nomes = ["ons_geracao", "nasa_clima_diario", "aneel_usinas", "manutencao_simulada"]
    faltando = [n for n in nomes if not (CLEAN / f"{n}.parquet").exists()]
    if faltando:
        raise FileNotFoundError(f"Camada clean incompleta ({faltando}); rode a etapa clean.")
    return {n: pd.read_parquet(CLEAN / f"{n}.parquet") for n in nomes}


def etapa_curated(clean: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    dim, ponte = dim_usina.construir(clean["ons_geracao"], clean["aneel_usinas"])
    tabelas = {
        "dim_usina": dim,
        "fato_geracao": fatos.fato_geracao(clean["ons_geracao"], dim),
        "fato_clima": fatos.fato_clima(clean["nasa_clima_diario"], dim),
        "fato_manutencao": fatos.fato_manutencao(clean["manutencao_simulada"], ponte, dim),
    }
    validacao.validar(tabelas)  # só grava se tudo passar
    for nome, df in {**tabelas, "ponte_usina_aneel": ponte}.items():
        gravar(df, CURATED, nome)
    return tabelas


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ingerir", action="store_true", help="roda as 3 ingestões antes")
    parser.add_argument("--simular", action="store_true", help="regenera os eventos simulados")
    parser.add_argument("--etapas", nargs="+", choices=ETAPAS, default=list(ETAPAS))
    parser.add_argument("--data-coleta", type=date.fromisoformat, default=None,
                        help="partição raw AAAA-MM-DD (padrão: a mais recente de cada fonte)")
    args = parser.parse_args()

    inicio = time.perf_counter()
    if args.ingerir:
        _rodar_modulo(ORQUESTRADOR_INGESTAO)
    if args.simular:
        _rodar_modulo(SIMULACAO)

    clean = None
    if "raw" in args.etapas:
        raw.particionar()
    if "clean" in args.etapas:
        clean = etapa_clean(args.data_coleta)
    if "curated" in args.etapas:
        etapa_curated(clean if clean is not None else _ler_clean())

    log.info("[pipeline] concluída em %.1f s", time.perf_counter() - inicio)


if __name__ == "__main__":
    main()
