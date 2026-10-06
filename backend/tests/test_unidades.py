"""Unidades do backend: cursor, Problem Details, rate limit, métricas e logs.

O `test_api.py` testa o contrato visto de fora, contra o banco real. Aqui ficam
as peças que sustentam esse contrato e que valem testar em isolamento — onde dá
para forçar o caso ruim (cursor corrompido, balde vazio, rota sem nome) sem
depender de dado nem de rede.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.config import Configuracao, configuracao
from backend.erros import TIPOS, problema, registrar
from backend.limites import (LimitadorTokenBucket, MiddlewareRateLimit,
                             ROTAS_ISENTAS, parse_limite)
from backend.observabilidade import (CABECALHO_REQUEST_ID, FormatadorJSON,
                                     Metricas, MiddlewareObservabilidade)
from backend.paginacao import codificar, decodificar
from backend.servicos import _checar_janela, _registros


# ----------------------------------------------------------------- paginação
@pytest.mark.parametrize("usina_id", [1, 12, 308, 999999])
def test_cursor_vai_e_volta(usina_id):
    assert decodificar(codificar(usina_id)) == usina_id


def test_cursor_ausente_comeca_do_zero():
    """Sem cursor, a primeira página é `usina_id > 0`."""
    assert decodificar(None) == 0
    assert decodificar("") == 0


def test_cursor_e_opaco_para_o_cliente():
    """base64 do id: não é para ser lido, mas também não esconde nada —
    o cursor não carrega informação além do último id da página."""
    assert codificar(12) != "12"
    assert "12" not in codificar(12)


# cursor vazio não entra aqui: ausência de cursor é a primeira página, não erro
@pytest.mark.parametrize("cursor", ["não-é-base64", "MTI=extra!", "YWJj", "===="])
def test_cursor_invalido_vira_422(cursor):
    with pytest.raises(HTTPException) as erro:
        decodificar(cursor)
    assert erro.value.status_code == 422
    assert "cursor inválido" in erro.value.detail


def test_cursor_de_texto_valido_em_base64_mas_nao_numerico_e_recusado():
    """`YWJj` decodifica para 'abc': base64 válido, id inválido."""
    with pytest.raises(HTTPException):
        decodificar("YWJj")


# ------------------------------------------------------- Problem Details
@pytest.fixture
def app_minima():
    """Um app com os handlers de erro registrados e nada mais."""
    app = FastAPI()
    registrar(app)

    @app.get("/erro/{codigo}")
    def erro(codigo: int):
        raise HTTPException(codigo, f"detalhe do {codigo}")

    @app.get("/explode")
    def explode():
        raise RuntimeError("falha não tratada")

    @app.get("/valida")
    def valida(numero: int):
        return {"numero": numero}

    return app


# o 500 é testado no caminho da exceção não tratada, logo abaixo
@pytest.mark.parametrize("codigo", [c for c in sorted(TIPOS) if c != 500])
def test_todo_codigo_conhecido_tem_type_e_title(app_minima, codigo):
    cliente = TestClient(app_minima)

    resposta = cliente.get(f"/erro/{codigo}")

    assert resposta.status_code == codigo
    assert resposta.headers["content-type"].startswith("application/problem+json")
    corpo = resposta.json()
    slug, titulo = TIPOS[codigo]
    assert corpo["type"].endswith(f"/{slug}")
    assert corpo["title"] == titulo
    assert corpo["status"] == codigo
    assert corpo["instance"] == f"/erro/{codigo}"


def test_codigo_desconhecido_cai_no_tipo_generico(app_minima):
    corpo = TestClient(app_minima).get("/erro/418").json()

    assert corpo["type"].endswith("/erro")
    assert corpo["title"] == "Erro"


def test_excecao_nao_tratada_vira_500_sem_vazar_a_mensagem(app_minima):
    """O cliente recebe Problem Details; o traceback fica no log do servidor."""
    cliente = TestClient(app_minima, raise_server_exceptions=False)

    resposta = cliente.get("/explode")

    assert resposta.status_code == 500
    corpo = resposta.json()
    assert corpo["status"] == 500
    assert "falha não tratada" not in json.dumps(corpo)


def test_erro_de_validacao_do_fastapi_tambem_sai_em_problem_details(app_minima):
    resposta = TestClient(app_minima).get("/valida", params={"numero": "abc"})

    assert resposta.status_code == 422
    assert resposta.headers["content-type"].startswith("application/problem+json")
    assert resposta.json()["type"].endswith("/validation-error")


def test_problema_inclui_o_request_id_quando_existe():
    """Correlação entre o erro que o cliente vê e a linha de log do servidor."""
    app = FastAPI()
    registrar(app)
    app.add_middleware(MiddlewareObservabilidade)

    @app.get("/erro")
    def erro():
        raise HTTPException(404, "sem isso")

    resposta = TestClient(app).get("/erro")

    assert resposta.json()["request_id"] == resposta.headers[CABECALHO_REQUEST_ID]


def test_problema_sem_request_id_nao_inventa_campo():
    class FalsaRequest:
        class state:  # noqa: N801 - imita request.state
            pass
        url = type("Url", (), {"path": "/x"})()

    corpo = json.loads(problema(FalsaRequest(), 404, "x").body)

    assert "request_id" not in corpo


# -------------------------------------------------------------- rate limit
def test_parse_limite_aceita_as_tres_unidades():
    assert parse_limite("60/minute") == (60, 60.0)
    assert parse_limite("10/second") == (10, 1.0)
    assert parse_limite("1000/hour") == (1000, 3600.0)
    assert parse_limite(" 5 / minute ".replace(" ", "")) == (5, 60.0)


@pytest.mark.parametrize("expressao", ["60/dia", "60", "60/", "60/MINUTE"])
def test_parse_limite_recusa_o_resto(expressao):
    with pytest.raises(ValueError, match="limite inválido"):
        parse_limite(expressao)


def test_balde_bloqueia_ao_esgotar_e_conta_o_restante():
    limitador = LimitadorTokenBucket(limite=3, periodo_s=60)

    resultados = [limitador.consumir("1.2.3.4") for _ in range(4)]

    assert [permitido for permitido, _, _ in resultados] == [True, True, True, False]
    assert [restantes for _, restantes, _ in resultados] == [2, 1, 0, 0]
    assert resultados[-1][2] > 0        # segundos até repor um token


def test_balde_repoe_com_o_tempo(monkeypatch):
    """Reposição contínua: 60/minuto = 1 token por segundo."""
    agora = [1000.0]
    monkeypatch.setattr("backend.limites.time.monotonic", lambda: agora[0])
    limitador = LimitadorTokenBucket(limite=60, periodo_s=60)
    for _ in range(60):
        limitador.consumir("ip")
    assert limitador.consumir("ip")[0] is False

    agora[0] += 2.0                     # dois segundos depois, dois tokens

    assert limitador.consumir("ip")[0] is True
    assert limitador.consumir("ip")[0] is True
    assert limitador.consumir("ip")[0] is False


def test_balde_nao_passa_da_capacidade(monkeypatch):
    agora = [0.0]
    monkeypatch.setattr("backend.limites.time.monotonic", lambda: agora[0])
    limitador = LimitadorTokenBucket(limite=5, periodo_s=5)
    limitador.consumir("ip")

    agora[0] += 3600                    # uma hora de inatividade

    _, restantes, _ = limitador.consumir("ip")
    assert restantes == 4               # 5 - 1, e não 3.600 tokens acumulados


def test_cada_ip_tem_seu_proprio_balde():
    limitador = LimitadorTokenBucket(limite=1, periodo_s=60)

    assert limitador.consumir("1.1.1.1")[0] is True
    assert limitador.consumir("1.1.1.1")[0] is False
    assert limitador.consumir("2.2.2.2")[0] is True


def test_baldes_inativos_sao_descartados(monkeypatch):
    """Scraping com IPs variados não pode crescer sem limite na memória."""
    agora = [0.0]
    monkeypatch.setattr("backend.limites.time.monotonic", lambda: agora[0])
    limitador = LimitadorTokenBucket(limite=10, periodo_s=60)
    for i in range(1000):
        limitador.consumir(f"10.0.{i // 256}.{i % 256}")
    assert len(limitador.baldes) == 1000

    agora[0] += 601                     # todos ficam inativos
    limitador.consumir("novo")

    assert len(limitador.baldes) == 1    # só o balde novo sobrou


def test_middleware_poe_headers_de_limite_e_429_em_problem_details():
    app = FastAPI()
    registrar(app)
    app.add_middleware(MiddlewareRateLimit, expressao="2/minute")

    @app.get("/x")
    def x():
        return {"ok": True}

    cliente = TestClient(app)
    primeira = cliente.get("/x")
    cliente.get("/x")
    bloqueada = cliente.get("/x")

    assert primeira.status_code == 200
    assert primeira.headers["X-RateLimit-Limit"] == "2"
    assert primeira.headers["X-RateLimit-Remaining"] == "1"
    assert bloqueada.status_code == 429
    assert bloqueada.headers["content-type"].startswith("application/problem+json")
    assert int(bloqueada.headers["Retry-After"]) >= 1
    assert bloqueada.json()["type"].endswith("/rate-limit")


def test_health_e_metrics_ficam_fora_do_limite():
    """Monitoramento não pode ser bloqueado pelo rate limit."""
    app = FastAPI()
    registrar(app)
    app.add_middleware(MiddlewareRateLimit, expressao="1/minute")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    cliente = TestClient(app)
    codigos = [cliente.get("/health").status_code for _ in range(5)]

    assert codigos == [200] * 5
    assert "/health" in ROTAS_ISENTAS


# ------------------------------------------------------------ observabilidade
def test_metricas_contam_por_metodo_rota_e_status():
    metricas = Metricas()

    metricas.registrar("GET", "/usinas", 200, 12.0)
    metricas.registrar("GET", "/usinas", 200, 30.0)
    metricas.registrar("GET", "/usinas", 404, 3.0)

    assert metricas.requisicoes[("GET", "/usinas", 200)] == 2
    assert metricas.requisicoes[("GET", "/usinas", 404)] == 1
    assert metricas.latencia_soma_ms["/usinas"] == 45.0
    assert metricas.latencia_contagem["/usinas"] == 3


def test_faixas_de_latencia_sao_cumulativas():
    """Histograma do Prometheus: o bucket `le` conta tudo abaixo dele."""
    metricas = Metricas()
    for duracao in (3.0, 12.0, 300.0):
        metricas.registrar("GET", "/x", 200, duracao)

    assert metricas.latencia_faixas[("/x", 5)] == 1
    assert metricas.latencia_faixas[("/x", 25)] == 2
    assert metricas.latencia_faixas[("/x", 500)] == 3


def test_exposicao_prometheus_tem_help_type_e_as_series():
    metricas = Metricas()
    metricas.registrar("GET", "/usinas", 200, 10.0)

    texto = metricas.prometheus()

    assert "# HELP solarwatch_requisicoes_total" in texto
    assert "# TYPE solarwatch_requisicoes_total counter" in texto
    assert 'solarwatch_requisicoes_total{metodo="GET",rota="/usinas",status="200"} 1' in texto
    assert 'solarwatch_latencia_ms_count{rota="/usinas"} 1' in texto
    assert texto.endswith("\n")


def test_metricas_vazias_ainda_expoem_o_uptime():
    texto = Metricas().prometheus()

    assert "solarwatch_uptime_segundos" in texto


def test_request_id_do_cliente_e_reaproveitado():
    """Permite correlacionar o log do cliente com o do servidor."""
    app = FastAPI()
    app.add_middleware(MiddlewareObservabilidade)

    @app.get("/x")
    def x():
        return {}

    resposta = TestClient(app).get("/x", headers={CABECALHO_REQUEST_ID: "abc123"})

    assert resposta.headers[CABECALHO_REQUEST_ID] == "abc123"


def test_request_id_e_gerado_quando_nao_vem():
    app = FastAPI()
    app.add_middleware(MiddlewareObservabilidade)

    @app.get("/x")
    def x():
        return {}

    cliente = TestClient(app)
    um = cliente.get("/x").headers[CABECALHO_REQUEST_ID]
    outro = cliente.get("/x").headers[CABECALHO_REQUEST_ID]

    assert um and outro and um != outro


def test_log_de_acesso_sai_em_json_com_os_campos_do_design(caplog):
    app = FastAPI()
    app.add_middleware(MiddlewareObservabilidade)

    @app.get("/x")
    def x():
        return {}

    with caplog.at_level(logging.INFO, logger="backend.acesso"):
        TestClient(app).get("/x")

    registro = next(r for r in caplog.records if r.name == "backend.acesso")
    corpo = json.loads(FormatadorJSON().format(registro))
    assert corpo["method"] == "GET" and corpo["path"] == "/x"
    assert corpo["status_code"] == 200 and corpo["latency_ms"] >= 0
    assert corpo["request_id"]


def test_formatador_json_serializa_excecao_e_tipos_estranhos():
    from pathlib import Path

    registro = logging.LogRecord("t", logging.ERROR, "f", 1, "falhou", None, None)
    registro.extra_json = {"caminho": Path("a/b")}
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        registro.exc_info = sys.exc_info()

    corpo = json.loads(FormatadorJSON().format(registro))

    assert corpo["mensagem"] == "falhou"
    assert corpo["caminho"] == "a/b" or corpo["caminho"].endswith("b")
    assert "ValueError" in corpo["excecao"]


def test_log_nunca_carrega_os_headers_da_requisicao(caplog):
    """§12.1: header completo não vai para o log, para não vazar nada incidental."""
    app = FastAPI()
    app.add_middleware(MiddlewareObservabilidade)

    @app.get("/x")
    def x():
        return {}

    with caplog.at_level(logging.INFO, logger="backend.acesso"):
        TestClient(app).get("/x", headers={"Authorization": "Bearer segredo-123"})

    texto = "\n".join(json.dumps(getattr(r, "extra_json", {})) for r in caplog.records)
    assert "segredo-123" not in texto
    assert "Authorization" not in texto


# ------------------------------------------------------------------ serviços
def test_registros_converte_nulos_do_pandas_em_none():
    """NaN vira `NaN` no JSON (inválido) e NaT quebra o Pydantic."""
    import numpy as np
    import pandas as pd

    df = pd.DataFrame({"a": [1.0, np.nan], "b": [pd.Timestamp("2026-01-01"), pd.NaT],
                       "c": ["x", None]})

    registros = _registros(df)

    assert registros[1] == {"a": None, "b": None, "c": None}
    assert "nan" not in json.dumps(registros, default=str).lower()


def test_registros_de_dataframe_vazio_e_lista_vazia():
    import pandas as pd

    assert _registros(pd.DataFrame()) == []


def test_janela_recusa_inicio_depois_do_fim():
    from datetime import date

    with pytest.raises(HTTPException) as erro:
        _checar_janela(date(2026, 3, 1), date(2026, 2, 1))

    assert erro.value.status_code == 422
    assert "anterior" in erro.value.detail


def test_janela_recusa_periodo_maior_que_o_maximo():
    from datetime import date, timedelta

    maximo = configuracao().maximo_dias_serie
    inicio = date(2026, 1, 1)

    _checar_janela(inicio, inicio + timedelta(days=maximo))        # na borda, passa
    with pytest.raises(HTTPException, match="excede o máximo"):
        _checar_janela(inicio, inicio + timedelta(days=maximo + 1))


def test_janela_aberta_e_aceita():
    """Sem início ou sem fim, a checagem não se aplica."""
    from datetime import date

    _checar_janela(None, None)
    _checar_janela(date(2026, 1, 1), None)
    _checar_janela(None, date(2026, 1, 1))


# ---------------------------------------------------------------- configuração
def test_configuracao_le_variavel_de_ambiente(monkeypatch):
    monkeypatch.setenv("SOLARWATCH_LIMITE_PADRAO_PAGINA", "7")
    monkeypatch.setenv("SOLARWATCH_AMBIENTE", "producao")

    config = Configuracao()

    assert config.limite_padrao_pagina == 7
    assert config.ambiente == "producao"


def test_configuracao_e_cacheada():
    """Uma instância por processo: `configuracao()` é chamada em toda requisição."""
    assert configuracao() is configuracao()


def test_cors_nunca_e_curinga():
    """§11 (API8): a origem permitida é explícita."""
    assert "*" not in configuracao().origens_permitidas


def test_limite_padrao_nao_passa_do_maximo():
    config = configuracao()

    assert config.limite_padrao_pagina <= config.limite_maximo_pagina
