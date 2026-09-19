"""Configuração da análise de sobrevivência de ativos."""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
EVENTOS = RAIZ / "dados" / "simulados" / "eventos_manutencao_simulados.parquet"
META_EVENTOS = EVENTOS.with_name(EVENTOS.stem + ".meta.json")
BANCO = RAIZ / "DB" / "solarwatch.duckdb"
CURATED = RAIZ / "dados" / "limpos" / "curated"
MODELOS = RAIZ / "ML" / "modelos"
RESULTADOS = Path(__file__).resolve().parent / "resultados"

# Centralização idêntica à do gerador (dados_simulados.py), para que os
# coeficientes estimados sejam diretamente comparáveis aos verdadeiros.
POTENCIA_REFERENCIA_MW = 30.0
ANO_REFERENCIA = 2018
SUBSISTEMA_REFERENCIA = "SE"

COVARIAVEIS = ["log_potencia_mw_c", "subsistema_NE", "subsistema_S", "subsistema_N",
               "ano_entrada_c"]
ESTRATO = "fonte"   # linha de base própria por fonte: os riscos NÃO são proporcionais entre elas

# Horizontes do card do frontend e do endpoint da API
HORIZONTES_MESES = (6, 12, 24, 36)

DISTRIBUICOES = ("weibull", "lognormal", "loglogistico", "exponencial")

ALPHA = 0.05
K_FOLDS = 5
SEED = 42
