"""Banco estático: esquema, carga, restrições e views.

O banco é gerado de `DB/esquema.py`, que também desenha o MER — então o
esquema é a fonte da verdade e o que vale testar é: (1) que a especificação é
coerente consigo mesma, (2) que o DDL gerado dela realmente **impõe** PK, FK e
NOT NULL no DuckDB, (3) que as views calculam o que dizem calcular, e (4) que o
*build-then-swap* nunca deixa a API ver um banco pela metade.

Nenhum teste toca `DB/solarwatch.duckdb`: a curated de brinquedo é gerada da
própria especificação em `tmp_path`, o que faz os testes acompanharem o esquema
quando ele ganha uma coluna.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from DB import criar_banco
from DB.esquema import (INDICES, TABELAS, VIEWS, ddl_tabela, select_de_parquet)

HORAS = 48
INICIO_UTC = datetime(2026, 7, 1, 3, tzinfo=timezone.utc)   # 00h de Brasília


# ----------------------------------------------------- coerência da especificação
def test_toda_pk_e_coluna_obrigatoria():
    """PK nula é um banco que aceita linha sem identidade."""
    for nome, tabela in TABELAS.items():
        obrigatorias = {col for col, _, obrigatoria, _ in tabela["colunas"] if obrigatoria}
        assert set(tabela["pk"]) <= obrigatorias, nome


def test_toda_fk_aponta_para_pk_existente():
    for nome, tabela in TABELAS.items():
        for coluna, (tabela_ref, coluna_ref) in tabela["fk"].items():
            assert coluna in [c for c, _, _, _ in tabela["colunas"]], (nome, coluna)
            assert tabela_ref in TABELAS, (nome, tabela_ref)
            assert TABELAS[tabela_ref]["pk"] == [coluna_ref], (nome, coluna_ref)


def test_indices_referenciam_colunas_que_existem():
    for indice, (tabela, colunas) in INDICES.items():
        assert tabela in TABELAS, indice
        existentes = {c for c, _, _, _ in TABELAS[tabela]["colunas"]}
        assert set(colunas) <= existentes, indice


def test_toda_tabela_declara_origem_grao_e_descricao():
    """O MER e os logs saem daqui; sem isso o diagrama fica mudo."""
    for nome, tabela in TABELAS.items():
        assert tabela["origem"].endswith(".parquet"), nome
        assert tabela["grao"] and tabela["descricao"], nome
        assert all(desc for _, _, _, desc in tabela["colunas"]), nome


def test_nenhuma_coluna_repetida():
    for nome, tabela in TABELAS.items():
        colunas = [c for c, _, _, _ in tabela["colunas"]]
        assert len(colunas) == len(set(colunas)), nome


# ------------------------------------------------------------------------- DDL
def test_ddl_marca_not_null_so_nas_obrigatorias():
    ddl = ddl_tabela("dim_usina")

    assert "usina_id UBIGINT NOT NULL" in ddl
    assert "potencia_mw DOUBLE," in ddl          # opcional: sem NOT NULL
    assert "NOT NULL" not in ddl.split("potencia_mw DOUBLE")[1].split("\n")[0]


def test_ddl_declara_pk_e_fk():
    assert "PRIMARY KEY (usina_id)" in ddl_tabela("dim_usina")

    ddl = ddl_tabela("fato_geracao")
    assert "PRIMARY KEY (usina_id, timestamp_utc)" in ddl
    assert "FOREIGN KEY (usina_id) REFERENCES dim_usina(usina_id)" in ddl


def test_select_de_parquet_faz_cast_na_ordem_do_ddl():
    sql = select_de_parquet("fato_manutencao", Path("x.parquet"))

    assert "CAST(usina_id AS UBIGINT) AS usina_id" in sql
    assert "CAST(simulado AS BOOLEAN) AS simulado" in sql
    ordem = [sql.index(f"AS {col}") for col, _, _, _ in TABELAS["fato_manutencao"]["colunas"]]
    assert ordem == sorted(ordem)
    assert "read_parquet('x.parquet')" in sql


# ------------------------------------------------------- curated de brinquedo
def _valor(tipo: str, i: int):
    return {
        "UBIGINT": i, "INTEGER": i, "DOUBLE": float(i), "BOOLEAN": True,
        "VARCHAR": f"v{i}", "DATE": date(2020, 1, 1),
        "TIMESTAMPTZ": INICIO_UTC, "TIMESTAMP": INICIO_UTC.replace(tzinfo=None),
    }[tipo]


def tabela_de_brinquedo(nome: str, /, n: int = 1, **sobrepor) -> pd.DataFrame:
    """Linhas geradas da própria especificação, com as colunas que importam
    sobrepostas. Assim o teste não quebra quando o esquema ganha uma coluna."""
    dados = {col: [_valor(tipo, i + 1) for i in range(n)]
             for col, tipo, _, _ in TABELAS[nome]["colunas"]}
    dados.update({col: list(valores) for col, valores in sobrepor.items()})
    return pd.DataFrame(dados)


@pytest.fixture
def curated(tmp_path, monkeypatch):
    """Uma curated mínima e coerente: 2 usinas, 48 h de geração, 2 dias de clima."""
    pasta = tmp_path / "curated"
    pasta.mkdir()
    horas = pd.date_range(INICIO_UTC, periods=HORAS, freq="h")

    tabelas = {
        "dim_usina": tabela_de_brinquedo(
            "dim_usina", n=2, usina_id=[1, 2], chave_unidade=["u1", "u2"],
            nome=["Usina Um", "Usina Dois"], fonte=["solar", "eolica"],
            tipo_unidade=["usina", "pequenas_usinas"], regiao=["NE", "N"],
            potencia_mw=[1.0, None],            # a segunda não tem cadastro
            qualidade_vinculo=["exata", "sem_vinculo"],
            metodo_vinculo=["ceg", "sem_vinculo"], n_usinas_aneel=[1, 0]),
        "fato_geracao": tabela_de_brinquedo(
            "fato_geracao", n=HORAS, usina_id=[1] * HORAS, timestamp_utc=horas,
            energia_mwh=[0.5] * HORAS, fonte=["solar"] * HORAS, regiao=["NE"] * HORAS,
            flag_qualidade=["original"] * HORAS),
        "fato_clima": tabela_de_brinquedo(
            "fato_clima", n=2, usina_id=[1, 1],
            data=[date(2026, 7, 1), date(2026, 7, 2)],
            local_clima=["p1", "p1"], irradiancia_kwh_m2=[5.0, 5.5],
            vento_ms=[4.0, 4.5], temperatura_c=[25.0, 26.0]),
        "fato_manutencao": tabela_de_brinquedo(
            "fato_manutencao", n=1, usina_id=[1], tempo_dias=[365],
            evento_ocorreu=[True], simulado=[True]),
        "ponte_usina_aneel": tabela_de_brinquedo(
            "ponte_usina_aneel", n=1, usina_id=[1], ceg_aneel=["UFV.RS.BA.000001-0.1"]),
    }
    for nome, df in tabelas.items():
        df.to_parquet(pasta / TABELAS[nome]["origem"] if nome in TABELAS
                      else pasta / f"{nome}.parquet", index=False)

    monkeypatch.setattr(criar_banco, "CURATED", pasta)
    return pasta


@pytest.fixture
def banco(curated, tmp_path):
    return criar_banco.criar(tmp_path / "teste.duckdb")


def conectar(caminho):
    return duckdb.connect(str(caminho), read_only=True)


# -------------------------------------------------------------------- carga
def test_cria_todas_as_tabelas_com_as_linhas_da_curated(banco):
    con = conectar(banco)
    try:
        assert con.execute("SELECT count(*) FROM dim_usina").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM fato_geracao").fetchone()[0] == HORAS
        assert con.execute("SELECT count(*) FROM fato_clima").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM fato_manutencao").fetchone()[0] == 1
        tabelas = {t[0] for t in con.execute("SHOW TABLES").fetchall()}
        assert set(TABELAS) <= tabelas
    finally:
        con.close()


def test_tipos_declarados_valem_no_banco(banco):
    con = conectar(banco)
    try:
        tipos = dict(con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'dim_usina'").fetchall())
    finally:
        con.close()

    assert tipos["usina_id"] == "UBIGINT"
    assert tipos["potencia_mw"] == "DOUBLE"
    assert tipos["data_operacao"] == "DATE"
    assert tipos["primeira_medicao_utc"].startswith("TIMESTAMP")


def test_indices_e_views_existem(banco):
    con = conectar(banco)
    try:
        indices = {i[0] for i in con.execute(
            "SELECT index_name FROM duckdb_indexes()").fetchall()}
        views = {v[0] for v in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
    finally:
        con.close()

    assert set(INDICES) <= indices
    assert set(VIEWS) == views


# --------------------------------------------------------------- restrições
def test_pk_duplicada_e_recusada_pelo_banco(banco):
    """A validação do ETL já barra antes; aqui se confirma a trava no banco."""
    con = duckdb.connect(str(banco))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO dim_usina (usina_id, chave_unidade, nome, fonte, "
                        "tipo_unidade, regiao, n_usinas_aneel, metodo_vinculo, "
                        "qualidade_vinculo) VALUES "
                        "(1, 'x', 'x', 'solar', 'usina', 'NE', 0, 'ceg', 'exata')")
    finally:
        con.close()


def test_coluna_obrigatoria_nao_aceita_nulo(banco):
    con = duckdb.connect(str(banco))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO dim_usina (usina_id, chave_unidade, nome, fonte, "
                        "tipo_unidade, regiao, n_usinas_aneel, metodo_vinculo, "
                        "qualidade_vinculo) VALUES "
                        "(99, 'x', NULL, 'solar', 'usina', 'NE', 0, 'ceg', 'exata')")
    finally:
        con.close()


def test_fato_orfao_e_recusado_pela_fk(banco):
    con = duckdb.connect(str(banco))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO fato_geracao VALUES "
                        "(999, '2026-07-01 00:00:00+00', 1.0, 'solar', 'NE', 'original')")
    finally:
        con.close()


def test_energia_nula_e_aceita(banco):
    """Hora sem medição existe no banco com `energia_mwh` nulo e flag `faltante`."""
    con = duckdb.connect(str(banco))
    try:
        con.execute("INSERT INTO fato_geracao VALUES "
                    "(1, '2026-08-01 00:00:00+00', NULL, 'solar', 'NE', 'faltante')")
        nulos = con.execute("SELECT count(*) FROM fato_geracao "
                            "WHERE energia_mwh IS NULL").fetchone()[0]
    finally:
        con.close()

    assert nulos == 1


# -------------------------------------------------------------------- views
def test_geracao_diaria_agrega_no_dia_de_brasilia(banco):
    """48 h a partir de 03:00Z viram **dois** dias cheios no fuso de Brasília."""
    con = conectar(banco)
    try:
        linhas = con.execute("SELECT dia, energia_mwh, horas_validas "
                             "FROM vw_geracao_diaria ORDER BY dia").fetchall()
    finally:
        con.close()

    assert [str(l[0]) for l in linhas] == ["2026-07-01", "2026-07-02"]
    assert [l[1] for l in linhas] == [12.0, 12.0]        # 24 x 0,5 MWh
    assert [l[2] for l in linhas] == [24, 24]


def test_horas_faltantes_nao_contam_como_validas(banco):
    con = duckdb.connect(str(banco))
    try:
        con.execute("UPDATE fato_geracao SET flag_qualidade = 'faltante', energia_mwh = NULL "
                    "WHERE timestamp_utc = '2026-07-01 03:00:00+00'")
        dia = con.execute("SELECT energia_mwh, horas_validas FROM vw_geracao_diaria "
                          "WHERE dia = '2026-07-01'").fetchone()
    finally:
        con.close()

    assert dia == (11.5, 23)


def test_fator_de_capacidade_so_sai_de_dia_completo_e_com_potencia(banco):
    con = duckdb.connect(str(banco))
    try:
        linhas = con.execute("SELECT usina_id, fator_capacidade, irradiancia_kwh_m2 "
                             "FROM vw_fator_capacidade_diario ORDER BY dia").fetchall()
        # tira uma hora do primeiro dia: ele deixa de ter 24 horas válidas
        con.execute("UPDATE fato_geracao SET flag_qualidade = 'faltante' "
                    "WHERE timestamp_utc = '2026-07-01 03:00:00+00'")
        depois = con.execute("SELECT count(*) FROM vw_fator_capacidade_diario").fetchone()[0]
    finally:
        con.close()

    assert [l[0] for l in linhas] == [1, 1]            # a usina 2 não tem potência
    assert linhas[0][1] == pytest.approx(0.5)          # 12 MWh / (1 MW x 24 h)
    assert linhas[0][2] == 5.0                         # clima do dia veio junto
    assert depois == 1


def test_geracao_nacional_por_hora_conta_usinas(banco):
    con = conectar(banco)
    try:
        linha = con.execute("SELECT energia_mwh, usinas FROM vw_geracao_nacional_hora "
                            "WHERE timestamp_utc = '2026-07-01 03:00:00+00'").fetchone()
    finally:
        con.close()

    assert linha == (0.5, 1)


# ------------------------------------------------------------ build-then-swap
def test_banco_antigo_sobrevive_a_falha_na_carga(curated, tmp_path, monkeypatch):
    """Se a carga falhar, a API continua servindo o banco anterior."""
    destino = tmp_path / "banco.duckdb"
    criar_banco.criar(destino)
    tamanho_original = destino.stat().st_size
    (curated / "fato_clima.parquet").unlink()

    with pytest.raises(FileNotFoundError, match="camada curated"):
        criar_banco.criar(destino)

    assert destino.stat().st_size == tamanho_original
    con = conectar(destino)
    try:
        assert con.execute("SELECT count(*) FROM fato_clima").fetchone()[0] == 2
    finally:
        con.close()


def test_recarga_substitui_o_banco_inteiro(curated, tmp_path):
    """Cada carga reconstrói do zero: não há resto da carga anterior."""
    destino = tmp_path / "banco.duckdb"
    criar_banco.criar(destino)

    dim = pd.read_parquet(curated / "dim_usina.parquet").iloc[:1]
    dim.to_parquet(curated / "dim_usina.parquet", index=False)
    geracao = pd.read_parquet(curated / "fato_geracao.parquet")
    geracao.to_parquet(curated / "fato_geracao.parquet", index=False)
    criar_banco.criar(destino)

    con = conectar(destino)
    try:
        assert con.execute("SELECT count(*) FROM dim_usina").fetchone()[0] == 1
    finally:
        con.close()


def test_temporario_nao_fica_para_tras(curated, tmp_path):
    destino = tmp_path / "banco.duckdb"

    criar_banco.criar(destino)

    assert not list(tmp_path.glob("*.tmp"))


def test_curated_ausente_diz_o_que_rodar(tmp_path, monkeypatch):
    monkeypatch.setattr(criar_banco, "CURATED", tmp_path / "vazia")

    with pytest.raises(FileNotFoundError, match="ETL.pipeline"):
        criar_banco.criar(tmp_path / "x.duckdb")


def test_resumo_responde_as_consultas_da_api(banco, caplog):
    """Fumaça: as consultas que a API faz rodam contra o banco recém-criado."""
    import logging

    with caplog.at_level(logging.INFO, logger="DB.criar_banco"):
        criar_banco.resumo(banco)

    mensagens = " ".join(r.getMessage() for r in caplog.records)
    assert "usinas por fonte" in mensagens
    assert "fator de capacidade" in mensagens


# ---------------------------------------------------------------------- MER
def test_mer_e_desenhado_a_partir_da_mesma_especificacao(tmp_path):
    """O diagrama sai de `esquema.py`, então não pode sair de sincronia com o banco."""
    import matplotlib
    matplotlib.use("Agg")
    from DB import mer

    saida = mer.desenhar(tmp_path / "mer.png", dpi=50)

    assert saida.exists() and saida.stat().st_size > 1000
    # toda tabela da especificação tem uma caixa declarada no layout
    assert set(mer.POSICOES) == set(TABELAS), "o MER perdeu (ou inventou) uma tabela"
