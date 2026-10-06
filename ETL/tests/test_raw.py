"""Camada raw: partição por data de coleta, idempotência e localização.

A raw é a única camada que o projeto promete **imutável**: cada execução da
ingestão vira uma partição datada que nunca é reescrita. Os testes cobrem a
aritmética dessa promessa — qual data é usada, o que conta como "já está lá", e
qual partição o clean enxerga quando há várias.
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from ETL import raw


def gravar(caminho, linhas: int = 1, mtime: str | None = None):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"a": range(linhas)}).to_parquet(caminho, index=False)
    if mtime:
        instante = pd.Timestamp(mtime).timestamp()
        os.utime(caminho, (instante, instante))
    return caminho


@pytest.fixture
def bruto(tmp_path, monkeypatch):
    """Um dump de ingestão: três arquivos e a pasta mensal do ONS."""
    pasta = tmp_path / "bruto"
    gravar(pasta / "dados_aneel_bruto.parquet", mtime="2026-07-04 10:00")
    gravar(pasta / "dados_nasa_bruto.parquet", mtime="2026-07-04 10:00")
    gravar(pasta / "dados_nasa_diario_bruto.parquet", mtime="2026-07-04 10:00")
    gravar(pasta / "dados_ons_bruto" / "dados_ons_bruto_2026_06.parquet",
           mtime="2026-07-01 10:00")
    gravar(pasta / "dados_ons_bruto" / "dados_ons_bruto_2026_07.parquet",
           mtime="2026-07-04 10:00")
    monkeypatch.setattr(raw, "BRUTO", pasta)
    return pasta


# ------------------------------------------------------------------ particionar
def test_particiona_cada_fonte_na_data_do_arquivo(bruto):
    copiados = raw.particionar()

    assert set(copiados) == {"ons", "aneel", "nasa", "nasa_diario"}
    assert copiados["aneel"].parent.name == "2026-07-04"
    assert copiados["aneel"].exists()


def test_pasta_mensal_usa_a_coleta_mais_recente(bruto):
    """A partição do ONS é a do arquivo gravado por último, não a do mais antigo."""
    copiados = raw.particionar()

    assert copiados["ons"].parent.name == "2026-07-04"
    nomes = {p.name for p in copiados["ons"].glob("*.parquet")}
    assert nomes == {"dados_ons_bruto_2026_06.parquet", "dados_ons_bruto_2026_07.parquet"}


def test_data_coleta_explicita_tem_precedencia(bruto):
    copiados = raw.particionar(data_coleta=date(2020, 1, 1))

    assert all(p.parent.name == "2020-01-01" for p in copiados.values())


def test_mtime_e_preservado_na_copia(bruto):
    """A data de coleta vive no arquivo; copiar não pode "atualizar" o dado."""
    origem = bruto / "dados_aneel_bruto.parquet"

    destino = raw.particionar()["aneel"]

    assert int(destino.stat().st_mtime) == int(origem.stat().st_mtime)


def test_reparticionar_nao_copia_de_novo(bruto):
    destino = raw.particionar()["aneel"]
    antes = destino.stat().st_mtime_ns

    raw.particionar()

    assert destino.stat().st_mtime_ns == antes


def test_arquivo_alterado_na_origem_e_recopiado(bruto):
    destino = raw.particionar()["aneel"]
    tamanho_antes = destino.stat().st_size
    gravar(bruto / "dados_aneel_bruto.parquet", linhas=500, mtime="2026-07-04 10:00")

    raw.particionar()

    assert destino.stat().st_size != tamanho_antes


def test_fonte_ausente_apenas_avisa(bruto, caplog):
    """Rodar o ETL sem ter ingerido a NASA não derruba as outras fontes."""
    import logging

    (bruto / "dados_nasa_bruto.parquet").unlink()

    with caplog.at_level(logging.WARNING, logger="ETL.raw"):
        copiados = raw.particionar()

    assert "nasa" not in copiados
    assert set(copiados) == {"ons", "aneel", "nasa_diario"}
    assert "rode a ingestão" in caplog.text


def test_pasta_do_ons_vazia_conta_como_ausente(bruto):
    for arquivo in (bruto / "dados_ons_bruto").glob("*.parquet"):
        arquivo.unlink()

    copiados = raw.particionar()

    assert "ons" not in copiados


# --------------------------------------------------------- particoes/localizar
def test_particoes_ignora_pastas_que_nao_sao_data(bruto):
    raw.particionar()
    (bruto / "rascunho").mkdir()
    (bruto / "2026-13-99").mkdir()          # formato de data, mas não é partição válida

    nomes = [p.name for p in raw.particoes()]

    assert nomes == ["2026-07-04", "2026-13-99"]   # o padrão é sintático
    assert "rascunho" not in nomes


def test_localizar_pega_a_particao_mais_recente(bruto):
    raw.particionar(data_coleta=date(2026, 7, 1))
    raw.particionar(data_coleta=date(2026, 7, 4))

    caminho = raw.localizar("aneel")

    assert caminho.parent.name == "2026-07-04"


def test_localizar_aceita_uma_particao_especifica(bruto):
    raw.particionar(data_coleta=date(2026, 7, 1))
    raw.particionar(data_coleta=date(2026, 7, 4))

    caminho = raw.localizar("aneel", data_coleta=date(2026, 7, 1))

    assert caminho.parent.name == "2026-07-01"


def test_localizar_sem_particao_diz_o_que_fazer(bruto):
    with pytest.raises(FileNotFoundError, match="Rode a ingestão"):
        raw.localizar("aneel")


def test_localizar_data_inexistente_menciona_a_data(bruto):
    raw.particionar()

    with pytest.raises(FileNotFoundError, match="2020-01-01"):
        raw.localizar("aneel", data_coleta=date(2020, 1, 1))


def test_localizar_ignora_particao_com_pasta_do_ons_vazia(bruto):
    """Uma partição antiga com a pasta do ONS vazia não pode mascarar a boa."""
    raw.particionar(data_coleta=date(2026, 7, 1))
    vazia = bruto / "2026-07-09" / "dados_ons_bruto"
    vazia.mkdir(parents=True)

    caminho = raw.localizar("ons")

    assert caminho.parent.name == "2026-07-01"
