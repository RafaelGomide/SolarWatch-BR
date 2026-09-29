"""Ingestão da NASA POWER API — clima horário e diário por coordenada.

Para cada local de `locais.csv` — pontos derivados das coordenadas reais das
usinas da ANEEL por `gerar_locais.py`, com estado e subsistema para cruzar com
os dados do ONS — baixa duas séries:

1. HORÁRIA (`dados/bruto/dados_nasa_bruto.parquet`), em UTC:
   - ALLSKY_SFC_SW_DWN: irradiância global horizontal na superfície (Wh/m²)
   - WS10M / WS50M: velocidade do vento a 10 m e 50 m (m/s)
   - T2M: temperatura do ar a 2 m (°C)
2. DIÁRIA (`dados/bruto/dados_nasa_diario_bruto.parquet`), em hora solar local (LST):
   - ALLSKY_SFC_SW_DWN (kWh/m²/dia), WS10M, WS50M, T2M, T2M_MAX, T2M_MIN

Por que as duas: a irradiância HORÁRIA da NASA é publicada com ~3 meses de
atraso (vem -999 no período recente), enquanto a DIÁRIA em LST fica disponível
com poucos dias de atraso. Vento e temperatura horários atrasam só ~2 dias.

Tudo é gravado sem transformação. Valores ausentes vêm como -999 (fill value da API).

Uso (o `locais.csv` precisa existir; gere-o depois da ingestão da ANEEL com
`python -m ingestao.nasa_power.gerar_locais`):
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

from ingestao.armazenamento import gravar_parquet
from ingestao.http import get_com_retry, nova_sessao

SERIES = {
    "horaria": {
        "url": "https://power.larc.nasa.gov/api/temporal/hourly/point",
        "parametros": ["ALLSKY_SFC_SW_DWN", "WS10M", "WS50M", "T2M"],
        "coluna_tempo": "data_hora_utc",
        "extra": {"time-standard": "UTC"},
    },
    "diaria": {
        "url": "https://power.larc.nasa.gov/api/temporal/daily/point",
        "parametros": ["ALLSKY_SFC_SW_DWN", "WS10M", "WS50M", "T2M", "T2M_MAX", "T2M_MIN"],
        "coluna_tempo": "data_lst",
        # LST (padrão da API): em UTC a irradiância diária recente não é publicada
        "extra": {"time-standard": "LST"},
    },
}
COLUNAS_LOCAL = ["local", "municipio", "id_estado", "id_subsistema", "latitude", "longitude"]
PAUSA_ENTRE_CHAMADAS_S = 1.0  # cortesia com a API pública

RAIZ = Path(__file__).resolve().parents[2]
LOCAIS = Path(__file__).resolve().parent / "locais.csv"
SAIDA = RAIZ / "dados" / "bruto" / "dados_nasa_bruto.parquet"
SAIDA_DIARIA = RAIZ / "dados" / "bruto" / "dados_nasa_diario_bruto.parquet"

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
    sessao: requests.Session, local: pd.Series, inicio: date, fim: date, serie: str
) -> pd.DataFrame:
    config = SERIES[serie]
    frames = []
    for ini_janela, fim_janela in _janelas_anuais(inicio, fim):
        resp = get_com_retry(
            sessao,
            config["url"],
            params={
                "parameters": ",".join(config["parametros"]),
                "community": "RE",
                "latitude": local.latitude,
                "longitude": local.longitude,
                "start": ini_janela.strftime("%Y%m%d"),
                "end": fim_janela.strftime("%Y%m%d"),
                "format": "JSON",
                **config["extra"],
            },
        )
        resp.raise_for_status()
        valores = resp.json()["properties"]["parameter"]
        # {"T2M": {"2026091000": 21.3, ...}, ...} -> uma linha por hora (ou dia)
        df = pd.DataFrame(valores).rename_axis(config["coluna_tempo"]).reset_index()
        frames.append(df)
        time.sleep(PAUSA_ENTRE_CHAMADAS_S)

    df = pd.concat(frames, ignore_index=True)
    # Identificação do local primeiro, depois hora e parâmetros
    return df.assign(**{coluna: local[coluna] for coluna in COLUNAS_LOCAL})[
        COLUNAS_LOCAL + [config["coluna_tempo"]] + config["parametros"]
    ]


def baixar(inicio: date, fim: date) -> list[Path]:
    locais = pd.read_csv(LOCAIS, dtype={"latitude": float, "longitude": float})
    sessao = nova_sessao()

    gravados = []
    for serie, saida in [("horaria", SAIDA), ("diaria", SAIDA_DIARIA)]:
        frames = []
        for _, local in locais.iterrows():
            log.info("Baixando série %s de %s (%.2f, %.2f) de %s a %s",
                     serie, local.local, local.latitude, local.longitude, inicio, fim)
            df = _baixar_local(sessao, local, inicio, fim, serie)
            frames.append(df)
            log.info("  %d linhas", len(df))
        gravados.append(gravar_parquet(pd.concat(frames, ignore_index=True), saida))
    return gravados


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
