"""Regras da `validacao`: PK única, obrigatórias, faixas e integridade referencial.

O contrato é: nada é gravado se alguma regra falhar, e a mensagem lista **todos**
os problemas de uma vez — um `ValueError` por execução, não um por regra.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ETL.validacao import REGRAS, validar


@pytest.fixture
def tabelas() -> dict[str, pd.DataFrame]:
    """Um conjunto curated mínimo e válido: duas usinas, dois fatos de cada."""
    dim = pd.DataFrame({
        "usina_id": [1, 2],
        "chave_unidade": ["usina_a", "usina_b"],
        "nome": ["Usina A", "Usina B"],
        "fonte": ["solar", "eolica"],
        "regiao": ["NE", "S"],
        "potencia_mw": [50.0, 120.0],
        "lat": [-9.4, -29.1],
        "lon": [-40.5, -51.2],
    })
    geracao = pd.DataFrame({
        "usina_id": [1, 1, 2, 2],
        "timestamp_utc": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 01:00"] * 2),
        "energia_mwh": [10.0, 12.0, 30.0, 28.0],
        "fonte": ["solar", "solar", "eolica", "eolica"],
        "flag_qualidade": ["original"] * 4,
    })
    clima = pd.DataFrame({
        "usina_id": [1, 2],
        "data": pd.to_datetime(["2026-01-01", "2026-01-01"]).date,
        "local_clima": ["ne_01", "s_01"],
        "irradiancia_kwh_m2": [6.2, 4.1],
        "vento_ms": [4.0, 8.5],
        "temperatura_c": [28.0, 19.0],
    })
    manutencao = pd.DataFrame({
        "usina_id": [1, 2],
        "tempo_dias": [365, 1200],
        "evento_ocorreu": [1, 0],
    })
    return {"dim_usina": dim, "fato_geracao": geracao, "fato_clima": clima,
            "fato_manutencao": manutencao}


def test_conjunto_valido_passa(tabelas):
    validar(tabelas)  # não levanta


def test_regras_cobrem_as_quatro_tabelas():
    assert set(REGRAS) == {"dim_usina", "fato_geracao", "fato_clima", "fato_manutencao"}


# --------------------------------------------------------------- chave primária
def test_pk_duplicada_na_dimensao(tabelas):
    tabelas["dim_usina"].loc[1, "usina_id"] = 1

    with pytest.raises(ValueError, match="1 chaves primárias duplicadas"):
        validar(tabelas)


def test_pk_composta_duplicada_no_fato(tabelas):
    """Duas medições da mesma usina na mesma hora: o grão foi violado."""
    tabelas["fato_geracao"].loc[1, "timestamp_utc"] = pd.Timestamp("2026-01-01 00:00")

    with pytest.raises(ValueError, match=r"fato_geracao: 1 chaves primárias"):
        validar(tabelas)


def test_mesma_usina_em_horas_diferentes_e_valido(tabelas):
    """A PK é composta: repetir o `usina_id` é o normal num fato horário."""
    assert tabelas["fato_geracao"]["usina_id"].duplicated().any()
    validar(tabelas)


# ---------------------------------------------------------- colunas obrigatórias
@pytest.mark.parametrize(("tabela", "coluna"), [
    ("dim_usina", "nome"),
    ("dim_usina", "fonte"),
    ("fato_geracao", "flag_qualidade"),
    ("fato_clima", "local_clima"),
    ("fato_manutencao", "tempo_dias"),
])
def test_nulo_em_obrigatoria_falha(tabelas, tabela, coluna):
    tabelas[tabela].loc[0, coluna] = None

    with pytest.raises(ValueError, match=rf"{tabela}: 1 nulos em '{coluna}'"):
        validar(tabelas)


def test_nulo_permitido_por_desenho_passa(tabelas):
    """`energia_mwh` nula com flag `faltante` é dado ausente declarado, não erro.

    É o resultado normal de um gap longo demais para interpolar: a hora existe
    na grade, a medida não existe, e a flag diz isso.
    """
    tabelas["fato_geracao"].loc[0, "energia_mwh"] = None
    tabelas["fato_geracao"].loc[0, "flag_qualidade"] = "faltante"

    validar(tabelas)


def test_potencia_nula_na_dimensao_passa(tabelas):
    """Vínculo inconsistente ou ausente publica `potencia_mw` nula de propósito."""
    tabelas["dim_usina"].loc[0, "potencia_mw"] = None

    validar(tabelas)


# ------------------------------------------------------------------------ faixas
@pytest.mark.parametrize(("tabela", "coluna", "valor"), [
    ("dim_usina", "potencia_mw", -1.0),
    ("dim_usina", "lat", -40.0),          # fora do território
    ("dim_usina", "lat", 10.0),
    ("dim_usina", "lon", -80.0),
    ("dim_usina", "lon", 0.0),            # meridiano de Greenwich: sinal trocado
    ("fato_geracao", "energia_mwh", -5.0),
    ("fato_clima", "irradiancia_kwh_m2", 15.0),
    ("fato_clima", "vento_ms", -2.0),
    ("fato_clima", "temperatura_c", 70.0),
    ("fato_clima", "temperatura_c", -20.0),
    ("fato_manutencao", "tempo_dias", 0),  # tempo de sobrevivência começa em 1
])
def test_valor_fora_da_faixa_falha(tabelas, tabela, coluna, valor):
    tabelas[tabela].loc[0, coluna] = valor

    with pytest.raises(ValueError, match=rf"{tabela}: 1 valores de '{coluna}' fora"):
        validar(tabelas)


@pytest.mark.parametrize(("coluna", "valor"), [
    ("potencia_mw", 0.0),
    ("lat", -34.0),
    ("lat", 5.5),
    ("lon", -74.0),
    ("lon", -34.0),
])
def test_borda_da_faixa_e_aceita(tabelas, coluna, valor):
    """As faixas são fechadas: o extremo exato passa."""
    tabelas["dim_usina"].loc[0, coluna] = valor

    validar(tabelas)


def test_faixa_ignora_nulos(tabelas):
    """A faixa é avaliada só sobre os valores presentes (`dropna`)."""
    tabelas["fato_clima"].loc[0, "irradiancia_kwh_m2"] = None

    validar(tabelas)


# ------------------------------------------------------- integridade referencial
def test_fato_orfao_falha(tabelas):
    tabelas["fato_geracao"].loc[3, "usina_id"] = 999

    with pytest.raises(ValueError, match="fato_geracao: 1 linhas com usina_id fora"):
        validar(tabelas)


@pytest.mark.parametrize("tabela", ["fato_geracao", "fato_clima", "fato_manutencao"])
def test_orfaos_sao_checados_em_todos_os_fatos(tabelas, tabela):
    tabelas[tabela].loc[0, "usina_id"] = 999

    with pytest.raises(ValueError, match=rf"{tabela}: 1 linhas com usina_id fora"):
        validar(tabelas)


def test_usina_sem_fato_nao_e_erro(tabelas):
    """A integridade é só numa direção: uma usina pode não ter clima vinculado.

    É o caso dos agregados de pequenas usinas, sem cadastro nem coordenada.
    """
    tabelas["fato_clima"] = tabelas["fato_clima"].iloc[:1]

    validar(tabelas)


# ------------------------------------------------------------ relatório de erros
def test_todos_os_erros_aparecem_de_uma_vez(tabelas):
    """Uma execução, uma lista: não é preciso rodar a pipeline 4 vezes."""
    # uma terceira unidade repetindo o ID 1 — sem *substituir* o 2, para os
    # fatos da usina B não virarem órfãos e inflarem a contagem
    tabelas["dim_usina"] = pd.concat(
        [tabelas["dim_usina"], tabelas["dim_usina"].iloc[[0]].assign(chave_unidade="usina_c")],
        ignore_index=True)
    tabelas["dim_usina"].loc[0, "nome"] = None
    tabelas["fato_clima"].loc[0, "vento_ms"] = 100.0
    tabelas["fato_manutencao"].loc[0, "usina_id"] = 999

    with pytest.raises(ValueError) as erro:
        validar(tabelas)

    mensagem = str(erro.value)
    assert "Validação da camada curated falhou" in mensagem
    assert "chaves primárias duplicadas" in mensagem
    assert "nulos em 'nome'" in mensagem
    assert "valores de 'vento_ms' fora" in mensagem
    assert "linhas com usina_id fora" in mensagem
    assert mensagem.count("\n  - ") == 4


def test_erro_na_dimensao_aparece_tambem_nos_fatos(tabelas):
    """Perder um `usina_id` na dim derruba os fatos dele junto.

    Sobrescrever o ID 2 por 1 produz dois sintomas: a PK duplicada e os fatos da
    usina B virando órfãos. A mensagem mostra os dois, o que ajuda a enxergar a
    causa — é um problema da dimensão, não três problemas independentes.
    """
    tabelas["dim_usina"].loc[1, "usina_id"] = 1

    with pytest.raises(ValueError) as erro:
        validar(tabelas)

    mensagem = str(erro.value)
    assert "dim_usina: 1 chaves primárias duplicadas" in mensagem
    assert "fato_geracao: 2 linhas com usina_id fora da dim_usina" in mensagem
    assert "fato_clima: 1 linhas com usina_id fora da dim_usina" in mensagem
    assert "fato_manutencao: 1 linhas com usina_id fora da dim_usina" in mensagem


def test_contagem_de_problemas_aparece_na_mensagem(tabelas):
    """Saber que são 3 linhas e não 3.000 muda o diagnóstico."""
    tabelas["fato_geracao"]["energia_mwh"] = [-1.0, -2.0, -3.0, 10.0]

    with pytest.raises(ValueError, match="3 valores de 'energia_mwh' fora"):
        validar(tabelas)
