"""Ingestão do ONS Dados Abertos — Geração por Usina em Base Horária.

Baixa os arquivos Parquet mensais do dataset `geracao-usina-2` (geração horária por usina,
com subsistema, estado e tipo de fonte: eólica, fotovoltaica, hidráulica,
térmica, nuclear) e grava **um Parquet por mês**, sem transformação, na pasta
`dados/bruto/dados_ons_bruto/`.

Um arquivo por mês (e não um consolidado único) evita manter todos os meses em
memória ao mesmo tempo, torna a ingestão incremental — rodar de novo só baixa e
regrava os meses pedidos — e a pasta é lida como um só dataset pelo glob
`dados_ons_bruto/*.parquet`.

O ONS publica o mesmo dado em CSV (~70 MB/mês) e Parquet (~4,6 MB/mês); o
Parquet é usado por ser ~15x menor e já vir tipado pela própria fonte
(`din_instante` timestamp, `val_geracao` double, demais colunas texto).

Uso:
    python -m ingestao.ONS.ingestao_ons --inicio 2026-06 --fim 2026-08

Sem argumentos, lê ONS_MES_INICIO / ONS_MES_FIM do `.env`; na ausência deles,
baixa os últimos 3 meses (incluindo o atual).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from ingestao.armazenamento import gravar_parquet
from ingestao.http import get_com_retry, nova_sessao

CKAN_URL = "https://dados.ons.org.br/api/3/action/package_show"
DATASET_ID = "geracao-usina-2"
URL_ARQUIVO = (
    "https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/"
    "geracao_usina_2_ho/GERACAO_USINA-2_{ano}_{mes:02d}.parquet"
)
PADRAO_MENSAL = re.compile(r"GERACAO_USINA-2_(\d{4})_(\d{2})\.parquet$")

RAIZ = Path(__file__).resolve().parents[2]
SAIDA = RAIZ / "dados" / "bruto" / "dados_ons_bruto"
NOME_ARQUIVO = "dados_ons_bruto_{ano}_{mes:02d}.parquet"

log = logging.getLogger("ingestao_ons")


def _meses(inicio: date, fim: date) -> list[tuple[int, int]]:
    meses, ano, mes = [], inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        meses.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


def _urls_disponiveis(sessao: requests.Session) -> dict[tuple[int, int], str]:
    """Lista os Parquets publicados via API CKAN do portal; cai no padrão de URL se falhar."""
    try:
        resp = get_com_retry(sessao, CKAN_URL, params={"id": DATASET_ID})
        recursos = resp.json()["result"]["resources"]
    except (requests.RequestException, KeyError, ValueError) as erro:
        log.warning("API CKAN indisponível (%s); usando padrão de URL do S3", erro)
        return {}

    urls = {}
    for recurso in recursos:
        url = recurso.get("url", "")
        # Arquivos antigos são anuais; só os mensais seguem este padrão
        if casamento := PADRAO_MENSAL.search(url):
            urls[(int(casamento[1]), int(casamento[2]))] = url
    return urls


def _parse_mes(valor: str) -> date:
    return date.fromisoformat(f"{valor}-01")


def baixar(inicio: date, fim: date, saida: Path = SAIDA) -> Path:
    """Baixa cada mês do período e grava um Parquet por mês em `saida`.

    Cada arquivo é gravado assim que é baixado: só um mês fica em memória por
    vez, e uma queda no meio do período preserva os meses já gravados.
    """
    sessao = nova_sessao()
    publicados = _urls_disponiveis(sessao)

    gravados = []
    for ano, mes in _meses(inicio, fim):
        url = publicados.get((ano, mes), URL_ARQUIVO.format(ano=ano, mes=mes))
        log.info("Baixando %04d-%02d: %s", ano, mes, url)
        resp = get_com_retry(sessao, url)
        if resp.status_code == 404:
            log.warning("%04d-%02d ainda não publicado, pulando", ano, mes)
            continue
        # Tipos como publicados pelo ONS; nenhuma conversão adicional
        df = pd.read_parquet(BytesIO(resp.content), engine="pyarrow")
        df["arquivo_origem"] = url.rsplit("/", 1)[-1]
        gravados.append(gravar_parquet(df, saida / NOME_ARQUIVO.format(ano=ano, mes=mes)))

    if not gravados:
        raise SystemExit("Nenhum arquivo baixado para o período informado.")

    log.info("%d mês(es) em %s", len(gravados), saida)
    return saida


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(RAIZ / ".env")

    hoje = date.today().replace(day=1)
    padrao_inicio = _meses(date(hoje.year - 1, hoje.month, 1), hoje)[-3]

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inicio", default=os.getenv("ONS_MES_INICIO") or None, help="AAAA-MM")
    parser.add_argument("--fim", default=os.getenv("ONS_MES_FIM") or None, help="AAAA-MM")
    args = parser.parse_args()

    inicio = _parse_mes(args.inicio) if args.inicio else date(*padrao_inicio, 1)
    fim = _parse_mes(args.fim) if args.fim else hoje
    if inicio > fim:
        parser.error("--inicio deve ser anterior ou igual a --fim")

    baixar(inicio, fim)


if __name__ == "__main__":
    main()
