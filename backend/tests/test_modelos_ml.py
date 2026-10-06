"""Registro de modelos e conexão com o banco: a degradação graciosa (§10.5).

A regra do system design é: se um modelo não carregar, o processo **não cai** e
só os endpoints daquele modelo respondem 503. O `test_api.py` verifica o efeito
em uma rota; aqui se verifica o mecanismo, com modelos de mentira em `tmp_path`
— incluindo os casos que não dá para provocar com o banco real (pickle
corrompido, parquet sem a coluna de índice, pasta inexistente).
"""

from __future__ import annotations

import json

import duckdb
import joblib
import pandas as pd
import pytest

from backend import db
from backend.modelos_ml import FONTES_PREVISAO, RegistroModelos

NOMES = dict(nome_sobrevivencia="sobrevivencia_cox.pkl",
             padrao_previsao="previsao_{fonte}.pkl",
             nome_probabilidades="probabilidades.parquet",
             nome_recorrencia="recorrencia.pkl",
             nome_esperadas="esperadas.parquet")


class FalsoPrevisor:
    """Dublê com a mesma superfície que o registro acessa ao logar."""

    nome = "falso"
    horizonte = 24
    horizontes_meses = (6, 12)


@pytest.fixture
def pasta_completa(tmp_path):
    joblib.dump(FalsoPrevisor(), tmp_path / NOMES["nome_sobrevivencia"])
    joblib.dump(FalsoPrevisor(), tmp_path / NOMES["nome_recorrencia"])
    for fonte in FONTES_PREVISAO:
        joblib.dump(FalsoPrevisor(), tmp_path / f"previsao_{fonte}.pkl")
    tabela = pd.DataFrame({"usina_id": [1, 2], "p_sem_manutencao_6m": [0.9, 0.8],
                           "idade_anos": [1.0, 2.0]})
    tabela.to_parquet(tmp_path / NOMES["nome_probabilidades"])
    pd.DataFrame({"usina_id": [1], "manutencoes_esperadas_6m": [0.3],
                  "idade_anos": [1.0], "taxa_relativa": [1.1]}).to_parquet(
        tmp_path / NOMES["nome_esperadas"])
    return tmp_path


# ------------------------------------------------------------------- carga ok
def test_carrega_tudo_quando_os_arquivos_existem(pasta_completa):
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.sobrevivencia is not None and registro.recorrencia is not None
    assert set(registro.previsao) == set(FONTES_PREVISAO)
    assert len(registro.probabilidades) == 2
    assert registro.probabilidades.index.name == "usina_id"
    assert registro.falhas == {}
    assert registro.sobrevivencia_disponivel and registro.recorrencia_disponivel


def test_metadados_do_pickle_sao_lidos(pasta_completa):
    caminho = pasta_completa / NOMES["nome_sobrevivencia"]
    caminho.with_name(caminho.name + ".meta.json").write_text(
        json.dumps({"c_index": 0.71}), encoding="utf-8")
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.metadados["sobrevivencia"]["c_index"] == 0.71


def test_metadados_ausentes_ou_corrompidos_nao_derrubam(pasta_completa):
    caminho = pasta_completa / NOMES["nome_sobrevivencia"]
    caminho.with_name(caminho.name + ".meta.json").write_text("{isso não é json",
                                                              encoding="utf-8")
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.metadados["sobrevivencia"] == {}
    assert registro.sobrevivencia is not None       # o modelo carregou mesmo assim


# ------------------------------------------------------------ degradação
def test_pasta_vazia_registra_falha_sem_excecao(tmp_path):
    registro = RegistroModelos()

    registro.carregar(tmp_path, **NOMES)

    assert registro.sobrevivencia is None and registro.previsao == {}
    assert set(registro.falhas) >= {"sobrevivencia", "recorrencia", "probabilidades",
                                     "esperadas", "previsao_solar", "previsao_eolica"}
    assert not registro.sobrevivencia_disponivel
    assert not registro.previsao_disponivel("solar")


def test_falha_de_um_modelo_nao_contamina_os_outros(pasta_completa):
    """É a regra do §10.5: só o endpoint dependente degrada."""
    (pasta_completa / NOMES["nome_sobrevivencia"]).unlink()
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.sobrevivencia is None
    assert "sobrevivencia" in registro.falhas
    assert set(registro.previsao) == set(FONTES_PREVISAO)
    assert registro.recorrencia is not None
    # a tabela pré-calculada ainda responde pela sobrevivência
    assert registro.sobrevivencia_disponivel


def test_pickle_corrompido_vira_falha_descrita(pasta_completa):
    (pasta_completa / NOMES["nome_sobrevivencia"]).write_bytes(b"isso nao e um pickle")
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.sobrevivencia is None
    assert ":" in registro.falhas["sobrevivencia"]      # "TipoDoErro: mensagem"


def test_tabela_sem_a_coluna_de_indice_vira_falha(pasta_completa):
    pd.DataFrame({"outra": [1]}).to_parquet(pasta_completa / NOMES["nome_probabilidades"])
    registro = RegistroModelos()

    registro.carregar(pasta_completa, **NOMES)

    assert registro.probabilidades is None
    assert "probabilidades" in registro.falhas


def test_so_a_tabela_pre_calculada_ja_atende(tmp_path):
    """Sem o pickle, a API ainda serve as usinas que estão na tabela."""
    pd.DataFrame({"usina_id": [1], "p_sem_manutencao_6m": [0.9]}).to_parquet(
        tmp_path / NOMES["nome_probabilidades"])
    registro = RegistroModelos()

    registro.carregar(tmp_path, **NOMES)

    assert registro.sobrevivencia is None
    assert registro.sobrevivencia_disponivel is True


def test_estado_resume_o_que_carregou_e_o_que_falhou(pasta_completa):
    (pasta_completa / "previsao_solar.pkl").unlink()
    registro = RegistroModelos()
    registro.carregar(pasta_completa, **NOMES)

    estado = registro.estado()

    assert estado["sobrevivencia"] is True
    assert estado["previsao"] == {"solar": False, "eolica": True}
    assert "previsao_solar" in estado["falhas"]
    assert estado["probabilidades_pre_calculadas"] is True


def test_recorrencia_e_esperadas_sao_opcionais(pasta_completa):
    """Chamar sem os nomes novos não deve registrar falha do que não foi pedido."""
    registro = RegistroModelos()

    registro.carregar(pasta_completa, nome_sobrevivencia=NOMES["nome_sobrevivencia"],
                      padrao_previsao=NOMES["padrao_previsao"],
                      nome_probabilidades=NOMES["nome_probabilidades"])

    assert registro.recorrencia is None and registro.esperadas is None
    assert "recorrencia" not in registro.falhas
    assert "esperadas" not in registro.falhas


# --------------------------------------------------------------------- banco
@pytest.fixture
def banco_minimo(tmp_path):
    caminho = tmp_path / "mini.duckdb"
    con = duckdb.connect(str(caminho))
    con.execute("CREATE TABLE dim_usina (usina_id UBIGINT)")
    con.execute("INSERT INTO dim_usina VALUES (1), (2)")
    con.execute("CREATE TABLE fato_geracao (usina_id UBIGINT, timestamp_utc TIMESTAMPTZ, "
                "energia_mwh DOUBLE)")
    con.execute("INSERT INTO fato_geracao VALUES "
                "(1, '2026-07-01 00:00:00+00', 1.0), (1, '2026-07-02 00:00:00+00', 2.0)")
    con.execute("CREATE TABLE fato_clima (usina_id UBIGINT, data DATE)")
    con.execute("INSERT INTO fato_clima VALUES (1, '2026-07-01')")
    con.close()
    yield caminho
    db.fechar()


def test_banco_ausente_diz_o_que_rodar(tmp_path):
    with pytest.raises(FileNotFoundError, match="DB.criar_banco"):
        db.abrir(tmp_path / "nao_existe.duckdb")


def test_conexao_antes_do_startup_e_erro_de_programacao():
    db.fechar()

    assert db.esta_disponivel() is False
    with pytest.raises(RuntimeError, match="lifespan"):
        db.conexao()


def test_cada_request_recebe_um_cursor_isolado(banco_minimo):
    db.abrir(banco_minimo)

    um, outro = db.conexao(), db.conexao()

    assert um is not outro
    assert um.execute("SELECT count(*) FROM dim_usina").fetchone()[0] == 2
    assert db.esta_disponivel() is True


def test_banco_e_aberto_somente_para_leitura(banco_minimo):
    db.abrir(banco_minimo)

    with pytest.raises(duckdb.Error):
        db.conexao().execute("INSERT INTO dim_usina VALUES (99)")


def test_cobertura_resume_periodo_e_volume(banco_minimo):
    db.abrir(banco_minimo)

    cobertura = db.cobertura()

    assert cobertura["usinas"] == 2
    assert cobertura["geracao"]["linhas"] == 2
    assert cobertura["geracao"]["inicio"] < cobertura["geracao"]["fim"]
    assert cobertura["clima"]["linhas"] == 1


def test_fechar_e_idempotente(banco_minimo):
    db.abrir(banco_minimo)

    db.fechar()
    db.fechar()

    assert db.esta_disponivel() is False
