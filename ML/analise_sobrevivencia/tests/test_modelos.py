"""Modelos de sobrevivência: suporte do Cox, extrapolação Weibull e previsor.

O `test_dados_simulados.py` testa o gerador. Aqui estão os modelos ajustados
sobre ele, com foco nas duas coisas que já quebraram de verdade neste projeto:

1. **o limite de suporte** — a linha de base do Cox fica plana depois do último
   evento com massa, e uma usina antiga caía justamente ali, voltando a dar
   `P = 1,00` (o bug do §10 da doc de sobrevivência);
2. **a sobrevivência condicional** — quem já operou 5 anos sem evento não parte
   do zero, e a conta é `S(t0+h)/S(t0)`, não `S(h)`.

A coorte é sintética e pequena, com efeito conhecido: potência maior => risco
maior, por construção.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ML.analise_sobrevivencia import avaliacao, modelos
from ML.analise_sobrevivencia.config import (ANO_REFERENCIA, COVARIAVEIS, ESTRATO,
                                             HORIZONTES_MESES, POTENCIA_REFERENCIA_MW)
from ML.analise_sobrevivencia.dados import _covariaveis
from ML.analise_sobrevivencia.modelos import (MINIMO_EM_RISCO, PrevisorSobrevivencia,
                                              ajustar_cox, ajustar_weibull_regressao,
                                              limites_de_suporte)

TEMPO, EVENTO = modelos.TEMPO, modelos.EVENTO


def coorte(n: int = 300, seed: int = 7, corte_anos: float = 8.0) -> pd.DataFrame:
    """Coorte sintética com risco crescente na potência e nas duas fontes."""
    rng = np.random.default_rng(seed)
    potencia = np.exp(rng.normal(np.log(POTENCIA_REFERENCIA_MW), 0.7, n))
    fonte = np.where(rng.random(n) < 0.5, "solar", "eolica")
    subsistema = rng.choice(["SE", "NE", "S", "N"], n, p=[0.4, 0.4, 0.15, 0.05])
    ano = rng.integers(2012, 2024, n)
    escala = np.where(fonte == "eolica", 6.0, 9.0) / (potencia / POTENCIA_REFERENCIA_MW) ** 0.3
    tempo = rng.weibull(1.4, n) * escala
    idade_maxima = corte_anos + (2024 - ano)            # usinas antigas observadas por mais tempo

    df = pd.DataFrame({
        "id_usina": np.arange(1, n + 1),
        "potencia_mw": potencia, "fonte": fonte, "id_subsistema": subsistema,
        "data_entrada_operacao": pd.to_datetime(ano.astype(str) + "-01-01"),
        TEMPO: np.minimum(tempo, idade_maxima),
        EVENTO: (tempo <= idade_maxima).astype(int),
    })
    return _covariaveis(df, "potencia_mw", "id_subsistema",
                        df["data_entrada_operacao"].dt.year)


@pytest.fixture(scope="module")
def dados():
    return coorte()


@pytest.fixture(scope="module")
def cox(dados):
    return ajustar_cox(dados)


@pytest.fixture(scope="module")
def previsor(dados, cox):
    return PrevisorSobrevivencia(cox, ajustar_weibull_regressao(dados),
                                 limites_de_suporte(dados))


# ------------------------------------------------------------------ covariáveis
def test_covariaveis_sao_centradas_na_referencia():
    """Centrar faz o intercepto ser a usina de referência — e os betas do
    gerador comparáveis com os estimados."""
    df = pd.DataFrame({"potencia_mw": [POTENCIA_REFERENCIA_MW, 60.0],
                       "id_subsistema": ["SE", "NE"]})

    com = _covariaveis(df, "potencia_mw", "id_subsistema",
                       pd.Series([ANO_REFERENCIA, ANO_REFERENCIA + 5]))

    assert com["log_potencia_mw_c"].iat[0] == 0.0
    assert com["log_potencia_mw_c"].iat[1] == pytest.approx(np.log(2))
    assert com["ano_entrada_c"].tolist() == [0, 5]


def test_subsistema_se_e_a_categoria_de_referencia():
    """SE não ganha coluna: é o nível contra o qual os outros são medidos."""
    df = pd.DataFrame({"potencia_mw": [30.0], "id_subsistema": ["SE"]})

    com = _covariaveis(df, "potencia_mw", "id_subsistema", pd.Series([2018]))

    assert "subsistema_SE" not in com.columns
    assert com[["subsistema_NE", "subsistema_S", "subsistema_N"]].to_numpy().sum() == 0
    assert set(COVARIAVEIS) <= set(com.columns)


# -------------------------------------------------------------- limite de suporte
def test_limite_de_suporte_exige_massa_em_risco(dados):
    """O bug original: bastava existir um evento isolado lá na frente."""
    limites = limites_de_suporte(dados)

    for estrato, grupo in dados.groupby(ESTRATO):
        limite = limites[str(estrato)]
        em_risco = (grupo[TEMPO] >= limite).sum()
        ultimo_evento = grupo.loc[grupo[EVENTO] == 1, TEMPO].max()
        assert em_risco >= MINIMO_EM_RISCO
        assert limite <= ultimo_evento


def test_limite_ignora_evento_isolado_no_fim_da_cauda(dados):
    """Uma usina de 30 anos com evento não pode esticar o suporte do estrato.

    Era exatamente o bug: o limite era o último tempo COM EVENTO, então um
    evento isolado na cauda levava o suporte com ele e a linha de base plana
    entre os dois virava `P = 1,00` para quem caía no meio.
    """
    com_outlier = pd.concat([dados, dados.iloc[[0]].assign(**{TEMPO: 30.0, EVENTO: 1})],
                            ignore_index=True)

    antes = limites_de_suporte(dados)
    depois = limites_de_suporte(com_outlier)

    assert max(depois.values()) < 20            # longe do evento isolado em 30
    # a linha extra só muda o limite na terceira casa (ela conta como 1 em risco)
    for estrato, limite in antes.items():
        assert depois[estrato] == pytest.approx(limite, abs=0.05)


def test_limite_mais_exigente_encurta_o_suporte(dados):
    frouxo = limites_de_suporte(dados, minimo_em_risco=1)
    rigoroso = limites_de_suporte(dados, minimo_em_risco=100)

    assert all(rigoroso[k] <= frouxo[k] for k in frouxo)


def test_limite_existe_mesmo_sem_massa_suficiente():
    """Estrato minúsculo: devolve o primeiro evento em vez de falhar."""
    poucos = coorte(n=25, seed=3)

    limites = limites_de_suporte(poucos, minimo_em_risco=MINIMO_EM_RISCO)

    assert set(limites) == set(poucos[ESTRATO].astype(str).unique())
    assert all(np.isfinite(list(limites.values())))


# ------------------------------------------------------------------------- Cox
def test_cox_estratificado_nao_estima_coeficiente_de_fonte(cox):
    """Estratificar dá linha de base própria a cada fonte: ela sai dos betas."""
    assert ESTRATO not in cox.summary.index
    assert set(cox.params_.index) == set(COVARIAVEIS)


def test_cox_recupera_o_sinal_do_efeito_de_potencia(cox):
    """Na coorte, potência maior falha antes: o coeficiente é positivo."""
    assert float(cox.params_["log_potencia_mw_c"]) > 0
    assert cox.concordance_index_ > 0.55


def test_cox_ignora_linhas_com_covariavel_nula(dados):
    """Usina sem potência confiável não pode entrar no ajuste."""
    com_nulo = dados.copy()
    com_nulo.loc[com_nulo.index[:10], "log_potencia_mw_c"] = np.nan

    modelo = ajustar_cox(com_nulo)

    assert modelo.weights.sum() == len(dados) - 10


# ------------------------------------------------------------------- Weibull AFT
def test_weibull_ajusta_uma_regressao_por_fonte(dados):
    ajustes = ajustar_weibull_regressao(dados)

    assert set(ajustes) == set(dados[ESTRATO].astype(str).unique())
    for ajuste in ajustes.values():
        assert "rho_" in ajuste.params_.index.get_level_values(0)


def test_weibull_continua_decaindo_alem_do_ultimo_evento(dados):
    """A razão de existir da extrapolação: o Cox fica plano, a Weibull não."""
    ajustes = ajustar_weibull_regressao(dados)
    uma = dados.iloc[[0]][COVARIAVEIS]

    curva = ajustes[str(dados[ESTRATO].iat[0])].predict_survival_function(
        uma, times=[10.0, 20.0, 40.0])

    valores = curva.iloc[:, 0].to_numpy()
    assert valores[0] > valores[1] > valores[2]
    assert valores[2] < valores[0]


# --------------------------------------------------------------------- previsor
def test_previsor_devolve_um_horizonte_por_coluna(previsor, dados):
    usinas = dados.head(5).assign(idade_anos=1.0)

    saida = previsor.prever(usinas)

    for meses in HORIZONTES_MESES:
        assert f"p_sem_manutencao_{meses}m" in saida.columns
    assert len(saida) == 5


def test_probabilidade_cai_com_o_horizonte(previsor, dados):
    usinas = dados.head(20).assign(idade_anos=2.0)

    saida = previsor.prever(usinas)
    colunas = [f"p_sem_manutencao_{m}m" for m in sorted(HORIZONTES_MESES)]

    valores = saida[colunas].to_numpy()
    assert np.all(np.diff(valores, axis=1) <= 1e-9)
    assert np.all((valores >= 0) & (valores <= 1))


def test_probabilidade_condicional_e_a_razao_das_curvas(previsor, dados):
    """A conta é S(t0+h)/S(t0), não S(h): é o que "condicional na idade" significa."""
    meses = min(HORIZONTES_MESES)
    horizonte = meses / 12
    uma = dados.iloc[[0]].copy().assign(idade_anos=3.0)

    saida = previsor.prever(uma)
    curva = previsor._curvas(uma, np.array([3.0, 3.0 + horizonte])).iloc[:, 0].to_numpy()

    assert saida[f"p_sem_manutencao_{meses}m"].iat[0] == pytest.approx(
        curva[1] / curva[0], rel=1e-6)
    assert bool(saida["condicional_na_idade"].iat[0]) is True


def test_com_risco_crescente_a_probabilidade_cai_com_a_idade(previsor, dados):
    """Weibull com rho > 1: a usina velha tem risco maior AGORA.

    Não é contraintuitivo — é desgaste. A sobrevivência condicional maior
    aconteceria com risco decrescente (mortalidade infantil), que não é o caso
    de manutenção corretiva de equipamento.
    """
    uma = dados.iloc[[0]].copy()
    coluna = f"p_sem_manutencao_{min(HORIZONTES_MESES)}m"

    nova = previsor.prever(uma.assign(idade_anos=0.5))[coluna].iat[0]
    velha = previsor.prever(uma.assign(idade_anos=6.0))[coluna].iat[0]

    assert velha < nova


def test_sem_idade_a_previsao_e_incondicional(previsor, dados):
    saida = previsor.prever(dados.head(3).drop(columns=["idade_anos"], errors="ignore"),
                            condicional=False)

    assert not saida["condicional_na_idade"].any()


def test_usina_mais_antiga_que_o_suporte_usa_weibull(previsor, dados):
    """É aqui que estava o `P = 1,00`: além do suporte, o método muda."""
    limite = max(previsor.limites_suporte.values())
    uma = dados.iloc[[0]].copy()

    dentro = previsor.prever(uma.assign(idade_anos=limite * 0.5))
    fora = previsor.prever(uma.assign(idade_anos=limite + 5))

    assert dentro["metodo_extrapolacao"].iat[0] == "cox"
    assert fora["metodo_extrapolacao"].iat[0] == "weibull"
    coluna = f"p_sem_manutencao_{max(HORIZONTES_MESES)}m"
    assert fora[coluna].iat[0] < 1.0        # o sintoma do bug era exatamente 1,00


def test_risco_relativo_ordena_as_usinas(previsor, dados):
    uma = dados.iloc[[0]].copy()

    pequena = previsor.prever(uma.assign(log_potencia_mw_c=-1.0, idade_anos=1.0))
    grande = previsor.prever(uma.assign(log_potencia_mw_c=1.0, idade_anos=1.0))

    assert grande["risco_relativo"].iat[0] > pequena["risco_relativo"].iat[0]
    coluna = f"p_sem_manutencao_{max(HORIZONTES_MESES)}m"
    assert grande[coluna].iat[0] < pequena[coluna].iat[0]


def test_horizontes_customizados_sao_respeitados(dados, cox):
    previsor = PrevisorSobrevivencia(cox, horizontes_meses=(3, 9))

    saida = previsor.prever(dados.head(2).assign(idade_anos=1.0))

    assert "p_sem_manutencao_3m" in saida and "p_sem_manutencao_9m" in saida
    assert "p_sem_manutencao_6m" not in saida


def test_tempo_mediano_e_positivo(previsor, dados):
    mediano = previsor.tempo_mediano_anos(dados.head(5))

    assert (mediano > 0).all()


def test_resumo_de_coeficientes_tem_hazard_ratio_e_ic(previsor):
    resumo = previsor.resumo_coeficientes()

    assert set(resumo.columns) >= {"covariavel", "coef", "hazard_ratio",
                                   "ic_inferior", "ic_superior", "p_valor"}
    np.testing.assert_allclose(resumo["hazard_ratio"], np.exp(resumo["coef"]), rtol=1e-6)


# -------------------------------------------------------------------- avaliação
def test_cindex_fora_da_amostra_fica_abaixo_do_treino(dados, cox):
    """O C-index de treino é otimista; o k-fold é a medida honesta."""
    resultado = avaliacao.cindex_validacao_cruzada(dados, k=3)

    assert resultado["k"] == 3 and len(resultado["scores"]) == 3
    assert 0.4 < resultado["cindex_medio"] < cox.concordance_index_ + 0.05
    assert resultado["cindex_desvio"] >= 0


def test_schoenfeld_tem_uma_linha_por_covariavel(dados, cox):
    tabela = avaliacao.testar_riscos_proporcionais(cox, dados)

    assert set(tabela["covariavel"]) == set(COVARIAVEIS)
    assert ((tabela["p_valor"] >= 0) & (tabela["p_valor"] <= 1)).all()


def test_recuperacao_dos_betas_marca_quem_esta_dentro_do_ic(dados, cox):
    meta = {"beta_verdadeiro": {c: 0.0 for c in COVARIAVEIS}}

    tabela = avaliacao.recuperacao_dos_parametros(cox, meta)

    assert set(tabela.columns) >= {"covariavel", "estimado", "verdadeiro",
                                    "dentro_do_ic", "erro"}
    np.testing.assert_allclose(tabela["erro"], tabela["estimado"] - tabela["verdadeiro"])


def test_recuperacao_sem_metadados_devolve_tabela_vazia(cox):
    assert avaliacao.recuperacao_dos_parametros(cox, {}).empty


def test_calibracao_compara_previsto_com_km_observado(previsor, dados):
    tabela = avaliacao.calibracao(previsor, dados, tempos_anos=(1, 2), n_grupos=3)

    assert len(tabela) == 6                            # 3 grupos x 2 tempos
    assert set(tabela["grupo_risco"]) == {"g1", "g2", "g3"}
    assert ((tabela["previsto"] >= 0) & (tabela["previsto"] <= 1)).all()
    np.testing.assert_allclose(tabela["erro"], tabela["previsto"] - tabela["observado_km"])
    # o grupo de maior risco tem de sobreviver menos, observado e previsto
    em_1_ano = tabela[tabela["tempo_anos"] == 1].set_index("grupo_risco")
    assert em_1_ano.loc["g3", "previsto"] < em_1_ano.loc["g1", "previsto"]
