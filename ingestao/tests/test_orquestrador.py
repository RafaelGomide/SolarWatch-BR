"""Orquestrador `python -m ingestao`: ordem, repasse de período e falhas.

Nenhum subprocesso é criado: `subprocess.run` é substituído por um dublê que
registra o comando e devolve o código pedido.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import pytest

from ingestao.__main__ import ETAPAS, NOMES, comando, rodar


@dataclass
class _Resultado:
    returncode: int


class _Chamadas(list):
    """Lista dos comandos executados, com o conjunto de módulos que devem falhar."""
    falhas: set[str]


@pytest.fixture
def executados(monkeypatch):
    """Captura os comandos, sem executar nada. `falhas` define quem retorna erro."""
    chamadas = _Chamadas()
    chamadas.falhas = falhas = set()

    def falso_run(argumentos, **_):
        chamadas.append(argumentos)
        modulo = argumentos[argumentos.index("-m") + 1]
        return _Resultado(1 if any(f in modulo for f in falhas) else 0)

    monkeypatch.setattr(subprocess, "run", falso_run)
    return chamadas


def _modulos(chamadas) -> list[str]:
    return [c[c.index("-m") + 1] for c in chamadas]


# ------------------------------------------------------------------- ordem
def test_ordem_canonica_das_etapas():
    """ANEEL antes de locais, locais antes de NASA: a NASA consulta as
    coordenadas que o locais.csv acabou de receber do cadastro."""
    assert NOMES == ["ons", "aneel", "locais", "nasa"]
    assert NOMES.index("aneel") < NOMES.index("locais") < NOMES.index("nasa")


def test_roda_as_quatro_etapas_na_ordem(executados):
    situacao = rodar(list(ETAPAS))

    assert _modulos(executados) == [
        "ingestao.ONS.ingestao_ons",
        "ingestao.aneel.ingestao_aneel",
        "ingestao.nasa_power.gerar_locais",
        "ingestao.nasa_power.ingestao_nasa_power",
    ]
    assert set(situacao.values()) == {"ok"}


def test_cada_etapa_roda_com_o_interpretador_atual(executados):
    rodar([ETAPAS[0]])
    assert executados[0][:2] == [sys.executable, "-m"]


# ------------------------------------------------------------------ período
def test_periodo_vai_so_para_quem_aceita(executados):
    rodar(list(ETAPAS), inicio="2026-01", fim="2026-08")

    por_modulo = {c[c.index("-m") + 1]: c for c in executados}
    assert por_modulo["ingestao.ONS.ingestao_ons"][-4:] == \
        ["--inicio", "2026-01", "--fim", "2026-08"]
    assert por_modulo["ingestao.nasa_power.ingestao_nasa_power"][-4:] == \
        ["--inicio", "2026-01", "--fim", "2026-08"]
    # ANEEL é um retrato do cadastro: não tem período
    assert "--inicio" not in por_modulo["ingestao.aneel.ingestao_aneel"]
    assert "--inicio" not in por_modulo["ingestao.nasa_power.gerar_locais"]


def test_periodo_parcial(executados):
    rodar([ETAPAS[0]], inicio="2026-01")
    assert executados[0][-2:] == ["--inicio", "2026-01"]
    assert "--fim" not in executados[0]


def test_sem_periodo_nao_passa_flags():
    assert comando(ETAPAS[0], None, None) == [sys.executable, "-m", "ingestao.ONS.ingestao_ons"]


# ------------------------------------------------------------------- falhas
def test_para_na_primeira_falha(executados):
    executados.falhas.add("aneel")

    situacao = rodar(list(ETAPAS))

    assert _modulos(executados) == ["ingestao.ONS.ingestao_ons", "ingestao.aneel.ingestao_aneel"]
    assert situacao == {"ons": "ok", "aneel": "falhou", "locais": "pulada", "nasa": "pulada"}


def test_seguir_continua_e_relata_no_fim(executados):
    executados.falhas.add("aneel")

    situacao = rodar(list(ETAPAS), seguir=True)

    assert len(executados) == 4
    assert situacao["aneel"] == "falhou"
    assert [n for n, e in situacao.items() if e == "ok"] == ["ons", "locais", "nasa"]


def test_falha_da_ultima_etapa_nao_apaga_o_sucesso_das_anteriores(executados):
    executados.falhas.add("nasa_power.ingestao")

    situacao = rodar(list(ETAPAS))

    assert situacao["ons"] == situacao["aneel"] == situacao["locais"] == "ok"
    assert situacao["nasa"] == "falhou"


# ----------------------------------------------------------- seleção de fontes
def test_subconjunto_respeita_a_ordem_canonica_e_nao_a_digitada(executados):
    escolhidas = [etapa for etapa in ETAPAS if etapa.nome in {"nasa", "ons"}]

    rodar(escolhidas)

    assert _modulos(executados) == ["ingestao.ONS.ingestao_ons",
                                    "ingestao.nasa_power.ingestao_nasa_power"]


def test_lista_vazia_nao_roda_nada(executados):
    assert rodar([]) == {}
    assert executados == []
