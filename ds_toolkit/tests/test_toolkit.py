"""`ds_toolkit`: as funções que a pipeline do SolarWatch realmente usa.

O toolkit tem ~60 funções de uso geral (gráficos, ML, estatística). Aqui estão
testadas as **12 que o projeto chama** — se uma delas mudar de comportamento, o
ETL, o ML ou a API mudam com ela, e o teste que acusa isso vale muito mais que
cobertura de uma função que ninguém invoca:

    limpar_nomes_colunas  converter_tipos      tratar_duplicados  mesclar_seguro
    padronizar_texto      relatorio_qualidade  criar_features_data
    salvar_modelo         carregar_modelo      preparar_sobrevivencia
    kaplan_meier          cox_ph               modelos_parametricos_sobrevivencia

O resto é testado por quem o usa, ou não é usado.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import ds_toolkit as dst


# ------------------------------------------------------- limpar_nomes_colunas
def test_limpa_acento_espaco_e_pontuacao():
    """O caso do cadastro da ANEEL: 'MdaPotenciaOutorgadaKw' -> snake_case."""
    df = pd.DataFrame(columns=["Preço do Imóvel (R$)", "MdaPotenciaOutorgadaKw",
                               "  Espaços  nas  Pontas  "])

    colunas = list(dst.limpar_nomes_colunas(df).columns)

    assert colunas == ["preco_do_imovel_r", "mdapotenciaoutorgadakw", "espacos_nas_pontas"]


def test_coluna_que_comeca_com_digito_ganha_prefixo():
    """Nome iniciado por número é inválido em SQL e em acesso por atributo."""
    df = pd.DataFrame(columns=["2024", "2025"])

    assert list(dst.limpar_nomes_colunas(df).columns) == ["col_2024", "col_2025"]


def test_nomes_duplicados_recebem_sufixo():
    """'Preço' e 'preco' colapsariam no mesmo nome; o segundo é desambiguado."""
    df = pd.DataFrame([[1, 2]], columns=["Preço", "preco"])

    assert list(dst.limpar_nomes_colunas(df).columns) == ["preco", "preco_2"]


def test_coluna_sem_nome_ganha_rotulo():
    df = pd.DataFrame([[1]], columns=["   "])

    assert list(dst.limpar_nomes_colunas(df).columns) == ["coluna_sem_nome"]


def test_limpar_nomes_nao_altera_o_original_por_padrao():
    df = pd.DataFrame(columns=["Coluna Um"])

    dst.limpar_nomes_colunas(df)

    assert list(df.columns) == ["Coluna Um"]


# -------------------------------------------------------------- converter_tipos
@pytest.mark.parametrize(("texto", "esperado"), [
    ("1.400,00", 1400.0),         # formato da ANEEL
    ("30.000,00", 30000.0),
    ("R$ 1.234,56", 1234.56),
    ("-20,12", -20.12),
    ("500,00", 500.0),
    ("0", 0.0),
])
def test_numero_brasileiro_vira_float(texto, esperado):
    df = pd.DataFrame({"v": [texto]})

    convertido = dst.converter_tipos(df, colunas_numericas=["v"], verbose=False)

    assert convertido["v"].iat[0] == pytest.approx(esperado)


def test_numero_invalido_vira_nulo_e_nao_explode():
    df = pd.DataFrame({"v": ["1.000,00", "não informado", ""]})

    convertido = dst.converter_tipos(df, colunas_numericas=["v"], verbose=False)

    assert convertido["v"].iat[0] == 1000.0
    assert convertido["v"].isna().sum() == 2


def test_decimal_brasileiro_desligado_le_ponto_como_decimal():
    df = pd.DataFrame({"v": ["1.5"]})

    assert dst.converter_tipos(df, colunas_numericas=["v"], decimal_brasileiro=False,
                               verbose=False)["v"].iat[0] == 1.5
    # com a interpretação brasileira, o ponto é milhar
    assert dst.converter_tipos(df, colunas_numericas=["v"], verbose=False)["v"].iat[0] == 15.0


def test_data_com_formato_explicito():
    df = pd.DataFrame({"d": ["2026-07-04", "1900-01-03", "data ruim"]})

    convertido = dst.converter_tipos(df, colunas_data=["d"], formato_data="%Y-%m-%d",
                                     verbose=False)

    assert convertido["d"].iat[0] == pd.Timestamp("2026-07-04")
    assert convertido["d"].iat[1] == pd.Timestamp("1900-01-03")   # sentinela preservada
    assert pd.isna(convertido["d"].iat[2])


def test_data_brasileira_usa_dayfirst():
    df = pd.DataFrame({"d": ["05/10/2026"]})

    convertido = dst.converter_tipos(df, colunas_data=["d"], verbose=False)

    assert convertido["d"].iat[0] == pd.Timestamp("2026-10-05")    # 5 de outubro


def test_converter_tipos_nao_altera_o_original():
    df = pd.DataFrame({"v": ["1.000,00"]})

    dst.converter_tipos(df, colunas_numericas=["v"], verbose=False)

    assert df["v"].iat[0] == "1.000,00"


# ------------------------------------------------------------ tratar_duplicados
def test_duplicados_por_chave_de_negocio_mantendo_o_ultimo():
    """É o que o clean do ONS e da ANEEL fazem: a última linha é a mais recente."""
    df = pd.DataFrame({"ceg": ["A", "A", "B"], "valor": [1, 2, 3]})

    limpo = dst.tratar_duplicados(df, subset=["ceg"], manter="last", verbose=False)

    assert len(limpo) == 2
    assert limpo.loc[limpo["ceg"] == "A", "valor"].iat[0] == 2


def test_duplicados_reindexa_o_resultado():
    """Sem o reset, o índice fica com buracos e `loc` posicional engana."""
    df = pd.DataFrame({"a": [1, 1, 2]})

    limpo = dst.tratar_duplicados(df, verbose=False)

    assert list(limpo.index) == list(range(len(limpo)))


def test_manter_false_remove_todas_as_ocorrencias():
    df = pd.DataFrame({"a": [1, 1, 2]})

    assert list(dst.tratar_duplicados(df, manter=False, verbose=False)["a"]) == [2]


# --------------------------------------------------------------- mesclar_seguro
def test_merge_valida_a_cardinalidade_esperada():
    """`validar` é o que impede um join N:N silencioso de inflar a fato."""
    esquerda = pd.DataFrame({"k": [1, 2], "a": ["x", "y"]})
    direita_duplicada = pd.DataFrame({"k": [1, 1], "b": ["p", "q"]})

    with pytest.raises(pd.errors.MergeError):
        dst.mesclar_seguro(esquerda, direita_duplicada, on="k", como="left",
                           validar="one_to_one", verbose=False)


def test_merge_remove_a_coluna_de_diagnostico():
    """O indicador é interno; não pode vazar para a camada curated."""
    esquerda = pd.DataFrame({"k": [1], "a": ["x"]})
    direita = pd.DataFrame({"k": [1], "b": ["y"]})

    resultado = dst.mesclar_seguro(esquerda, direita, on="k", como="left", verbose=False)

    assert "_merge_status" not in resultado.columns
    assert list(resultado.columns) == ["k", "a", "b"]


def test_merge_left_preserva_as_linhas_sem_par():
    esquerda = pd.DataFrame({"k": [1, 2], "a": ["x", "y"]})
    direita = pd.DataFrame({"k": [1], "b": ["p"]})

    resultado = dst.mesclar_seguro(esquerda, direita, on="k", como="left", verbose=False)

    assert len(resultado) == 2
    assert pd.isna(resultado.loc[resultado["k"] == 2, "b"].iat[0])


def test_merge_inner_descarta_quem_nao_tem_par():
    esquerda = pd.DataFrame({"k": [1, 2]})
    direita = pd.DataFrame({"k": [2, 3]})

    resultado = dst.mesclar_seguro(esquerda, direita, on="k", como="inner", verbose=False)

    assert list(resultado["k"]) == [2]


def test_merge_avisa_explosao_de_linhas(capsys):
    """O relatório é a razão de existir da função: um left join que AUMENTA."""
    esquerda = pd.DataFrame({"k": [1], "a": ["x"]})
    direita = pd.DataFrame({"k": [1, 1], "b": ["p", "q"]})

    resultado = dst.mesclar_seguro(esquerda, direita, on="k", como="left", verbose=True)

    saida = capsys.readouterr().out
    assert len(resultado) == 2
    assert "ATENÇÃO" in saida and "AUMENTOU" in saida


def test_merge_por_varias_chaves():
    esquerda = pd.DataFrame({"k1": [1, 1], "k2": ["a", "b"], "v": [10, 20]})
    direita = pd.DataFrame({"k1": [1], "k2": ["b"], "w": [99]})

    resultado = dst.mesclar_seguro(esquerda, direita, on=["k1", "k2"], como="left",
                                   verbose=False)

    assert resultado.loc[resultado["k2"] == "b", "w"].iat[0] == 99
    assert pd.isna(resultado.loc[resultado["k2"] == "a", "w"].iat[0])


# -------------------------------------------------------------- padronizar_texto
def test_padroniza_grafias_do_mesmo_nome():
    """É o que sustenta o vínculo por nome: 'Caetité  2' e 'CAETITE 2' colapsam."""
    df = pd.DataFrame({"nome": ["Caetité  2", "CAETITE 2", " caetite 2 "]})

    padronizado = dst.padronizar_texto(
        df, ["nome"], minusculas=True, remover_acentos=True, remover_pontuacao=True,
        verbose=False)
    # o ETL ainda colapsa os espaços repetidos depois da pontuação removida
    valores = padronizado["nome"].str.replace(r"\s+", " ", regex=True).str.strip()

    assert set(valores) == {"caetite 2"}


def test_pontuacao_e_removida_e_nao_trocada_por_espaco():
    """Consequência prática: um hífen COLA as palavras.

    'caetite-2' vira 'caetite2', que **não** casa com 'caetite 2'. O vínculo
    por nome do ETL depende disso: grafias com hífen no lugar do espaço não são
    equivalentes às com espaço, e é por isso que o `_vincular` cai no fallback
    de remover palavras do fim em vez de confiar na normalização.
    """
    df = pd.DataFrame({"nome": ["caetite-2", "caetite 2"]})

    padronizado = dst.padronizar_texto(df, ["nome"], remover_pontuacao=True, verbose=False)

    assert list(padronizado["nome"]) == ["caetite2", "caetite 2"]


def test_padronizar_texto_preserva_nulos():
    df = pd.DataFrame({"t": ["  Um  ", None]})

    padronizado = dst.padronizar_texto(df, ["t"], verbose=False)

    assert padronizado["t"].iat[0] == "um"
    assert pd.isna(padronizado["t"].iat[1])


def test_padronizar_texto_aplica_substituicoes_por_ultimo():
    df = pd.DataFrame({"t": ["N/I", "vv"]})

    padronizado = dst.padronizar_texto(
        df, ["t"], mapa_substituicoes={"n/i": "", "vv": "vila velha"}, verbose=False)

    assert list(padronizado["t"]) == ["", "vila velha"]


def test_padronizar_texto_escolhe_as_colunas_de_texto_sozinho():
    df = pd.DataFrame({"t": [" A "], "n": [1]})

    padronizado = dst.padronizar_texto(df, verbose=False)

    assert padronizado["t"].iat[0] == "a"
    assert padronizado["n"].iat[0] == 1          # numérica intacta


# ----------------------------------------------------------- relatorio_qualidade
def test_relatorio_aponta_nulos_e_cardinalidade():
    df = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "constante": ["x"] * 4,
        "com_nulos": [1.0, None, None, None],
    })

    diag = dst.relatorio_qualidade(df, verbose=False).set_index("coluna")

    assert diag.loc["com_nulos", "nulos"] == 3
    assert "constante" in str(diag.loc["constante", "flags"])
    assert "possivel_id" in str(diag.loc["id", "flags"])
    assert "muitos_nulos" in str(diag.loc["com_nulos", "flags"])


def test_relatorio_tem_uma_linha_por_coluna():
    df = pd.DataFrame({"a": [1], "b": [2], "c": [3]})

    assert len(dst.relatorio_qualidade(df, verbose=False)) == 3


def test_relatorio_de_dataframe_vazio_nao_explode():
    dst.relatorio_qualidade(pd.DataFrame({"a": []}), verbose=False)


# --------------------------------------------------------- criar_features_data
def test_features_de_data_extrai_os_componentes():
    df = pd.DataFrame({"d": pd.to_datetime(["2026-07-04", "2026-01-01"])})

    com_features = dst.criar_features_data(
        df, "d", componentes=("ano", "mes", "dia_semana", "fim_de_semana"), verbose=False)

    assert list(com_features["d_ano"]) == [2026, 2026]
    assert list(com_features["d_mes"]) == [7, 1]
    assert com_features["d_dia_semana"].iat[0] == 5          # 04/07/2026 é sábado
    assert com_features["d_fim_de_semana"].iat[0] == 1
    assert com_features["d_fim_de_semana"].iat[1] == 0       # 01/01/2026 é quinta


def test_features_ciclicas_preservam_a_circularidade():
    """Dezembro é vizinho de janeiro: é o motivo de existir seno/cosseno."""
    df = pd.DataFrame({"d": pd.to_datetime(["2026-12-15", "2026-01-15", "2026-06-15"])})

    com = dst.criar_features_data(df, "d", componentes=("mes",), ciclicas=True, verbose=False)
    ponto = lambda i: np.array([com["d_mes_sen"].iat[i], com["d_mes_cos"].iat[i]])
    distancia = lambda i, j: float(np.linalg.norm(ponto(i) - ponto(j)))

    assert distancia(0, 1) < distancia(0, 2)    # dez-jan mais perto que dez-jun


def test_features_de_data_convertem_texto_antes():
    df = pd.DataFrame({"d": ["04/07/2026"]})

    com = dst.criar_features_data(df, "d", componentes=("ano",), verbose=False)

    assert com["d_ano"].iat[0] == 2026


# ------------------------------------------------------ salvar/carregar_modelo
def test_modelo_vai_e_volta_com_metadados(tmp_path, capsys):
    from sklearn.linear_model import LinearRegression

    modelo = LinearRegression().fit([[0], [1]], [0, 1])
    caminho = tmp_path / "sub" / "modelo.pkl"

    dst.salvar_modelo(modelo, caminho, metadados={"rmse": 1.5}, verbose=False)
    recarregado = dst.carregar_modelo(caminho, verbose=False)

    assert caminho.exists()                       # criou a pasta
    meta = json.loads(caminho.with_name(caminho.name + ".meta.json").read_text(encoding="utf-8"))
    assert meta["classe"] == "LinearRegression"
    assert meta["rmse"] == 1.5
    assert "sklearn_versao" in meta and "salvo_em" in meta
    assert recarregado.predict([[2]])[0] == pytest.approx(2.0)


def test_carregar_modelo_avisa_mudanca_de_versao_do_sklearn(tmp_path):
    """O aviso que explica previsão estranha depois de atualizar a lib."""
    from sklearn.linear_model import LinearRegression

    caminho = tmp_path / "m.pkl"
    dst.salvar_modelo(LinearRegression().fit([[0], [1]], [0, 1]), caminho, verbose=False)
    meta_path = caminho.with_name(caminho.name + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sklearn_versao"] = "0.0.1"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.warns(UserWarning, match="sklearn"):
        dst.carregar_modelo(caminho, verbose=False)


def test_carregar_modelo_sem_metadados_funciona(tmp_path):
    import joblib

    caminho = tmp_path / "cru.pkl"
    joblib.dump({"a": 1}, caminho)

    assert dst.carregar_modelo(caminho, verbose=False) == {"a": 1}


# ------------------------------------------------------------- sobrevivência
@pytest.fixture
def dados_sobrevivencia():
    """Coorte pequena com efeito conhecido: o grupo 1 falha antes."""
    rng = np.random.default_rng(42)
    n = 200
    grupo = rng.integers(0, 2, n)
    tempo = rng.exponential(scale=np.where(grupo == 1, 2.0, 6.0))
    corte = 8.0
    return pd.DataFrame({
        "tempo": np.minimum(tempo, corte),
        "evento": (tempo <= corte).astype(int),
        "grupo": grupo.astype(float),
    })


def test_preparar_sobrevivencia_deriva_o_tempo_das_datas():
    """Com início e fim, o tempo é calculado na unidade pedida."""
    df = pd.DataFrame({
        "inicio": pd.to_datetime(["2020-01-01", "2020-01-01"]),
        "fim": pd.to_datetime(["2021-01-01", "2022-01-01"]),
        "evento": [1, 0],
    })

    preparado = dst.preparar_sobrevivencia(
        df, "tempo_anos", "evento", coluna_inicio="inicio", coluna_fim="fim",
        unidade="anos", verbose=False)

    assert preparado["tempo_anos"].iat[0] == pytest.approx(366 / 365.25, abs=0.01)
    assert preparado["tempo_anos"].iat[1] == pytest.approx(731 / 365.25, abs=0.01)
    assert preparado["evento"].tolist() == [1, 0]


def test_preparar_sobrevivencia_remove_tempo_invalido():
    """Tempo nulo ou <= 0 não serve para os modelos: sai com aviso."""
    df = pd.DataFrame({"tempo": [10.0, 0.0, -1.0, None, 5.0], "evento": [1, 1, 0, 1, 0]})

    preparado = dst.preparar_sobrevivencia(df, "tempo", "evento", verbose=False)

    assert preparado["tempo"].tolist() == [10.0, 5.0]
    assert list(preparado.index) == [0, 1]


def test_preparar_sobrevivencia_mapeia_evento_textual():
    df = pd.DataFrame({"tempo": [1.0, 2.0], "status": ["obito", "vivo"]})

    preparado = dst.preparar_sobrevivencia(
        df, "tempo", "status", mapa_evento={"obito": 1, "vivo": 0}, verbose=False)

    assert preparado["status"].tolist() == [1, 0]
    assert preparado["status"].dtype == int


def test_preparar_sobrevivencia_descarta_evento_fora_de_0_1():
    df = pd.DataFrame({"tempo": [1.0, 2.0, 3.0], "evento": [1, 0, 7]})

    preparado = dst.preparar_sobrevivencia(df, "tempo", "evento", verbose=False)

    assert len(preparado) == 2


def test_kaplan_meier_decresce_e_estima_a_mediana(dados_sobrevivencia):
    resultado = dst.kaplan_meier(dados_sobrevivencia, "tempo", "evento",
                                 plotar=False, verbose=False)

    km = resultado["ajustes"]["geral"]
    valores = km.survival_function_.iloc[:, 0].to_numpy()

    assert valores[0] <= 1.0
    assert np.all(np.diff(valores) <= 1e-9)        # monótona não crescente
    mediana = resultado["medianas"]["geral"]["mediana"]
    assert 0 < mediana < 8                         # dentro da janela observada


def test_kaplan_meier_por_grupo_separa_as_curvas(dados_sobrevivencia):
    resultado = dst.kaplan_meier(dados_sobrevivencia, "tempo", "evento",
                                 coluna_grupo="grupo", plotar=False, verbose=False)

    assert set(resultado["ajustes"]) == {"0.0", "1.0"}
    # o grupo 1 falha antes: mediana menor
    assert (resultado["medianas"]["1.0"]["mediana"]
            < resultado["medianas"]["0.0"]["mediana"])
    # e o log-rank detecta a diferença
    assert resultado["logrank"]["p_valor"] < 0.001


def test_cox_ph_recupera_o_sinal_do_efeito(dados_sobrevivencia):
    """O grupo 1 falha antes, então o coeficiente tem de ser positivo."""
    resultado = dst.cox_ph(dados_sobrevivencia, "tempo", "evento",
                           covariaveis=["grupo"], plotar=False, verbose=False)

    modelo = resultado["modelo"] if isinstance(resultado, dict) else resultado
    beta = float(modelo.params_["grupo"])
    assert beta > 0
    assert float(modelo.summary.loc["grupo", "p"]) < 0.01


def test_modelos_parametricos_comparam_por_aic(dados_sobrevivencia):
    resultado = dst.modelos_parametricos_sobrevivencia(
        dados_sobrevivencia, "tempo", "evento", plotar=False, verbose=False)

    assert set(resultado["modelo"]) == {"exponencial", "weibull", "lognormal",
                                        "loglogistico"}
    # a tabela vem ordenada do melhor (menor AIC) para o pior
    assert list(resultado["AIC"]) == sorted(resultado["AIC"])
    por_modelo = resultado.set_index("modelo")
    # a exponencial tem um parâmetro (risco constante); a Weibull, dois
    assert len(por_modelo.loc["exponencial", "parametros"]) == 1
    assert len(por_modelo.loc["weibull", "parametros"]) == 2
    assert (por_modelo["mediana_prevista"] > 0).all()
