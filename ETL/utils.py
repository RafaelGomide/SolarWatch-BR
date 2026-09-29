"""Funções auxiliares reutilizadas pelas camadas clean e curated."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

import ds_toolkit as dst
from ETL.config import VERBOSE_TOOLKIT
from ingestao.armazenamento import gravar_parquet

log = logging.getLogger(__name__)


def ler_parquet(caminho: Path) -> pd.DataFrame:
    """Lê um Parquet ou, se `caminho` for pasta, todos os `*.parquet` dela.

    O bruto do ONS é gravado como um arquivo por mês; a pasta inteira é lida
    como um único dataset (glob `dados_ons_bruto/*.parquet`). O glob também
    ignora sobras de escrita interrompida (`.parquet.tmp`).
    """
    if caminho.is_file():
        return pd.read_parquet(caminho)

    arquivos = sorted(caminho.glob("*.parquet"))
    if not arquivos:
        raise FileNotFoundError(f"Nenhum .parquet em {caminho}")
    log.info("[ler] %s: %d arquivo(s)", caminho.name, len(arquivos))
    return pd.concat((pd.read_parquet(a) for a in arquivos), ignore_index=True)


def gravar(df: pd.DataFrame, pasta: Path, nome: str) -> Path:
    """Grava uma tabela da camada em Parquet (escrita atômica, zstd)."""
    return gravar_parquet(df.reset_index(drop=True), pasta / f"{nome}.parquet")


def normalizar_nome(df: pd.DataFrame, coluna: str, destino: str) -> pd.DataFrame:
    """Cria `destino` com uma chave de comparação de nomes: minúsculas, sem
    acento, sem pontuação e sem espaços repetidos.

    "Caetité  2" / "CAETITE 2" / "caetite-2" -> "caetite 2"
    """
    # Normaliza só os valores distintos e mapeia de volta (1,3 mi linhas do ONS
    # têm ~1.200 nomes distintos)
    unicos = pd.DataFrame({destino: df[coluna].dropna().unique()})
    unicos[coluna] = unicos[destino]
    unicos = dst.padronizar_texto(
        unicos, [destino], minusculas=True, remover_acentos=True,
        remover_pontuacao=True, verbose=VERBOSE_TOOLKIT,
    )
    unicos[destino] = unicos[destino].str.replace(r"\s+", " ", regex=True).str.strip()
    df = df.copy()
    df[destino] = df[coluna].map(dict(zip(unicos[coluna], unicos[destino]))).astype("string")
    return df


def limpar_textos(df: pd.DataFrame, colunas: list[str], vazios: tuple[str, ...] = ("",)) -> pd.DataFrame:
    """Remove espaços nas pontas e converte marcadores de vazio em nulo."""
    df = df.copy()
    for c in colunas:
        s = df[c].astype("string").str.strip()
        df[c] = s.mask(s.isin(vazios))
    return df


def completar_grade(
    df: pd.DataFrame, chave: str, tempo: str, colunas_fixas: list[str], freq: str = "h"
) -> tuple[pd.DataFrame, int]:
    """Garante uma linha por período (`freq`: 'h' hora, 'D' dia) entre a primeira
    e a última observação de cada `chave`. Linhas criadas herdam os atributos
    fixos da série e ficam com os valores nulos (tratados depois como gap)."""
    limites = df.groupby(chave)[tempo].agg(["min", "max", "size"])
    esperado = ((limites["max"] - limites["min"]) / pd.Timedelta(1, unit=freq)).astype(int) + 1
    if (esperado == limites["size"]).all():
        return df, 0

    grade = pd.concat(
        [pd.DataFrame({chave: k, tempo: pd.date_range(r["min"], r["max"], freq=freq)})
         for k, r in limites.iterrows()],
        ignore_index=True,
    )
    completo = grade.merge(df, on=[chave, tempo], how="left")
    completo[colunas_fixas] = completo.groupby(chave)[colunas_fixas].transform(
        lambda s: s.ffill().bfill()
    )
    return completo, len(completo) - len(df)


def interpolar_gaps_curtos(
    df: pd.DataFrame, chave: str, colunas: list[str], max_gap: int
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Interpola linearmente, dentro de cada série (`chave`), só os buracos de
    até `max_gap` valores consecutivos. `df` deve estar ordenado por chave e tempo.

    Retorna (df, mascara_interpolado, mascara_faltante) por linha: uma linha é
    'interpolada' se algum valor foi preenchido e 'faltante' se algum valor
    continua nulo.
    """
    df = df.copy()
    interpolado = pd.Series(False, index=df.index)
    for col in colunas:
        nulo = df[col].isna()
        if not nulo.any():
            continue
        # cada sequência de nulos compartilha o id com o último valor válido anterior
        sequencia = (~nulo).groupby(df[chave]).cumsum()
        tamanho = nulo.groupby([df[chave], sequencia]).transform("sum")
        estimado = df.groupby(chave)[col].transform(
            lambda s: s.interpolate(method="linear", limit_area="inside")
        )
        preencher = nulo & (tamanho <= max_gap) & estimado.notna()
        df.loc[preencher, col] = estimado[preencher]
        interpolado |= preencher
    faltante = df[colunas].isna().any(axis=1)
    return df, interpolado, faltante


def listar_faltantes(df: pd.DataFrame, colunas: list[str]) -> pd.Series:
    """Nomes das medidas ainda nulas em cada linha, separados por vírgula (nulo se nenhuma).

    O `flag_qualidade` diz *se* falta algo; esta coluna diz *o quê*. A distinção
    importa por causa da latência desigual da NASA POWER: a irradiância horária
    sai com ~3 meses de atraso enquanto vento e temperatura atrasam ~2 dias,
    então quase toda linha recente tem irradiância nula e o resto perfeitamente
    utilizável. Sem esta coluna, `faltante` marcaria a série inteira e não daria
    para saber que só uma variável está ausente.
    """
    nulos = df[colunas].isna()
    # bool * str no numpy devolve o nome ou "", e o produto de matrizes concatena:
    # vetorizado, sem apply linha a linha
    lista = nulos.dot(np.array([f"{c}," for c in colunas], dtype=object)).str.rstrip(",")
    return lista.replace("", pd.NA).astype("string")


def flag_qualidade(interpolado: pd.Series, faltante: pd.Series, extra: pd.Series | None = None) -> pd.Series:
    """Flag textual por linha: 'faltante' > 'interpolado' > flag extra > 'original'."""
    flag = pd.Series("original", index=interpolado.index, dtype="string")
    if extra is not None:
        flag = flag.mask(extra.notna(), extra)
    flag = flag.mask(interpolado, "interpolado")
    return flag.mask(faltante, "faltante")


def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Distância em km entre pares de coordenadas (vetorizado)."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def base_ceg(ceg: pd.Series) -> pd.Series:
    """CEG sem o sufixo de versão ('UHE.PH.AM.000190-2.01' -> 'UHE.PH.AM.000190-2').
    ONS usa sufixo de 2 dígitos e ANEEL de 1; a base é comparável entre os dois."""
    return ceg.str.rsplit(".", n=1).str[0]
