"""Gravação da camada bruta, compartilhada pelas ingestões."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

COMPRESSAO = "zstd"

log = logging.getLogger(__name__)


def gravar_parquet(df: pd.DataFrame, saida: Path) -> Path:
    """Grava `df` em Parquet de forma atômica (build-then-swap).

    Escreve num arquivo temporário e só então substitui o definitivo, para
    nunca deixar um `.parquet` pela metade se o processo cair no meio.
    """
    saida.parent.mkdir(parents=True, exist_ok=True)
    temporario = saida.with_suffix(".parquet.tmp")
    df.to_parquet(temporario, index=False, engine="pyarrow", compression=COMPRESSAO)
    os.replace(temporario, saida)
    log.info("Gravado %s (%d linhas, %.1f MB)", saida, len(df), saida.stat().st_size / 1024**2)
    return saida
