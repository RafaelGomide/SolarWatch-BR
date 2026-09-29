"""Raw layer: particiona o dump bruto da ingestão por data de coleta.

A ingestão grava sempre em `dados/bruto/dados_<fonte>_bruto.parquet` — ou, no
caso do ONS, na pasta `dados/bruto/dados_ons_bruto/` com um Parquet por mês.
Esta etapa arquiva uma cópia imutável em `dados/bruto/AAAA-MM-DD/`, onde a data
é a da coleta (modificação do arquivo; do arquivo mais recente, quando é pasta).
Assim cada execução da ingestão preserva um retrato histórico, e o clean sempre
lê a partição mais recente de cada fonte.
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


def _arquivos(origem: Path) -> list[Path]:
    """Os Parquets de uma fonte: o próprio arquivo, ou o conteúdo da pasta."""
    return sorted(origem.glob("*.parquet")) if origem.is_dir() else [origem]


def _copiar(origem: Path, destino: Path) -> bool:
    """Copia preservando o mtime (= data de coleta). Devolve False se já estava lá."""
    if (destino.exists()
            and destino.stat().st_size == origem.stat().st_size
            and int(destino.stat().st_mtime) == int(origem.stat().st_mtime)):
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origem, destino)
    return True


def particionar(data_coleta: date | None = None) -> dict[str, Path]:
    """Copia cada arquivo bruto para a partição da sua data de coleta.

    Idempotente: se a partição já tem o mesmo arquivo (mesmo tamanho e horário
    de modificação), nada é copiado.
    """
    copiados = {}
    for fonte, nome in ARQUIVOS_BRUTOS.items():
        origem = BRUTO / nome
        arquivos = _arquivos(origem) if origem.exists() else []
        if not arquivos:
            log.warning("[raw] %s não encontrado; rode a ingestão de %s", nome, fonte)
            continue
        # Numa pasta mensal, a coleta é a do arquivo gravado mais recentemente
        dia = data_coleta or date.fromtimestamp(max(a.stat().st_mtime for a in arquivos))
        destino = BRUTO / dia.isoformat() / nome
        novos = sum(_copiar(a, destino / a.name if origem.is_dir() else destino) for a in arquivos)
        if novos:
            log.info("[raw] %s -> %s (%d de %d arquivo(s))", nome,
                     destino.relative_to(BRUTO.parent.parent), novos, len(arquivos))
        copiados[fonte] = destino
    return copiados


def particoes() -> list[Path]:
    return sorted(p for p in BRUTO.iterdir() if p.is_dir() and PADRAO_PARTICAO.match(p.name))


def localizar(fonte: str, data_coleta: date | None = None) -> Path:
    """Caminho do bruto de uma fonte na partição pedida ou, sem data, na mais
    recente que contém aquele arquivo."""
    nome = ARQUIVOS_BRUTOS[fonte]
    candidatas = [p / nome for p in particoes() if (p / nome).exists() and _arquivos(p / nome)]
    if data_coleta:
        candidatas = [c for c in candidatas if c.parent.name == data_coleta.isoformat()]
    if not candidatas:
        raise FileNotFoundError(
            f"Nenhuma partição com {nome}"
            + (f" em {data_coleta}" if data_coleta else "")
            + ". Rode a ingestão e a etapa raw."
        )
    return candidatas[-1]
