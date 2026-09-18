"""Ingestão do ONS Dados Abertos — Geração por Usina em Base Horária.

Baixa os CSVs mensais do dataset `geracao-usina-2` (geração horária por usina,
com subsistema, estado e tipo de fonte: eólica, fotovoltaica, hidráulica,
térmica, nuclear) e consolida tudo, sem transformação, em
`dados/bruto/dados_ons_bruto.csv`.

Uso:
    python -m ingestao.ONS.ingestao_ons --inicio 2026-06 --fim 2026-08

Sem argumentos, lê ONS_MES_INICIO / ONS_MES_FIM do `.env`; na ausência deles,
baixa os últimos 3 meses (incluindo o atual).
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import time
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

CKAN_URL = "https://dados.ons.org.br/api/3/action/package_show"
DATASET_ID = "geracao-usina-2"
URL_ARQUIVO = (
    "https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/"
    "geracao_usina_2_ho/GERACAO_USINA-2_{ano}_{mes:02d}.csv"
)
PADRAO_MENSAL = re.compile(r"GERACAO_USINA-2_(\d{4})_(\d{2})\.csv$")

RAIZ = Path(__file__).resolve().parents[2]
SAIDA = RAIZ / "dados" / "bruto" / "dados_ons_bruto.csv"

MAX_TENTATIVAS = 5
TIMEOUT_S = 120

log = logging.getLogger("ingestao_ons")


def _get_com_retry(sessao: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET com exponential backoff + jitter (GET é idempotente, retry é seguro)."""
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = sessao.get(url, timeout=TIMEOUT_S, **kwargs)
            if resp.status_code == 404:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as erro:
            if tentativa == MAX_TENTATIVAS:
                raise
            espera = min(2**tentativa, 60) + random.uniform(0, 1)
            log.warning("Falha em %s (%s). Nova tentativa em %.1fs", url, erro, espera)
            time.sleep(espera)
    raise RuntimeError("inalcançável")


def _meses(inicio: date, fim: date) -> list[tuple[int, int]]:
    meses, ano, mes = [], inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        meses.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


def _urls_disponiveis(sessao: requests.Session) -> dict[tuple[int, int], str]:
    """Lista os CSVs publicados via API CKAN do portal; cai no padrão de URL se falhar."""
    try:
        resp = _get_com_retry(sessao, CKAN_URL, params={"id": DATASET_ID})
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
    sessao = requests.Session()
    sessao.headers["User-Agent"] = "SolarWatch-BR/ingestao (dados abertos ONS)"
    publicados = _urls_disponiveis(sessao)

    frames = []
    for ano, mes in _meses(inicio, fim):
        url = publicados.get((ano, mes), URL_ARQUIVO.format(ano=ano, mes=mes))
        log.info("Baixando %04d-%02d: %s", ano, mes, url)
        resp = _get_com_retry(sessao, url)
        if resp.status_code == 404:
            log.warning("%04d-%02d ainda não publicado, pulando", ano, mes)
            continue
        # Mantém tudo como texto: camada bruta não interpreta tipos
        df = pd.read_csv(BytesIO(resp.content), sep=";", dtype=str, keep_default_na=False)
        df["arquivo_origem"] = url.rsplit("/", 1)[-1]
        frames.append(df)
        log.info("  %d linhas", len(df))

    if not frames:
        raise SystemExit("Nenhum arquivo baixado para o período informado.")

    bruto = pd.concat(frames, ignore_index=True)
    saida.parent.mkdir(parents=True, exist_ok=True)
    # Build-then-swap: escreve em arquivo temporário e substitui atomicamente
    temporario = saida.with_suffix(".csv.tmp")
    bruto.to_csv(temporario, index=False, encoding="utf-8")
    os.replace(temporario, saida)
    log.info("Gravado %s (%d linhas)", saida, len(bruto))
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
