"""Eventos recorrentes: painel de contagem, MCF, Andersen-Gill, PWP e fragilidade.

O que estes testes protegem é a **aritmética do processo de contagem**, onde um
erro não aparece como exceção e sim como número plausível e errado: intervalos
que se sobrepõem, um episódio censurado contado como evento, a MCF caindo, a
exposição da fragilidade medida em anos em vez de em eventos esperados.

Como no `test_dados_simulados.py`, a população vem do bruto real da ANEEL (uma
amostra dela) e nada é lido de `dados/simulados/`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ML.analise_sobrevivencia import dados_simulados as gerador
from ML.analise_sobrevivencia import recorrentes
from ML.analise_sobrevivencia.config import COVARIAVEIS, ESTRATO
from ML.analise_sobrevivencia.recorrentes import (EVENTO, FIM, ID, INICIO,
                                                  MAX_ESTRATO, PrevisorRecorrencia,
                                                  ajustar_andersen_gill,
                                                  ajustar_pwp_gap,
                                                  ajustar_pwp_tempo_total, comparar,
                                                  comparar_erros_padrao,
                                                  estimar_frailty_gama,
                                                  eventos_esperados,
                                                  funcao_media_cumulativa, preparar)

SEMENTE = 42

pytestmark = pytest.mark.skipif(
    not gerador.ENTRADA.exists(),
    reason="bruto da ANEEL ausente; rode python -m ingestao.aneel.ingestao_aneel",
)


@pytest.fixture(scope="module")
def painel():
    """Painel de episódios de uma amostra da população (determinístico)."""
    usinas, data_corte = gerador.carregar_usinas(1.0)
    primeiro = gerador.simular(usinas, data_corte, SEMENTE)
    bruto = gerador.simular_recorrentes(primeiro, usinas, data_corte, SEMENTE)
    return preparar(bruto)


@pytest.fixture(scope="module")
def mcf(painel):
    return funcao_media_cumulativa(painel)


@pytest.fixture(scope="module")
def ag(painel):
    return ajustar_andersen_gill(painel)


# --------------------------------------------------------------------- preparar
def test_intervalos_sao_validos(painel):
    assert (painel[FIM] > painel[INICIO]).all()
    assert (painel[INICIO] >= 0).all()


def test_episodios_de_uma_usina_sao_encadeados_sem_buraco(painel):
    """O fim de um episódio é o início do próximo: o relógio não pula tempo."""
    for _, grupo in painel.sort_values([ID, INICIO]).groupby(ID):
        if len(grupo) < 2:
            continue
        np.testing.assert_allclose(grupo[INICIO].to_numpy()[1:],
                                   grupo[FIM].to_numpy()[:-1], rtol=1e-9)


def test_ultimo_episodio_de_cada_usina_e_censurado(painel):
    """O trecho final é sempre "chegou ao fim da observação sem falhar"."""
    ultimos = painel.sort_values(FIM).groupby(ID).last()

    assert (ultimos[EVENTO] == 0).all()


def test_estrato_de_episodio_e_limitado(painel):
    """Episódios altos entram todos no mesmo estrato: sozinhos têm poucos eventos."""
    assert painel[recorrentes.ESTRATO_EPISODIO].max() <= MAX_ESTRATO
    assert painel[recorrentes.ESTRATO_EPISODIO].min() >= 1
    # o estrato usado no ajuste combina fonte e episódio
    assert painel["estrato"].str.contains(r"^(?:solar|eolica)\|\d+$", regex=True).all()


def test_covariaveis_do_painel_sao_as_do_projeto(painel):
    assert set(COVARIAVEIS) <= set(painel.columns)
    assert painel[COVARIAVEIS].notna().all().all()


def test_intervalo_invalido_e_recusado(painel):
    quebrado = painel.copy()
    quebrado.loc[quebrado.index[0], FIM] = quebrado.loc[quebrado.index[0], INICIO]

    with pytest.raises(ValueError, match="intervalo vazio"):
        preparar(quebrado.drop(columns=[c for c in COVARIAVEIS if c in quebrado]))


def test_uma_usina_pode_ter_vario_eventos(painel):
    """O ponto de todo o módulo: o 1º evento não é o fim da história."""
    por_usina = painel.groupby(ID)[EVENTO].sum()

    assert por_usina.max() >= 2
    assert (por_usina >= 1).mean() > 0.3


# -------------------------------------------------------------------------- MCF
def test_mcf_e_monotona_e_comeca_perto_de_zero(mcf):
    for grupo, curva in mcf.groupby(ESTRATO):
        valores = curva.sort_values("tempo_anos")["mcf"].to_numpy()
        assert np.all(np.diff(valores) >= -1e-12), grupo
        assert valores[0] >= 0


def test_mcf_tem_uma_curva_por_fonte(mcf, painel):
    assert set(mcf[ESTRATO]) == set(painel[ESTRATO].unique())
    assert (mcf["em_risco"] > 0).all()
    assert (mcf["eventos"] > 0).all()


def test_conjunto_de_risco_da_mcf_encolhe_com_o_tempo(mcf):
    """Y(s) é quem ainda está sob observação: nunca cresce."""
    for _, curva in mcf.groupby(ESTRATO):
        em_risco = curva.sort_values("tempo_anos")["em_risco"].to_numpy()
        assert np.all(np.diff(em_risco) <= 0)


def test_mcf_calculada_a_mao_em_um_painel_minimo():
    """Nelson-Aalen na mão: 3 usinas, 2 eventos, conjuntos de risco conhecidos."""
    painel = pd.DataFrame({
        ID: [1, 1, 2, 3],
        INICIO: [0.0, 1.0, 0.0, 0.0],
        FIM: [1.0, 3.0, 2.0, 4.0],
        EVENTO: [1, 0, 1, 0],
        ESTRATO: ["solar"] * 4,
    })

    curva = funcao_media_cumulativa(painel).set_index("tempo_anos")

    # em t=1: as 3 usinas estão sob observação -> incremento 1/3
    assert curva.loc[1.0, "em_risco"] == 3
    assert curva.loc[1.0, "mcf"] == pytest.approx(1 / 3)
    # em t=2: as três ainda estão (o acompanhamento de cada uma vai até o seu
    # último episódio, e a usina 2 termina exatamente em 2) -> incremento 1/3
    assert curva.loc[2.0, "em_risco"] == 3
    assert curva.loc[2.0, "mcf"] == pytest.approx(1 / 3 + 1 / 3)


def test_eventos_esperados_e_a_diferenca_da_mcf(mcf):
    fonte = mcf[ESTRATO].iat[0]

    esperados = eventos_esperados(mcf, idade_anos=2.0, horizonte_anos=1.0, grupo=fonte)
    curva = mcf[mcf[ESTRATO] == fonte]
    direto = (np.interp(3.0, curva["tempo_anos"], curva["mcf"])
              - np.interp(2.0, curva["tempo_anos"], curva["mcf"]))

    assert esperados == pytest.approx(direto)
    assert esperados >= 0


def test_eventos_esperados_cresce_com_o_horizonte(mcf):
    fonte = mcf[ESTRATO].iat[0]

    seis_meses = eventos_esperados(mcf, 3.0, 0.5, fonte)
    tres_anos = eventos_esperados(mcf, 3.0, 3.0, fonte)

    assert 0 <= seis_meses <= tres_anos


def test_eventos_esperados_de_grupo_inexistente_e_nan(mcf):
    assert np.isnan(eventos_esperados(mcf, 1.0, 1.0, "nuclear"))


# ---------------------------------------------------- Andersen-Gill e PWP
def test_andersen_gill_estima_todas_as_covariaveis(ag):
    assert set(ag.params_.index) == set(COVARIAVEIS)
    assert np.isfinite(ag.params_.to_numpy()).all()


def test_andersen_gill_recupera_o_efeito_de_potencia(ag):
    """Potência maior => mais manutenções por ano (é o efeito do gerador).

    O sinal e a magnitude saem certos, mas com o erro padrão **agrupado por
    usina** o efeito fica no limite da significância (p ≈ 0,06 nesta
    população). É a mesma lição de `test_erro_padrao_robusto...`: tratar
    episódios como independentes daria um p bonito e falso.
    """
    beta = float(ag.params_["log_potencia_mw_c"])
    verdadeiro = gerador.BETA["log_potencia_mw_c"]

    assert beta > 0
    assert beta == pytest.approx(verdadeiro, abs=0.15)
    assert float(ag.summary.loc["log_potencia_mw_c", "p"]) < 0.10


def test_erro_padrao_robusto_e_maior_que_o_ingenuo(painel):
    """Episódios da mesma usina são correlacionados (a fragilidade).

    Ignorar o agrupamento produz IC estreito demais — é o erro clássico do
    Andersen-Gill, e o `cluster_col` existe para corrigi-lo.
    """
    comparacao = comparar_erros_padrao(painel).set_index("covariavel")

    assert set(comparacao.index) == set(COVARIAVEIS)
    assert comparacao.loc["log_potencia_mw_c", "razao"] > 1
    np.testing.assert_allclose(comparacao["razao"],
                               comparacao["agrupado"] / comparacao["ingenuo"])
    assert comparacao["razao"].mean() > 1, "o SE agrupado deveria ser maior na média"


def test_pwp_tempo_total_e_gap_time_rodam_e_discordam_do_ag(painel, ag):
    """São perguntas diferentes: taxa (AG) x risco do próximo evento (PWP)."""
    pwp_total = ajustar_pwp_tempo_total(painel)
    pwp_gap = ajustar_pwp_gap(painel)

    tabela = comparar({"andersen_gill": ag, "pwp_tempo_total": pwp_total,
                       "pwp_gap": pwp_gap})

    assert set(tabela["modelo"]) == {"andersen_gill", "pwp_tempo_total", "pwp_gap"}
    assert len(tabela) == 3 * len(COVARIAVEIS)
    assert tabela["erro_padrao"].gt(0).all()


def test_comparar_marca_quem_cobre_o_beta_verdadeiro(ag):
    tabela = comparar({"andersen_gill": ag}, beta_verdadeiro=gerador.BETA)

    assert {"beta_verdadeiro", "erro", "ic_cobre"} <= set(tabela.columns)
    np.testing.assert_allclose(tabela["erro"], tabela["coef"] - tabela["beta_verdadeiro"])
    assert tabela["ic_cobre"].sum() >= 1, "nenhum IC cobriu o valor verdadeiro"


# -------------------------------------------------------------------- frailty
@pytest.fixture(scope="module")
def frailty(painel, mcf):
    return estimar_frailty_gama(painel, mcf)


def test_frailty_estimada_e_positiva_e_significativa(frailty):
    """`theta` zero significaria usinas idênticas — o gerador diz o contrário.

    O `lr_vs_poisson` é o teste: a binomial negativa (= AG com fragilidade gama)
    tem de ganhar do Poisson, que supõe usinas homogêneas.
    """
    assert frailty["theta"] > 0
    assert frailty["ic_inferior"] > 0
    assert frailty["lr_vs_poisson"] > 10


def test_frailty_recupera_a_ordem_de_grandeza_da_variancia(frailty):
    """O gerador usa VARIANCIA_FRAILTY; a estimativa não precisa bater na casa
    decimal, mas tem de ficar no mesmo patamar — e o IC, conter algo plausível."""
    verdadeira = gerador.VARIANCIA_FRAILTY

    assert verdadeira / 5 < frailty["theta"] < verdadeira * 5
    assert frailty["ic_inferior"] < frailty["theta"] < frailty["ic_superior"]


def test_frailty_posterior_ordena_as_usinas(frailty, painel):
    """Quem teve mais eventos do que a exposição previa recebe Z maior."""
    por_usina = frailty["por_usina"]

    assert len(por_usina) == painel[ID].nunique()
    assert (por_usina["frailty_posterior"] > 0).all()
    assert por_usina["frailty_posterior"].corr(por_usina["eventos"]) > 0.3


def test_exposicao_da_frailty_e_medida_em_eventos_esperados(frailty):
    """Não em anos: dois anos de usina nova não valem dois de usina velha.

    A MCF da fonte na idade da usina é a escala certa, e é o offset do modelo.
    """
    por_usina = frailty["por_usina"]

    assert (por_usina["exposicao"] > 0).all()
    # a exposição cresce com a observação, mas não proporcionalmente
    correlacao = por_usina["exposicao"].corr(por_usina["observacao_anos"])
    assert correlacao > 0.8
    razao = por_usina["exposicao"] / por_usina["observacao_anos"]
    assert razao.std() > 0, "se fosse proporcional, a razão seria constante"


def test_frailty_posterior_encolhe_para_um(frailty):
    """Bayes empírico: com pouca evidência, Z fica perto de 1 em vez de disparar."""
    por_usina = frailty["por_usina"]
    sem_evento = por_usina[por_usina["eventos"] == 0]["frailty_posterior"]

    assert (sem_evento < 1).all()
    assert (sem_evento > 0).all()


def test_sobredispersao_e_o_sintoma_que_justifica_a_fragilidade(painel):
    """Variância da contagem bem acima da média: um Poisson simples não serve."""
    contagem = painel.groupby(ID)[EVENTO].sum()

    assert contagem.var() / contagem.mean() > 1.5


# ------------------------------------------------------------------- previsor
def test_previsor_devolve_uma_contagem_por_horizonte(ag, mcf, painel):
    previsor = PrevisorRecorrencia(ag, mcf)
    usinas = painel.drop_duplicates(ID).head(5).assign(idade_anos=3.0)

    saida = previsor.prever(usinas)

    for meses in previsor.horizontes_meses:
        coluna = f"manutencoes_esperadas_{meses}m"
        assert coluna in saida
        assert (saida[coluna] >= 0).all()


def test_contagem_esperada_cresce_com_o_horizonte(ag, mcf, painel):
    previsor = PrevisorRecorrencia(ag, mcf)
    usinas = painel.drop_duplicates(ID).head(10).assign(idade_anos=2.0)

    saida = previsor.prever(usinas)
    colunas = [f"manutencoes_esperadas_{m}m" for m in sorted(previsor.horizontes_meses)]

    valores = saida[colunas].to_numpy()
    assert np.all(np.diff(valores, axis=1) >= -1e-9)


def test_multiplicador_separa_usina_grande_de_pequena(ag, mcf, painel):
    previsor = PrevisorRecorrencia(ag, mcf)
    uma = painel.drop_duplicates(ID).head(1)

    pequena = previsor.multiplicador(uma.assign(log_potencia_mw_c=-1.0))[0]
    grande = previsor.multiplicador(uma.assign(log_potencia_mw_c=1.0))[0]

    assert grande > pequena
    assert pequena > 0


def test_taxa_relativa_vai_para_a_saida(ag, mcf, painel):
    previsor = PrevisorRecorrencia(ag, mcf)
    usinas = painel.drop_duplicates(ID).head(3).assign(idade_anos=1.0)

    saida = previsor.prever(usinas)

    np.testing.assert_allclose(saida["taxa_relativa"], previsor.multiplicador(usinas))


def test_previsao_e_o_produto_mcf_x_multiplicador(ag, mcf, painel):
    """A fórmula declarada na docstring, verificada ponto a ponto."""
    previsor = PrevisorRecorrencia(ag, mcf, horizontes_meses=(12,))
    uma = painel.drop_duplicates(ID).head(1).assign(idade_anos=4.0)

    saida = previsor.prever(uma)
    base = eventos_esperados(mcf, 4.0, 1.0, str(uma[ESTRATO].iat[0]))

    assert saida["manutencoes_esperadas_12m"].iat[0] == pytest.approx(
        round(base * previsor.multiplicador(uma)[0], 3))
