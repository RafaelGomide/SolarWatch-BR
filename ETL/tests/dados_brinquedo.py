"""Cadastro e séries de brinquedo para o teste de integração (§11.3).

Um recorte mínimo do mundo real, com um exemplo de cada caso que a pipeline tem
de tratar — e nada além disso. Quatro unidades solares/eólicas do ONS cobrem os
três métodos de vínculo e as quatro qualidades; oito usinas da ANEEL cobrem os
filtros de elegibilidade e as sujeiras do cadastro; as séries carregam um buraco
curto, um buraco longo, um negativo, uma duplicata e uma renomeação.

Os valores são escolhidos para que os números finais sejam **verificáveis à
mão**: 2 × 30 MW vinculados com pico de 45 MWh dão razão 0,75 (consistente);
10 MW com pico de 30 MWh dão 3,0 (inconsistente).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

INICIO = pd.Timestamp("2026-07-01")
HORAS = 72                      # 3 dias de geração horária
DATA_CORTE = pd.Timestamp("2026-07-04")

# Unidades do ONS. `pico` é o valor máximo de geração da série, que determina a
# `razao_pico_potencia` e, com ela, a qualidade do vínculo.
UNIDADES = [
    # chave      id_ons   ceg                      nome                                     modalidade                tipo_usina       pico
    ("UFV_A",   "UFVA",  "UFV.RS.BA.000001-0.01", "UFV BOM SOL I",                          "TIPO I",                 "FOTOVOLTAICA",  20.0),
    ("CJ_EOL",  "CJEOL", "-",                     "CONJUNTO EOLICO SANTA EUGENIA 230 KV",   "Conjunto de Usinas",     "EOLIELÉTRICA",  45.0),
    ("CJ_INC",  "CJINC", "-",                     "CONJUNTO EOLICO MORRO DO CHAPEU SUL II", "Conjunto de Usinas",     "EOLIELÉTRICA",  30.0),
    ("PQU",     None,    "-",                     "PQU MMAM MMGD",                          "Pequenas Usinas (MMGD)", "FOTOVOLTAICA",   0.3),
    ("HID",     "AMBA",  "UHE.PH.AM.000190-2.01", "BALBINA",                                "TIPO I",                 "HIDROELÉTRICA", 150.0),
]
UF_DA_UNIDADE = {"UFV_A": ("BA", "NE"), "CJ_EOL": ("BA", "NE"), "CJ_INC": ("RN", "NE"),
                 "PQU": ("AM", "N"), "HID": ("AM", "N")}
COMBUSTIVEL = {"FOTOVOLTAICA": "Fotovoltaica", "EOLIELÉTRICA": "Cinética do vento",
               "HIDROELÉTRICA": "Hidráulica"}
NOME_ESTADO = {"BA": "BAHIA", "RN": "RIO GRANDE DO NORTE", "AM": "AMAZONAS"}
NOME_SUBSISTEMA = {"NE": "NORDESTE", "N": "NORTE"}

# Sujeiras plantadas na série de geração
HORA_AUSENTE = 10        # linha removida: a grade tem de recriá-la (gap de 1 h)
HORA_NEGATIVA = 30       # consumo auxiliar: zerado com flag
HORA_DUPLICADA = 50      # mesma unidade e instante duas vezes
GAP_LONGO = range(20, 25)  # 5 horas nulas: acima do limite, fica faltante
HORA_RENOMEADA = 24      # até aqui a UFV_A tem o nome antigo


def _serie_horaria(pico: float) -> np.ndarray:
    """Curva simples cujo máximo diário é exatamente `pico`.

    O valor exato importa: a `razao_pico_potencia` da curated é calculada sobre
    ele, e é ela que decide a qualidade do vínculo.
    """
    curva = np.sin(np.pi * (np.arange(HORAS) % 24) / 23) ** 2
    return np.round(pico * curva / curva.max(), 3)


def ons_bruto() -> pd.DataFrame:
    partes = []
    for chave, id_ons, ceg, nome, modalidade, tipo, pico in UNIDADES:
        uf, subsistema = UF_DA_UNIDADE[chave]
        df = pd.DataFrame({
            "din_instante": pd.date_range(INICIO, periods=HORAS, freq="h"),
            "id_subsistema": subsistema,
            "nom_subsistema": NOME_SUBSISTEMA[subsistema],
            "id_estado": uf,
            "nom_estado": NOME_ESTADO[uf],
            "cod_modalidadeoperacao": modalidade,
            "nom_tipousina": tipo,
            "nom_tipocombustivel": COMBUSTIVEL[tipo],
            "nom_usina": nome,
            "id_ons": id_ons,
            "ceg": ceg,
            "val_geracao": _serie_horaria(pico),
            "arquivo_origem": "GERACAO_USINA-2_2026_07.parquet",
        })
        if chave == "UFV_A":
            # o ONS renomeia a usina no meio do período: vale o último nome
            df.loc[: HORA_RENOMEADA - 1, "nom_usina"] = "LBI_LG BARRO I"
            df.loc[HORA_NEGATIVA, "val_geracao"] = -1.5
            df = df.drop(index=HORA_AUSENTE)
            duplicada = df[df.index == HORA_DUPLICADA].copy()
            duplicada["val_geracao"] = 99.0          # valor antigo, descartado
            df = pd.concat([duplicada, df], ignore_index=False)
        if chave == "CJ_EOL":
            df.loc[list(GAP_LONGO), "val_geracao"] = np.nan
        partes.append(df)

    bruto = pd.concat(partes, ignore_index=True)
    for coluna in bruto.columns:
        if coluna not in ("din_instante", "val_geracao", "arquivo_origem"):
            bruto[coluna] = bruto[coluna].astype("string")
    return bruto


# Cadastro da ANEEL, no formato CRU da API (nomes CamelCase, números com vírgula
# decimal e ponto de milhar, datas ISO em texto).
CADASTRO = [
    # ceg,                    nome,                        tipo,  UF,  fase,         kW,          lat,       lon,      operação,     município
    ("UFV.RS.BA.000001-0.1", "Bom Sol I",                  "UFV", "BA", "Operação",  "30.000,00", "-10,10", "-41,10", "2019-05-10", "Bom Jesus da Lapa - BA"),
    ("EOL.CV.BA.000010-0.1", "Ventos de Santa Eugenia 01", "EOL", "BA", "Operação",  "30.000,00", "-11,00", "-42,00", "2015-03-01", "Caetité - BA"),
    ("EOL.CV.BA.000011-0.1", "Ventos de Santa Eugenia 02", "EOL", "BA", "Operação",  "30.000,00", "-11,20", "-42,20", "2015-07-15", "Caetité - BA, Igaporã - BA"),
    # em Construção: não entra no vínculo por nome, embora o nome case
    ("EOL.CV.BA.000012-0.1", "Ventos de Santa Eugenia 03", "EOL", "BA", "Construção", "30.000,00", "-11,40", "-42,40", "1900-01-03", "Caetité - BA"),
    ("EOL.CV.RN.000020-0.1", "Morro do Chapeu Sul II 01",  "EOL", "RN", "Operação",  "10.000,00", "-5,60",  "-35,90", "2017-01-20", "João Câmara - RN"),
    # mesma UF e fonte do conjunto, nome que não casa: fica sem vínculo
    ("EOL.CV.RN.000021-0.1", "Serra do Mel 01",            "EOL", "RN", "Operação",  "20.000,00", "-5,10",  "-37,00", "2016-11-05", "Serra do Mel - RN"),
    # abaixo de POTENCIA_MINIMA_MW_VINCULO (1 MW)
    ("UFV.RS.CE.000030-0.1", "Pequena Solar",              "UFV", "CE", "Operação",  "500,00",    "-4,00",  "-38,50", "2021-08-01", "Quixeré - CE"),
    # coordenada (0,0) e data-sentinela 1900-01-03: as duas sujeiras conhecidas
    ("UFV.RS.MG.000040-0.1", "Sol Sem Coordenada",         "UFV", "MG", "Operação",  "5.000,00",  "0",      "0",      "1900-01-03", "Não Informado"),
]


def aneel_bruto(data_retrato: str = "2026-07-04") -> pd.DataFrame:
    linhas = []
    for i, (ceg, nome, tipo, uf, fase, kw, lat, lon, operacao, municipios) in enumerate(CADASTRO, 1):
        linhas.append({
            "_id": i,
            "DatGeracaoConjuntoDados": data_retrato,
            "NomEmpreendimento": nome,
            "IdeNucleoCEG": ceg.split(".")[3].split("-")[0],
            "CodCEG": ceg,
            "SigUFPrincipal": uf,
            "SigTipoGeracao": tipo,
            "DscFaseUsina": fase,
            "DscOrigemCombustivel": "Eólica" if tipo == "EOL" else "Solar",
            "DscFonteCombustivel": "Cinética do vento" if tipo == "EOL" else "Radiação solar",
            "DscTipoOutorga": "Registro",
            "NomFonteCombustivel": "Cinética do vento" if tipo == "EOL" else "Radiação solar",
            "DatEntradaOperacao": operacao,
            "MdaPotenciaOutorgadaKw": kw,
            "MdaPotenciaFiscalizadaKw": kw,
            "MdaGarantiaFisicaKw": "0,00",
            "IdcGeracaoQualificada": "Não",
            "NumCoordNEmpreendimento": lat,
            "NumCoordEEmpreendimento": lon,
            "DatInicioVigencia": "2015-01-01",
            "DatFimVigencia": "2045-01-01",
            "DscPropriRegimePariticipacao": "nome de pessoa (descartado por LGPD)",
            "DscSubBacia": "",
            "DscMuninicpios": municipios,
        })
    return pd.DataFrame(linhas)


# Pontos de clima da NASA: um perto das usinas da Bahia, um perto das do RN.
LOCAIS = [
    ("bom_jesus_ba", "Bom Jesus da Lapa", "BA", "NE", -10.0, -41.0),
    ("joao_camara_rn", "João Câmara", "RN", "NE", -5.5, -35.8),
]
FILL = -999.0
DIAS_DIARIO = 5
DIA_AUSENTE = 2          # linha removida: gap de 1 dia, interpolável
GAP_VENTO_HORARIO = range(5, 7)   # 2 horas nulas no WS50M: interpoláveis


def nasa_bruto() -> pd.DataFrame:
    """Série horária. A irradiância vem toda com o fill value da API: é a
    latência de ~3 meses da variável horária, não um erro."""
    partes = []
    for local, municipio, uf, subsistema, lat, lon in LOCAIS:
        instantes = pd.date_range(INICIO, periods=48, freq="h")
        df = pd.DataFrame({
            "local": local, "municipio": municipio, "id_estado": uf,
            "id_subsistema": subsistema, "latitude": lat, "longitude": lon,
            "data_hora_utc": instantes.strftime("%Y%m%d%H"),
            "ALLSKY_SFC_SW_DWN": FILL,
            "WS10M": np.round(3 + np.sin(np.arange(48) / 4), 2),
            "WS50M": np.round(6 + np.sin(np.arange(48) / 4), 2),
            "T2M": np.round(25 + 4 * np.sin(np.arange(48) / 6), 2),
        })
        df.loc[list(GAP_VENTO_HORARIO), "WS50M"] = FILL
        partes.append(df)
    return pd.concat(partes, ignore_index=True)


def nasa_diario_bruto() -> pd.DataFrame:
    """Série diária. O último dia ainda não tem irradiância publicada (~1 semana
    de latência) e um dia do meio está ausente."""
    partes = []
    for local, municipio, uf, subsistema, lat, lon in LOCAIS:
        datas = pd.date_range(INICIO, periods=DIAS_DIARIO, freq="D")
        df = pd.DataFrame({
            "local": local, "municipio": municipio, "id_estado": uf,
            "id_subsistema": subsistema, "latitude": lat, "longitude": lon,
            "data_lst": datas.strftime("%Y%m%d"),
            "ALLSKY_SFC_SW_DWN": [5.0, 5.2, 5.4, 5.6, FILL],
            "WS10M": [3.0, 3.2, 3.4, 3.6, 3.8],
            "WS50M": [6.0, 6.2, 6.4, 6.6, 6.8],
            "T2M": [25.0, 25.5, 26.0, 26.5, 27.0],
            "T2M_MAX": [31.0, 31.5, 32.0, 32.5, 33.0],
            "T2M_MIN": [19.0, 19.5, 20.0, 20.5, 21.0],
        })
        if local == "bom_jesus_ba":
            df = df.drop(index=DIA_AUSENTE)
        partes.append(df)
    return pd.concat(partes, ignore_index=True)


# Eventos simulados, por usina da ANEEL (é o grão em que a simulação roda).
EVENTOS = [
    # ceg,                    entrada,      evento, data_evento,  tempo_anos
    ("UFV.RS.BA.000001-0.1", "2019-05-10", 1, "2022-05-10", 3.00),
    ("EOL.CV.BA.000010-0.1", "2015-03-01", 0, None,        11.35),
    ("EOL.CV.BA.000011-0.1", "2015-07-15", 1, "2018-07-15", 3.00),
    ("EOL.CV.RN.000020-0.1", "2017-01-20", 0, None,         9.45),
    # usina em Construção, sem vínculo: tem de ser descartada na curated
    ("EOL.CV.BA.000012-0.1", "2020-01-01", 1, "2023-01-01", 3.00),
]


def manutencao_simulada() -> pd.DataFrame:
    fonte = {"UFV": "solar", "EOL": "eolica"}
    linhas = []
    for i, (ceg, entrada, evento, data_evento, tempo) in enumerate(EVENTOS, 1):
        tipo = ceg[:3]
        linhas.append({
            "id_usina": i, "ceg": ceg, "fonte": fonte[tipo], "sig_tipo_geracao": tipo,
            "id_estado": ceg.split(".")[2], "id_subsistema": "NE", "potencia_mw": 30.0,
            "data_entrada_operacao": pd.Timestamp(entrada), "data_corte": DATA_CORTE,
            "tempo_anos": tempo, "evento": evento,
            "data_evento": pd.Timestamp(data_evento) if data_evento else pd.NaT,
            "tipo_evento": "inversor" if tipo == "UFV" else "caixa_multiplicadora",
            "motivo_censura": "evento" if evento else "administrativa",
            "fim_observacao_anos": tempo, "frailty": 1.0,
        })
    df = pd.DataFrame(linhas)
    df["tipo_evento"] = df["tipo_evento"].where(df["evento"] == 1).astype("string")
    return df


def escrever(bruto: Path, simulado: Path) -> None:
    """Grava o bruto de brinquedo no layout que a ingestão produz."""
    pasta_ons = bruto / "dados_ons_bruto"
    pasta_ons.mkdir(parents=True, exist_ok=True)
    ons_bruto().to_parquet(pasta_ons / "dados_ons_bruto_2026_07.parquet", index=False)
    aneel_bruto().to_parquet(bruto / "dados_aneel_bruto.parquet", index=False)
    nasa_bruto().to_parquet(bruto / "dados_nasa_bruto.parquet", index=False)
    nasa_diario_bruto().to_parquet(bruto / "dados_nasa_diario_bruto.parquet", index=False)
    simulado.parent.mkdir(parents=True, exist_ok=True)
    manutencao_simulada().to_parquet(simulado, index=False)
