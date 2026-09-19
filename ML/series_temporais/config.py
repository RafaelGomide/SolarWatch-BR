"""Configuração da previsão de geração (séries temporais)."""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
BANCO = RAIZ / "DB" / "solarwatch.duckdb"
CURATED = RAIZ / "dados" / "limpos" / "curated"
MODELOS = RAIZ / "ML" / "modelos"

FUSO_BRASIL = "America/Sao_Paulo"
FREQUENCIA = "h"

# Horizonte de previsão: 24 h à frente (o dia seguinte inteiro).
# Toda feature derivada do alvo usa defasagem >= HORIZONTE_H, então a previsão
# de qualquer hora do horizonte só depende de dado disponível no momento do
# corte. É isso que permite prever 24 h de uma vez, sem recursão e sem leakage.
HORIZONTE_H = 24

# Backtesting com janela deslizante: N janelas consecutivas de HORIZONTE_H horas,
# cada uma prevista por um modelo treinado só com o passado daquela origem.
N_JANELAS = 6
MINIMO_TREINO_H = 24 * 30  # 30 dias de histórico mínimo antes da 1ª previsão

# Features do Gradient Boosting (todas com defasagem >= HORIZONTE_H)
LAGS_H = [24, 25, 26, 48, 72, 168]        # mesma hora de 1, 2, 3 e 7 dias atrás
JANELAS_MOVEIS_H = [24, 72, 168]          # médias móveis, deslocadas do horizonte

# SARIMA: sazonalidade diária (24 h). Ordens pequenas por causa do custo —
# (1,0,1)(1,1,1,24) leva ~35 s por ajuste em 1.900 pontos.
SARIMA_ORDEM = (1, 0, 1)
SARIMA_SAZONAL = (1, 1, 1, 24)

# MAPE só é calculado acima deste piso (% da média da série). A geração solar
# é zero à noite (26,6% das horas), e MAPE com y≈0 explode para infinito.
PISO_MAPE_FRACAO_MEDIA = 0.05

SEED = 42
