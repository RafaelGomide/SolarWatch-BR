"""Logs estruturados e métricas (system design §12).

- **Logs JSON** com `timestamp`, `method`, `path`, `status_code`, `latency_ms`,
  `client_ip` e `request_id` (correlation id por request). Headers completos
  nunca são logados, para não vazar nada incidental (§12.1).
- **Métricas** em `/metrics`, no formato texto do Prometheus, mantidas em
  memória do processo. Não há stack de observabilidade 24/7 no orçamento zero
  (§12.2): as métricas ficam expostas para inspeção sob demanda.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

CABECALHO_REQUEST_ID = "X-Request-ID"
_FAIXAS_LATENCIA = (5, 10, 25, 50, 100, 250, 500, 1000)


class FormatadorJSON(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        corpo = {
            "timestamp": self.formatTime(registro, "%Y-%m-%dT%H:%M:%S%z"),
            "nivel": registro.levelname,
            "logger": registro.name,
            "mensagem": registro.getMessage(),
        }
        corpo.update(getattr(registro, "extra_json", {}) or {})
        if registro.exc_info:
            corpo["excecao"] = self.formatException(registro.exc_info)
        return json.dumps(corpo, ensure_ascii=False, default=str)


def configurar_logs(nivel: int = logging.INFO, json_ativo: bool = True) -> None:
    manipulador = logging.StreamHandler(sys.stdout)
    manipulador.setFormatter(FormatadorJSON() if json_ativo
                             else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    raiz = logging.getLogger()
    raiz.handlers = [manipulador]
    raiz.setLevel(nivel)
    logging.getLogger("uvicorn.access").disabled = True  # o middleware já loga


class Metricas:
    """Contadores simples em memória, expostos no formato do Prometheus."""

    def __init__(self) -> None:
        self.requisicoes: dict[tuple[str, str, int], int] = defaultdict(int)
        self.latencia_soma_ms: dict[str, float] = defaultdict(float)
        self.latencia_contagem: dict[str, int] = defaultdict(int)
        self.latencia_faixas: dict[tuple[str, int], int] = defaultdict(int)
        self.inicio = time.time()

    def registrar(self, metodo: str, rota: str, status: int, duracao_ms: float) -> None:
        self.requisicoes[(metodo, rota, status)] += 1
        self.latencia_soma_ms[rota] += duracao_ms
        self.latencia_contagem[rota] += 1
        for faixa in _FAIXAS_LATENCIA:
            if duracao_ms <= faixa:
                self.latencia_faixas[(rota, faixa)] += 1

    def prometheus(self) -> str:
        linhas = [
            "# HELP solarwatch_uptime_segundos Tempo desde o start do processo",
            "# TYPE solarwatch_uptime_segundos gauge",
            f"solarwatch_uptime_segundos {time.time() - self.inicio:.1f}",
            "# HELP solarwatch_requisicoes_total Requisições por método, rota e status",
            "# TYPE solarwatch_requisicoes_total counter",
        ]
        for (metodo, rota, status), total in sorted(self.requisicoes.items()):
            linhas.append(
                f'solarwatch_requisicoes_total{{metodo="{metodo}",rota="{rota}",status="{status}"}} {total}')

        linhas += ["# HELP solarwatch_latencia_ms Latência acumulada por rota",
                   "# TYPE solarwatch_latencia_ms summary"]
        for rota, soma in sorted(self.latencia_soma_ms.items()):
            contagem = self.latencia_contagem[rota]
            linhas.append(f'solarwatch_latencia_ms_sum{{rota="{rota}"}} {soma:.2f}')
            linhas.append(f'solarwatch_latencia_ms_count{{rota="{rota}"}} {contagem}')
            for faixa in _FAIXAS_LATENCIA:
                acumulado = self.latencia_faixas[(rota, faixa)]
                linhas.append(f'solarwatch_latencia_ms_bucket{{rota="{rota}",le="{faixa}"}} {acumulado}')
        return "\n".join(linhas) + "\n"


metricas = Metricas()


class MiddlewareObservabilidade(BaseHTTPMiddleware):
    """Gera o request_id, mede a latência, registra a métrica e loga em JSON."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(CABECALHO_REQUEST_ID) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        inicio = time.perf_counter()

        resposta = await call_next(request)

        duracao_ms = (time.perf_counter() - inicio) * 1000
        rota = request.scope.get("route").path if request.scope.get("route") else request.url.path
        metricas.registrar(request.method, rota, resposta.status_code, duracao_ms)
        resposta.headers[CABECALHO_REQUEST_ID] = request_id
        resposta.headers["X-Response-Time-Ms"] = f"{duracao_ms:.1f}"

        logging.getLogger("backend.acesso").info(
            "%s %s -> %d", request.method, request.url.path, resposta.status_code,
            extra={"extra_json": {
                "method": request.method, "path": request.url.path, "rota": rota,
                "status_code": resposta.status_code, "latency_ms": round(duracao_ms, 2),
                "client_ip": request.client.host if request.client else None,
                "request_id": request_id,
            }},
        )
        return resposta
