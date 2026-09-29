"""`ingestao.http.get_com_retry`: o que é repetido e o que falha na hora."""

from __future__ import annotations

import pytest
import requests
import responses

from ingestao.http import MAX_TENTATIVAS, get_com_retry, nova_sessao

URL = "https://exemplo.test/recurso"


@responses.activate
def test_200_retorna_na_primeira():
    responses.get(URL, json={"ok": True}, status=200)
    resp = get_com_retry(nova_sessao(), URL)
    assert resp.status_code == 200
    assert len(responses.calls) == 1


@responses.activate
def test_404_volta_sem_excecao_e_sem_retry():
    """O ONS depende disso para pular um mês ainda não publicado."""
    responses.get(URL, status=404)
    resp = get_com_retry(nova_sessao(), URL)
    assert resp.status_code == 404
    assert len(responses.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
@responses.activate
def test_4xx_levanta_na_primeira_tentativa(status):
    """Requisição inválida não melhora repetindo: erro na hora, sem 5 tentativas."""
    responses.get(URL, status=status)
    with pytest.raises(requests.HTTPError):
        get_com_retry(nova_sessao(), URL)
    assert len(responses.calls) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
@responses.activate
def test_transitorio_repete_ate_o_limite(status):
    responses.get(URL, status=status)
    with pytest.raises(requests.HTTPError):
        get_com_retry(nova_sessao(), URL)
    assert len(responses.calls) == MAX_TENTATIVAS


@responses.activate
def test_transitorio_que_se_resolve_devolve_a_resposta_boa():
    responses.get(URL, status=503)
    responses.get(URL, json={"ok": True}, status=200)
    resp = get_com_retry(nova_sessao(), URL)
    assert resp.status_code == 200
    assert len(responses.calls) == 2


@responses.activate
def test_erro_de_conexao_repete():
    responses.get(URL, body=requests.ConnectionError("sem rede"))
    with pytest.raises(requests.ConnectionError):
        get_com_retry(nova_sessao(), URL)
    assert len(responses.calls) == MAX_TENTATIVAS


@responses.activate
def test_backoff_e_exponencial_com_jitter(esperas):
    responses.get(URL, status=503)
    with pytest.raises(requests.HTTPError):
        get_com_retry(nova_sessao(), URL)

    assert len(esperas) == MAX_TENTATIVAS - 1          # a última tentativa não espera
    for i, espera in enumerate(esperas, start=1):
        assert 2**i <= espera < 2**i + 1               # base exponencial + jitter de 0 a 1 s
    assert esperas == sorted(esperas)


@responses.activate
def test_user_agent_identifica_o_projeto():
    """Cortesia com APIs públicas: a chamada diz quem está chamando."""
    responses.get(URL, json={}, status=200)
    get_com_retry(nova_sessao(), URL)
    assert "SolarWatch-BR" in responses.calls[0].request.headers["User-Agent"]
