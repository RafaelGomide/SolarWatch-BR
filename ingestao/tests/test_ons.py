"""Ingestão do ONS: geração de meses, descoberta de URLs e gravação por mês."""

from __future__ import annotations

from datetime import date
from io import BytesIO

import pandas as pd
import pytest
import responses

from ingestao.ONS.ingestao_ons import (PADRAO_MENSAL, URL_ARQUIVO, _meses,
                                       _urls_disponiveis, baixar)
from ingestao.http import nova_sessao

CKAN = "https://dados.ons.org.br/api/3/action/package_show"


# --------------------------------------------------------------------- _meses
@pytest.mark.parametrize(("inicio", "fim", "esperado"), [
    (date(2026, 7, 1), date(2026, 7, 1), [(2026, 7)]),
    (date(2026, 7, 1), date(2026, 9, 1), [(2026, 7), (2026, 8), (2026, 9)]),
    # o dia do mês é irrelevante: o grão é mensal
    (date(2026, 7, 31), date(2026, 8, 1), [(2026, 7), (2026, 8)]),
    # virada de ano, que era onde a aritmética manual costumava errar
    (date(2025, 11, 1), date(2026, 2, 1), [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]),
    (date(2026, 12, 1), date(2027, 1, 1), [(2026, 12), (2027, 1)]),
    # fim antes do início: lista vazia, nada é baixado
    (date(2026, 9, 1), date(2026, 7, 1), []),
])
def test_meses(inicio, fim, esperado):
    assert _meses(inicio, fim) == esperado


def test_meses_ano_inteiro_tem_12_entradas():
    meses = _meses(date(2026, 1, 1), date(2026, 12, 1))
    assert len(meses) == 12
    assert meses[0] == (2026, 1) and meses[-1] == (2026, 12)


# -------------------------------------------------------------- PADRAO_MENSAL
@pytest.mark.parametrize(("nome", "esperado"), [
    ("GERACAO_USINA-2_2026_07.parquet", ("2026", "07")),
    ("https://s3.amazonaws.com/x/y/GERACAO_USINA-2_2001_12.parquet", ("2001", "12")),
])
def test_padrao_mensal_casa_arquivos_mensais(nome, esperado):
    casamento = PADRAO_MENSAL.search(nome)
    assert casamento is not None
    assert (casamento[1], casamento[2]) == esperado


@pytest.mark.parametrize("nome", [
    # Arquivo ANUAL: foi o que quebrou a primeira versão, que fazia rsplit("_", 2)
    # e tentava int("USINA-2") como ano.
    "GERACAO_USINA-2_2015.parquet",
    "GERACAO_USINA-2_2026_07.csv",
    "GERACAO_USINA-2_2026_07.xlsx",
    "GERACAO_USINA-2_2026_07.parquet.tmp",
    "OUTRO_DATASET_2026_07.parquet",
    "GERACAO_USINA-2_26_7.parquet",
])
def test_padrao_mensal_ignora_o_resto(nome):
    assert PADRAO_MENSAL.search(nome) is None


# --------------------------------------------------------- _urls_disponiveis
def _ckan_com(urls: list[str]) -> dict:
    return {"result": {"resources": [{"url": u} for u in urls]}}


@responses.activate
def test_urls_disponiveis_filtra_so_os_parquets_mensais():
    responses.get(CKAN, json=_ckan_com([
        "https://s3/x/GERACAO_USINA-2_2026_07.parquet",
        "https://s3/x/GERACAO_USINA-2_2026_08.parquet",
        "https://s3/x/GERACAO_USINA-2_2015.parquet",      # anual
        "https://s3/x/GERACAO_USINA-2_2026_07.csv",       # outro formato
    ]), status=200)

    urls = _urls_disponiveis(nova_sessao())
    assert set(urls) == {(2026, 7), (2026, 8)}
    assert urls[(2026, 7)].endswith("GERACAO_USINA-2_2026_07.parquet")


@pytest.mark.parametrize("resposta", [
    {"status": 500},                       # portal fora do ar
    {"json": {"sem": "result"}},           # formato inesperado (KeyError)
    {"body": "isto não é json"},           # resposta não parseável (ValueError)
])
@responses.activate
def test_urls_disponiveis_degrada_para_dicionario_vazio(resposta):
    """CKAN é otimização, não ponto único de falha: sem ele, usa-se o padrão de URL do S3."""
    responses.get(CKAN, **resposta)
    assert _urls_disponiveis(nova_sessao()) == {}


# ----------------------------------------------------------------- baixar()
def _parquet_de(linhas: int, marcador: str) -> bytes:
    buffer = BytesIO()
    pd.DataFrame({
        "din_instante": pd.date_range("2026-07-01", periods=linhas, freq="h"),
        "nom_usina": [marcador] * linhas,
        "val_geracao": range(linhas),
    }).to_parquet(buffer, index=False)
    return buffer.getvalue()


@responses.activate
def test_baixar_grava_um_arquivo_por_mes(tmp_path):
    responses.get(CKAN, status=500)  # força o caminho do padrão de URL
    for mes, marcador in [(7, "julho"), (8, "agosto")]:
        responses.get(URL_ARQUIVO.format(ano=2026, mes=mes),
                      body=_parquet_de(3, marcador), status=200,
                      content_type="application/octet-stream")

    saida = baixar(date(2026, 7, 1), date(2026, 8, 1), saida=tmp_path)

    assert saida == tmp_path
    arquivos = sorted(p.name for p in tmp_path.glob("*.parquet"))
    assert arquivos == ["dados_ons_bruto_2026_07.parquet", "dados_ons_bruto_2026_08.parquet"]

    julho = pd.read_parquet(tmp_path / "dados_ons_bruto_2026_07.parquet")
    assert len(julho) == 3
    assert julho["nom_usina"].eq("julho").all()
    # proveniência: o nome do arquivo NA FONTE, não o nosso
    assert julho["arquivo_origem"].unique().tolist() == ["GERACAO_USINA-2_2026_07.parquet"]


@responses.activate
def test_baixar_pula_mes_ainda_nao_publicado(tmp_path):
    responses.get(CKAN, status=500)
    responses.get(URL_ARQUIVO.format(ano=2026, mes=7), body=_parquet_de(2, "julho"), status=200)
    responses.get(URL_ARQUIVO.format(ano=2026, mes=8), status=404)   # mês corrente sem publicação

    baixar(date(2026, 7, 1), date(2026, 8, 1), saida=tmp_path)

    assert [p.name for p in tmp_path.glob("*.parquet")] == ["dados_ons_bruto_2026_07.parquet"]


@responses.activate
def test_baixar_sem_nenhum_mes_publicado_falha_sem_criar_pasta(tmp_path):
    responses.get(CKAN, status=500)
    responses.get(URL_ARQUIVO.format(ano=2030, mes=1), status=404)
    destino = tmp_path / "nao_deve_existir"

    with pytest.raises(SystemExit):
        baixar(date(2030, 1, 1), date(2030, 1, 1), saida=destino)

    assert not destino.exists()


@responses.activate
def test_baixar_prefere_a_url_publicada_pelo_ckan(tmp_path):
    """Quando o CKAN responde, a URL vem dele; o padrão de URL é só fallback."""
    url_ckan = "https://s3.exemplo.test/publicado/GERACAO_USINA-2_2026_07.parquet"
    responses.get(CKAN, json=_ckan_com([url_ckan]), status=200)
    responses.get(url_ckan, body=_parquet_de(1, "ckan"), status=200)

    baixar(date(2026, 7, 1), date(2026, 7, 1), saida=tmp_path)

    urls_chamadas = [chamada.request.url for chamada in responses.calls]
    assert url_ckan in urls_chamadas
    assert URL_ARQUIVO.format(ano=2026, mes=7) not in urls_chamadas


@responses.activate
def test_reingerir_um_mes_nao_apaga_os_outros(tmp_path):
    """Ganho do arquivo-por-mês: a ingestão virou incremental."""
    responses.get(CKAN, status=500)
    responses.get(URL_ARQUIVO.format(ano=2026, mes=7), body=_parquet_de(2, "julho"), status=200)
    responses.get(URL_ARQUIVO.format(ano=2026, mes=8), body=_parquet_de(5, "agosto v2"), status=200)

    baixar(date(2026, 7, 1), date(2026, 7, 1), saida=tmp_path)   # só julho
    baixar(date(2026, 8, 1), date(2026, 8, 1), saida=tmp_path)   # depois só agosto

    assert len(list(tmp_path.glob("*.parquet"))) == 2
    assert len(pd.read_parquet(tmp_path / "dados_ons_bruto_2026_07.parquet")) == 2
    assert len(pd.read_parquet(tmp_path / "dados_ons_bruto_2026_08.parquet")) == 5
