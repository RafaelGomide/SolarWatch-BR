"""Schemas de resposta (Pydantic v2). São eles que geram o OpenAPI de /docs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Fonte = Literal["solar", "eolica"]
Regiao = Literal["N", "NE", "SE", "S"]


class ProblemDetails(BaseModel):
    """RFC 9457 — formato único de erro da API."""
    type: str = Field(examples=["https://solarwatch.example/errors/not-found"])
    title: str = Field(examples=["Recurso não encontrado"])
    status: int = Field(examples=[404])
    detail: str = Field(examples=["Nenhuma usina com id=9999"])
    instance: str = Field(examples=["/api/v1/usinas/9999"])
    request_id: str | None = None


class Usina(BaseModel):
    usina_id: int
    nome: str
    fonte: Fonte
    regiao: Regiao
    id_estado: str | None = None
    municipio: str | None = None
    potencia_mw: float | None = Field(
        None, description="Nula quando o vínculo ONS×ANEEL não é confiável (ver qualidade_vinculo)")
    lat: float | None = None
    lon: float | None = None
    data_operacao: date | None = None
    tipo_unidade: str
    qualidade_vinculo: str


class UsinaDetalhe(Usina):
    n_usinas_aneel: int
    pico_geracao_mw: float | None = None
    primeira_medicao_utc: datetime | None = None
    ultima_medicao_utc: datetime | None = None


class PaginaUsinas(BaseModel):
    data: list[Usina]
    next_cursor: str | None = Field(None, description="Cursor da próxima página (base64)")
    total_estimado: int


class PontoGeracao(BaseModel):
    timestamp: datetime = Field(description="Início da hora, em UTC")
    energia_mwh: float | None = Field(description="Nulo quando a fonte não publicou o dado")
    flag_qualidade: str = Field(description="original | interpolado | negativo_zerado | faltante")


class SerieGeracao(BaseModel):
    usina_id: int
    nome: str
    fonte: Fonte
    inicio: datetime | None
    fim: datetime | None
    total_mwh: float
    horas: int
    data: list[PontoGeracao]


class PontoGeracaoNacional(BaseModel):
    periodo: datetime
    fonte: Fonte
    energia_mwh: float
    usinas: int


class GeracaoNacional(BaseModel):
    granularidade: Literal["hora", "dia"]
    inicio: datetime | None
    fim: datetime | None
    data: list[PontoGeracaoNacional]


class PontoClima(BaseModel):
    data: date
    irradiancia_kwh_m2: float | None
    vento_ms: float | None = Field(description="Vento médio a 50 m")
    temperatura_c: float | None
    temperatura_max_c: float | None = None
    temperatura_min_c: float | None = None
    flag_qualidade: str
    medidas_faltantes: str | None = Field(
        default=None,
        description="Variáveis sem valor no dia, separadas por vírgula (latência de publicação da NASA POWER)")


class SerieClima(BaseModel):
    usina_id: int
    local_clima: str = Field(description="Ponto NASA POWER usado como referência")
    distancia_km: float | None
    metodo_vinculo_clima: str
    aviso: str
    data: list[PontoClima]


class PontoPrevisao(BaseModel):
    timestamp: datetime
    energia_mwh_prevista: float


class Previsao(BaseModel):
    fonte: Fonte
    usina_id: int | None = None
    modelo: str
    horizonte_h: int
    origem: datetime = Field(description="Última hora observada; a previsão começa depois dela")
    metodo: str = Field(description="modelo_por_fonte | rateio_proporcional")
    participacao_usina: float | None = Field(
        None, description="Fração da geração da fonte atribuída à usina (só em rateio)")
    premissa_clima: str
    metricas_backtesting: dict | None = None
    data: list[PontoPrevisao]


class ProbabilidadeHorizonte(BaseModel):
    horizonte_meses: int
    horizonte_dias: int
    probabilidade_sobrevivencia: float


class Sobrevivencia(BaseModel):
    usina_id: int
    fonte: Fonte
    idade_anos: float | None
    condicional_na_idade: bool
    risco_relativo: float | None = None
    tempo_mediano_anos: float | None = None
    metodo_extrapolacao: str | None = None
    simulado: bool = Field(True, description="SEMPRE verdadeiro: os eventos de manutenção são sintéticos")
    aviso: str
    horizontes: list[ProbabilidadeHorizonte]


class ManutencoesHorizonte(BaseModel):
    horizonte_meses: int
    horizonte_dias: int
    manutencoes_esperadas: float = Field(
        description="Número esperado de manutenções corretivas no horizonte, não uma probabilidade")


class Recorrencia(BaseModel):
    usina_id: int
    fonte: Fonte
    idade_anos: float | None
    taxa_relativa: float | None = Field(
        None, description="Taxa de eventos da usina face à usina de referência (Andersen-Gill)")
    modelo: str = Field("andersen_gill + MCF", description="Modelos combinados na estimativa")
    metodo: str = Field(description="Como o número é produzido, incluindo a aproximação assumida")
    simulado: bool = Field(True, description="SEMPRE verdadeiro: os eventos de manutenção são sintéticos")
    aviso: str
    horizontes: list[ManutencoesHorizonte]


class Saude(BaseModel):
    status: Literal["ok", "degradado"]
    versao: str
    ambiente: str
    banco: bool
    modelos: dict
    cobertura: dict | None = None
