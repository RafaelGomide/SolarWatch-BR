"""Esquema do banco estático do SolarWatch BR — fonte única da verdade.

Tanto o DDL executado por `criar_banco.py` quanto o diagrama MER desenhado por
`mer.py` são gerados a partir daqui, então o desenho não sai de sincronia com o
banco.

Modelo: estrela simples (system design §4). `dim_usina` no centro, três
tabelas-fato em grãos diferentes e uma tabela-ponte de auditoria do vínculo
ONS × ANEEL.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
CURATED = RAIZ / "dados" / "limpos" / "curated"
BANCO = Path(__file__).resolve().parent / "solarwatch.duckdb"

# (nome, tipo DuckDB, obrigatória, descrição)
TABELAS: dict[str, dict] = {
    "dim_usina": {
        "descricao": "Unidade geradora solar/eólica medida pelo ONS (usina, conjunto ou agregado)",
        "origem": "dim_usina.parquet",
        "grao": "usina",
        "pk": ["usina_id"],
        "fk": {},
        "colunas": [
            ("usina_id", "UBIGINT", True, "Identificador sequencial, estável entre cargas"),
            ("chave_unidade", "VARCHAR", True, "Chave natural do ONS (id_ons ou PQU|nome|UF)"),
            ("id_ons", "VARCHAR", False, "Código da unidade no ONS"),
            ("ceg_ons", "VARCHAR", False, "CEG informado pelo ONS"),
            ("nome", "VARCHAR", True, "Nome canônico (mais recente no ONS)"),
            ("nome_normalizado", "VARCHAR", False, "Nome sem acento/pontuação (busca)"),
            ("fonte", "VARCHAR", True, "solar | eolica"),
            ("tipo_unidade", "VARCHAR", True, "usina | conjunto | pequenas_usinas"),
            ("modalidade_ons", "VARCHAR", False, "Modalidade de operação original do ONS"),
            ("regiao", "VARCHAR", True, "Subsistema do SIN: N | NE | SE | S"),
            ("id_estado", "VARCHAR", False, "UF"),
            ("municipio", "VARCHAR", False, "Município (das usinas ANEEL vinculadas)"),
            ("potencia_mw", "DOUBLE", False, "Potência outorgada; nula se o vínculo não é confiável"),
            ("lat", "DOUBLE", False, "Latitude (centroide das usinas vinculadas)"),
            ("lon", "DOUBLE", False, "Longitude (centroide das usinas vinculadas)"),
            ("data_operacao", "DATE", False, "Entrada em operação mais antiga entre as vinculadas"),
            ("n_usinas_aneel", "INTEGER", True, "Quantas usinas da ANEEL foram vinculadas"),
            ("metodo_vinculo", "VARCHAR", True, "ceg | nome | sem_vinculo"),
            ("qualidade_vinculo", "VARCHAR", True, "exata | consistente | inconsistente | sem_vinculo"),
            ("potencia_aneel_vinculada_mw", "DOUBLE", False, "Soma vinculada, sempre (auditoria)"),
            ("pico_geracao_mw", "DOUBLE", False, "Maior geração horária observada"),
            ("razao_pico_potencia", "DOUBLE", False, "pico / potência vinculada (verificação)"),
            ("primeira_medicao_utc", "TIMESTAMPTZ", False, "Início da série de geração"),
            ("ultima_medicao_utc", "TIMESTAMPTZ", False, "Fim da série de geração"),
        ],
    },
    "fato_geracao": {
        "descricao": "Geração verificada pelo ONS",
        "origem": "fato_geracao.parquet",
        "grao": "usina × hora",
        "pk": ["usina_id", "timestamp_utc"],
        "fk": {"usina_id": ("dim_usina", "usina_id")},
        "colunas": [
            ("usina_id", "UBIGINT", True, "FK dim_usina"),
            ("timestamp_utc", "TIMESTAMPTZ", True, "Início da hora, em UTC"),
            ("energia_mwh", "DOUBLE", False, "Energia na hora; nula quando flag = faltante"),
            ("fonte", "VARCHAR", True, "solar | eolica (desnormalizado)"),
            ("regiao", "VARCHAR", True, "Subsistema (desnormalizado)"),
            ("flag_qualidade", "VARCHAR", True, "original | interpolado | negativo_zerado | faltante"),
        ],
    },
    "fato_clima": {
        "descricao": "Clima diário do ponto NASA POWER de referência da usina",
        "origem": "fato_clima.parquet",
        "grao": "usina × dia",
        "pk": ["usina_id", "data"],
        "fk": {"usina_id": ("dim_usina", "usina_id")},
        "colunas": [
            ("usina_id", "UBIGINT", True, "FK dim_usina"),
            ("data", "DATE", True, "Dia (hora solar local ≈ horário de Brasília)"),
            ("irradiancia_kwh_m2", "DOUBLE", False, "Irradiância global horizontal do dia"),
            ("vento_ms", "DOUBLE", False, "Vento médio a 50 m"),
            ("vento_10m_ms", "DOUBLE", False, "Vento médio a 10 m"),
            ("temperatura_c", "DOUBLE", False, "Temperatura média a 2 m"),
            ("temperatura_max_c", "DOUBLE", False, "Temperatura máxima"),
            ("temperatura_min_c", "DOUBLE", False, "Temperatura mínima"),
            ("flag_qualidade", "VARCHAR", True, "original | interpolado | faltante"),
            ("local_clima", "VARCHAR", True, "Ponto NASA usado como referência"),
            ("distancia_km", "DOUBLE", False, "Distância usina → ponto NASA"),
            ("metodo_vinculo_clima", "VARCHAR", True, "mais_proximo | mais_proximo_distante | mesma_uf | mesmo_subsistema"),
        ],
    },
    "fato_manutencao": {
        "descricao": "Tempo até o 1º evento de manutenção corretiva (DADO SIMULADO)",
        "origem": "fato_manutencao.parquet",
        "grao": "usina",
        "pk": ["usina_id"],
        "fk": {"usina_id": ("dim_usina", "usina_id")},
        "colunas": [
            ("usina_id", "UBIGINT", True, "FK dim_usina"),
            ("tempo_dias", "INTEGER", True, "Tempo até o evento ou até a censura"),
            ("evento_ocorreu", "BOOLEAN", True, "true = evento observado; false = censura à direita"),
            ("data_primeiro_evento", "DATE", False, "Data do 1º evento (nula se censurado)"),
            ("tipo_evento", "VARCHAR", False, "Componente afetado"),
            ("n_usinas_consideradas", "INTEGER", True, "Usinas ANEEL agregadas na unidade"),
            ("simulado", "BOOLEAN", True, "Sempre true: dado sintético"),
        ],
    },
    "ponte_usina_aneel": {
        "descricao": "Auditoria do vínculo: usina do cadastro ANEEL → unidade do ONS",
        "origem": "ponte_usina_aneel.parquet",
        "grao": "usina ANEEL",
        "pk": ["ceg_aneel"],
        "fk": {"usina_id": ("dim_usina", "usina_id")},
        "colunas": [
            ("ceg_aneel", "VARCHAR", True, "CEG da usina no cadastro da ANEEL"),
            ("usina_id", "UBIGINT", True, "FK dim_usina"),
            ("chave_unidade", "VARCHAR", True, "Chave natural da unidade do ONS"),
            ("metodo_vinculo", "VARCHAR", True, "ceg | nome"),
            ("nucleo_usado", "VARCHAR", False, "Trecho do nome que casou (vínculo por nome)"),
            ("potencia_outorgada_mw", "DOUBLE", False, "Potência da usina ANEEL"),
            ("municipio", "VARCHAR", False, "Município da usina ANEEL"),
            ("data_entrada_operacao", "DATE", False, "Entrada em operação da usina ANEEL"),
        ],
    },
}

# system design §4.4 — declarados para documentar intenção de acesso
INDICES = {
    "idx_dim_usina_fonte_regiao": ("dim_usina", ["fonte", "regiao"]),
    "idx_fato_geracao_timestamp": ("fato_geracao", ["timestamp_utc"]),
    "idx_fato_clima_data": ("fato_clima", ["data"]),
}

VIEWS = {
    "vw_geracao_diaria": """
        SELECT usina_id,
               CAST(timestamp_utc AT TIME ZONE 'America/Sao_Paulo' AS DATE) AS dia,
               SUM(energia_mwh) AS energia_mwh,
               COUNT(*) FILTER (WHERE flag_qualidade <> 'faltante') AS horas_validas
        FROM fato_geracao
        GROUP BY ALL
    """,
    "vw_geracao_nacional_hora": """
        SELECT timestamp_utc, fonte, regiao,
               SUM(energia_mwh) AS energia_mwh,
               COUNT(DISTINCT usina_id) AS usinas
        FROM fato_geracao
        GROUP BY ALL
    """,
    "vw_fator_capacidade_diario": """
        SELECT g.usina_id, g.dia, u.fonte, u.regiao, g.energia_mwh, u.potencia_mw,
               g.energia_mwh / (u.potencia_mw * 24) AS fator_capacidade,
               c.irradiancia_kwh_m2, c.vento_ms, c.temperatura_c
        FROM vw_geracao_diaria g
        JOIN dim_usina u USING (usina_id)
        LEFT JOIN fato_clima c ON c.usina_id = g.usina_id AND c.data = g.dia
        WHERE u.potencia_mw IS NOT NULL AND g.horas_validas = 24
    """,
}


def ddl_tabela(nome: str) -> str:
    """Monta o CREATE TABLE a partir da especificação."""
    tabela = TABELAS[nome]
    linhas = [
        f"    {col} {tipo}{'' if nulo_ok else ' NOT NULL'}"
        for col, tipo, obrigatoria, _ in tabela["colunas"]
        for nulo_ok in [not obrigatoria]
    ]
    linhas.append(f"    PRIMARY KEY ({', '.join(tabela['pk'])})")
    for col, (tab_ref, col_ref) in tabela["fk"].items():
        linhas.append(f"    FOREIGN KEY ({col}) REFERENCES {tab_ref}({col_ref})")
    return f"CREATE TABLE {nome} (\n" + ",\n".join(linhas) + "\n);"


def select_de_parquet(nome: str, caminho: Path) -> str:
    """SELECT com cast explícito de cada coluna, na ordem do DDL."""
    colunas = ",\n           ".join(
        f"CAST({col} AS {tipo}) AS {col}" for col, tipo, _, _ in TABELAS[nome]["colunas"]
    )
    return f"SELECT {colunas}\n    FROM read_parquet('{caminho.as_posix()}')"
