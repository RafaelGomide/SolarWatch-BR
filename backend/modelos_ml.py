"""Carga dos modelos de ML no startup, com degradação graciosa.

System design §10.5: se um modelo não carregar, os endpoints de dado bruto
continuam funcionando normalmente e **apenas** os endpoints dependentes daquele
modelo respondem 503 com Problem Details. O processo nunca cai por causa disso.

Modelos:
- `sobrevivencia_cox.pkl`  → P(sem manutenção em N meses), por usina;
- `previsao_<fonte>.pkl`   → geração horária das próximas 24 h, por fonte.

O unpickle precisa que as classes originais sejam importáveis
(`ML.analise_sobrevivencia.modelos`, `ML.series_temporais.modelos`), por isso a
raiz do repositório precisa estar no `sys.path` — o que acontece naturalmente ao
rodar `uvicorn backend.main:app` a partir da raiz.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

log = logging.getLogger("backend.modelos")

FONTES_PREVISAO = ("solar", "eolica")


class RegistroModelos:
    """Guarda os modelos carregados e o motivo de cada falha."""

    def __init__(self) -> None:
        self.sobrevivencia: Any | None = None
        self.previsao: dict[str, Any] = {}
        self.probabilidades: pd.DataFrame | None = None
        self.falhas: dict[str, str] = {}

    # ------------------------------------------------------------------ carga
    def carregar(self, pasta: Path, nome_sobrevivencia: str, padrao_previsao: str,
                 nome_probabilidades: str) -> None:
        self._carregar_sobrevivencia(pasta / nome_sobrevivencia)
        for fonte in FONTES_PREVISAO:
            self._carregar_previsao(fonte, pasta / padrao_previsao.format(fonte=fonte))
        self._carregar_probabilidades(pasta / nome_probabilidades)

    def _carregar_sobrevivencia(self, caminho: Path) -> None:
        try:
            self.sobrevivencia = joblib.load(caminho)
            log.info("[modelos] sobrevivência carregada (%s, horizontes %s)",
                     caminho.name, self.sobrevivencia.horizontes_meses)
        except Exception as erro:
            self.falhas["sobrevivencia"] = f"{type(erro).__name__}: {erro}"
            log.warning("[modelos] sobrevivência indisponível: %s", erro)

    def _carregar_previsao(self, fonte: str, caminho: Path) -> None:
        try:
            self.previsao[fonte] = joblib.load(caminho)
            log.info("[modelos] previsão '%s' carregada (%s, horizonte %d h)",
                     fonte, self.previsao[fonte].nome, self.previsao[fonte].horizonte)
        except Exception as erro:
            self.falhas[f"previsao_{fonte}"] = f"{type(erro).__name__}: {erro}"
            log.warning("[modelos] previsão '%s' indisponível: %s", fonte, erro)

    def _carregar_probabilidades(self, caminho: Path) -> None:
        """Tabela pré-calculada: atalho rápido quando a usina já está nela."""
        try:
            self.probabilidades = pd.read_parquet(caminho).set_index("usina_id")
            log.info("[modelos] probabilidades pré-calculadas: %d usinas",
                     len(self.probabilidades))
        except Exception as erro:
            self.falhas["probabilidades"] = f"{type(erro).__name__}: {erro}"
            log.warning("[modelos] probabilidades pré-calculadas indisponíveis: %s", erro)

    # ----------------------------------------------------------------- estado
    @property
    def sobrevivencia_disponivel(self) -> bool:
        return self.sobrevivencia is not None or self.probabilidades is not None

    def previsao_disponivel(self, fonte: str) -> bool:
        return fonte in self.previsao

    def estado(self) -> dict:
        return {
            "sobrevivencia": self.sobrevivencia is not None,
            "probabilidades_pre_calculadas": self.probabilidades is not None,
            "previsao": {f: f in self.previsao for f in FONTES_PREVISAO},
            "falhas": self.falhas,
        }


registro = RegistroModelos()
