"""Ingestão da NASA POWER API — clima horário por coordenada.

Para cada local de `locais.csv` (polos de geração solar/eólica, com estado e
subsistema para cruzar com os dados do ONS), baixa a série horária de:

- ALLSKY_SFC_SW_DWN: irradiância global horizontal na superfície (Wh/m²)
- WS10M / WS50M: velocidade do vento a 10 m e 50 m (m/s)
- T2M: temperatura do ar a 2 m (°C)

e consolida tudo, sem transformação, em `dados/bruto/dados_nasa_bruto.csv`.
Horários em UTC (o ONS publica em horário de Brasília; o alinhamento é feito
na etapa de transformação). Valores ausentes vêm como -999 (fill value da API).

Uso:
    python -m ingestao.nasa_power.ingestao_nasa_power --inicio 2026-06 --fim 2026-08

Sem argumentos, lê NASA_MES_INICIO / NASA_MES_FIM do `.env` (ou, na ausência,
ONS_MES_INICIO / ONS_MES_FIM, para manter os dois períodos alinhados); sem
nenhum deles, baixa os últimos 3 meses (incluindo o atual).
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from ingestao.http import get_com_retry, nova_sessao

API_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
PARAMETROS = ["ALLSKY_SFC_SW_DWN", "WS10M", "WS50M", "T2M"]
COLUNAS_LOCAL = ["local", "municipio", "id_estado", "id_subsistema", "latitude", "longitude"]
PAUSA_ENTRE_CHAMADAS_S = 1.0  # cortesia com a API pública

RAIZ = Path(__file__).resolve().parents[2]
LOCAIS = Path(__file__).resolve().parent / "locais.csv"
SAIDA = RAIZ / "dados" / "bruto" / "dados_nasa_bruto.csv"

log = logging.getLogger("ingestao_nasa_power")


def _janelas_anuais(inicio: date, fim: date) -> list[tuple[date, date]]:
    """Quebra o período em janelas de no máximo um ano civil por requisição."""
    janelas = []
    atual = inicio
    while atual <= fim:
        fim_janela = min(date(atual.year, 12, 31), fim)
        janelas.append((atual, fim_janela))
        atual = date(atual.year + 1, 1, 1)
    return janelas


def _baixar_local(
    sessao: requests.Session, local: pd.Series, inicio: date, fim: date
) -> pd.DataFrame:
    frames = []
    for ini_janela, fim_janela in _janelas_anuais(inicio, fim):
        resp = get_com_retry(
            sessao,
            API_URL,
            params={
                "parameters": ",".join(PARAMETROS),
                "community": "RE",
                "latitude": local.latitude,
                "longitude": local.longitude,
                "start": ini_janela.strftime("%Y%m%d"),
                "end": fim_janela.strftime("%Y%m%d"),
                "format": "JSON",
                "time-standard": "UTC",
            },
        )
        resp.raise_for_status()
        serie = resp.json()["properties"]["parameter"]
        # {"T2M": {"2026091000": 21.3, ...}, ...} -> uma linha por hora
        df = pd.DataFrame(serie).rename_axis("data_hora_utc").reset_index()
        frames.append(df)
        time.sleep(PAUSA_ENTRE_CHAMADAS_S)

    df = pd.concat(frames, ignore_index=True)
    # Identificação do local primeiro, depois hora e parâmetros
    return df.assign(**{coluna: local[coluna] for coluna in COLUNAS_LOCAL})[
        COLUNAS_LOCAL + ["data_hora_utc"] + PARAMETROS
    ]


def baixar(inicio: date, fim: date, saida: Path = SAIDA) -> Path:
    locais = pd.read_csv(LOCAIS, dtype={"latitude": float, "longitude": float})
    sessao = nova_sessao()

    frames = []
    for _, local in locais.iterrows():
        log.info("Baixando %s (%.2f, %.2f) de %s a %s",
                 local.local, local.latitude, local.longitude, inicio, fim)
        df = _baixar_local(sessao, local, inicio, fim)
        frames.append(df)
        log.info("  %d linhas", len(df))

    bruto = pd.concat(frames, ignore_index=True)
    saida.parent.mkdir(parents=True, exist_ok=True)
    # Build-then-swap: escreve em arquivo temporário e substitui atomicamente
    temporario = saida.with_suffix(".csv.tmp")
    bruto.to_csv(temporario, index=False, encoding="utf-8")
    os.replace(temporario, saida)
    log.info("Gravado %s (%d linhas)", saida, len(bruto))
    return saida


def _mes(valor: str) -> date:
    return date.fromisoformat(f"{valor}-01")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(RAIZ / ".env")

    hoje = date.today()
    ano, mes = (hoje.year, hoje.month - 2) if hoje.month > 2 else (hoje.year - 1, hoje.month + 10)
    padrao_inicio = date(ano, mes, 1)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--inicio",
        default=os.getenv("NASA_MES_INICIO") or os.getenv("ONS_MES_INICIO") or None,
        help="AAAA-MM",
    )
    parser.add_argument(
        "--fim",
        default=os.getenv("NASA_MES_FIM") or os.getenv("ONS_MES_FIM") or None,
        help="AAAA-MM",
    )
    args = parser.parse_args()

    inicio = _mes(args.inicio) if args.inicio else padrao_inicio
    if args.fim:
        fim_mes = _mes(args.fim)
        fim = fim_mes.replace(day=calendar.monthrange(fim_mes.year, fim_mes.month)[1])
    else:
        fim = hoje
    fim = min(fim, hoje)
    if inicio > fim:
        parser.error("--inicio deve ser anterior ou igual a --fim")

    baixar(inicio, fim)


if __name__ == "__main__":
    main()
