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


@pytest.fixture(scope="module")
def um_agregado(cliente):
    """Um agregado estadual de pequenas usinas (MMGD), que não tem cadastro."""
    pagina = cliente.get(f"{PREFIXO}/usinas",
                         params={"tipo_unidade": "pequenas_usinas", "limit": 5}).json()
    if not pagina["data"]:
        pytest.skip("nenhum agregado 'pequenas_usinas' no banco")
    return pagina["data"][0]


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
                 "/usinas/{usina_id}/recorrencia", "/geracao/nacional"]:
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


def test_filtro_por_tipo_de_unidade(cliente):
    """A lista mistura três grãos de medição do ONS; o filtro separa."""
    totais = {}
    for tipo in ("usina", "conjunto", "pequenas_usinas"):
        corpo = cliente.get(f"{PREFIXO}/usinas",
                            params={"tipo_unidade": tipo, "limit": 50}).json()
        assert all(u["tipo_unidade"] == tipo for u in corpo["data"])
        totais[tipo] = corpo["total_estimado"]

    completo = cliente.get(f"{PREFIXO}/usinas", params={"limit": 1}).json()
    assert sum(totais.values()) == completo["total_estimado"]


def test_agregado_estadual_nao_tem_cadastro(cliente):
    """Nenhum agregado tem potência, coordenada ou data: é a natureza do grão.

    O ONS publica a soma da geração distribuída de um estado numa linha só, e
    não existe usina correspondente na ANEEL para vincular.
    """
    corpo = cliente.get(f"{PREFIXO}/usinas",
                        params={"tipo_unidade": "pequenas_usinas", "limit": 100}).json()
    assert corpo["data"], "esperava agregados no banco"
    assert all(u["potencia_mw"] is None and u["lat"] is None and u["data_operacao"] is None
               for u in corpo["data"])
    assert all(u["qualidade_vinculo"] == "sem_vinculo" for u in corpo["data"])


def test_filtro_de_tipo_combina_com_os_outros(cliente):
    corpo = cliente.get(f"{PREFIXO}/usinas",
                        params={"tipo_unidade": "conjunto", "fonte": "eolica",
                                "regiao": "NE", "limit": 50}).json()
    assert corpo["data"], "esperava conjuntos eólicos no NE"
    assert all(u["tipo_unidade"] == "conjunto" and u["fonte"] == "eolica"
               and u["regiao"] == "NE" for u in corpo["data"])


def test_agregado_mantem_a_geracao_medida(cliente, um_agregado):
    """O que falta é o cadastro, não o dado: a geração do agregado é real."""
    resposta = cliente.get(f"{PREFIXO}/usinas/{um_agregado['usina_id']}/geracao")

    assert resposta.status_code == 200
    assert resposta.json()["total_mwh"] > 0


@pytest.mark.parametrize("recurso", ["sobrevivencia", "recorrencia"])
def test_agregado_explica_por_que_nao_ha_estimativa(cliente, um_agregado, recurso):
    """404 com o diagnóstico certo: não é vínculo incompleto, é grão agregado.

    Os dois casos caem no mesmo 404 e têm encaminhamentos opostos — um conjunto
    com vínculo incompleto pode ganhar cadastro quando o vínculo melhorar; um
    agregado estadual não vai ganhar nunca.
    """
    resposta = cliente.get(f"{PREFIXO}/usinas/{um_agregado['usina_id']}/{recurso}")
    if resposta.status_code == 503:
        pytest.skip(f"modelo de {recurso} indisponível")

    assert resposta.status_code == 404
    detalhe = resposta.json()["detail"]
    assert "agregado estadual" in detalhe
    assert "vínculo" not in detalhe          # o diagnóstico errado para este caso
    assert "tipo_unidade=" in detalhe        # diz como listar só quem tem cadastro


def test_usina_inexistente_devolve_problem_details(cliente):
    resposta = cliente.get(f"{PREFIXO}/usinas/999999")
    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("application/problem+json")
    corpo = resposta.json()
    assert corpo["status"] == 404 and corpo["instance"].endswith("/usinas/999999")
    assert {"type", "title", "detail"} <= corpo.keys()


@pytest.mark.parametrize("parametros", [
    {"limit": 0}, {"limit": 500}, {"fonte": "nuclear"}, {"cursor": "não-é-base64"},
    {"tipo_unidade": "agregado"}, {"tipo_unidade": "pequenas usinas"},
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


def test_recorrencia_devolve_contagem_esperada_crescente(cliente, uma_usina):
    """Ao contrário da sobrevivência, aqui o número CRESCE com o horizonte: é uma
    contagem acumulada de manutenções, não uma probabilidade."""
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/recorrencia")
    if resposta.status_code == 503:
        pytest.skip("modelo de recorrência indisponível")
    corpo = resposta.json()
    assert corpo["simulado"] is True
    assert "SINTÉTICOS" in corpo["aviso"]
    assert corpo["horizontes"], "esperava ao menos um horizonte"

    esperadas = [h["manutencoes_esperadas"] for h in corpo["horizontes"]]
    assert all(n >= 0 for n in esperadas), "contagem esperada não pode ser negativa"
    assert esperadas == sorted(esperadas), "a contagem acumulada não pode diminuir"
    assert corpo["taxa_relativa"] > 0


def test_recorrencia_e_sobrevivencia_contam_a_mesma_historia(cliente, uma_usina):
    """Coerência entre os dois cards: se o número esperado de manutenções é alto,
    a probabilidade de passar o período sem nenhuma tem que ser baixa."""
    usina_id = uma_usina["usina_id"]
    rec = cliente.get(f"{PREFIXO}/usinas/{usina_id}/recorrencia")
    sob = cliente.get(f"{PREFIXO}/usinas/{usina_id}/sobrevivencia")
    if rec.status_code == 503 or sob.status_code == 503:
        pytest.skip("modelos indisponíveis")

    por_horizonte = {h["horizonte_meses"]: h["manutencoes_esperadas"] for h in rec.json()["horizontes"]}
    for horizonte in sob.json()["horizontes"]:
        esperadas = por_horizonte.get(horizonte["horizonte_meses"])
        if esperadas is None:
            continue
        p_zero = horizonte["probabilidade_sobrevivencia"]
        # P(nenhum evento) <= P(nenhum evento sob Poisson com a mesma média) não
        # vale exatamente (o processo não é Poisson), mas as duas grandezas têm
        # que andar no mesmo sentido: muita manutenção esperada, pouca chance de zero.
        if esperadas >= 1.0:
            assert p_zero < 0.75, f"{esperadas:.2f} manutenções esperadas com P(zero)={p_zero:.2f}"


def test_recorrencia_aceita_horizontes_customizados(cliente, uma_usina):
    resposta = cliente.get(f"{PREFIXO}/usinas/{uma_usina['usina_id']}/recorrencia",
                           params={"horizontes": [3, 18]})
    if resposta.status_code == 503:
        pytest.skip("modelo de recorrência indisponível")
    assert [h["horizonte_meses"] for h in resposta.json()["horizontes"]] == [3, 18]


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
