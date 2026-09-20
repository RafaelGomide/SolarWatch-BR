"""SolarWatch BR — API pública de leitura.

Monolito modular em FastAPI (system design §6.3): serve a API REST e, se a pasta
`frontend/` existir, também os estáticos — tudo no mesmo processo, como manda o
orçamento de uma instância única no free tier.

Rodar em desenvolvimento (a partir da raiz do repositório):

    uvicorn backend.main:app --reload

Documentação interativa: http://localhost:8000/docs
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import db, erros
from backend.config import configuracao
from backend.limites import MiddlewareRateLimit
from backend.modelos_ml import registro
from backend.observabilidade import MiddlewareObservabilidade, configurar_logs
from backend.rotas import geracao, sistema, usinas

log = logging.getLogger("backend")


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    """Abre o banco e carrega os modelos uma única vez, no startup.

    Falha no banco é fatal (sem ele não há API). Falha em modelo é registrada e
    degrada só os endpoints dependentes (§10.5).
    """
    cfg = configuracao()
    configurar_logs()
    log.info("[startup] SolarWatch BR %s (%s)", cfg.versao, cfg.ambiente)

    db.abrir(cfg.banco)
    registro.carregar(cfg.modelos, cfg.modelo_sobrevivencia, cfg.modelo_previsao,
                      cfg.probabilidades_sobrevivencia)
    if registro.falhas:
        log.warning("[startup] modo degradado: %s", list(registro.falhas))

    yield

    db.fechar()
    log.info("[shutdown] encerrado")


def criar_app() -> FastAPI:
    cfg = configuracao()
    app = FastAPI(
        title=cfg.titulo,
        version=cfg.versao,
        description=(
            "API pública de leitura sobre geração solar e eólica no Brasil: dados do ONS, "
            "clima da NASA POWER e cadastro da ANEEL, mais dois modelos de ML "
            "(previsão de geração e sobrevivência de ativos).\n\n"
            "**Somente leitura** — todos os endpoints são `GET`. Erros seguem "
            "[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457).\n\n"
            "⚠️ Os dados de manutenção usados em `/sobrevivencia` são **simulados**."
        ),
        lifespan=ciclo_de_vida,
        docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
    )

    # Rate limit por IP, token bucket em memória do processo (§5.9).
    # Implementação própria: ver o porquê em backend/limites.py
    app.add_middleware(MiddlewareRateLimit, expressao=cfg.rate_limit)

    app.add_middleware(MiddlewareObservabilidade)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.origens_permitidas,   # nunca "*" (§11, API8)
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-Ms",
                        "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    )

    erros.registrar(app)

    app.include_router(usinas.roteador, prefix=cfg.prefixo_api)
    app.include_router(geracao.roteador, prefix=cfg.prefixo_api)
    app.include_router(sistema.roteador, prefix=cfg.prefixo_api)
    app.include_router(sistema.roteador)   # /health e /metrics também sem prefixo

    if cfg.frontend.exists():
        app.mount("/app", StaticFiles(directory=cfg.frontend, html=True), name="frontend")
        log.info("[startup] frontend servido em /app a partir de %s", cfg.frontend)

    @app.get("/", include_in_schema=False)
    def raiz():
        return RedirectResponse("/docs")

    return app


app = criar_app()
