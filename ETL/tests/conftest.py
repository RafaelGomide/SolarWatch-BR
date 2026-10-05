"""Fixtures comuns dos testes do ETL.

Nenhum teste lê os dados reais do projeto: tudo é construído em memória ou em
`tmp_path`. O `ds_toolkit` roda de verdade (o caminho de código é o mesmo da
pipeline), só não imprime os relatórios.
"""

from __future__ import annotations

import pytest

import ETL.validacao as validacao


@pytest.fixture(autouse=True)
def toolkit_silencioso(monkeypatch):
    """Desliga os relatórios do ds_toolkit durante os testes.

    `relatorio_qualidade` continua sendo chamado — só não despeja uma tabela
    por tabela validada na saída do pytest.
    """
    monkeypatch.setattr(validacao, "VERBOSE_TOOLKIT", False)
