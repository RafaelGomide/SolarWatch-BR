"""Erros no formato Problem Details (RFC 9457), como manda o system design §5.4.

Toda resposta de erro tem o mesmo corpo e o content-type `application/problem+json`:

    {"type": ..., "title": ..., "status": 404, "detail": ..., "instance": "/api/v1/usinas/9999"}
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.config import configuracao

log = logging.getLogger("backend.erros")

# O nome da constante 422 mudou entre versões do Starlette; o literal é estável
CODIGO_VALIDACAO = 422

TIPOS = {
    status.HTTP_404_NOT_FOUND: ("not-found", "Recurso não encontrado"),
    CODIGO_VALIDACAO: ("validation-error", "Parâmetro inválido"),
    status.HTTP_429_TOO_MANY_REQUESTS: ("rate-limit", "Limite de requisições excedido"),
    status.HTTP_503_SERVICE_UNAVAILABLE: ("service-unavailable", "Recurso temporariamente indisponível"),
    status.HTTP_500_INTERNAL_SERVER_ERROR: ("internal-error", "Erro interno"),
}


class ProblemaHTTP(StarletteHTTPException):
    """HTTPException que carrega um `detail` já pensado para o Problem Details."""


def problema(request: Request, codigo: int, detalhe: str, titulo: str | None = None) -> JSONResponse:
    slug, titulo_padrao = TIPOS.get(codigo, ("erro", "Erro"))
    corpo = {
        "type": f"{configuracao().url_erros}/{slug}",
        "title": titulo or titulo_padrao,
        "status": codigo,
        "detail": detalhe,
        "instance": request.url.path,
    }
    if (request_id := getattr(request.state, "request_id", None)) is not None:
        corpo["request_id"] = request_id
    return JSONResponse(corpo, status_code=codigo, media_type="application/problem+json")


def registrar(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return problema(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validacao(request: Request, exc: RequestValidationError):
        erros = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()
        )
        return problema(request, CODIGO_VALIDACAO, erros or "Parâmetro inválido")

    @app.exception_handler(Exception)
    async def _inesperado(request: Request, exc: Exception):
        # O detalhe técnico fica no log, nunca no corpo da resposta
        log.exception("erro não tratado em %s", request.url.path)
        return problema(request, status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "Erro inesperado ao processar a requisição.")
