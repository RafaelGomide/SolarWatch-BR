"""Fixtures comuns dos testes de ingestão.

Nenhum teste toca a rede: todas as respostas HTTP são simuladas com `responses`.
"""

from __future__ import annotations

import pytest

import ingestao.http as http


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    """Anula o backoff e as pausas de cortesia.

    Sem isso, um teste de retry dormiria os mesmos 2+4+8+16 s da execução real.
    """
    monkeypatch.setattr(http.time, "sleep", lambda _: None)


@pytest.fixture
def esperas(monkeypatch):
    """Registra as esperas pedidas pelo backoff, em vez de dormir."""
    registradas: list[float] = []
    monkeypatch.setattr(http.time, "sleep", registradas.append)
    return registradas
