"""Fixtures comuns dos testes do ETL.

Nenhum teste lê os dados reais do projeto: tudo é construído em memória, escrito
em `tmp_path` ou gerado por `dados_brinquedo.py`. O `ds_toolkit` roda de verdade
(o caminho de código é o mesmo da pipeline), só não imprime os relatórios.
"""

from __future__ import annotations

import pytest

from ETL import utils, validacao
from ETL.clean import aneel, manutencao, nasa, ons
from ETL.curated import dim_usina, fatos

# Cada módulo importa `VERBOSE_TOOLKIT` para o seu próprio namespace, então o
# silenciamento precisa ser aplicado em todos — não só em `ETL.config`.
MODULOS_VERBOSOS = (validacao, utils, ons, nasa, aneel, manutencao, dim_usina, fatos)


def silenciar(mp: pytest.MonkeyPatch) -> None:
    """Desliga os relatórios do `ds_toolkit` nos módulos do ETL.

    As funções continuam sendo chamadas — o caminho de código é o da pipeline —,
    só não despejam uma tabela por tabela processada na saída do pytest.
    """
    for modulo in MODULOS_VERBOSOS:
        mp.setattr(modulo, "VERBOSE_TOOLKIT", False)


@pytest.fixture(autouse=True)
def toolkit_silencioso(monkeypatch):
    silenciar(monkeypatch)
