"""Geração do `locais.csv` a partir do cadastro da ANEEL (sem rede)."""

from __future__ import annotations

import pandas as pd
import pytest

from ingestao.nasa_power.gerar_locais import _municipio, _slug, candidatas, gerar, selecionar


def _usina(nome, uf, tipo, kw, lat, lon, municipio=None, fase="Operação", ceg=None):
    return {
        "NomEmpreendimento": nome, "CodCEG": ceg or f"{tipo}.RS.{uf}.{abs(hash(nome)) % 999999:06d}-0.1",
        "SigUFPrincipal": uf, "SigTipoGeracao": tipo, "DscFaseUsina": fase,
        "MdaPotenciaFiscalizadaKw": f"{kw:,.2f}".replace(",", "@").replace(".", ",").replace("@", "."),
        "NumCoordNEmpreendimento": str(lat).replace(".", ","),
        "NumCoordEEmpreendimento": str(lon).replace(".", ","),
        "DscMuninicpios": f"{municipio or nome} - {uf}",
    }


@pytest.fixture
def cadastro(tmp_path):
    """Cadastro sintético: BA eólica com duas usinas, PI solar, e casos a descartar."""
    def gravar(linhas):
        caminho = tmp_path / "aneel.parquet"
        pd.DataFrame(linhas).to_parquet(caminho, index=False)
        return caminho
    return gravar


def test_converte_numeros_no_formato_brasileiro(cadastro):
    entrada = cadastro([_usina("Grande", "BA", "EOL", 11_832.10, -11.43252, -42.13743)])
    cand = candidatas(1.0, entrada=entrada)
    assert cand["potencia_mw"].iloc[0] == pytest.approx(11.8321)
    assert cand["latitude"].iloc[0] == pytest.approx(-11.43252)


@pytest.mark.parametrize(("linha", "motivo"), [
    (_usina("Minúscula", "BA", "EOL", 4.0, -11.4, -42.1), "abaixo de 1 MW"),
    (_usina("Em obras", "BA", "EOL", 50_000, -11.4, -42.1, fase="Construção"), "não opera"),
    (_usina("Coordenada zerada", "BA", "EOL", 50_000, 0.0, 0.0), "fora do Brasil"),
    (_usina("Hídrica", "BA", "UHE", 50_000, -11.4, -42.1), "outra fonte"),
])
def test_descarta_candidatas_inelegiveis(cadastro, linha, motivo):
    valida = _usina("Válida", "BA", "EOL", 80_000, -11.43, -42.13)
    cand = candidatas(1.0, entrada=cadastro([valida, linha]))
    assert cand["usina_referencia"].tolist() == ["Válida"], motivo


def test_descarta_coordenada_incoerente_com_a_uf(cadastro):
    """Caso real: 'Fótons de São George' consta em MS com coordenada no Piauí.
    Sendo a maior 'usina de MS', ela viraria o ponto de clima do estado inteiro."""
    linhas = [_usina(f"MS {i}", "MS", "UFV", 20_000, -21.5 - i / 100, -53.8) for i in range(5)]
    linhas.append(_usina("Fótons de São George", "MS", "UFV", 78_200, -3.81563, -41.13687))

    cand = candidatas(1.0, entrada=cadastro(linhas))

    assert "Fótons de São George" not in cand["usina_referencia"].tolist()
    assert len(cand) == 5


def test_escolhe_a_maior_usina_de_cada_par_fonte_uf(cadastro):
    entrada = cadastro([
        _usina("Eólica média", "BA", "EOL", 60_000, -11.4, -42.1),
        _usina("Eólica maior", "BA", "EOL", 80_000, -11.5, -42.2),
        _usina("Solar do PI", "PI", "UFV", 100_000, -7.6, -45.2),
    ])
    pontos = gerar(entrada=entrada, gravar=False)

    assert set(pontos["usina_referencia"]) == {"Eólica maior", "Solar do PI"}
    assert pontos.loc[pontos["id_estado"].eq("BA"), "fonte_predominante"].iloc[0] == "eolica"
    assert pontos.loc[pontos["id_estado"].eq("PI"), "id_subsistema"].iloc[0] == "NE"


def test_nao_aceita_dois_pontos_vizinhos(cadastro):
    """Dois pontos a poucos quilômetros pedem a mesma série climática duas vezes."""
    entrada = cadastro([
        _usina("Primeira", "BA", "EOL", 80_000, -11.40, -42.10),
        _usina("Vizinha", "BA", "EOL", 79_000, -11.42, -42.12),      # ~3 km
        _usina("Distante", "BA", "EOL", 78_000, -12.50, -43.00),     # ~150 km
    ])
    cand = candidatas(1.0, entrada=entrada)

    pontos = selecionar(cand, por_grupo=2, mw_minimo_grupo=50.0, min_km=50.0)

    assert pontos["usina_referencia"].tolist() == ["Primeira", "Distante"]


def test_grupo_sem_capacidade_relevante_nao_ganha_ponto(cadastro):
    entrada = cadastro([
        _usina("Grande", "BA", "EOL", 80_000, -11.4, -42.1),
        _usina("Isolada de RR", "RR", "UFV", 1_050, 2.8, -61.1),
    ])
    pontos = gerar(entrada=entrada, gravar=False)
    assert pontos["id_estado"].tolist() == ["BA"]


def test_selecao_e_deterministica_com_potencias_empatadas(cadastro):
    """Castilho 1 a 5 têm 49,999 MW cada; sem desempate por CEG, o ponto
    escolhido mudaria de uma execução para outra."""
    entrada = cadastro([
        _usina(f"Castilho {i}", "SP", "UFV", 49_999, -20.78 - i / 1000, -51.51,
               ceg=f"UFV.RS.SP.03410{i}-8.1") for i in range(1, 6)
    ])
    escolhas = {gerar(entrada=entrada, gravar=False)["usina_referencia"].iloc[0] for _ in range(5)}
    assert escolhas == {"Castilho 1"}


def test_nomes_de_local_distinguem_fontes_no_mesmo_municipio(cadastro):
    """Só acontece em município grande: pontos a menos de `min_km` são
    descartados antes, então a colisão de nome exige distância real."""
    entrada = cadastro([
        _usina("Eólica", "BA", "EOL", 80_000, -11.40, -42.10, municipio="Barreiras"),
        _usina("Solar", "BA", "UFV", 90_000, -12.20, -42.60, municipio="Barreiras"),  # ~100 km
    ])
    pontos = gerar(entrada=entrada, gravar=False)
    assert sorted(pontos["local"]) == ["barreiras_ba_eol", "barreiras_ba_sol"]


def test_gerar_grava_o_csv_com_as_colunas_do_contrato(cadastro, tmp_path):
    from ingestao.nasa_power.ingestao_nasa_power import COLUNAS_LOCAL

    entrada = cadastro([_usina("Única", "BA", "EOL", 80_000, -11.4, -42.1)])
    saida = tmp_path / "locais.csv"

    gerar(entrada=entrada, saida=saida, gravar=True)

    lido = pd.read_csv(saida)
    assert all(coluna in lido.columns for coluna in COLUNAS_LOCAL)
    assert {"usina_referencia", "potencia_mw", "ceg"} <= set(lido.columns)  # procedência


@pytest.mark.parametrize(("entrada", "esperado"), [
    ("São Gonçalo do Amarante - CE", "São Gonçalo do Amarante"),
    ("Caetité - BA, Igaporã - BA", "Caetité"),          # usina em mais de um município
    ("Uibaí", "Uibaí"),
])
def test_municipio(entrada, esperado):
    assert _municipio(entrada) == esperado


@pytest.mark.parametrize(("entrada", "esperado"), [
    ("Uibaí_BA", "uibai_ba"),
    ("São Gonçalo do Amarante_CE", "sao_goncalo_do_amarante_ce"),
    ("Lagoa do Barro do Piauí_PI", "lagoa_do_barro_do_piaui_pi"),
])
def test_slug(entrada, esperado):
    assert _slug(entrada) == esperado
