"""Utilitários do ETL: leitura, grade temporal, interpolação e chaves.

A regra que mais importa aqui é a da interpolação: um buraco de até
`MAX_GAP_INTERPOLACAO_H` horas (3) é preenchido linearmente; um de 4 horas fica
inteiro nulo — e não parcialmente preenchido, que é o que o
`interpolate(limit=...)` do pandas faria.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ETL.config import MAX_GAP_INTERPOLACAO_H
from ETL.utils import (base_ceg, completar_grade, flag_qualidade,
                       interpolar_gaps_curtos, ler_parquet, listar_faltantes)


def serie(valores, chave="usina_a", inicio="2026-01-01", freq="h") -> pd.DataFrame:
    """Uma série temporal contígua com uma medida por período."""
    return pd.DataFrame({
        "chave": chave,
        "instante": pd.date_range(inicio, periods=len(valores), freq=freq),
        "medida": np.array(valores, dtype="float64"),
    })


def com_buraco(tamanho: int) -> pd.DataFrame:
    """Série com um buraco de `tamanho` nulos entre dois valores válidos.

    Os extremos estão sobre a reta y = 10x, então o valor interpolado correto de
    cada hora do buraco é conhecido exatamente.
    """
    return serie([0.0] + [np.nan] * tamanho + [10.0 * (tamanho + 1)])


# ----------------------------------------------------- interpolar_gaps_curtos
def test_buraco_de_3_horas_e_interpolado():
    df, interpolado, faltante = interpolar_gaps_curtos(
        com_buraco(3), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert not df["medida"].isna().any()
    assert interpolado.sum() == 3
    assert faltante.sum() == 0
    np.testing.assert_allclose(df["medida"], [0, 10, 20, 30, 40])


def test_buraco_de_4_horas_nao_e_interpolado():
    """Nem parcialmente: as 4 horas continuam nulas."""
    df, interpolado, faltante = interpolar_gaps_curtos(
        com_buraco(4), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert df["medida"].isna().sum() == 4
    assert interpolado.sum() == 0
    assert faltante.sum() == 4


def test_buraco_longo_nao_e_preenchido_pela_metade():
    """A diferença em relação ao `interpolate(limit=3)` do pandas.

    Com `limit`, o pandas preencheria as 3 primeiras horas de um buraco de 24 e
    deixaria 21 nulas — inventando dado no começo do buraco e devolvendo uma
    série meio medida, meio sintética. Aqui o buraco é medido antes de decidir.
    """
    entrada = com_buraco(24)
    df, _, _ = interpolar_gaps_curtos(entrada, "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert df["medida"].isna().sum() == 24
    # o comportamento que estamos evitando, para o teste não ser tautológico
    assert entrada["medida"].interpolate(limit=3).isna().sum() == 21


@pytest.mark.parametrize("tamanho", [1, 2, 3])
def test_buracos_ate_o_limite_sao_preenchidos(tamanho):
    df, interpolado, _ = interpolar_gaps_curtos(
        com_buraco(tamanho), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert interpolado.sum() == tamanho
    assert not df["medida"].isna().any()


@pytest.mark.parametrize("tamanho", [4, 5, 12, 48])
def test_buracos_acima_do_limite_ficam_nulos(tamanho):
    df, interpolado, faltante = interpolar_gaps_curtos(
        com_buraco(tamanho), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert interpolado.sum() == 0
    assert df["medida"].isna().sum() == tamanho == faltante.sum()


def test_nao_extrapola_as_pontas():
    """`limit_area="inside"`: a latência da NASA nunca é inventada.

    Um nulo no fim da série não tem valor válido depois dele, então não há o que
    interpolar — mesmo sendo um buraco de uma hora só.
    """
    df, interpolado, faltante = interpolar_gaps_curtos(
        serie([np.nan, 10.0, 20.0, np.nan]), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert interpolado.sum() == 0
    assert list(faltante) == [True, False, False, True]


def test_nao_interpola_entre_series_diferentes():
    """O buraco do fim da usina A não é fechado com o começo da usina B."""
    a = serie([1.0, 2.0, np.nan], chave="usina_a")
    b = serie([np.nan, 5.0, 6.0], chave="usina_b")

    df, interpolado, faltante = interpolar_gaps_curtos(
        pd.concat([a, b], ignore_index=True), "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert interpolado.sum() == 0
    assert faltante.sum() == 2


def test_cada_serie_tem_seu_proprio_buraco():
    """Buracos de tamanhos diferentes na mesma chamada: um passa, o outro não."""
    curto = com_buraco(2).assign(chave="usina_a")
    longo = com_buraco(6).assign(chave="usina_b")

    df, interpolado, _ = interpolar_gaps_curtos(
        pd.concat([curto, longo], ignore_index=True), "chave", ["medida"],
        MAX_GAP_INTERPOLACAO_H)

    nulos = df.groupby("chave")["medida"].apply(lambda s: int(s.isna().sum()))
    assert nulos["usina_a"] == 0
    assert nulos["usina_b"] == 6
    assert interpolado.sum() == 2


def test_varias_colunas_marcam_interpolado_e_faltante_na_mesma_linha():
    """Uma linha pode ter uma medida preenchida e outra ainda nula."""
    df = serie([0.0, np.nan, 20.0])
    df["outra"] = [0.0, np.nan, np.nan]  # buraco que toca a ponta: não interpola

    df, interpolado, faltante = interpolar_gaps_curtos(
        df, "chave", ["medida", "outra"], MAX_GAP_INTERPOLACAO_H)

    assert list(interpolado) == [False, True, False]
    assert list(faltante) == [False, True, True]
    assert flag_qualidade(interpolado, faltante).tolist() == [
        "original", "faltante", "faltante"]


def test_nao_modifica_o_dataframe_recebido():
    entrada = com_buraco(2)
    interpolar_gaps_curtos(entrada, "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert entrada["medida"].isna().sum() == 2


def test_serie_sem_nulos_passa_intacta():
    entrada = serie([1.0, 2.0, 3.0])
    df, interpolado, faltante = interpolar_gaps_curtos(
        entrada, "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    pd.testing.assert_frame_equal(df, entrada)
    assert not interpolado.any() and not faltante.any()


# ------------------------------------------------------------ completar_grade
def test_grade_completa_segue_o_caminho_rapido():
    entrada = serie([1.0, 2.0, 3.0])
    df, criadas = completar_grade(entrada, "chave", "instante", ["chave"])

    assert criadas == 0
    assert df is entrada  # nada foi copiado nem remontado


def test_hora_ausente_virou_linha_nula():
    entrada = serie([1.0, 2.0, 3.0, 4.0]).drop(index=2).reset_index(drop=True)
    df, criadas = completar_grade(entrada, "chave", "instante", ["chave"])

    assert criadas == 1
    assert len(df) == 4
    assert df["instante"].diff().dropna().eq(np.timedelta64(1, "h")).all()
    criada = df[df["medida"].isna()]
    assert len(criada) == 1
    assert criada["chave"].iat[0] == "usina_a"  # atributo fixo herdado da série


def test_atributos_fixos_sao_preenchidos_nas_linhas_criadas():
    entrada = serie([1.0, 2.0, 3.0]).assign(fonte="solar", id_estado="BA")
    entrada = entrada.drop(index=1).reset_index(drop=True)

    df, criadas = completar_grade(entrada, "chave", "instante", ["fonte", "id_estado"])

    assert criadas == 1
    assert df["fonte"].notna().all() and df["id_estado"].notna().all()
    assert set(df["fonte"]) == {"solar"}


def test_grade_respeita_os_limites_de_cada_serie():
    """Cada série é completada entre o SEU primeiro e o SEU último instante.

    A usina B entrou em operação depois; a grade não inventa horas anteriores à
    primeira medição dela nem estende a série da usina A.
    """
    a = serie([1.0, 2.0, 3.0], chave="usina_a", inicio="2026-01-01 00:00")
    b = pd.concat([serie([9.0], chave="usina_b", inicio="2026-01-01 03:00"),
                   serie([5.0], chave="usina_b", inicio="2026-01-01 06:00")],
                  ignore_index=True)

    df, criadas = completar_grade(pd.concat([a, b], ignore_index=True),
                                  "chave", "instante", ["chave"])

    limites = df.groupby("chave")["instante"].agg(["min", "max", "size"])
    assert limites.loc["usina_a", "size"] == 3                      # já estava completa
    assert limites.loc["usina_b", "size"] == 4                      # 03h..06h
    assert limites.loc["usina_b", "min"] == pd.Timestamp("2026-01-01 03:00")
    assert limites.loc["usina_a", "max"] == pd.Timestamp("2026-01-01 02:00")
    assert criadas == 2


def test_grade_diaria():
    entrada = serie([1.0, 2.0, 3.0, 4.0], freq="D").drop(index=[1, 2]).reset_index(drop=True)
    df, criadas = completar_grade(entrada, "chave", "instante", ["chave"], freq="D")

    assert criadas == 2
    assert len(df) == 4
    assert df["medida"].isna().sum() == 2


def test_linha_ausente_e_valor_nulo_recebem_o_mesmo_tratamento():
    """A razão de existir da grade: os dois tipos de buraco viram a mesma coisa.

    Aqui a hora 1 está *ausente* e a hora 3 está *nula*. Depois da grade, as
    duas são nulos interpoláveis e saem com o mesmo valor da reta.
    """
    entrada = serie([0.0, 10.0, 20.0, 30.0, 40.0])
    entrada.loc[3, "medida"] = np.nan
    entrada = entrada.drop(index=1).reset_index(drop=True)

    df, criadas = completar_grade(entrada, "chave", "instante", ["chave"])
    df, interpolado, faltante = interpolar_gaps_curtos(
        df, "chave", ["medida"], MAX_GAP_INTERPOLACAO_H)

    assert criadas == 1
    assert interpolado.sum() == 2 and faltante.sum() == 0
    np.testing.assert_allclose(df["medida"], [0, 10, 20, 30, 40])


# ------------------------------------------------------------------- base_ceg
def test_base_ceg_iguala_ons_e_aneel():
    """O ONS usa sufixo de versão de 2 dígitos e a ANEEL de 1; a base é a mesma."""
    ons = base_ceg(pd.Series(["EOL.EL.BA.000190-2.01"]))
    aneel = base_ceg(pd.Series(["EOL.EL.BA.000190-2.1"]))

    assert ons.iat[0] == aneel.iat[0] == "EOL.EL.BA.000190-2"


@pytest.mark.parametrize(("ceg", "esperado"), [
    ("UFV.RS.MG.045678-1.01", "UFV.RS.MG.045678-1"),
    ("UFV.RS.MG.045678-1.1", "UFV.RS.MG.045678-1"),
    # sem sufixo de versão, o último trecho removido é o número da usina: a
    # função pressupõe o sufixo, que as duas fontes sempre trazem
    ("UFV.RS.MG.045678-1", "UFV.RS.MG"),
])
def test_base_ceg_remove_apenas_o_ultimo_trecho(ceg, esperado):
    assert base_ceg(pd.Series([ceg])).iat[0] == esperado


def test_base_ceg_preserva_nulos():
    resultado = base_ceg(pd.Series(["EOL.EL.CE.000001-1.01", None], dtype="string"))

    assert resultado.iat[0] == "EOL.EL.CE.000001-1"
    assert pd.isna(resultado.iat[1])


# ------------------------------------------- listar_faltantes / flag_qualidade
def test_listar_faltantes_nomeia_as_medidas_ausentes():
    """A latência desigual da NASA: falta a irradiância, o resto é utilizável."""
    df = pd.DataFrame({
        "irradiancia_kwh_m2": [5.0, np.nan, np.nan],
        "vento_ms": [3.0, 4.0, np.nan],
    })

    lista = listar_faltantes(df, ["irradiancia_kwh_m2", "vento_ms"])

    assert pd.isna(lista.iat[0])
    assert lista.iat[1] == "irradiancia_kwh_m2"
    assert lista.iat[2] == "irradiancia_kwh_m2,vento_ms"


def test_listar_faltantes_e_nulo_quando_nada_falta():
    df = pd.DataFrame({"a": [1.0], "b": [2.0]})

    assert listar_faltantes(df, ["a", "b"]).isna().all()


def test_flag_qualidade_respeita_a_precedencia():
    indice = range(4)
    interpolado = pd.Series([False, True, False, True], index=indice)
    faltante = pd.Series([False, False, True, True], index=indice)
    extra = pd.Series([pd.NA, pd.NA, pd.NA, "suspeito"], index=indice, dtype="string")

    flag = flag_qualidade(interpolado, faltante, extra)

    assert flag.tolist() == ["original", "interpolado", "faltante", "faltante"]


def test_flag_extra_vence_original_e_perde_para_interpolado():
    interpolado = pd.Series([False, True])
    faltante = pd.Series([False, False])
    extra = pd.Series(["suspeito", "suspeito"], dtype="string")

    assert flag_qualidade(interpolado, faltante, extra).tolist() == [
        "suspeito", "interpolado"]


# ---------------------------------------------------------------- ler_parquet
def test_ler_parquet_concatena_a_pasta(tmp_path):
    """O bruto do ONS é um arquivo por mês, lido como um dataset só."""
    for mes in (1, 2, 3):
        pd.DataFrame({"mes": [mes, mes]}).to_parquet(
            tmp_path / f"dados_ons_bruto_2026_{mes:02d}.parquet")

    df = ler_parquet(tmp_path)

    assert len(df) == 6
    assert sorted(df["mes"].unique()) == [1, 2, 3]


def test_ler_parquet_ignora_sobras_de_escrita_interrompida(tmp_path):
    pd.DataFrame({"mes": [1]}).to_parquet(tmp_path / "bom.parquet")
    (tmp_path / "interrompido.parquet.tmp").write_bytes(b"lixo")

    assert len(ler_parquet(tmp_path)) == 1


def test_ler_parquet_aceita_arquivo_unico(tmp_path):
    caminho = tmp_path / "unico.parquet"
    pd.DataFrame({"a": [1, 2]}).to_parquet(caminho)

    assert len(ler_parquet(caminho)) == 2


def test_ler_parquet_falha_em_pasta_vazia(tmp_path):
    with pytest.raises(FileNotFoundError, match="Nenhum"):
        ler_parquet(tmp_path)
