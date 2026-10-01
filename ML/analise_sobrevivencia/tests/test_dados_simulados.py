"""Testes do gerador de eventos simulados.

Três grupos, que respondem a perguntas diferentes:

1. **Invariantes** do esquema de saída (doc §9): o arquivo é internamente
   coerente — tempo positivo, evento e data combinando, intervalos encadeados.
2. **Determinismo:** a mesma semente dá o mesmo arquivo, e sementes diferentes
   dão arquivos diferentes. Sem isso, nada do que a documentação afirma sobre
   os dados é verificável depois.
3. **Recuperação dos parâmetros:** replicando a população, os `beta` estimados
   convergem para os do gerador. É o teste que pega um erro de sinal ou de
   escala no modelo gerador, que os invariantes não pegariam.

Nenhum teste toca o arquivo gravado em `dados/simulados/`: tudo é gerado em
memória a partir do bruto da ANEEL.

    pytest ML/analise_sobrevivencia/tests -q
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ML.analise_sobrevivencia import dados_simulados as gerador

SEMENTE = 42
COLUNAS_ESPERADAS = [
    "id_usina", "ceg", "fonte", "sig_tipo_geracao", "id_estado", "id_subsistema",
    "potencia_mw", "data_entrada_operacao", "data_corte", "tempo_anos", "evento",
    "data_evento", "tipo_evento", "motivo_censura", "fim_observacao_anos", "frailty",
]

pytestmark = pytest.mark.skipif(
    not gerador.ENTRADA.exists(),
    reason="bruto da ANEEL ausente; rode python -m ingestao.aneel.ingestao_aneel",
)


@pytest.fixture(scope="module")
def populacao():
    return gerador.carregar_usinas(1.0)


@pytest.fixture(scope="module")
def eventos(populacao):
    usinas, data_corte = populacao
    return gerador.simular(usinas, data_corte, SEMENTE)


# ------------------------------------------------------------------ invariantes
def test_esquema_de_saida(eventos):
    assert list(eventos.columns) == COLUNAS_ESPERADAS
    assert eventos["id_usina"].is_unique
    assert eventos["ceg"].is_unique


def test_tempo_positivo_e_evento_binario(eventos):
    assert (eventos["tempo_anos"] > 0).all()
    assert set(eventos["evento"].unique()) <= {0, 1}
    assert eventos["evento"].sum() > 0, "um conjunto sem nenhum evento não serviria para nada"


def test_evento_data_e_tipo_andam_juntos(eventos):
    com_evento = eventos["evento"] == 1
    assert (eventos.loc[com_evento, "data_evento"].notna()).all()
    assert (eventos.loc[com_evento, "tipo_evento"].notna()).all()
    assert (eventos.loc[~com_evento, "data_evento"].isna()).all()
    assert (eventos.loc[~com_evento, "tipo_evento"].isna()).all()


def test_evento_acontece_dentro_da_janela_de_observacao(eventos):
    com_evento = eventos[eventos["evento"] == 1]
    assert (com_evento["data_evento"] <= com_evento["data_corte"]).all()
    assert (com_evento["data_evento"] >= com_evento["data_entrada_operacao"]).all()


def test_tempo_bate_com_as_datas(eventos):
    """`tempo_anos` tem que ser a distância entre a entrada em operação e o fim
    da observação daquela usina — senão o dado conta duas histórias."""
    fim = eventos["data_evento"].fillna(
        eventos["data_entrada_operacao"]
        + pd.to_timedelta(eventos["fim_observacao_anos"] * 365.25, unit="D"))
    esperado = (fim - eventos["data_entrada_operacao"]).dt.days / 365.25
    assert np.allclose(eventos["tempo_anos"], esperado, atol=0.01)


def test_tipos_de_evento_pertencem_a_fonte(eventos):
    for fonte, probabilidades in gerador.TIPOS_EVENTO.items():
        tipos = eventos.loc[eventos["fonte"] == fonte, "tipo_evento"].dropna().unique()
        assert set(tipos) <= set(probabilidades), f"tipo estranho na fonte {fonte}"


def test_motivo_da_censura_e_coerente_com_o_evento(eventos):
    assert (eventos.loc[eventos["evento"] == 1, "motivo_censura"] == "evento").all()
    motivos = set(eventos.loc[eventos["evento"] == 0, "motivo_censura"])
    assert motivos <= {"administrativa", "perda_acompanhamento"}
    assert "perda_acompanhamento" in motivos, "censura aleatória ligada deveria aparecer"


def test_fragilidade_e_positiva_e_centrada_em_um(eventos):
    assert (eventos["frailty"] > 0).all()
    assert eventos["frailty"].mean() == pytest.approx(1.0, abs=0.1)


# -------------------------------------------------------------------- flags
def test_frailty_desligada_deixa_todos_em_um(populacao):
    usinas, data_corte = populacao
    sem = gerador.simular(usinas, data_corte, SEMENTE, variancia_frailty=0.0)
    assert (sem["frailty"] == 1.0).all()


def test_censura_aleatoria_desligada_some_com_a_perda(populacao):
    usinas, data_corte = populacao
    sem = gerador.simular(usinas, data_corte, SEMENTE, taxa_censura=0.0)
    assert "perda_acompanhamento" not in set(sem["motivo_censura"])
    # e o fim da observação volta a ser só a janela administrativa
    administrativa = (sem["data_corte"] - sem["data_entrada_operacao"]).dt.days / 365.25
    assert np.allclose(sem["fim_observacao_anos"], administrativa, atol=0.01)


def test_mais_censura_aleatoria_significa_menos_eventos(populacao):
    usinas, data_corte = populacao
    poucos = gerador.simular(usinas, data_corte, SEMENTE, taxa_censura=0.0)["evento"].sum()
    muitos = gerador.simular(usinas, data_corte, SEMENTE, taxa_censura=0.20)["evento"].sum()
    assert muitos < poucos


# --------------------------------------------------------------- determinismo
def test_mesma_semente_gera_o_mesmo_conjunto(populacao, eventos):
    usinas, data_corte = populacao
    repetido = gerador.simular(usinas, data_corte, SEMENTE)
    pd.testing.assert_frame_equal(eventos, repetido)


def test_sementes_diferentes_geram_conjuntos_diferentes(populacao, eventos):
    usinas, data_corte = populacao
    outro = gerador.simular(usinas, data_corte, SEMENTE + 1)
    assert not np.allclose(eventos["tempo_anos"], outro["tempo_anos"])
    # a população é a mesma, só os sorteios mudam
    assert eventos["ceg"].tolist() == outro["ceg"].tolist()


# ---------------------------------------------------------------- recorrentes
@pytest.fixture(scope="module")
def painel(populacao, eventos):
    usinas, data_corte = populacao
    return gerador.simular_recorrentes(eventos, usinas, data_corte, SEMENTE)


def test_episodios_sao_encadeados_sem_buraco(painel):
    ordenado = painel.sort_values(["id_usina", "episodio"])
    anterior = ordenado.groupby("id_usina")["t_fim_anos"].shift().fillna(0.0)
    assert np.allclose(ordenado["t_inicio_anos"], anterior)
    assert (painel["t_fim_anos"] > painel["t_inicio_anos"]).all()
    assert (painel["gap_anos"] > 0).all()


def test_ultimo_episodio_e_sempre_censurado(painel):
    ultimos = painel.sort_values(["id_usina", "episodio"]).groupby("id_usina").tail(1)
    assert (ultimos["evento"] == 0).all()


def test_primeiro_episodio_reproduz_o_arquivo_de_primeiro_evento(painel, eventos):
    primeiro = painel[painel["episodio"] == 1].set_index("id_usina")
    referencia = eventos.set_index("id_usina")
    assert np.allclose(primeiro["t_fim_anos"], referencia["tempo_anos"])
    assert (primeiro["evento"] == referencia["evento"]).all()


def test_a_usina_carrega_a_mesma_fragilidade_em_todos_os_episodios(painel):
    assert (painel.groupby("id_usina")["frailty"].nunique() == 1).all()


# ------------------------------------------------------- variante com PH violado
@pytest.fixture(scope="module")
def ph_violado(populacao):
    usinas, data_corte = populacao
    return gerador.simular_ph_violado(usinas, data_corte, SEMENTE)


def test_variante_tem_eventos_dos_dois_lados_do_corte(ph_violado):
    """Sem eventos nos dois períodos não há como estimar os dois betas."""
    com_evento = ph_violado[ph_violado["evento"] == 1]
    contagem = com_evento["periodo_do_evento"].value_counts()
    assert contagem.get("antes", 0) > 50
    assert contagem.get("depois", 0) > 50


def test_periodo_do_evento_bate_com_o_corte(ph_violado):
    corte = gerador.EFEITO_TEMPO_DEPENDENTE["corte_anos"]
    antes = ph_violado["periodo_do_evento"] == "antes"
    assert (ph_violado.loc[antes, "tempo_anos"] <= corte).all()
    assert (ph_violado.loc[~antes, "tempo_anos"] > corte).all()


def test_variante_nao_tem_fragilidade(ph_violado):
    """A variante isola o efeito tempo-dependente: fragilidade entraria como
    uma segunda causa de violação de PH e tornaria o diagnóstico ambíguo."""
    assert (ph_violado["frailty"] == 1.0).all()


# ------------------------------------------------- recuperação dos parâmetros
@pytest.mark.slow
def test_populacao_replicada_recupera_os_betas(populacao):
    """Com 10x a população e sem fragilidade, o Cox tem que acertar os betas.

    A fragilidade fica desligada de propósito: com ela, o coeficiente marginal
    é **atenuado** por construção, e o teste cobraria do modelo um número que
    ele não deveria devolver.
    """
    from ML.analise_sobrevivencia.diagnosticos import ajustar_cox_em

    usinas, data_corte = populacao
    grande = pd.concat([usinas] * 10, ignore_index=True)
    bruto = gerador.simular(grande, data_corte, SEMENTE, variancia_frailty=0.0)
    resumo = ajustar_cox_em(bruto).summary

    for covariavel in ("log_potencia_mw_c", "ano_entrada_c"):
        linha = resumo.loc[covariavel]
        verdadeiro = gerador.BETA[covariavel]
        assert linha["coef lower 95%"] <= verdadeiro <= linha["coef upper 95%"], (
            f"{covariavel}: {verdadeiro} fora de "
            f"[{linha['coef lower 95%']:.3f}, {linha['coef upper 95%']:.3f}]")
        assert abs(linha["coef"] - verdadeiro) < 0.05


# --------------------------------------------------- variante de riscos competitivos
@pytest.fixture(scope="module")
def competitivos(populacao):
    usinas, data_corte = populacao
    return gerador.simular_riscos_competitivos(usinas, data_corte, SEMENTE)


def test_causa_so_existe_quando_ha_evento(competitivos):
    com_evento = competitivos["evento"] == 1
    assert competitivos.loc[com_evento, "tipo_evento"].notna().all()
    assert competitivos.loc[~com_evento, "tipo_evento"].isna().all()


def test_cada_causa_pertence_a_sua_fonte(competitivos):
    for fonte, causas in gerador.CAUSAS_COMPETITIVAS.items():
        observadas = competitivos.loc[competitivos["fonte"] == fonte, "tipo_evento"].dropna()
        assert set(observadas.unique()) <= set(causas)


def test_a_composicao_das_falhas_muda_com_a_idade(competitivos):
    """É o que distingue riscos competitivos de um rótulo sorteado: causas de
    forma k baixa dominam cedo, as de k alto dominam tarde."""
    eolicas = competitivos[(competitivos["fonte"] == "eolica") & (competitivos["evento"] == 1)]
    cedo = eolicas[eolicas["tempo_anos"] < 2]["tipo_evento"].value_counts(normalize=True)
    tarde = eolicas[eolicas["tempo_anos"] > 6]["tipo_evento"].value_counts(normalize=True)

    # sistema_eletrico tem k = 1,0 (sem desgaste) e caixa_multiplicadora k = 2,2
    assert cedo["sistema_eletrico"] > tarde["sistema_eletrico"]
    assert tarde["caixa_multiplicadora"] > cedo["caixa_multiplicadora"]


def test_variante_competitiva_e_deterministica(populacao, competitivos):
    usinas, data_corte = populacao
    repetido = gerador.simular_riscos_competitivos(usinas, data_corte, SEMENTE)
    pd.testing.assert_frame_equal(competitivos, repetido)


# ------------------------------------------- variante com covariáveis ONS/NASA
@pytest.fixture(scope="module")
def com_clima(populacao):
    usinas, data_corte = populacao
    return gerador.simular_com_clima(usinas, data_corte, SEMENTE)


def test_toda_usina_recebe_clima(com_clima):
    assert com_clima["vento_50m_ms"].notna().all()
    assert com_clima["temperatura_2m_c"].notna().all()
    assert com_clima["ponto_clima"].notna().all()


def test_fator_de_capacidade_e_plausivel_e_marcado(com_clima):
    fc = com_clima["fator_capacidade"]
    assert ((fc > 0) & (fc <= 1)).all(), "fator de capacidade fora de (0, 1]"
    assert com_clima["fator_capacidade_imputado"].any(), "esperava imputação onde falta vínculo"
    assert (~com_clima["fator_capacidade_imputado"]).any(), "esperava algum FC medido"


def test_variante_com_clima_e_deterministica(populacao, com_clima):
    usinas, data_corte = populacao
    repetido = gerador.simular_com_clima(usinas, data_corte, SEMENTE)
    pd.testing.assert_frame_equal(com_clima, repetido)


@pytest.mark.slow
def test_cox_recupera_os_betas_das_covariaveis_externas(com_clima):
    """O sinal vindo do ONS e da NASA tem que ser recuperável — senão a variante
    só estaria acrescentando ruído com nome bonito."""
    from lifelines import CoxPHFitter

    from ML.analise_sobrevivencia.dados import _covariaveis

    df = _covariaveis(com_clima, "potencia_mw", "id_subsistema",
                      com_clima["data_entrada_operacao"].dt.year)
    referencias = gerador.REFERENCIAS_EXTERNAS
    df["vento_50m_c"] = df["vento_50m_ms"] - referencias["vento_50m_ms"]
    df["temperatura_c"] = df["temperatura_2m_c"] - referencias["temperatura_2m_c"]
    df["fator_capacidade_c"] = df["fator_capacidade"] - referencias["fator_capacidade"]

    covariaveis = [*gerador.BETA, *gerador.BETA_EXTERNO]
    modelo = CoxPHFitter().fit(df[["tempo_anos", "evento", "fonte", *covariaveis]],
                               "tempo_anos", "evento", strata=["fonte"])
    for covariavel, verdadeiro in gerador.BETA_EXTERNO.items():
        linha = modelo.summary.loc[covariavel]
        assert linha["coef lower 95%"] <= verdadeiro <= linha["coef upper 95%"], (
            f"{covariavel}: {verdadeiro} fora do IC")
