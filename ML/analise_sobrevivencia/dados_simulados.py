"""Simulação de eventos de manutenção corretiva/falha por usina (dados sintéticos).

Não existe dataset público granular de falhas de usinas no Brasil. Este script
gera eventos *plausíveis* sobre o cadastro REAL da ANEEL (usinas solares e
eólicas em operação), para alimentar a análise de sobrevivência do projeto.

Modelo gerador — Weibull de riscos proporcionais (PH):

    h(t | x) = h0_fonte(t) * exp(beta · x)
    h0_fonte(t) = (k / lambda) * (t / lambda) ** (k - 1)

    Amostragem por inversão:  T = lambda * (E / exp(beta · x)) ** (1 / k),
    com E ~ Exponencial(1).

- `k` (forma) > 1 em ambas as fontes: risco crescente com a idade (desgaste).
- A linha de base é própria de cada fonte (k e lambda diferentes), então o
  risco de solar vs. eólica NÃO é proporcional — num Cox, use `fonte` como
  estrato, não como covariável.
- As covariáveis têm efeito proporcional conhecido (`BETA`), o que permite
  validar se o Cox/Weibull ajustado recupera os coeficientes verdadeiros.

Censura à direita (administrativa): cada usina é observada da data de entrada
em operação até a data do retrato da ANEEL. Se o tempo simulado até o evento
passa dessa data, a usina fica censurada (`evento = 0`, sem `data_evento`).

Saída:
    dados/simulados/eventos_manutencao_simulados.parquet
    dados/simulados/eventos_manutencao_simulados.meta.json  (parâmetros + semente)

Uso:
    python -m ML.analise_sobrevivencia.dados_simulados
    python -m ML.analise_sobrevivencia.dados_simulados --seed 7 --potencia-minima-mw 5
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ingestao.armazenamento import gravar_parquet

RAIZ = Path(__file__).resolve().parents[2]
ENTRADA = RAIZ / "dados" / "bruto" / "dados_aneel_bruto.parquet"
SAIDA = RAIZ / "dados" / "simulados" / "eventos_manutencao_simulados.parquet"

SEED_PADRAO = 42
POTENCIA_MINIMA_MW_PADRAO = 1.0  # exclui microssistemas de 1 kW (pessoas físicas)
DATA_MINIMA_ENTRADA = "1990-01-01"  # descarta sentinela 1900-01-03 e datas implausíveis

# Linha de base Weibull por fonte (tempo em anos)
# - forma k: > 1 = risco cresce com o desgaste
# - escala lambda: ~63% das usinas teriam o 1º evento grave até lambda anos
WEIBULL = {
    "eolica": {"k": 1.6, "lambda_anos": 5.5},  # caixa multiplicadora, pás, gerador
    "solar": {"k": 1.3, "lambda_anos": 7.0},   # inversores dominam as falhas
}

# Efeitos verdadeiros (log hazard ratio) das covariáveis
BETA = {
    "log_potencia_mw_c": 0.20,  # por unidade de ln(MW) acima de ln(30 MW): mais equipamentos
    "subsistema_NE": 0.25,      # salinidade, calor e poeira no Nordeste
    "subsistema_S": 0.10,       # rajadas e variação térmica no Sul
    "subsistema_N": 0.15,       # umidade e acesso logístico difícil
    "ano_entrada_c": -0.04,     # por ano após 2018: tecnologia mais nova, mais confiável
}
POTENCIA_REFERENCIA_MW = 30.0
ANO_REFERENCIA = 2018

# Tipo do evento, sorteado só quando ele ocorre (não afeta o tempo)
TIPOS_EVENTO = {
    "eolica": {"caixa_multiplicadora": 0.30, "sistema_eletrico": 0.30, "pas": 0.20, "gerador": 0.20},
    "solar": {"inversor": 0.55, "rastreador": 0.20, "modulos": 0.15, "transformador": 0.10},
}

# UF -> subsistema do SIN (mesma codificação de id_subsistema do ONS)
UF_SUBSISTEMA = {
    **dict.fromkeys(["AM", "PA", "AP", "RR", "TO", "MA"], "N"),
    **dict.fromkeys(["BA", "RN", "PI", "CE", "PE", "PB", "SE", "AL"], "NE"),
    **dict.fromkeys(["MG", "SP", "RJ", "ES", "GO", "DF", "MT", "MS", "RO", "AC"], "SE"),
    **dict.fromkeys(["RS", "SC", "PR"], "S"),
}
FONTE = {"UFV": "solar", "EOL": "eolica"}

log = logging.getLogger("dados_simulados")


def carregar_usinas(potencia_minima_mw: float) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Seleciona as usinas reais que servem de base para a simulação."""
    aneel = pd.read_parquet(ENTRADA)
    data_corte = pd.Timestamp(aneel["DatGeracaoConjuntoDados"].max())

    usinas = pd.DataFrame({
        "ceg": aneel["CodCEG"],
        "sig_tipo_geracao": aneel["SigTipoGeracao"],
        "fase": aneel["DscFaseUsina"],
        "id_estado": aneel["SigUFPrincipal"],
        "potencia_mw": pd.to_numeric(
            aneel["MdaPotenciaOutorgadaKw"]
            .str.replace(".", "", regex=False)
            .str.replace(",", ".", regex=False),
            errors="coerce",
        ) / 1000,
        "data_entrada_operacao": pd.to_datetime(aneel["DatEntradaOperacao"], errors="coerce"),
    })
    usinas = usinas[
        (usinas["fase"] == "Operação")
        & usinas["sig_tipo_geracao"].isin(list(FONTE))
        & (usinas["potencia_mw"] >= potencia_minima_mw)
        & (usinas["data_entrada_operacao"] >= DATA_MINIMA_ENTRADA)
        & (usinas["data_entrada_operacao"] < data_corte)
    ].drop(columns="fase")

    usinas["fonte"] = usinas["sig_tipo_geracao"].map(FONTE)
    usinas["id_subsistema"] = usinas["id_estado"].map(UF_SUBSISTEMA)
    sem_subsistema = usinas["id_subsistema"].isna()
    if sem_subsistema.any():
        log.warning("%d usinas sem subsistema mapeado, descartadas", sem_subsistema.sum())
        usinas = usinas[~sem_subsistema]

    return usinas.sort_values("ceg").reset_index(drop=True), data_corte


def _preditor_linear(usinas: pd.DataFrame) -> pd.Series:
    x = {
        "log_potencia_mw_c": np.log(usinas["potencia_mw"]) - np.log(POTENCIA_REFERENCIA_MW),
        "subsistema_NE": (usinas["id_subsistema"] == "NE").astype(float),
        "subsistema_S": (usinas["id_subsistema"] == "S").astype(float),
        "subsistema_N": (usinas["id_subsistema"] == "N").astype(float),
        "ano_entrada_c": usinas["data_entrada_operacao"].dt.year - ANO_REFERENCIA,
    }
    return sum(BETA[nome] * valor for nome, valor in x.items())


def simular(usinas: pd.DataFrame, data_corte: pd.Timestamp, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = usinas.copy()

    k = df["fonte"].map(lambda f: WEIBULL[f]["k"])
    escala = df["fonte"].map(lambda f: WEIBULL[f]["lambda_anos"])
    eta = _preditor_linear(df)

    # Inversão da função de sobrevivência Weibull-PH
    tempo_ate_evento = escala * (rng.exponential(size=len(df)) / np.exp(eta)) ** (1 / k)
    tempo_observavel = (data_corte - df["data_entrada_operacao"]).dt.days / 365.25

    df["evento"] = (tempo_ate_evento <= tempo_observavel).astype(int)
    df["tempo_anos"] = np.where(df["evento"] == 1, tempo_ate_evento, tempo_observavel)
    df["data_evento"] = df["data_entrada_operacao"] + pd.to_timedelta(
        np.where(df["evento"] == 1, tempo_ate_evento * 365.25, np.nan), unit="D"
    ).round("D")
    df["data_corte"] = data_corte

    tipos = pd.Series(pd.NA, index=df.index, dtype="string")
    for fonte, probs in TIPOS_EVENTO.items():
        alvo = (df["fonte"] == fonte) & (df["evento"] == 1)
        tipos[alvo] = rng.choice(list(probs), size=int(alvo.sum()), p=list(probs.values()))
    df["tipo_evento"] = tipos

    df.insert(0, "id_usina", np.arange(1, len(df) + 1))
    return df[[
        "id_usina", "ceg", "fonte", "sig_tipo_geracao", "id_estado", "id_subsistema",
        "potencia_mw", "data_entrada_operacao", "data_corte",
        "tempo_anos", "evento", "data_evento", "tipo_evento",
    ]]


def _gravar_metadados(saida: Path, seed: int, potencia_minima_mw: float, df: pd.DataFrame) -> None:
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": "Dados SINTÉTICOS de 1º evento de manutenção corretiva/falha por usina.",
        "fonte_usinas": str(ENTRADA.relative_to(RAIZ)),
        "seed": seed,
        "potencia_minima_mw": potencia_minima_mw,
        "data_minima_entrada": DATA_MINIMA_ENTRADA,
        "modelo": "Weibull de riscos proporcionais, linha de base por fonte",
        "weibull_por_fonte": WEIBULL,
        "beta_verdadeiro": BETA,
        "referencias_centralizacao": {
            "potencia_mw": POTENCIA_REFERENCIA_MW, "ano_entrada": ANO_REFERENCIA,
        },
        "tipos_evento": TIPOS_EVENTO,
        "n_usinas": len(df),
        "n_eventos": int(df["evento"].sum()),
    }
    caminho = saida.with_name(saida.stem + ".meta.json")
    caminho.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED_PADRAO)
    parser.add_argument("--potencia-minima-mw", type=float, default=POTENCIA_MINIMA_MW_PADRAO)
    args = parser.parse_args()

    usinas, data_corte = carregar_usinas(args.potencia_minima_mw)
    log.info("%d usinas reais da ANEEL como base (retrato de %s)", len(usinas), data_corte.date())

    eventos = simular(usinas, data_corte, args.seed)
    for fonte, grupo in eventos.groupby("fonte"):
        log.info("  %s: %d usinas, %d eventos, censura %.1f%%",
                 fonte, len(grupo), grupo["evento"].sum(), 100 * (1 - grupo["evento"].mean()))

    gravar_parquet(eventos, SAIDA)
    _gravar_metadados(SAIDA, args.seed, args.potencia_minima_mw, eventos)


if __name__ == "__main__":
    main()
