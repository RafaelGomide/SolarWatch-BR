"""Raw layer: particiona o dump bruto da ingestão por data de coleta.

A ingestão grava sempre em `dados/bruto/dados_<fonte>_bruto.parquet`. Esta etapa
arquiva uma cópia imutável em `dados/bruto/AAAA-MM-DD/`, onde a data é a da
coleta (modificação do arquivo). Assim cada execução da ingestão preserva um
retrato histórico, e o clean sempre lê a partição mais recente de cada fonte.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import date
from pathlib import Path

from ETL.config import ARQUIVOS_BRUTOS, BRUTO

PADRAO_PARTICAO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger(__name__)


def particionar(data_coleta: date | None = None) -> dict[str, Path]:
    """Copia cada arquivo bruto para a partição da sua data de coleta.

    Idempotente: se a partição já tem o mesmo arquivo (mesmo tamanho e horário
    de modificação), nada é copiado.
    """
    copiados = {}
    for fonte, nome in ARQUIVOS_BRUTOS.items():
        origem = BRUTO / nome
        if not origem.exists():
            log.warning("[raw] %s não encontrado; rode a ingestão de %s", origem.name, fonte)
            continue
        dia = data_coleta or date.fromtimestamp(origem.stat().st_mtime)
        destino = BRUTO / dia.isoformat() / nome
        mesmo = (
            destino.exists()
            and destino.stat().st_size == origem.stat().st_size
            and int(destino.stat().st_mtime) == int(origem.stat().st_mtime)
        )
        if not mesmo:
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origem, destino)  # preserva o mtime (= data de coleta)
            log.info("[raw] %s -> %s", nome, destino.relative_to(BRUTO.parent.parent))
        copiados[fonte] = destino
    return copiados


def particoes() -> list[Path]:
    return sorted(p for p in BRUTO.iterdir() if p.is_dir() and PADRAO_PARTICAO.match(p.name))


def localizar(fonte: str, data_coleta: date | None = None) -> Path:
    """Caminho do bruto de uma fonte na partição pedida ou, sem data, na mais
    recente que contém aquele arquivo."""
    nome = ARQUIVOS_BRUTOS[fonte]
    candidatas = [p / nome for p in particoes() if (p / nome).exists()]
    if data_coleta:
        candidatas = [c for c in candidatas if c.parent.name == data_coleta.isoformat()]
    if not candidatas:
        raise FileNotFoundError(
            f"Nenhuma partição com {nome}"
            + (f" em {data_coleta}" if data_coleta else "")
            + ". Rode a ingestão e a etapa raw."
        )
    return candidatas[-1]
