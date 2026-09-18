"""Configuração central do ETL: caminhos, mapeamentos e limiares de limpeza."""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Camadas
BRUTO = RAIZ / "dados" / "bruto"                  # raw: saída da ingestão (+ partições AAAA-MM-DD/)
SIMULADOS = RAIZ / "dados" / "simulados"
CLEAN = RAIZ / "dados" / "limpos" / "clean"
CURATED = RAIZ / "dados" / "limpos" / "curated"

ARQUIVOS_BRUTOS = {
    "ons": "dados_ons_bruto.parquet",
    "nasa": "dados_nasa_bruto.parquet",
    "nasa_diario": "dados_nasa_diario_bruto.parquet",
    "aneel": "dados_aneel_bruto.parquet",
}
ARQUIVO_SIMULADO = SIMULADOS / "eventos_manutencao_simulados.parquet"

# Fuso: ONS publica em horário de Brasília; NASA em UTC. Tudo é normalizado para UTC.
FUSO_BRASIL = "America/Sao_Paulo"

# Gaps: buracos de até N horas consecutivas são interpolados linearmente;
# maiores ficam nulos com flag explícita (interpolar 1 dia de geração solar inventaria dado).
MAX_GAP_INTERPOLACAO_H = 3

# Filtros de qualidade da ANEEL
POTENCIA_MINIMA_MW_VINCULO = 1.0          # usinas elegíveis para vincular ao ONS
DATA_SENTINELA_ANEEL = "1900-01-03"
DATA_MINIMA_PLAUSIVEL_UFV_EOL = "1990-01-01"
LIMITES_BRASIL = {"lat": (-34.0, 5.5), "lon": (-74.0, -34.0)}

# Clima: acima desta distância o vínculo usina -> ponto NASA é marcado como distante
DISTANCIA_MAXIMA_CLIMA_KM = 300.0

FONTE_ONS = {
    "FOTOVOLTAICA": "solar",
    "EOLIELÉTRICA": "eolica",
    "HIDROELÉTRICA": "hidraulica",
    "TÉRMICA": "termica",
    "NUCLEAR": "nuclear",
}
FONTE_ANEEL = {"UFV": "solar", "EOL": "eolica"}
FONTES_CURATED = ("solar", "eolica")

TIPO_UNIDADE_ONS = {
    "Conjunto de Usinas": "conjunto",
    "Pequenas Usinas (MMGD)": "pequenas_usinas",
    "Pequenas Usinas (Tipo III)": "pequenas_usinas",
}  # demais modalidades (TIPO I, II-A, II-B...) = "usina"

# UF -> subsistema do SIN (mesma codificação de id_subsistema do ONS)
UF_SUBSISTEMA = {
    **dict.fromkeys(["AM", "PA", "AP", "RR", "TO", "MA"], "N"),
    **dict.fromkeys(["BA", "RN", "PI", "CE", "PE", "PB", "SE", "AL"], "NE"),
    **dict.fromkeys(["MG", "SP", "RJ", "ES", "GO", "DF", "MT", "MS", "RO", "AC"], "SE"),
    **dict.fromkeys(["RS", "SC", "PR"], "S"),
}

# Palavras genéricas removidas do nome de um conjunto do ONS antes de procurar
# as usinas correspondentes na ANEEL ("CONJUNTO EOLICO CAETITE" -> "caetite")
PALAVRAS_GENERICAS_CONJUNTO = {
    "conjunto", "complexo", "cj", "eolico", "eolica", "eolicas", "fotovoltaico",
    "fotovoltaica", "fotovoltaicas", "solar", "ufv", "eol", "usina", "usinas",
}

VERBOSE_TOOLKIT = True  # relatórios impressos pelas funções do ds_toolkit
