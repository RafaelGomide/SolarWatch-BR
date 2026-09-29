"""Gera `locais.csv` (pontos de consulta da NASA POWER) a partir do cadastro da ANEEL.

Os pontos eram definidos à mão, com coordenadas aproximadas de sede de
município. Aqui eles passam a vir das **coordenadas reais das usinas** do SIGA:
para cada par (fonte, UF) com capacidade relevante, escolhe-se a maior usina em
operação e usa-se a coordenada dela como ponto de clima daquela região.

Critérios (todos ajustáveis por CLI):
  - só `UFV`/`EOL` em fase "Operação", com coordenada dentro do Brasil;
  - potência fiscalizada mínima por usina (`--mw-minimo`), o que descarta os
    milhares de registros minúsculos de geração distribuída;
  - só grupos (fonte, UF) com capacidade instalada total acima de
    `--mw-minimo-grupo`, para não gastar chamada de API em UF irrelevante;
  - coordenada coerente com a UF declarada: o SIGA tem registros com UF de um
    estado e coordenada de outro, e um ponto desses arruinaria a região toda
    (`--max-km-uf`, medido contra a coordenada mediana das usinas da UF);
  - até `--por-grupo` pontos por grupo, e nunca dois pontos a menos de
    `--min-km` um do outro (ponto redundante = requisição repetida).

Uso:
    python -m ingestao.nasa_power.gerar_locais              # grava locais.csv
    python -m ingestao.nasa_power.gerar_locais --por-grupo 2 --dry-run

Ordem na pipeline: ingestão da ANEEL -> este script -> ingestão da NASA.
"""

from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parents[2]
ENTRADA = RAIZ / "dados" / "bruto" / "dados_aneel_bruto.parquet"
SAIDA = Path(__file__).resolve().parent / "locais.csv"

FONTE = {"UFV": "solar", "EOL": "eolica"}
# UF -> subsistema do SIN (mesma codificação do ONS). Espelha ETL.config.UF_SUBSISTEMA,
# repetido aqui para a ingestão não depender do pacote ETL (é o ETL que importa a ingestão).
UF_SUBSISTEMA = {
    **dict.fromkeys(["AM", "PA", "AP", "RR", "TO", "MA"], "N"),
    **dict.fromkeys(["BA", "RN", "PI", "CE", "PE", "PB", "SE", "AL"], "NE"),
    **dict.fromkeys(["MG", "SP", "RJ", "ES", "GO", "DF", "MT", "MS", "RO", "AC"], "SE"),
    **dict.fromkeys(["RS", "SC", "PR"], "S"),
}
LIMITES_BRASIL = {"lat": (-34.0, 5.5), "lon": (-74.0, -34.0)}
COLUNAS_SAIDA = ["local", "municipio", "id_estado", "id_subsistema", "fonte_predominante",
                 "latitude", "longitude", "usina_referencia", "potencia_mw", "ceg"]

log = logging.getLogger("gerar_locais")


def _numero(serie: pd.Series) -> pd.Series:
    """'11.832,10' / '-3,49527778' -> float (formato numérico da ANEEL)."""
    limpo = serie.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(limpo, errors="coerce")


def _slug(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", sem_acento.lower())).strip("_")


def _municipio(valor: str) -> str:
    """'São Gonçalo do Amarante - CE' -> 'São Gonçalo do Amarante'.

    Usinas que cruzam divisa listam vários municípios separados por vírgula;
    fica o primeiro, que é onde a coordenada do empreendimento cai.
    """
    primeiro = str(valor).split(",")[0]
    return re.sub(r"\s*-\s*[A-Z]{2}\s*$", "", primeiro).strip()


def _haversine_km(lat1: float, lon1: float, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Distância na esfera (raio 6.371 km). Mesma fórmula de `ETL.utils.haversine_km`,
    repetida aqui para não inverter a dependência entre os pacotes."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def _coerentes_com_a_uf(df: pd.DataFrame, max_km: float) -> pd.DataFrame:
    """Descarta usinas cuja coordenada está absurdamente longe das outras da mesma UF.

    Sem malha territorial, a referência é a **mediana** das coordenadas das
    usinas daquela UF (robusta a alguns pontos errados). O limite é folgado de
    propósito: nenhum estado brasileiro tem 700 km da mediana à borda, então o
    filtro pega troca de UF, não usina distante de fato. Ex.: as cinco unidades
    "Fótons de São George" estão cadastradas em MS com coordenada no Piauí,
    a 2.094 km — sozinhas elas levariam o ponto de clima de MS para o Nordeste.
    """
    mediana = df.groupby("id_estado")[["latitude", "longitude"]].transform("median")
    km = _haversine_km(df["latitude"].to_numpy(), df["longitude"].to_numpy(),
                       mediana["latitude"].to_numpy(), mediana["longitude"].to_numpy())
    fora = km > max_km
    if fora.any():
        pior = df.loc[fora].assign(km=km[fora]).nlargest(5, "km")
        log.warning("[locais] %d usina(s) com coordenada incoerente com a UF descartada(s); "
                    "maiores desvios: %s", int(fora.sum()),
                    [f"{u.usina_referencia} ({u.id_estado}, {u.km:.0f} km)" for u in pior.itertuples()])
    return df.loc[~fora]


def candidatas(mw_minimo: float, max_km_uf: float = 700.0, entrada: Path = ENTRADA) -> pd.DataFrame:
    """Usinas solares/eólicas em operação, com coordenada plausível e potência mínima."""
    df = pd.read_parquet(entrada, columns=[
        "NomEmpreendimento", "CodCEG", "SigUFPrincipal", "SigTipoGeracao", "DscFaseUsina",
        "MdaPotenciaFiscalizadaKw", "NumCoordNEmpreendimento", "NumCoordEEmpreendimento",
        "DscMuninicpios"])

    df = df[df["SigTipoGeracao"].isin(FONTE) & df["DscFaseUsina"].eq("Operação")]
    df = df.assign(
        fonte=df["SigTipoGeracao"].map(FONTE),
        id_estado=df["SigUFPrincipal"].str.strip(),
        potencia_mw=_numero(df["MdaPotenciaFiscalizadaKw"]) / 1000,
        latitude=_numero(df["NumCoordNEmpreendimento"]),
        longitude=_numero(df["NumCoordEEmpreendimento"]),
        municipio=df["DscMuninicpios"].map(_municipio),
        usina_referencia=df["NomEmpreendimento"].str.strip(),
        ceg=df["CodCEG"].str.strip(),
    )
    df["id_subsistema"] = df["id_estado"].map(UF_SUBSISTEMA)

    total = len(df)
    df = df[
        df["latitude"].between(*LIMITES_BRASIL["lat"])
        & df["longitude"].between(*LIMITES_BRASIL["lon"])
        & df["id_subsistema"].notna()
        & (df["potencia_mw"] >= mw_minimo)
    ]
    df = _coerentes_com_a_uf(df, max_km_uf)
    log.info("[locais] %d usinas em operação -> %d candidatas (coordenada válida e >= %.1f MW)",
             total, len(df), mw_minimo)
    # CEG como critério de desempate: há dezenas de usinas com potência idêntica
    # (Castilho 4 e 5, ambas 49,999 MW), e sem desempate a escolha oscilaria entre execuções
    return df.sort_values(["potencia_mw", "ceg"], ascending=[False, True], kind="mergesort")


def selecionar(cand: pd.DataFrame, por_grupo: int, mw_minimo_grupo: float,
               min_km: float) -> pd.DataFrame:
    """Escolhe os pontos: as maiores usinas de cada (fonte, UF) relevante, sem
    aceitar dois pontos a menos de `min_km` de distância."""
    capacidade = cand.groupby(["fonte", "id_estado"])["potencia_mw"].sum()
    grupos = capacidade[capacidade >= mw_minimo_grupo].sort_values(ascending=False)
    log.info("[locais] %d de %d grupos (fonte, UF) com >= %.0f MW instalados",
             len(grupos), len(capacidade), mw_minimo_grupo)

    escolhidas: list[pd.Series] = []
    for fonte, uf in grupos.index:
        do_grupo = cand[cand["fonte"].eq(fonte) & cand["id_estado"].eq(uf)]
        aceitas, vizinhas = 0, 0
        for usina in do_grupo.itertuples():
            if aceitas >= por_grupo:
                break
            if escolhidas:
                dist = _haversine_km(usina.latitude, usina.longitude,
                                     np.array([e["latitude"] for e in escolhidas]),
                                     np.array([e["longitude"] for e in escolhidas]))
                if dist.min() < min_km:
                    vizinhas += 1
                    log.debug("[locais] %s (%s/%s) descartada: %.0f km de um ponto já escolhido",
                              usina.usina_referencia, fonte, uf, dist.min())
                    continue
            escolhidas.append(cand.loc[usina.Index])
            aceitas += 1
        log.info("[locais] %s/%s: %d ponto(s) (%.0f MW no grupo, %d candidata(s) a < %.0f km de "
                 "outro ponto)", fonte, uf, aceitas, grupos[(fonte, uf)], vizinhas, min_km)

    pontos = pd.DataFrame(escolhidas).reset_index(drop=True)
    pontos = pontos.rename(columns={"fonte": "fonte_predominante"})
    pontos["local"] = [_slug(f"{m}_{uf}") for m, uf in zip(pontos["municipio"], pontos["id_estado"])]
    # Dois pontos no mesmo município (uma solar, uma eólica) precisam de nomes distintos
    repetido = pontos["local"].duplicated(keep=False)
    pontos.loc[repetido, "local"] += "_" + pontos.loc[repetido, "fonte_predominante"].str[:3]

    pontos["potencia_mw"] = pontos["potencia_mw"].round(3)
    pontos[["latitude", "longitude"]] = pontos[["latitude", "longitude"]].round(5)
    return pontos[COLUNAS_SAIDA]


def cobertura(cand: pd.DataFrame, pontos: pd.DataFrame, raio_km: float = 300.0) -> None:
    """Loga quanto da capacidade instalada fica a até `raio_km` de algum ponto."""
    lat, lon = pontos["latitude"].to_numpy(), pontos["longitude"].to_numpy()
    dist = np.array([_haversine_km(u.latitude, u.longitude, lat, lon).min()
                     for u in cand.itertuples()])
    peso = cand["potencia_mw"].to_numpy()
    dentro = dist <= raio_km
    log.info("[locais] %d pontos | distância usina->ponto: mediana %.0f km, p90 %.0f km, máx %.0f km",
             len(pontos), np.median(dist), np.percentile(dist, 90), dist.max())
    log.info("[locais] capacidade a <= %.0f km de um ponto: %.1f%% (%.0f de %.0f MW)",
             raio_km, 100 * peso[dentro].sum() / peso.sum(), peso[dentro].sum(), peso.sum())


def gerar(por_grupo: int = 1, mw_minimo: float = 1.0, mw_minimo_grupo: float = 50.0,
          min_km: float = 50.0, max_km_uf: float = 700.0, saida: Path = SAIDA,
          gravar: bool = True) -> pd.DataFrame:
    cand = candidatas(mw_minimo, max_km_uf)
    pontos = selecionar(cand, por_grupo, mw_minimo_grupo, min_km)
    cobertura(cand, pontos)
    if gravar:
        saida.write_text(pontos.to_csv(index=False), encoding="utf-8")
        log.info("[locais] gravado %s (%d pontos)", saida, len(pontos))
    return pontos


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--por-grupo", type=int, default=1, help="pontos por (fonte, UF)")
    parser.add_argument("--mw-minimo", type=float, default=1.0, help="potência mínima da usina")
    parser.add_argument("--mw-minimo-grupo", type=float, default=50.0,
                        help="capacidade mínima do par (fonte, UF) para merecer um ponto")
    parser.add_argument("--min-km", type=float, default=50.0,
                        help="distância mínima entre dois pontos")
    parser.add_argument("--max-km-uf", type=float, default=700.0,
                        help="desvio máximo da coordenada mediana da própria UF")
    parser.add_argument("--dry-run", action="store_true", help="só mostra, não grava o CSV")
    args = parser.parse_args()

    pontos = gerar(args.por_grupo, args.mw_minimo, args.mw_minimo_grupo, args.min_km,
                   args.max_km_uf, gravar=not args.dry_run)
    print(pontos.to_string(index=False))


if __name__ == "__main__":
    main()
