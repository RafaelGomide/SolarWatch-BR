"""Configuração do backend, via variáveis de ambiente (prefixo SOLARWATCH_)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ = Path(__file__).resolve().parents[1]


class Configuracao(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLARWATCH_", env_file=RAIZ / ".env",
                                      extra="ignore")

    # Identidade
    titulo: str = "SolarWatch BR API"
    versao: str = "1.0.0"
    prefixo_api: str = "/api/v1"          # versionamento por path (system design §5.7)
    ambiente: str = "desenvolvimento"

    # Dados e modelos
    banco: Path = RAIZ / "DB" / "solarwatch.duckdb"
    modelos: Path = RAIZ / "ML" / "modelos"
    frontend: Path = RAIZ / "frontend"     # servido se existir (§6.2)

    # Limites de proteção (o free tier tem CPU escassa)
    rate_limit: str = "60/minute"          # token bucket por IP (§5.9)
    limite_padrao_pagina: int = 50
    limite_maximo_pagina: int = 200
    maximo_dias_serie: int = 120           # janela máxima de /geracao e /clima

    # CORS restrito ao frontend, nunca "*" (§11, API8)
    origens_permitidas: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
    )

    # Modelos de ML
    modelo_sobrevivencia: str = "sobrevivencia_cox.pkl"
    modelo_recorrencia: str = "sobrevivencia_recorrencia.pkl"
    modelo_previsao: str = "previsao_{fonte}.pkl"
    probabilidades_sobrevivencia: str = "sobrevivencia_probabilidades_por_usina.parquet"
    esperadas_recorrencia: str = "recorrentes_esperadas_por_usina.parquet"

    @property
    def url_erros(self) -> str:
        return "https://solarwatch.example/errors"


@lru_cache
def configuracao() -> Configuracao:
    return Configuracao()
