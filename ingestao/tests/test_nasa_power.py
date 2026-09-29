"""Ingestão da NASA POWER: janelas de requisição e montagem da série."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import responses

from ingestao.http import nova_sessao
from ingestao.nasa_power.ingestao_nasa_power import SERIES, _baixar_local, _janelas_anuais

URL_HORARIA = SERIES["horaria"]["url"]
URL_DIARIA = SERIES["diaria"]["url"]

LOCAL = pd.Series({
    "local": "uibai_ba", "municipio": "Uibaí", "id_estado": "BA",
    "id_subsistema": "NE", "latitude": -11.43252, "longitude": -42.13743,
})


# ------------------------------------------------------------ _janelas_anuais
@pytest.mark.parametrize(("inicio", "fim", "esperado"), [
    # dentro do mesmo ano: uma requisição só
    (date(2026, 7, 1), date(2026, 9, 18), [(date(2026, 7, 1), date(2026, 9, 18))]),
    (date(2026, 1, 1), date(2026, 12, 31), [(date(2026, 1, 1), date(2026, 12, 31))]),
    # cruzando o ano: corta em 31/12 e recomeça em 01/01
    (date(2025, 11, 1), date(2026, 2, 10),
     [(date(2025, 11, 1), date(2025, 12, 31)), (date(2026, 1, 1), date(2026, 2, 10))]),
    (date(2024, 6, 1), date(2026, 3, 1),
     [(date(2024, 6, 1), date(2024, 12, 31)),
      (date(2025, 1, 1), date(2025, 12, 31)),
      (date(2026, 1, 1), date(2026, 3, 1))]),
    # um único dia
    (date(2026, 5, 5), date(2026, 5, 5), [(date(2026, 5, 5), date(2026, 5, 5))]),
])
def test_janelas_anuais(inicio, fim, esperado):
    assert _janelas_anuais(inicio, fim) == esperado


def test_janelas_anuais_nao_deixa_buraco_nem_sobreposicao():
    janelas = _janelas_anuais(date(2020, 3, 15), date(2026, 8, 20))
    assert janelas[0][0] == date(2020, 3, 15)
    assert janelas[-1][1] == date(2026, 8, 20)
    for (_, fim_anterior), (inicio_seguinte, _) in zip(janelas, janelas[1:]):
        assert (fim_anterior - inicio_seguinte).days == -1
    for inicio, fim in janelas:
        assert inicio <= fim
        assert inicio.year == fim.year          # nenhuma janela cruza o ano


def test_janelas_anuais_fim_antes_do_inicio_nao_gera_requisicao():
    assert _janelas_anuais(date(2026, 9, 1), date(2026, 7, 1)) == []


# --------------------------------------------------------------- _baixar_local
def _resposta(parametros: list[str], instantes: list[str]) -> dict:
    """Formato da NASA: {"properties": {"parameter": {"T2M": {"2026070100": 21.3}}}}."""
    return {"properties": {"parameter": {
        p: {instante: float(i) for i, instante in enumerate(instantes)}
        for p in parametros
    }}}


@responses.activate
def test_baixar_local_monta_uma_linha_por_hora_com_o_local():
    parametros = SERIES["horaria"]["parametros"]
    responses.get(URL_HORARIA, json=_resposta(parametros, ["2026070100", "2026070101"]), status=200)

    df = _baixar_local(nova_sessao(), LOCAL, date(2026, 7, 1), date(2026, 7, 1), "horaria")

    assert len(df) == 2
    # identificação do local primeiro, depois tempo, depois os parâmetros crus da NASA
    assert list(df.columns) == ["local", "municipio", "id_estado", "id_subsistema",
                                "latitude", "longitude", "data_hora_utc", *parametros]
    assert df["local"].eq("uibai_ba").all()
    assert df["data_hora_utc"].tolist() == ["2026070100", "2026070101"]


@responses.activate
def test_baixar_local_usa_uma_requisicao_por_janela_anual():
    parametros = SERIES["horaria"]["parametros"]
    responses.get(URL_HORARIA, json=_resposta(parametros, ["2025120100"]), status=200)
    responses.get(URL_HORARIA, json=_resposta(parametros, ["2026010100", "2026010101"]), status=200)

    df = _baixar_local(nova_sessao(), LOCAL, date(2025, 12, 1), date(2026, 1, 2), "horaria")

    assert len(responses.calls) == 2
    assert len(df) == 3          # as janelas são concatenadas numa série só


@responses.activate
def test_baixar_local_envia_os_parametros_certos_da_serie():
    parametros = SERIES["diaria"]["parametros"]
    responses.get(URL_DIARIA, json=_resposta(parametros, ["20260701"]), status=200)

    _baixar_local(nova_sessao(), LOCAL, date(2026, 7, 1), date(2026, 7, 1), "diaria")

    enviado = responses.calls[0].request.params
    assert enviado["parameters"].split(",") == parametros
    assert enviado["start"] == "20260701" and enviado["end"] == "20260701"
    assert (float(enviado["latitude"]), float(enviado["longitude"])) == (-11.43252, -42.13743)
    assert enviado["community"] == "RE"
    # LST na diária: em UTC a NASA não publica a irradiância diária recente
    assert enviado["time-standard"] == "LST"


@responses.activate
def test_serie_horaria_pede_utc():
    parametros = SERIES["horaria"]["parametros"]
    responses.get(URL_HORARIA, json=_resposta(parametros, ["2026070100"]), status=200)

    _baixar_local(nova_sessao(), LOCAL, date(2026, 7, 1), date(2026, 7, 1), "horaria")

    assert responses.calls[0].request.params["time-standard"] == "UTC"


@responses.activate
def test_fill_value_chega_cru_na_camada_bruta():
    """-999 é da fonte e só vira nulo no ETL; a ingestão não transforma nada."""
    parametros = SERIES["horaria"]["parametros"]
    corpo = {"properties": {"parameter": {p: {"2026070100": -999.0} for p in parametros}}}
    responses.get(URL_HORARIA, json=corpo, status=200)

    df = _baixar_local(nova_sessao(), LOCAL, date(2026, 7, 1), date(2026, 7, 1), "horaria")

    assert (df[parametros] == -999.0).all().all()
