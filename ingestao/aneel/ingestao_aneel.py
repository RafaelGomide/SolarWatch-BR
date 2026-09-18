"""Ingestão da ANEEL — SIGA (Sistema de Informações de Geração da ANEEL).

Consulta a API pública CKAN do portal de dados abertos da ANEEL (endpoint
`datastore_search`) e baixa o cadastro de empreendimentos de geração solar
fotovoltaica (UFV) e eólica (EOL): nome, CEG, UF, município, potência
outorgada/fiscalizada, fase da usina, data de entrada em operação,
coordenadas etc. O resultado é gravado, sem transformação, em
`dados/bruto/dados_aneel_bruto.csv`.

Uso:
    python -m ingestao.aneel.ingestao_aneel
    python -m ingestao.aneel.ingestao_aneel --tipos UFV EOL UHE
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

from ingestao.http import get_com_retry, nova_sessao

API_URL = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search"
# Recurso "siga-empreendimentos-geracao-diario.csv" (atualizado diariamente)
RESOURCE_ID = "2f65a1b0-19b8-4360-8238-b34ab4693d55"
TIPOS_PADRAO = ["UFV", "EOL"]  # UFV = solar fotovoltaica, EOL = eólica
TAMANHO_PAGINA = 5000
PAUSA_ENTRE_CHAMADAS_S = 0.5  # cortesia com a API pública

RAIZ = Path(__file__).resolve().parents[2]
SAIDA = RAIZ / "dados" / "bruto" / "dados_aneel_bruto.csv"

log = logging.getLogger("ingestao_aneel")


def _pagina(sessao: requests.Session, tipos: list[str], offset: int) -> dict:
    resp = get_com_retry(
        sessao,
        API_URL,
        params={
            "resource_id": RESOURCE_ID,
            "filters": json.dumps({"SigTipoGeracao": tipos}),
            "limit": TAMANHO_PAGINA,
            "offset": offset,
            # Ordenação estável: sem ela, a paginação por offset pode repetir/pular linhas
            "sort": "_id asc",
        },
    )
    resp.raise_for_status()
    corpo = resp.json()
    if not corpo.get("success"):
        raise RuntimeError(f"API da ANEEL retornou erro: {corpo.get('error')}")
    return corpo["result"]


def baixar(tipos: list[str], saida: Path = SAIDA) -> Path:
    sessao = nova_sessao()

    registros: list[dict] = []
    colunas: list[str] = []
    total = None
    offset = 0
    while total is None or offset < total:
        resultado = _pagina(sessao, tipos, offset)
        if total is None:
            total = resultado["total"]
            colunas = [campo["id"] for campo in resultado["fields"]]
            log.info("%d empreendimentos dos tipos %s", total, ", ".join(tipos))
        elif resultado["total"] != total:
            # O recurso é republicado diariamente; se mudar no meio, o retrato fica inconsistente
            raise RuntimeError(
                f"Total mudou durante a paginação ({total} -> {resultado['total']}); rode de novo."
            )
        lote = resultado["records"]
        if not lote:
            break
        registros.extend(lote)
        offset += len(lote)
        log.info("  %d/%d", len(registros), total)
        time.sleep(PAUSA_ENTRE_CHAMADAS_S)

    bruto = pd.DataFrame(registros, columns=colunas).drop_duplicates(subset="_id")
    if len(bruto) != total:
        raise RuntimeError(f"Esperados {total} registros, recebidos {len(bruto)}.")

    saida.parent.mkdir(parents=True, exist_ok=True)
    # Build-then-swap: escreve em arquivo temporário e substitui atomicamente
    temporario = saida.with_suffix(".csv.tmp")
    bruto.to_csv(temporario, index=False, encoding="utf-8")
    os.replace(temporario, saida)
    log.info("Gravado %s (%d linhas)", saida, len(bruto))
    return saida


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tipos",
        nargs="+",
        default=TIPOS_PADRAO,
        help="Siglas SigTipoGeracao (padrão: UFV EOL)",
    )
    args = parser.parse_args()

    baixar([tipo.upper() for tipo in args.tipos])


if __name__ == "__main__":
    main()
