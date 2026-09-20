"""Endpoints operacionais: saúde, métricas e metadados do dataset."""

from __future__ import annotations

from fastapi import APIRouter, Response

from backend import db
from backend.config import configuracao
from backend.modelos_ml import registro
from backend.observabilidade import metricas
from backend.schemas import Saude

roteador = APIRouter(tags=["sistema"])


@roteador.get("/health", response_model=Saude, summary="Saúde do serviço")
def saude():
    """`degradado` quando o banco está de pé mas algum modelo não carregou —
    a API continua servindo os dados históricos (system design §10.5)."""
    banco_ok = db.esta_disponivel()
    modelos = registro.estado()
    tudo_ok = banco_ok and not modelos["falhas"]
    return {
        "status": "ok" if tudo_ok else "degradado",
        "versao": configuracao().versao,
        "ambiente": configuracao().ambiente,
        "banco": banco_ok,
        "modelos": modelos,
        "cobertura": db.cobertura() if banco_ok else None,
    }


@roteador.get("/metrics", summary="Métricas no formato Prometheus",
              response_class=Response, include_in_schema=True)
def obter_metricas():
    return Response(metricas.prometheus(), media_type="text/plain; version=0.0.4")
