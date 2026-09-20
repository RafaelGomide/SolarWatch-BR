"""Testes de contrato da API.

Rodam contra o banco real (`DB/solarwatch.duckdb`), que é read-only — não há
fixture de escrita nem risco de efeito colateral. Se o banco não existir, a
suíte inteira é pulada com uma mensagem dizendo o que rodar.

    pytest backend/tests -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import configuracao
from backend.main import app
from backend.modelos_ml import registro

PREFIXO = configuracao().prefixo_api

pytestmark = pytest.mark.skipif(
    not configuracao().banco.exists(),
    reason="banco ausente; rode python -m ETL.pipeline && python -m DB.criar_banco",
)


@pytest.fixture(scope="module")
def cliente():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def uma_usina(cliente):
    """Uma usina que tenha potência e data de operação (serve a todos os endpoints)."""
    pagina = cliente.get(f"{PREFIXO}/usinas", params={"limit": 200}).json()
    completas = [u for u in pagina["data"] if u["potencia_mw"] and u["data_operacao"]]
    assert completas, "nenhuma usina com cadastro completo no banco"
    return completas[0]


# ------------------------------------------------------------------ sistema
def test_health_responde_ok_ou_degradado(cliente):
    corpo = cliente.get("/health").json()
    assert corpo["status"] in {"ok", "degradado"}
    assert corpo["banco"] is True
    assert corpo["cobertura"]["usinas"] > 0


def test_metrics_no_formato_prometheus(cliente):
    resposta = cliente.get("/metrics")
    assert resposta.status_code == 200
    assert "solarwatch_requisicoes_total" in resposta.text


def test_toda_resposta_tem_request_id(cliente):
    resposta = cliente.get("/health")
    assert resposta.headers["X-Request-ID"]
    assert float(resposta.headers["X-Response-Time-Ms"]) >= 0


def test_openapi_expoe_os_endpoints_versionados(cliente):
    caminhos = cliente.get("/openapi.json").json()["paths"]
    for rota in ["/usinas", "/usinas/{usina_id}/geracao", "/usinas/{usina_id}/sobrevivencia",
                 "/geracao/nacional"]:
        assert f"{PREFIXO}{rota}" in caminhos


# ------------------------------------------------------------------- usinas
def test_listagem_respeita_o_limite(cliente):
    corpo = cliente.get(f"{PREFIXO}/usinas", params={"limit": 3}).json()
    assert len(corpo["data"]) == 3
    assert corpo["total_estimado"] >= 3


def test_paginacao_por_cursor_nao_repete_itens(cliente):
    primeira = cliente.get(f"{PREFIXO}/usinas", params={"limit": 5}).json()
    assert primeira["next_cursor"]
    segunda = cliente.get(f"{PREFIXO}/usinas",
                          params={"limit": 5, "cursor": primeira["next_cursor"]}).json()
    ids_primeira = {u["usina_id"] for u in primeira["data"]}
    ids_segunda = {u["usina_id"] for u in segunda["data"]}
    assert not ids_primeira & ids_segunda
    assert min(ids_segunda) > max(ids_primeira)


def test_filtro_por_fonte_e_regiao(cliente):
    corpo = cliente.get(f"{PREFIXO}/usinas",
                        params={"fonte": "solar", "regiao": "NE", "limit": 50}).json()
    assert corpo["data"], "esperava usinas solares no NE"
    assert all(u["fonte"] == "solar" and u["regiao"] == "NE" for u in corpo["data"])


def test_usina_inexistente_devolve_problem_details(cliente):
    resposta = cliente.get(f"{PREFIXO}/usinas/999999")
    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("application/problem+json")
    corpo = resposta.json()
    assert corpo["status"] == 404 and corpo["instance"].endswith("/usinas/999999")
    assert {"type", "title", "detail"} <= corpo.keys()


@pytest.mark.parametrize("parametros", [
    {"limit": 0}, {"limit": 500}, {"fonte": "nuclear"}, {"cursor": "não-é-base64"},
])
def test_parametros_invalidos_devolvem_422(cliente, parametros):
    resposta = cliente.get(f"{PREFIXO}/usinas", params=parametros)
    assert resposta.status_code == 422
    assert resposta.json()["status"] == 422


# ------------------------------------------------------------------ geração
def test_serie_de_geracao_de_uma_usina(cliente, uma_usina):
    corpo = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/geracao").json()
    assert corpo["horas"] > 0
    assert len(corpo["data"]) == corpo["horas"]
    primeiro = corpo["data"][0]
    assert {"timestamp", "energia_mwh", "flag_qualidade"} <= primeiro.keys()


def test_janela_maior_que_o_maximo_e_recusada(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/geracao",
                           params={"inicio": "2020-01-01", "fim": "2026-09-01"})
    assert resposta.status_code == 422
    assert "excede o máximo" in resposta.json()["detail"]


def test_inicio_depois_do_fim_e_recusado(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/geracao",
                           params={"inicio": "2026-09-10", "fim": "2026-09-01"})
    assert resposta.status_code == 422


@pytest.mark.parametrize("granularidade", ["dia", "hora"])
def test_geracao_nacional(cliente, granularidade):
    corpo = cliente.get(f"{PREFIXO}/geracao/nacional",
                        params={"granularidade": granularidade, "fonte": "solar",
                                "inicio": "2026-09-01", "fim": "2026-09-05"}).json()
    assert corpo["granularidade"] == granularidade
    assert corpo["data"] and all(p["fonte"] == "solar" for p in corpo["data"])
    assert all(p["energia_mwh"] >= 0 for p in corpo["data"])


# -------------------------------------------------------------------- clima
def test_serie_de_clima(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/clima")
    if resposta.status_code == 404:
        pytest.skip("usina sem ponto de clima de referência")
    corpo = resposta.json()
    assert corpo["local_clima"] and corpo["metodo_vinculo_clima"]
    assert "não da coordenada exata" in corpo["aviso"]


# ------------------------------------------------------------------- modelos
def test_previsao_por_fonte(cliente):
    # a checagem é feita aqui dentro: no import o startup ainda não carregou os modelos
    resposta = cliente.get(f"{PREFIXO}/geracao/previsao", params={"fonte": "solar"})
    if resposta.status_code == 503:
        pytest.skip("modelo de previsão solar indisponível")
    corpo = resposta.json()
    assert corpo["horizonte_h"] == len(corpo["data"]) == 24
    assert corpo["metodo"] == "modelo_por_fonte"
    assert all(p["energia_mwh_prevista"] >= 0 for p in corpo["data"])


def test_previsao_por_usina_declara_o_rateio(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/previsao")
    if resposta.status_code == 503:
        pytest.skip("modelo de previsão indisponível")
    corpo = resposta.json()
    assert corpo["metodo"] == "rateio_proporcional"
    assert 0 <= corpo["participacao_usina"] <= 1


def test_sobrevivencia_avisa_que_o_dado_e_simulado(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/sobrevivencia")
    if resposta.status_code == 503:
        pytest.skip("modelo de sobrevivência indisponível")
    corpo = resposta.json()
    assert corpo["simulado"] is True
    assert "SINTÉTICOS" in corpo["aviso"]
    assert corpo["horizontes"], "esperava ao menos um horizonte"
    probabilidades = [h["probabilidade_sobrevivencia"] for h in corpo["horizontes"]]
    assert all(0 <= p <= 1 for p in probabilidades)
    # quanto maior o horizonte, menor (ou igual) a probabilidade de sobreviver
    assert probabilidades == sorted(probabilidades, reverse=True)


def test_modelo_indisponivel_degrada_so_o_endpoint_dependente(cliente, uma_usina, monkeypatch):
    """Sem modelo de previsão, /previsao responde 503 e /geracao segue 200 (§10.5)."""
    monkeypatch.setattr(registro, "previsao", {})
    monkeypatch.setitem(registro.falhas, "previsao_solar", "simulado no teste")

    indisponivel = cliente.get(f"{PREFIXO}/geracao/previsao", params={"fonte": "solar"})
    assert indisponivel.status_code == 503
    assert indisponivel.headers["content-type"].startswith("application/problem+json")

    historico = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/geracao")
    assert historico.status_code == 200


# -------------------------------------------------------------- rate limit
def test_token_bucket_bloqueia_e_repoe():
    """Unitário: o balde esvazia na capacidade e volta a encher com o tempo."""
    from backend.limites import LimitadorTokenBucket

    limitador = LimitadorTokenBucket(limite=3, periodo_s=60)
    assert [limitador.consumir("1.2.3.4")[0] for _ in range(4)] == [True, True, True, False]
    assert limitador.consumir("5.6.7.8")[0], "o balde é por IP"

    _, _, reset = limitador.consumir("1.2.3.4")
    assert reset > 0, "deve informar em quantos segundos haverá token de novo"

    limitador.baldes["1.2.3.4"].ultimo_acesso -= 30   # simula 30 s de espera
    assert limitador.consumir("1.2.3.4")[0], "após 30 s há tokens repostos (3/min)"


def test_429_em_problem_details_com_headers():
    """Integração, em um app isolado com limite baixo (não gasta o do resto da suíte)."""
    from fastapi import FastAPI

    from backend import erros as modulo_erros
    from backend.limites import MiddlewareRateLimit

    mini = FastAPI()
    mini.add_middleware(MiddlewareRateLimit, expressao="2/minute")
    modulo_erros.registrar(mini)

    @mini.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(mini) as c:
        assert c.get("/ping").status_code == 200
        assert c.get("/ping").headers["X-RateLimit-Remaining"] == "0"

        bloqueado = c.get("/ping")
        assert bloqueado.status_code == 429
        assert bloqueado.headers["content-type"].startswith("application/problem+json")
        assert int(bloqueado.headers["Retry-After"]) >= 1
        assert bloqueado.json()["type"].endswith("/rate-limit")
