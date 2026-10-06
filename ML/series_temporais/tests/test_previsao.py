"""Previsão de geração: features sem vazamento, métricas, baselines e backtesting.

O risco número um de um modelo de série temporal é **vazamento temporal**: uma
feature que, na hora de prever, usa um valor que ainda não existia. Quando isso
acontece o erro de backtesting fica ótimo e o de produção fica péssimo — e nada
no código reclama. Por isso a maior parte destes testes verifica *fronteiras de
tempo*, não números de acurácia.

Os dados são uma senoide diária sintética: previsível de propósito, para que um
baseline sazonal acerte quase exatamente e qualquer violação dessa expectativa
seja um bug, não ruído.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ML.series_temporais import backtesting, features, metricas
from ML.series_temporais.config import (HORIZONTE_H, LAGS_H, MINIMO_TREINO_H,
                                        N_JANELAS, PISO_MAPE_FRACAO_MEDIA)
from ML.series_temporais.modelos import (GradientBoosting, MediaMovelSazonal,
                                         ModeloPrevisao, NaiveSazonal, catalogo)

ALVO = features.ALVO


def serie_sintetica(dias: int = 45, nome: str = "solar", amplitude: float = 100.0,
                    ruido: float = 0.0, seed: int = 7) -> pd.DataFrame:
    """Geração horária com um ciclo diário limpo (zero à noite) e clima."""
    horas = dias * 24
    indice = pd.date_range("2026-01-01", periods=horas, freq="h")
    ciclo = np.clip(np.sin(np.pi * (indice.hour - 6) / 12), 0, None) * amplitude
    rng = np.random.default_rng(seed)
    valores = ciclo + (rng.normal(0, ruido, horas) if ruido else 0)
    return pd.DataFrame(
        {ALVO: np.clip(valores, 0, None),
         "irradiancia_kwh_m2": np.repeat(np.linspace(4, 6, dias), 24),
         "vento_ms": 5.0, "temperatura_c": 25.0,
         "serie": nome, "fonte": "solar"},
        index=indice)


# --------------------------------------------------------------------- features
def test_defasagem_menor_que_o_horizonte_e_recusada():
    """A trava central contra vazamento: prever 24 h usando o valor de 1 h atrás."""
    df = serie_sintetica(dias=10)

    with pytest.raises(ValueError, match="vazamento"):
        features.construir(df, horizonte=24, lags=[1, 24])


def test_todas_as_defasagens_configuradas_respeitam_o_horizonte():
    """Invariante da configuração, não só da função."""
    assert min(LAGS_H) >= HORIZONTE_H


def test_lag_traz_exatamente_o_valor_de_24_horas_antes():
    df = serie_sintetica(dias=5)

    X = features.construir(df, horizonte=24, lags=[24], janelas=[])

    assert X["lag_24h"].iloc[:24].isna().all()          # não há passado ainda
    np.testing.assert_allclose(X["lag_24h"].iloc[24:], df[ALVO].iloc[:-24])


def test_media_movel_e_deslocada_do_horizonte():
    """A média móvel não pode incluir a própria hora prevista nem as 23 anteriores."""
    df = serie_sintetica(dias=10)

    X = features.construir(df, horizonte=24, lags=[24], janelas=[24])
    posicao = 100
    esperado = df[ALVO].iloc[posicao - 24 - 24 + 1: posicao - 24 + 1].mean()

    assert X["media_24h"].iloc[posicao] == pytest.approx(esperado)


def test_features_ciclicas_de_hora_existem():
    """Hora 23 e hora 0 têm de ficar vizinhas no espaço de features."""
    X = features.construir(serie_sintetica(dias=5), horizonte=24, lags=[24], janelas=[])

    assert {"data_hora_hora_sen", "data_hora_hora_cos"} <= set(X.columns)
    meia_noite = X[X.index.hour == 0].iloc[0]
    vinte_tres = X[X.index.hour == 23].iloc[0]
    meio_dia = X[X.index.hour == 12].iloc[0]
    distancia = lambda a, b: float(np.hypot(a["data_hora_hora_sen"] - b["data_hora_hora_sen"],
                                            a["data_hora_hora_cos"] - b["data_hora_hora_cos"]))
    assert distancia(meia_noite, vinte_tres) < distancia(meia_noite, meio_dia)


def test_indice_temporal_e_preservado():
    """Os mesmos instantes, na mesma ordem (o índice ganha o nome `data_hora`)."""
    df = serie_sintetica(dias=5)

    X = features.construir(df, horizonte=24, lags=[24], janelas=[])

    assert X.index.name == "data_hora"
    pd.testing.assert_index_equal(X.index, df.index, check_names=False)


def test_colunas_de_feature_excluem_alvo_e_identificacao():
    X = features.construir(serie_sintetica(dias=5), horizonte=24, lags=[24], janelas=[])

    colunas = features.colunas_de_feature(X)

    assert ALVO not in colunas and "serie" not in colunas and "fonte" not in colunas
    assert "irradiancia_kwh_m2" in colunas            # clima é feature declarada


# --------------------------------------------------------------------- métricas
def test_metricas_de_previsao_perfeita():
    y = pd.Series([1.0, 2.0, 3.0], index=pd.date_range("2026-01-01", periods=3, freq="h"))

    m = metricas.calcular(y, y.copy())

    assert m["rmse"] == 0 and m["mae"] == 0 and m["vies"] == 0
    assert m["mape_%"] == 0 and m["n"] == 3


def test_vies_tem_sinal_de_subestimacao():
    """`vies` positivo = o modelo previu MENOS que o observado."""
    indice = pd.date_range("2026-01-01", periods=3, freq="h")
    y = pd.Series([10.0, 10.0, 10.0], index=indice)

    assert metricas.calcular(y, pd.Series([8.0] * 3, index=indice))["vies"] == 2.0
    assert metricas.calcular(y, pd.Series([12.0] * 3, index=indice))["vies"] == -2.0


def test_mape_ignora_as_horas_de_geracao_zero():
    """O motivo de existir o piso: 26,6% das horas solares são zero e o MAPE
    dividiria por zero nelas."""
    indice = pd.date_range("2026-01-01", periods=4, freq="h")
    y = pd.Series([0.0, 0.0, 100.0, 100.0], index=indice)
    previsto = pd.Series([5.0, 5.0, 110.0, 90.0], index=indice)

    m = metricas.calcular(y, previsto)

    assert np.isfinite(m["mape_%"])
    assert m["mape_%"] == pytest.approx(10.0)          # só as duas horas com geração
    assert m["cobertura_mape_smape_%"] == 50.0


def test_piso_do_mape_e_relativo_a_media_da_serie():
    indice = pd.date_range("2026-01-01", periods=2, freq="h")
    y = pd.Series([1.0, 1000.0], index=indice)

    m = metricas.calcular(y, y.copy())

    piso = PISO_MAPE_FRACAO_MEDIA * y.mean()
    assert piso > 1.0                                  # o ponto pequeno fica de fora
    assert m["cobertura_mape_smape_%"] == 50.0


def test_nrmse_usa_todos_os_pontos():
    """A métrica comparável entre séries de escalas diferentes."""
    indice = pd.date_range("2026-01-01", periods=4, freq="h")
    y = pd.Series([0.0, 0.0, 10.0, 10.0], index=indice)
    previsto = pd.Series([1.0, 1.0, 11.0, 11.0], index=indice)

    m = metricas.calcular(y, previsto)

    assert m["rmse"] == pytest.approx(1.0)
    assert m["nrmse_%"] == pytest.approx(1.0 / 5.0 * 100)


def test_metricas_alinham_e_descartam_nulos():
    """Previsão e observação podem ter índices diferentes; o que vale é a interseção."""
    y = pd.Series([1.0, 2.0, np.nan], index=pd.date_range("2026-01-01", periods=3, freq="h"))
    previsto = pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01 01:00", periods=2, freq="h"))

    m = metricas.calcular(y, previsto)

    assert m["n"] == 1


def test_resumo_ordena_por_erro_dentro_de_cada_serie():
    painel = pd.DataFrame({
        "serie": ["solar"] * 4, "fonte": ["solar"] * 4,
        "modelo": ["bom", "bom", "ruim", "ruim"], "janela": [1, 1, 1, 1],
        "data_hora": pd.date_range("2026-01-01", periods=2, freq="h").tolist() * 2,
        "y": [10.0, 10.0, 10.0, 10.0], "previsto": [10.0, 10.0, 50.0, 50.0],
    })

    resumo = metricas.resumir(painel)

    assert list(resumo["modelo"]) == ["bom", "ruim"]
    assert resumo["janelas"].tolist() == [1, 1]


def test_ganho_sobre_baseline_e_percentual_de_reducao_de_rmse():
    resumo = pd.DataFrame({"serie": ["solar", "solar"],
                           "modelo": ["naive_sazonal", "gradient_boosting"],
                           "rmse": [100.0, 75.0]})

    com_ganho = metricas.ganho_sobre_baseline(resumo)

    por_modelo = com_ganho.set_index("modelo")["ganho_vs_baseline_%"]
    assert por_modelo["naive_sazonal"] == 0
    assert por_modelo["gradient_boosting"] == pytest.approx(25.0)
    assert "rmse_baseline" not in com_ganho.columns


# ---------------------------------------------------------------------- modelos
def test_modelo_nao_ajustado_recusa_prever():
    with pytest.raises(RuntimeError, match="não foi ajustado"):
        NaiveSazonal().prever(serie_sintetica(dias=2))


def test_naive_sazonal_repete_o_dia_anterior():
    df = serie_sintetica(dias=10)
    treino, teste = df.iloc[:-24], df.iloc[-24:]

    previsto = NaiveSazonal().ajustar(treino).prever(teste.drop(columns=[ALVO]))

    np.testing.assert_allclose(previsto.to_numpy(), treino[ALVO].iloc[-24:].to_numpy())


def test_naive_sazonal_acerta_quase_tudo_numa_serie_ciclica():
    """Sanidade do dado sintético: sem ruído, repetir o dia anterior é perfeito."""
    df = serie_sintetica(dias=10)
    treino, teste = df.iloc[:-24], df.iloc[-24:]

    previsto = NaiveSazonal().ajustar(treino).prever(teste.drop(columns=[ALVO]))

    assert metricas.calcular(teste[ALVO], previsto)["rmse"] == pytest.approx(0, abs=1e-9)


def test_media_movel_sazonal_usa_a_media_dos_ultimos_dias():
    df = serie_sintetica(dias=12, ruido=5.0)
    treino, teste = df.iloc[:-24], df.iloc[-24:]

    modelo = MediaMovelSazonal(dias=3)
    previsto = modelo.ajustar(treino).prever(teste.drop(columns=[ALVO]))
    primeira = teste.index[0]
    esperado = np.mean([treino[ALVO].get(primeira - pd.Timedelta(days=d)) for d in (1, 2, 3)])

    assert previsto.iloc[0] == pytest.approx(esperado)


def test_gradient_boosting_bate_o_baseline_com_ruido():
    """Com ruído, repetir o dia anterior propaga o ruído; o modelo deve suavizar."""
    df = serie_sintetica(dias=40, ruido=12.0)
    treino, teste = df.iloc[:-24], df.iloc[-24:]
    futuro = teste.drop(columns=[ALVO])

    gb = GradientBoosting().ajustar(treino).prever(futuro)
    naive = NaiveSazonal().ajustar(treino).prever(futuro)

    assert (metricas.calcular(teste[ALVO], gb)["rmse"]
            < metricas.calcular(teste[ALVO], naive)["rmse"])


def test_gradient_boosting_nunca_preve_geracao_negativa():
    df = serie_sintetica(dias=40, ruido=20.0)
    treino, teste = df.iloc[:-24], df.iloc[-24:]

    previsto = GradientBoosting().ajustar(treino).prever(teste.drop(columns=[ALVO]))

    assert (previsto >= 0).all()


def test_gradient_boosting_devolve_uma_previsao_por_hora_do_horizonte():
    df = serie_sintetica(dias=40)
    treino, teste = df.iloc[:-24], df.iloc[-24:]

    previsto = GradientBoosting().ajustar(treino).prever(teste.drop(columns=[ALVO]))

    assert len(previsto) == 24
    pd.testing.assert_index_equal(previsto.index, teste.index)
    assert previsto.notna().all()


def test_importancias_somam_variacao_e_apontam_o_ciclo_diario():
    df = serie_sintetica(dias=40, ruido=8.0)
    gb = GradientBoosting().ajustar(df.iloc[:-24])
    X = features.construir(df.iloc[:-24], horizonte=HORIZONTE_H)
    y = X[ALVO]

    importancias = gb.importancias(X[features.colunas_de_feature(X)].fillna(0), y, n_repeticoes=2)

    assert len(importancias) >= 5
    coluna = [c for c in importancias.columns if "import" in c.lower() or "media" in c.lower()][0]
    assert importancias[coluna].iloc[0] >= importancias[coluna].iloc[-1]


def test_catalogo_tem_os_baselines_e_o_modelo_principal():
    nomes = [m.nome for m in catalogo(com_sarima=False)]

    assert "naive_sazonal" in nomes and "gradient_boosting" in nomes
    assert "sarima" not in nomes
    assert all(isinstance(m, ModeloPrevisao) for m in catalogo(com_sarima=False))


# ------------------------------------------------------------------ backtesting
def test_origens_sao_consecutivas_e_terminam_na_ultima_janela():
    indice = pd.date_range("2026-01-01", periods=MINIMO_TREINO_H + 24 * 10, freq="h")

    cortes = backtesting.origens(indice, n_janelas=4, horizonte=24)

    assert len(cortes) == 4
    assert cortes == sorted(cortes)
    assert np.all(np.diff(cortes) == 24)              # janelas emendadas
    assert cortes[-1] == len(indice) - 24             # a última termina no fim da série


def test_origens_respeitam_o_minimo_de_treino():
    """Uma série curta devolve menos janelas em vez de treinar com 3 dias."""
    indice = pd.date_range("2026-01-01", periods=MINIMO_TREINO_H + 48, freq="h")

    cortes = backtesting.origens(indice, n_janelas=6, horizonte=24)

    assert len(cortes) == 2
    assert min(cortes) >= MINIMO_TREINO_H


def test_serie_curta_demais_falha_com_explicacao():
    indice = pd.date_range("2026-01-01", periods=48, freq="h")

    with pytest.raises(ValueError, match="curta demais"):
        backtesting.origens(indice, n_janelas=1, horizonte=24)


def test_backtesting_nao_deixa_o_treino_alcancar_o_teste():
    """A garantia que o `assert` interno faz por janela, verificada de fora."""
    df = serie_sintetica(dias=45, ruido=5.0)

    previsoes, tempos = backtesting.rodar(df, [NaiveSazonal()], n_janelas=3)

    for janela, grupo in previsoes.groupby("janela"):
        corte = grupo["data_hora"].min()
        assert len(grupo) == 24
        assert grupo["data_hora"].is_monotonic_increasing
        assert corte > df.index[MINIMO_TREINO_H - 1]


def test_backtesting_avalia_todos_os_modelos_nas_mesmas_janelas():
    df = serie_sintetica(dias=45, ruido=5.0)

    previsoes, tempos = backtesting.rodar(
        df, [NaiveSazonal(), MediaMovelSazonal(dias=3)], n_janelas=3)

    por_modelo = previsoes.groupby("modelo")["data_hora"].apply(set)
    assert len(por_modelo) == 2
    assert por_modelo.iloc[0] == por_modelo.iloc[1]
    assert set(tempos["modelo"]) == {"naive_sazonal", "media_movel_sazonal"}
    assert (tempos["segundos"] >= 0).all()


def test_modelo_que_falha_nao_derruba_a_comparacao(caplog):
    """Um modelo com problema sai do painel; os outros continuam."""
    import logging

    class Quebrado(ModeloPrevisao):
        nome = "quebrado"

        def prever(self, futuro):
            raise RuntimeError("falha proposital")

    df = serie_sintetica(dias=45)

    with caplog.at_level(logging.WARNING):
        previsoes, _ = backtesting.rodar(df, [Quebrado(), NaiveSazonal()], n_janelas=2)

    assert set(previsoes["modelo"]) == {"naive_sazonal"}
    assert "quebrado" in caplog.text


def test_painel_de_previsoes_tem_as_colunas_que_as_metricas_esperam():
    df = serie_sintetica(dias=45)

    previsoes, _ = backtesting.rodar(df, [NaiveSazonal()], n_janelas=2)

    assert {"serie", "fonte", "modelo", "janela", "data_hora", "y", "previsto"} <= set(
        previsoes.columns)
    metricas.resumir(previsoes)        # o painel alimenta o resumo sem adaptação


def test_backtesting_usa_o_numero_de_janelas_configurado():
    df = serie_sintetica(dias=60)

    previsoes, _ = backtesting.rodar(df, [NaiveSazonal()])

    assert previsoes["janela"].nunique() == N_JANELAS
