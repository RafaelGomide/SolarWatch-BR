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

Fragilidade (frailty) gama
--------------------------
Duas usinas com a mesma potência, região e ano de entrada não são iguais: o
fabricante do equipamento, a qualidade da montagem e o regime de operação não
estão no cadastro público. O modelo representa isso com um fator aleatório por
usina, comum a todos os seus episódios:

    h(t | x, Z) = Z * h0_fonte(t) * exp(beta · x),   Z ~ Gama(1/theta, theta)

`Z` tem média 1 e variância `theta`, então ele não desloca o risco médio, só o
**espalha**: com o padrão `theta = 0,5`, metade das usinas tem `Z < 0,8` e 10%
tem `Z > 2` — usinas "problemáticas" que quebram mais do que suas covariáveis
explicam. `--variancia-frailty 0` desliga (todas as usinas com `Z = 1`).

`Z` é **latente**: aparece no arquivo como `frailty` só porque o dado é
simulado, para permitir validar o estimador. Não é covariável do modelo — num
dado real ele não seria observável.

Com frailty, o risco marginal (integrando `Z`) deixa de ser proporcional mesmo
com covariáveis constantes, e o Cox comum **atenua** os coeficientes: é o
efeito conhecido de heterogeneidade não observada.

Censura à direita, por dois mecanismos:

1. **Administrativa:** cada usina é observada da entrada em operação até a data
   do retrato da ANEEL. Quem não falhou até lá fica censurado.
2. **Aleatória (perda de acompanhamento):** `C ~ Exponencial(taxa)`, independente
   do tempo até o evento. Representa o que tira a usina da observação sem ser
   falha — descomissionamento, venda com troca de operador, saída do dado
   público, repotenciação que zera o histórico.

O tempo observado é `min(T, C_administrativa, C_aleatória)` e `evento = 1` só
quando o mínimo é `T`. A coluna `motivo_censura` registra qual dos três venceu.

A censura aleatória é **independente** do tempo até a falha por construção
(sorteada sem olhar para `T` nem para as covariáveis). Isso é o que a análise de
sobrevivência supõe, e é o que torna Kaplan-Meier e Cox consistentes; censura
*informativa* — por exemplo, tirar da observação justo as usinas que estão
prestes a falhar — enviesaria tudo, e não é o caso aqui.

Eventos RECORRENTES
-------------------
Manutenção corretiva não acontece uma vez só: a usina é reparada e volta a
operar sob risco. Além do 1º evento, o script gera o **processo completo** de
cada usina, como um processo de renovação com deterioração:

    gap_j ~ Weibull(k_fonte, lambda_fonte),  com  eta_j = beta·x + gamma*(j-1)

ou seja, o relógio zera a cada reparo ("as good as repaired"), mas cada
episódio novo carrega um risco `exp(gamma)` vezes maior que o anterior — o
reparo não devolve a usina ao estado de fábrica. O processo de cada usina segue
até ultrapassar a data do retrato, e o último episódio fica censurado.

O episódio 1 do arquivo recorrente é, por construção, **o mesmo** evento do
arquivo de 1º evento: os episódios seguintes são gerados a partir dele, então
os dois arquivos são consistentes e o de 1º evento continua idêntico ao de
antes para a mesma semente.

Variante com efeito tempo-dependente
------------------------------------
O conjunto principal satisfaz riscos proporcionais **por construção** dentro de
cada fonte, o que o torna inútil para testar se um diagnóstico de PH funciona:
não há violação para detectar. O script gera também uma variante em que o efeito
de uma covariável **muda no tempo**, com violação de tamanho conhecido:

    beta(t) = BETA_ANTES  se t <= CORTE_ANOS,  BETA_DEPOIS  caso contrário

Um efeito constante por partes (e não uma função contínua do tempo) é de
propósito: mantém a inversão da Weibull exata em cada trecho, sem integração
numérica, e é exatamente a alternativa que os testes de Schoenfeld têm em mente.

Variante com riscos competitivos
--------------------------------
No conjunto principal o `tipo_evento` é sorteado **depois** que o tempo já foi
decidido, e por isso não carrega informação: a causa é um rótulo, não um
mecanismo. A variante de riscos competitivos inverte isso — cada causa tem seu
próprio relógio Weibull, e vence a que chegar primeiro:

    T = min_c T_c,   T_c ~ Weibull(k_c, lambda_c) · exp(-eta/k_c),   causa = argmin

Com formas `k_c` diferentes por causa, a composição das falhas **muda com a
idade**: defeito elétrico e de inversor aparece cedo, desgaste mecânico e
degradação de módulo aparece tarde. É o dado certo para modelos de
riscos competitivos (hazard específico por causa, Fine-Gray) e para mostrar por
que `1 − KM` superestima a incidência de uma causa isolada.

Variante com covariáveis das outras fontes do projeto
-----------------------------------------------------
As covariáveis acima vêm todas do cadastro. A variante `--com-clima` acrescenta
três que vêm do resto do pipeline:

- **vento a 50 m** e **temperatura**, médias do ponto NASA POWER mais próximo
  da coordenada da usina (camada clean do ETL);
- **fator de capacidade real**, calculado da geração do ONS para as usinas que
  o vínculo ONS×ANEEL alcança; nas demais, imputado pela média da fonte e
  subsistema, com a coluna `fator_capacidade_imputado` marcando quais.

A história física: mais vento e fator de capacidade mais alto significam mais
ciclos de carga e mais horas sob esforço; temperatura mais alta degrada
eletrônica de potência. Os três entram com efeito conhecido (`BETA_EXTERNO`),
então continuam sendo recuperáveis por um Cox que os inclua.

Saída:
    dados/simulados/eventos_manutencao_simulados.parquet       (1 linha por usina)
    dados/simulados/eventos_manutencao_simulados.meta.json
    dados/simulados/eventos_manutencao_recorrentes.parquet     (1 linha por episódio)
    dados/simulados/eventos_manutencao_recorrentes.meta.json
    dados/simulados/eventos_manutencao_ph_violado.parquet      (variante de diagnóstico)
    dados/simulados/eventos_manutencao_ph_violado.meta.json
    dados/simulados/eventos_manutencao_competitivos.parquet    (variante de riscos competitivos)
    dados/simulados/eventos_manutencao_competitivos.meta.json
    dados/simulados/eventos_manutencao_clima.parquet           (com covariáveis ONS/NASA)
    dados/simulados/eventos_manutencao_clima.meta.json

Uso:
    python -m ML.analise_sobrevivencia.dados_simulados
    python -m ML.analise_sobrevivencia.dados_simulados --seed 7 --potencia-minima-mw 5
    python -m ML.analise_sobrevivencia.dados_simulados --gamma-recorrencia 0.0
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
SAIDA_RECORRENTES = RAIZ / "dados" / "simulados" / "eventos_manutencao_recorrentes.parquet"
SAIDA_PH_VIOLADO = RAIZ / "dados" / "simulados" / "eventos_manutencao_ph_violado.parquet"
SAIDA_COMPETITIVOS = RAIZ / "dados" / "simulados" / "eventos_manutencao_competitivos.parquet"
SAIDA_CLIMA = RAIZ / "dados" / "simulados" / "eventos_manutencao_clima.parquet"
CLEAN = RAIZ / "dados" / "limpos" / "clean"
CURATED = RAIZ / "dados" / "limpos" / "curated"

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

# Variante de diagnóstico: efeito de uma covariável que muda no tempo.
# O valor de "antes" é grande e o de "depois" troca de sinal — uma violação
# gritante, escolhida para medir o PISO de detecção do teste: se o Schoenfeld
# não pega esta, não pega nenhuma.
EFEITO_TEMPO_DEPENDENTE = {
    "covariavel": "log_potencia_mw_c",
    "beta_antes": 0.60,    # usina grande falha muito mais nos primeiros anos
    "beta_depois": -0.10,  # depois de amaciada, o porte deixa de pesar
    "corte_anos": 3.0,
}

# Censura aleatória: taxa anual da exponencial de perda de acompanhamento.
# 0,03/ano = tempo médio de 33 anos até sair da observação, o que com ~6 anos de
# acompanhamento médio tira da ordem de 10% das usinas. 0 desliga.
TAXA_CENSURA_ALEATORIA = 0.03

# Fragilidade: variância do fator aleatório por usina (média sempre 1).
# 0,5 = desvio-padrão de 0,71; 0 desliga a heterogeneidade não observada.
VARIANCIA_FRAILTY = 0.5

# Recorrência: cada episódio após o 1º tem risco exp(GAMMA) vezes o anterior.
# 0,15 = +16% de risco por reparo acumulado. Com gamma = 0 o processo vira uma
# renovação pura (reparo perfeito), útil para comparar.
GAMMA_RECORRENCIA = 0.15
# A deterioração satura: depois de tantos reparos a usina já está no seu pior
# estado, e continuar multiplicando o risco levaria a centenas de eventos numa
# usina de alta fragilidade — que o gerador produziria, mas nenhuma operação real.
SATURACAO_DETERIORACAO = 10
# Trava de segurança contra um processo que dispare. Com gamma > 0 os gaps
# encurtam a cada episódio, então uma usina muito antiga e de alto risco pode
# acumular dezenas de eventos; a trava só evita laço sem fim, e é avisada em log.
MAX_EPISODIOS = 60

# Tipo do evento, sorteado só quando ele ocorre (não afeta o tempo)
TIPOS_EVENTO = {
    "eolica": {"caixa_multiplicadora": 0.30, "sistema_eletrico": 0.30, "pas": 0.20, "gerador": 0.20},
    "solar": {"inversor": 0.55, "rastreador": 0.20, "modulos": 0.15, "transformador": 0.10},
}

# Covariáveis vindas do ONS e da NASA (variante --com-clima), com efeito conhecido.
# Centralizadas em valores típicos do parque para que o intercepto não mude de
# significado e os coeficientes sejam lidos como desvio em torno do típico.
BETA_EXTERNO = {
    "vento_50m_c": 0.08,          # por m/s acima de 7: mais ciclos de carga
    "temperatura_c": 0.05,        # por °C acima de 25: degrada eletrônica de potência
    "fator_capacidade_c": 1.20,   # por unidade acima de 0,35 (= +0,12 a cada 10 p.p.)
}
REFERENCIAS_EXTERNAS = {"vento_50m_ms": 7.0, "temperatura_2m_c": 25.0, "fator_capacidade": 0.35}

# Riscos competitivos: um relógio Weibull por causa. A FORMA é o que distingue
# os mecanismos — k < 1,5 falha cedo (defeito de fabricação, eletrônica),
# k > 1,8 falha tarde (desgaste, degradação). As escalas foram calibradas para
# que a mistura de causas observada fique próxima de TIPOS_EVENTO e a mediana
# do tempo total fique próxima da linha de base da fonte.
CAUSAS_COMPETITIVAS = {
    "eolica": {
        "sistema_eletrico": {"k": 1.0, "lambda_anos": 22.0},   # aleatório, sem desgaste
        "caixa_multiplicadora": {"k": 2.2, "lambda_anos": 13.0},  # desgaste mecânico
        "pas": {"k": 1.8, "lambda_anos": 17.0},                # fadiga, erosão de borda
        "gerador": {"k": 1.4, "lambda_anos": 22.0},
    },
    "solar": {
        "inversor": {"k": 1.1, "lambda_anos": 14.0},           # eletrônica de potência
        "rastreador": {"k": 1.7, "lambda_anos": 20.0},         # mecânico
        "modulos": {"k": 2.5, "lambda_anos": 22.0},            # degradação lenta
        "transformador": {"k": 1.0, "lambda_anos": 80.0},
    },
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


def _numero_br(serie: pd.Series) -> pd.Series:
    """'-20,12479858' -> float. Formato numérico da ANEEL."""
    return pd.to_numeric(
        serie.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce")


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
        # coordenadas: usadas só pela variante --com-clima, para achar o ponto NASA
        "lat": _numero_br(aneel["NumCoordNEmpreendimento"]),
        "lon": _numero_br(aneel["NumCoordEEmpreendimento"]),
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


def _covariaveis_do_gerador(usinas: pd.DataFrame) -> pd.DataFrame:
    """As covariáveis do modelo, na mesma centralização que a análise usa."""
    return pd.DataFrame({
        "log_potencia_mw_c": np.log(usinas["potencia_mw"]) - np.log(POTENCIA_REFERENCIA_MW),
        "subsistema_NE": (usinas["id_subsistema"] == "NE").astype(float),
        "subsistema_S": (usinas["id_subsistema"] == "S").astype(float),
        "subsistema_N": (usinas["id_subsistema"] == "N").astype(float),
        "ano_entrada_c": (usinas["data_entrada_operacao"].dt.year - ANO_REFERENCIA).astype(float),
    })


def _preditor_linear(usinas: pd.DataFrame) -> pd.Series:
    x = _covariaveis_do_gerador(usinas)
    return sum(BETA[nome] * x[nome] for nome in BETA)


def covariaveis_externas(usinas: pd.DataFrame) -> pd.DataFrame:
    """Anexa clima (NASA) e fator de capacidade (ONS) a cada usina da ANEEL.

    O clima vem do **ponto NASA mais próximo** da coordenada da usina, a mesma
    aproximação que o ETL usa na `fato_clima` — e pela mesma razão: a NASA
    entrega por coordenada consultada, não por usina.

    O fator de capacidade vem da geração medida do ONS, e só existe para as
    usinas que o vínculo ONS×ANEEL alcança. Para as demais é **imputado** pela
    média da fonte e subsistema, e a coluna `fator_capacidade_imputado` registra
    isso — o número continua utilizável, mas quem analisar sabe o que é medido
    e o que é preenchido.
    """
    from ETL.utils import haversine_km

    df = usinas.copy()

    clima = pd.read_parquet(CLEAN / "nasa_clima_diario.parquet")
    pontos = (clima.groupby(["local", "latitude", "longitude"])
              [["vento_50m_ms", "temperatura_2m_c"]].mean().reset_index())

    indices = [
        int(np.argmin(haversine_km(lat, lon, pontos["latitude"].to_numpy(),
                                   pontos["longitude"].to_numpy())))
        if np.isfinite(lat) and np.isfinite(lon) else -1
        for lat, lon in zip(df["lat"], df["lon"])
    ]
    for coluna in ("local", "vento_50m_ms", "temperatura_2m_c"):
        df[coluna] = [pontos[coluna].iat[i] if i >= 0 else np.nan for i in indices]
    df = df.rename(columns={"local": "ponto_clima"})

    df["fator_capacidade"] = _fator_de_capacidade(df)
    df["fator_capacidade_imputado"] = df["fator_capacidade"].isna()
    media = df.groupby(["fonte", "id_subsistema"])["fator_capacidade"].transform("mean")
    df["fator_capacidade"] = df["fator_capacidade"].fillna(media).fillna(
        df["fator_capacidade"].mean())

    log.info("[externas] clima de %d pontos NASA | fator de capacidade medido em %d de %d usinas",
             len(pontos), int((~df["fator_capacidade_imputado"]).sum()), len(df))
    return df


def _fator_de_capacidade(usinas: pd.DataFrame) -> pd.Series:
    """FC medido por usina da ANEEL, via ponte ONS×ANEEL.

    A geração do ONS é por **unidade** (muitas vezes um conjunto de usinas), e
    a ponte diz quais CEGs compõem cada unidade. O FC da unidade é atribuído a
    todas as suas usinas: é o melhor que o vínculo permite, e vale lembrar que
    um conjunto não tem fator de capacidade "por usina" observável.
    """
    ponte = CURATED / "ponte_usina_aneel.parquet"
    geracao = CURATED / "fato_geracao.parquet"
    dim = CURATED / "dim_usina.parquet"
    if not (ponte.exists() and geracao.exists() and dim.exists()):
        log.warning("[externas] camada curated ausente; fator de capacidade todo imputado")
        return pd.Series(np.nan, index=usinas.index)

    fato = pd.read_parquet(geracao, columns=["usina_id", "energia_mwh"])
    horas = fato.groupby("usina_id")["energia_mwh"].agg(["sum", "size"])
    potencias = pd.read_parquet(dim, columns=["usina_id", "potencia_mw"]).set_index("usina_id")
    fc = (horas["sum"] / (potencias["potencia_mw"] * horas["size"])).rename("fator_capacidade")
    fc = fc[(fc > 0) & (fc <= 1)]

    por_ceg = (pd.read_parquet(ponte, columns=["ceg_aneel", "usina_id"])
               .join(fc, on="usina_id").dropna(subset=["fator_capacidade"])
               .groupby("ceg_aneel")["fator_capacidade"].mean())
    return usinas["ceg"].map(por_ceg)


def _preditor_externo(usinas: pd.DataFrame) -> pd.Series:
    """Parte do preditor linear que vem do ONS e da NASA."""
    x = pd.DataFrame({
        "vento_50m_c": usinas["vento_50m_ms"] - REFERENCIAS_EXTERNAS["vento_50m_ms"],
        "temperatura_c": usinas["temperatura_2m_c"] - REFERENCIAS_EXTERNAS["temperatura_2m_c"],
        "fator_capacidade_c": usinas["fator_capacidade"] - REFERENCIAS_EXTERNAS["fator_capacidade"],
    }).fillna(0.0)
    return sum(BETA_EXTERNO[nome] * x[nome] for nome in BETA_EXTERNO)


def simular_com_clima(usinas: pd.DataFrame, data_corte: pd.Timestamp, seed: int,
                      variancia_frailty: float = VARIANCIA_FRAILTY,
                      taxa_censura: float = TAXA_CENSURA_ALEATORIA) -> pd.DataFrame:
    """Como `simular`, mas com o clima e o fator de capacidade no risco."""
    completas = covariaveis_externas(usinas)
    extra = _preditor_externo(completas)

    rng = np.random.default_rng(seed + 6)
    df = completas.copy()
    k = df["fonte"].map(lambda f: WEIBULL[f]["k"])
    escala = df["fonte"].map(lambda f: WEIBULL[f]["lambda_anos"])
    df["frailty"] = sortear_frailty(len(df), variancia_frailty, seed)
    eta = _preditor_linear(df) + np.log(df["frailty"]) + extra

    tempo_ate_evento = escala * (rng.exponential(size=len(df)) / np.exp(eta)) ** (1 / k)
    administrativo = (data_corte - df["data_entrada_operacao"]).dt.days / 365.25
    observavel = np.minimum(administrativo, sortear_censura_aleatoria(len(df), taxa_censura, seed))

    df["evento"] = (tempo_ate_evento <= observavel).astype(int)
    df["tempo_anos"] = np.where(df["evento"] == 1, tempo_ate_evento, observavel)
    df["motivo_censura"] = np.where(df["evento"] == 1, "evento", "censura")
    df["fim_observacao_anos"] = observavel
    dias = np.where(df["evento"] == 1, tempo_ate_evento, 0.0) * 365.25
    df["data_evento"] = df["data_entrada_operacao"] + pd.to_timedelta(
        np.where(df["evento"] == 1, dias, np.nan), unit="D").round("D")
    df["data_corte"] = data_corte

    df.insert(0, "id_usina", np.arange(1, len(df) + 1))
    return df[[
        "id_usina", "ceg", "fonte", "sig_tipo_geracao", "id_estado", "id_subsistema",
        "potencia_mw", "data_entrada_operacao", "data_corte", "tempo_anos", "evento",
        "data_evento", "motivo_censura", "fim_observacao_anos", "frailty",
        "ponto_clima", "vento_50m_ms", "temperatura_2m_c", "fator_capacidade",
        "fator_capacidade_imputado",
    ]]


def _gravar_metadados_clima(saida: Path, seed: int, df: pd.DataFrame) -> None:
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": ("Variante SINTÉTICA com covariáveis reais do ONS (fator de capacidade) "
                      "e da NASA (vento, temperatura)."),
        "fonte_usinas": ENTRADA.relative_to(RAIZ).as_posix(),
        "seed": seed,
        "seed_efetiva": seed + 6,
        "beta_verdadeiro": {**BETA, **BETA_EXTERNO},
        "referencias_centralizacao": {
            "potencia_mw": POTENCIA_REFERENCIA_MW, "ano_entrada": ANO_REFERENCIA,
            **REFERENCIAS_EXTERNAS,
        },
        "weibull_por_fonte": WEIBULL,
        "n_usinas": len(df),
        "n_eventos": int(df["evento"].sum()),
        "fator_capacidade_medido": int((~df["fator_capacidade_imputado"]).sum()),
        "fator_capacidade_imputado": int(df["fator_capacidade_imputado"].sum()),
    }
    caminho = saida.with_name(saida.stem + ".meta.json")
    caminho.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def sortear_frailty(n: int, variancia: float, seed: int) -> np.ndarray:
    """Fator de fragilidade por usina: Gama de média 1 e variância `variancia`.

    Fluxo aleatório próprio (`seed + 2`) para que ligar ou desligar a
    fragilidade não desloque os demais sorteios.
    """
    if variancia <= 0:
        return np.ones(n)
    rng = np.random.default_rng(seed + 2)
    return rng.gamma(shape=1 / variancia, scale=variancia, size=n)


def sortear_censura_aleatoria(n: int, taxa: float, seed: int) -> np.ndarray:
    """Tempo até a perda de acompanhamento, `C ~ Exponencial(taxa)`.

    Fluxo aleatório próprio (`seed + 4`) para que ligar ou desligar a censura
    aleatória não desloque os demais sorteios. Sorteado **sem olhar** para o
    tempo até o evento nem para as covariáveis: é isso que a torna independente.
    """
    if taxa <= 0:
        return np.full(n, np.inf)
    return np.random.default_rng(seed + 4).exponential(scale=1 / taxa, size=n)


def simular(usinas: pd.DataFrame, data_corte: pd.Timestamp, seed: int,
            variancia_frailty: float = VARIANCIA_FRAILTY,
            taxa_censura: float = TAXA_CENSURA_ALEATORIA) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = usinas.copy()

    k = df["fonte"].map(lambda f: WEIBULL[f]["k"])
    escala = df["fonte"].map(lambda f: WEIBULL[f]["lambda_anos"])
    df["frailty"] = sortear_frailty(len(df), variancia_frailty, seed)
    # log(Z) entra como deslocamento do preditor linear: Z multiplica o risco
    eta = _preditor_linear(df) + np.log(df["frailty"])

    # Inversão da função de sobrevivência Weibull-PH
    tempo_ate_evento = escala * (rng.exponential(size=len(df)) / np.exp(eta)) ** (1 / k)
    tempo_administrativo = (data_corte - df["data_entrada_operacao"]).dt.days / 365.25
    tempo_aleatorio = sortear_censura_aleatoria(len(df), taxa_censura, seed)

    # O que acontece primeiro encerra a observação
    tempo_observavel = np.minimum(tempo_administrativo, tempo_aleatorio)
    df["evento"] = (tempo_ate_evento <= tempo_observavel).astype(int)
    df["tempo_anos"] = np.where(df["evento"] == 1, tempo_ate_evento, tempo_observavel)
    df["motivo_censura"] = np.where(
        df["evento"] == 1, "evento",
        np.where(tempo_aleatorio < tempo_administrativo, "perda_acompanhamento", "administrativa"))
    df["fim_observacao_anos"] = tempo_observavel
    # Só converte em data quem teve evento: para quem não teve, o tempo sorteado
    # pode ser astronômico (fragilidade mínima) e estourar o float na conversão
    dias_ate_evento = np.where(df["evento"] == 1, tempo_ate_evento, 0.0) * 365.25
    df["data_evento"] = df["data_entrada_operacao"] + pd.to_timedelta(
        np.where(df["evento"] == 1, dias_ate_evento, np.nan), unit="D"
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
        "tempo_anos", "evento", "data_evento", "tipo_evento", "motivo_censura",
        "fim_observacao_anos", "frailty",
    ]]


def simular_recorrentes(eventos: pd.DataFrame, usinas: pd.DataFrame, data_corte: pd.Timestamp,
                        seed: int, gamma: float = GAMMA_RECORRENCIA) -> pd.DataFrame:
    """Processo completo de manutenções por usina, em formato de processo de contagem.

    Uma linha por episódio, com o intervalo `(t_inicio_anos, t_fim_anos]` desde
    a entrada em operação — é o formato que Andersen-Gill e PWP consomem
    diretamente. O último episódio de cada usina é sempre censurado
    (`evento = 0`): é o trecho em que ela chegou ao fim da observação sem falhar.

    A semente é deslocada (`seed + 1`) para que os sorteios dos episódios 2+ não
    consumam o mesmo fluxo aleatório do 1º evento, que precisa ficar intacto.
    """
    rng = np.random.default_rng(seed + 1)

    k = usinas["fonte"].map(lambda f: WEIBULL[f]["k"]).to_numpy()
    escala = usinas["fonte"].map(lambda f: WEIBULL[f]["lambda_anos"]).to_numpy()
    # Mesmo Z em todos os episódios da usina: é isso que torna a fragilidade
    # estimável (ela vira correlação entre os episódios de uma mesma usina)
    frailty = eventos["frailty"].to_numpy()
    eta = _preditor_linear(usinas).to_numpy() + np.log(frailty)
    # O fim da observação de cada usina já considera a censura aleatória
    observavel = eventos["fim_observacao_anos"].to_numpy()
    primeiro_tempo = eventos["tempo_anos"].to_numpy()
    primeiro_evento = eventos["evento"].to_numpy()

    linhas, truncadas = [], []
    for i in range(len(usinas)):
        t_inicio, episodio = 0.0, 1
        t_fim, evento = float(primeiro_tempo[i]), int(primeiro_evento[i])

        while True:
            linhas.append((i, episodio, t_inicio, t_fim, t_fim - t_inicio, evento))
            if evento == 0:
                break
            if episodio >= MAX_EPISODIOS:
                truncadas.append(int(eventos["id_usina"].iloc[i]))
                break
            # Reparado: o relógio do risco zera, mas o risco sobe exp(gamma) por episódio
            episodio += 1
            eta_episodio = eta[i] + gamma * min(episodio - 1, SATURACAO_DETERIORACAO)
            gap = escala[i] * (rng.exponential() / np.exp(eta_episodio)) ** (1 / k[i])
            t_inicio, t_fim = t_fim, t_fim + gap
            if t_fim > observavel[i]:               # passou do retrato: censura
                t_fim, evento = float(observavel[i]), 0

    if truncadas:
        log.warning("[recorrentes] %d usina(s) atingiram a trava de %d episódios (%s); "
                    "o processo delas fica truncado, sem o trecho censurado final",
                    len(truncadas), MAX_EPISODIOS, truncadas[:5])

    painel = pd.DataFrame(linhas, columns=["posicao", "episodio", "t_inicio_anos",
                                           "t_fim_anos", "gap_anos", "evento"])
    identificacao = eventos[["id_usina", "ceg", "fonte", "id_estado", "id_subsistema",
                             "potencia_mw", "data_entrada_operacao", "frailty",
                             "motivo_censura"]].reset_index(drop=True)
    painel = painel.join(identificacao, on="posicao").drop(columns="posicao")

    painel["data_inicio"] = painel["data_entrada_operacao"] + pd.to_timedelta(
        painel["t_inicio_anos"] * 365.25, unit="D").round("D")
    painel["data_fim"] = painel["data_entrada_operacao"] + pd.to_timedelta(
        painel["t_fim_anos"] * 365.25, unit="D").round("D")
    painel["data_corte"] = data_corte

    tipos = pd.Series(pd.NA, index=painel.index, dtype="string")
    for fonte, probs in TIPOS_EVENTO.items():
        alvo = (painel["fonte"] == fonte) & (painel["evento"] == 1)
        tipos[alvo] = rng.choice(list(probs), size=int(alvo.sum()), p=list(probs.values()))
    painel["tipo_evento"] = tipos

    return painel[[
        "id_usina", "ceg", "fonte", "id_estado", "id_subsistema", "potencia_mw",
        "data_entrada_operacao", "data_corte", "episodio",
        "t_inicio_anos", "t_fim_anos", "gap_anos", "evento", "data_inicio", "data_fim",
        "tipo_evento", "frailty", "motivo_censura",
    ]].sort_values(["id_usina", "episodio"]).reset_index(drop=True)


def simular_ph_violado(usinas: pd.DataFrame, data_corte: pd.Timestamp, seed: int,
                       efeito: dict | None = None,
                       variancia_frailty: float = 0.0) -> pd.DataFrame:
    """Variante em que o efeito de uma covariável muda no tempo (viola PH).

    O risco acumulado da Weibull é `Lambda0(t) = (t/lambda)**k`. Com o efeito
    constante por partes, o risco acumulado individual também é, e a inversão
    sai exata em cada trecho:

        H(t) = Lambda0(t) · e^(eta_antes)                             , t <= c
        H(t) = Lambda0(c) · e^(eta_antes) + [Lambda0(t) − Lambda0(c)] · e^(eta_depois)

    Sorteia-se `E ~ Exp(1)` e resolve-se `H(T) = E`: se `E` couber no primeiro
    trecho, vale a fórmula usual; senão, o que sobra é consumido na taxa do
    segundo trecho.

    A fragilidade fica **desligada** por padrão aqui: esta variante existe para
    isolar um efeito — heterogeneidade não observada também derruba a premissa
    de PH, e misturar as duas coisas tornaria o diagnóstico ambíguo.
    """
    efeito = efeito or EFEITO_TEMPO_DEPENDENTE
    rng = np.random.default_rng(seed + 3)
    df = usinas.copy()

    k = df["fonte"].map(lambda f: WEIBULL[f]["k"]).to_numpy()
    escala = df["fonte"].map(lambda f: WEIBULL[f]["lambda_anos"]).to_numpy()
    df["frailty"] = sortear_frailty(len(df), variancia_frailty, seed)

    coluna = efeito["covariavel"]
    x = _covariaveis_do_gerador(df)[coluna].to_numpy()
    # preditor linear sem a covariável de efeito variável, mais cada versão dela
    base = (_preditor_linear(df).to_numpy() - BETA[coluna] * x + np.log(df["frailty"]))
    eta_antes = base + efeito["beta_antes"] * x
    eta_depois = base + efeito["beta_depois"] * x

    corte = float(efeito["corte_anos"])
    lambda0_corte = (corte / escala) ** k
    e = rng.exponential(size=len(df))
    risco_ate_o_corte = lambda0_corte * np.exp(eta_antes)

    antes = e <= risco_ate_o_corte
    tempo = np.where(
        antes,
        escala * (e / np.exp(eta_antes)) ** (1 / k),
        escala * (lambda0_corte + (e - risco_ate_o_corte) / np.exp(eta_depois)) ** (1 / k),
    )

    observavel = ((data_corte - df["data_entrada_operacao"]).dt.days / 365.25).to_numpy()
    df["evento"] = (tempo <= observavel).astype(int)
    df["tempo_anos"] = np.where(df["evento"] == 1, tempo, observavel)
    df["periodo_do_evento"] = np.where(df["tempo_anos"] <= corte, "antes", "depois")
    dias = np.where(df["evento"] == 1, tempo, 0.0) * 365.25
    df["data_evento"] = df["data_entrada_operacao"] + pd.to_timedelta(
        np.where(df["evento"] == 1, dias, np.nan), unit="D").round("D")
    df["data_corte"] = data_corte

    df.insert(0, "id_usina", np.arange(1, len(df) + 1))
    return df[[
        "id_usina", "ceg", "fonte", "sig_tipo_geracao", "id_estado", "id_subsistema",
        "potencia_mw", "data_entrada_operacao", "data_corte",
        "tempo_anos", "evento", "data_evento", "periodo_do_evento", "frailty",
    ]]


def simular_riscos_competitivos(usinas: pd.DataFrame, data_corte: pd.Timestamp, seed: int,
                                variancia_frailty: float = VARIANCIA_FRAILTY,
                                taxa_censura: float = TAXA_CENSURA_ALEATORIA,
                                causas: dict | None = None) -> pd.DataFrame:
    """Um tempo latente por causa; observa-se o menor e qual causa venceu.

    Os tempos latentes das causas que **não** venceram são, por definição, não
    observáveis — não vão para o arquivo. O que se observa é exatamente o que
    se observaria no mundo: um tempo, um indicador de evento e uma causa.

    As covariáveis agem igual em todas as causas (mesmo `eta`), então o efeito
    de potência e região é o mesmo de sempre; o que muda entre as causas é a
    **forma** da linha de base, e é daí que vem a mudança de composição com a
    idade.
    """
    causas = causas or CAUSAS_COMPETITIVAS
    rng = np.random.default_rng(seed + 5)
    df = usinas.copy()

    df["frailty"] = sortear_frailty(len(df), variancia_frailty, seed)
    eta = (_preditor_linear(df) + np.log(df["frailty"])).to_numpy()

    nomes = sorted({causa for mapa in causas.values() for causa in mapa})
    tempos = np.full((len(df), len(nomes)), np.inf)
    for coluna, causa in enumerate(nomes):
        for fonte, mapa in causas.items():
            if causa not in mapa:
                continue
            alvo = (df["fonte"] == fonte).to_numpy()
            if not alvo.any():
                continue
            k, escala = mapa[causa]["k"], mapa[causa]["lambda_anos"]
            e = rng.exponential(size=int(alvo.sum()))
            tempos[alvo, coluna] = escala * (e / np.exp(eta[alvo])) ** (1 / k)

    vencedora = np.argmin(tempos, axis=1)
    tempo_ate_evento = tempos[np.arange(len(df)), vencedora]

    administrativo = (data_corte - df["data_entrada_operacao"]).dt.days.to_numpy() / 365.25
    aleatorio = sortear_censura_aleatoria(len(df), taxa_censura, seed)
    observavel = np.minimum(administrativo, aleatorio)

    df["evento"] = (tempo_ate_evento <= observavel).astype(int)
    df["tempo_anos"] = np.where(df["evento"] == 1, tempo_ate_evento, observavel)
    df["tipo_evento"] = pd.Series(
        np.where(df["evento"] == 1, np.array(nomes)[vencedora], None), index=df.index,
        dtype="string")
    df["motivo_censura"] = np.where(
        df["evento"] == 1, "evento",
        np.where(aleatorio < administrativo, "perda_acompanhamento", "administrativa"))
    df["fim_observacao_anos"] = observavel
    dias = np.where(df["evento"] == 1, tempo_ate_evento, 0.0) * 365.25
    df["data_evento"] = df["data_entrada_operacao"] + pd.to_timedelta(
        np.where(df["evento"] == 1, dias, np.nan), unit="D").round("D")
    df["data_corte"] = data_corte

    df.insert(0, "id_usina", np.arange(1, len(df) + 1))
    return df[[
        "id_usina", "ceg", "fonte", "sig_tipo_geracao", "id_estado", "id_subsistema",
        "potencia_mw", "data_entrada_operacao", "data_corte",
        "tempo_anos", "evento", "data_evento", "tipo_evento", "motivo_censura",
        "fim_observacao_anos", "frailty",
    ]]


def _gravar_metadados_competitivos(saida: Path, seed: int, df: pd.DataFrame) -> None:
    com_evento = df[df["evento"] == 1]
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": ("Variante SINTÉTICA com riscos competitivos: um tempo latente por "
                      "causa, observa-se o menor."),
        "fonte_usinas": ENTRADA.relative_to(RAIZ).as_posix(),
        "seed": seed,
        "seed_efetiva": seed + 5,
        "modelo": "Weibull por causa, com formas diferentes; covariáveis comuns a todas",
        "causas": CAUSAS_COMPETITIVAS,
        "beta_verdadeiro": BETA,
        "n_usinas": len(df),
        "n_eventos": int(com_evento.shape[0]),
        "causas_observadas": com_evento["tipo_evento"].value_counts().to_dict(),
    }
    caminho = saida.with_name(saida.stem + ".meta.json")
    caminho.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _gravar_metadados_ph(saida: Path, seed: int, efeito: dict, df: pd.DataFrame) -> None:
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": ("Variante SINTÉTICA com efeito de covariável dependente do tempo, "
                      "para testar diagnósticos de riscos proporcionais."),
        "fonte_usinas": ENTRADA.relative_to(RAIZ).as_posix(),
        "seed": seed,
        "seed_efetiva": seed + 3,
        "modelo": "Weibull com beta constante por partes (viola PH por construção)",
        "efeito_tempo_dependente": efeito,
        "beta_verdadeiro": {**BETA, efeito["covariavel"]: "muda no tempo, ver efeito_tempo_dependente"},
        "weibull_por_fonte": WEIBULL,
        "variancia_frailty": 0.0,
        "referencias_centralizacao": {
            "potencia_mw": POTENCIA_REFERENCIA_MW, "ano_entrada": ANO_REFERENCIA,
        },
        "n_usinas": len(df),
        "n_eventos": int(df["evento"].sum()),
        "eventos_antes_do_corte": int(((df["evento"] == 1) & (df["periodo_do_evento"] == "antes")).sum()),
        "eventos_depois_do_corte": int(((df["evento"] == 1) & (df["periodo_do_evento"] == "depois")).sum()),
    }
    caminho = saida.with_name(saida.stem + ".meta.json")
    caminho.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _gravar_metadados(saida: Path, seed: int, potencia_minima_mw: float, df: pd.DataFrame) -> None:
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": "Dados SINTÉTICOS de 1º evento de manutenção corretiva/falha por usina.",
        "fonte_usinas": ENTRADA.relative_to(RAIZ).as_posix(),
        "seed": seed,
        "potencia_minima_mw": potencia_minima_mw,
        "data_minima_entrada": DATA_MINIMA_ENTRADA,
        "modelo": ("Weibull de riscos proporcionais com fragilidade gama por usina, "
                   "linha de base por fonte"),
        "variancia_frailty": float(df["frailty"].var()) if "frailty" in df else 0.0,
        "censura": {
            "mecanismos": ["administrativa", "aleatoria_exponencial_independente"],
            "distribuicao": df["motivo_censura"].value_counts().to_dict(),
        },
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


def _gravar_metadados_recorrentes(saida: Path, seed: int, potencia_minima_mw: float,
                                  gamma: float, df: pd.DataFrame) -> None:
    por_usina = df.groupby("id_usina")["evento"].sum()
    meta = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "descricao": ("Dados SINTÉTICOS de manutenção corretiva RECORRENTE por usina, "
                      "em formato de processo de contagem (um intervalo por episódio)."),
        "fonte_usinas": ENTRADA.relative_to(RAIZ).as_posix(),
        "seed": seed,
        "seed_episodios_2_em_diante": seed + 1,
        "potencia_minima_mw": potencia_minima_mw,
        "modelo": ("Processo de renovação com deterioração: gap_j ~ Weibull(k, lambda) "
                   "com eta_j = beta·x + gamma*(j-1)"),
        "gamma_recorrencia": gamma,
        "saturacao_deterioracao": SATURACAO_DETERIORACAO,
        "variancia_frailty": float(df.drop_duplicates("id_usina")["frailty"].var()),
        "max_episodios": MAX_EPISODIOS,
        "weibull_por_fonte": WEIBULL,
        "beta_verdadeiro": BETA,
        "referencias_centralizacao": {
            "potencia_mw": POTENCIA_REFERENCIA_MW, "ano_entrada": ANO_REFERENCIA,
        },
        "n_usinas": int(df["id_usina"].nunique()),
        "n_episodios": len(df),
        "n_eventos": int(df["evento"].sum()),
        "eventos_por_usina": {
            "media": float(por_usina.mean()), "maximo": int(por_usina.max()),
            "distribuicao": {str(n): int(q) for n, q in por_usina.value_counts().sort_index().items()},
        },
    }
    caminho = saida.with_name(saida.stem + ".meta.json")
    caminho.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED_PADRAO)
    parser.add_argument("--potencia-minima-mw", type=float, default=POTENCIA_MINIMA_MW_PADRAO)
    parser.add_argument("--gamma-recorrencia", type=float, default=GAMMA_RECORRENCIA,
                        help="log do fator de risco por episódio (0 = reparo perfeito)")
    parser.add_argument("--variancia-frailty", type=float, default=VARIANCIA_FRAILTY,
                        help="variância da fragilidade gama por usina (0 = desliga)")
    parser.add_argument("--taxa-censura-aleatoria", type=float, default=TAXA_CENSURA_ALEATORIA,
                        help="taxa anual da censura aleatória independente (0 = só administrativa)")
    parser.add_argument("--com-clima", action="store_true",
                        help="gera também a variante com covariáveis do ONS e da NASA")
    args = parser.parse_args()

    usinas, data_corte = carregar_usinas(args.potencia_minima_mw)
    log.info("%d usinas reais da ANEEL como base (retrato de %s)", len(usinas), data_corte.date())

    eventos = simular(usinas, data_corte, args.seed, args.variancia_frailty,
                      args.taxa_censura_aleatoria)
    log.info("[censura] %s", eventos["motivo_censura"].value_counts().to_dict())
    log.info("[frailty] variância pedida %.2f, obtida %.2f | Z: mín %.2f, mediana %.2f, máx %.2f",
             args.variancia_frailty, eventos["frailty"].var(), eventos["frailty"].min(),
             eventos["frailty"].median(), eventos["frailty"].max())
    for fonte, grupo in eventos.groupby("fonte"):
        log.info("  %s: %d usinas, %d eventos, censura %.1f%%",
                 fonte, len(grupo), grupo["evento"].sum(), 100 * (1 - grupo["evento"].mean()))

    gravar_parquet(eventos, SAIDA)
    _gravar_metadados(SAIDA, args.seed, args.potencia_minima_mw, eventos)

    recorrentes = simular_recorrentes(eventos, usinas, data_corte, args.seed,
                                      args.gamma_recorrencia)
    por_usina = recorrentes.groupby("id_usina")["evento"].sum()
    log.info("[recorrentes] %d episódios, %d eventos | eventos por usina: média %.2f, máx %d",
             len(recorrentes), int(recorrentes["evento"].sum()),
             por_usina.mean(), int(por_usina.max()))
    log.info("[recorrentes] usinas com 0/1/2+ eventos: %d / %d / %d",
             int((por_usina == 0).sum()), int((por_usina == 1).sum()), int((por_usina >= 2).sum()))

    gravar_parquet(recorrentes, SAIDA_RECORRENTES)
    _gravar_metadados_recorrentes(SAIDA_RECORRENTES, args.seed, args.potencia_minima_mw,
                                  args.gamma_recorrencia, recorrentes)

    ph = simular_ph_violado(usinas, data_corte, args.seed)
    por_periodo = ph[ph["evento"] == 1]["periodo_do_evento"].value_counts()
    log.info("[ph-violado] %d eventos (%d antes de %.0f anos, %d depois) | efeito de %s: %+.2f -> %+.2f",
             int(ph["evento"].sum()), int(por_periodo.get("antes", 0)),
             EFEITO_TEMPO_DEPENDENTE["corte_anos"], int(por_periodo.get("depois", 0)),
             EFEITO_TEMPO_DEPENDENTE["covariavel"], EFEITO_TEMPO_DEPENDENTE["beta_antes"],
             EFEITO_TEMPO_DEPENDENTE["beta_depois"])
    gravar_parquet(ph, SAIDA_PH_VIOLADO)
    _gravar_metadados_ph(SAIDA_PH_VIOLADO, args.seed, EFEITO_TEMPO_DEPENDENTE, ph)

    competitivos = simular_riscos_competitivos(usinas, data_corte, args.seed,
                                               args.variancia_frailty,
                                               args.taxa_censura_aleatoria)
    com_evento = competitivos[competitivos["evento"] == 1]
    log.info("[competitivos] %d eventos | causas: %s", len(com_evento),
             com_evento["tipo_evento"].value_counts().to_dict())
    gravar_parquet(competitivos, SAIDA_COMPETITIVOS)
    _gravar_metadados_competitivos(SAIDA_COMPETITIVOS, args.seed, competitivos)

    if args.com_clima:
        clima = simular_com_clima(usinas, data_corte, args.seed, args.variancia_frailty,
                                  args.taxa_censura_aleatoria)
        log.info("[com-clima] %d eventos | FC medido em %d usinas", int(clima["evento"].sum()),
                 int((~clima["fator_capacidade_imputado"]).sum()))
        gravar_parquet(clima, SAIDA_CLIMA)
        _gravar_metadados_clima(SAIDA_CLIMA, args.seed, clima)


if __name__ == "__main__":
    main()
