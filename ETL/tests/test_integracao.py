"""Integração: `raw → clean → curated` sobre um cadastro de brinquedo.

Os testes unitários verificam cada regra isolada; este verifica o que só aparece
quando as regras se compõem — que a sujeira plantada no bruto chega ao modelo
estrela com a marca certa, que o vínculo ONS × ANEEL classifica cada unidade
como deve, e que a validação deixa passar (ou barra) o conjunto inteiro.

O dado de entrada está em `dados_brinquedo.py`: 5 unidades do ONS, 8 usinas da
ANEEL, 2 pontos da NASA e 5 eventos simulados, escolhidos para que todo número
final seja verificável à mão.

A pipeline roda **uma vez** por módulo (fixture de escopo `module`) com os
caminhos de `config.py` redirecionados para `tmp_path`; os testes só inspecionam
o resultado. A segunda metade do arquivo testa o `_vincular` diretamente, com
cadastros pequenos, nos casos que o conjunto de brinquedo não cobre.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ETL import pipeline, raw
from ETL.curated import dim_usina
from ETL.curated.dim_usina import FAIXA_RAZAO_PICO_POTENCIA, _vincular

from . import dados_brinquedo as brinquedo
from .conftest import silenciar


# ------------------------------------------------------------------- a pipeline
@pytest.fixture(scope="module")
def caminhos(tmp_path_factory):
    raiz = tmp_path_factory.mktemp("solarwatch")
    caminhos = {"bruto": raiz / "bruto", "clean": raiz / "clean",
                "curated": raiz / "curated",
                "simulado": raiz / "simulados" / "eventos.parquet"}
    caminhos["bruto"].mkdir(parents=True)
    brinquedo.escrever(caminhos["bruto"], caminhos["simulado"])
    return caminhos


def _com_caminhos(caminhos, mp) -> None:
    """Redireciona as pastas de `config.py` em cada módulo que as importou."""
    silenciar(mp)
    mp.setattr(raw, "BRUTO", caminhos["bruto"])
    mp.setattr(pipeline, "CLEAN", caminhos["clean"])
    mp.setattr(pipeline, "CURATED", caminhos["curated"])
    mp.setattr(pipeline, "ARQUIVO_SIMULADO", caminhos["simulado"])
    mp.setattr(dim_usina, "CURATED", caminhos["curated"])


def rodar_curated(caminhos, clean) -> dict[str, pd.DataFrame]:
    with pytest.MonkeyPatch.context() as mp:
        _com_caminhos(caminhos, mp)
        return pipeline.etapa_curated(clean)


@pytest.fixture(scope="module")
def executada(caminhos):
    """Roda as três etapas e devolve as tabelas de cada camada."""
    with pytest.MonkeyPatch.context() as mp:
        _com_caminhos(caminhos, mp)
        particoes = raw.particionar()
        clean = pipeline.etapa_clean(None)
        curated = pipeline.etapa_curated(clean)
    return {"caminhos": caminhos, "particoes": particoes, "clean": clean, "curated": curated}


@pytest.fixture
def clean(executada):
    return executada["clean"]


@pytest.fixture
def curated(executada):
    return executada["curated"]


def unidade(dim: pd.DataFrame, chave: str) -> pd.Series:
    return dim[dim["chave_unidade"] == chave].iloc[0]


# ------------------------------------------------------------------ camada raw
def test_raw_particiona_as_quatro_fontes(executada):
    particoes = executada["particoes"]

    assert set(particoes) == {"ons", "nasa", "nasa_diario", "aneel"}
    # a data da partição vem do mtime do arquivo, não de uma data fixa
    assert all(p.parent.name.count("-") == 2 for p in particoes.values())
    assert (particoes["ons"] / "dados_ons_bruto_2026_07.parquet").exists()


def test_raw_e_idempotente(executada):
    """Reparticionar não copia nada: a camada bruta é imutável."""
    arquivo = executada["particoes"]["aneel"]
    antes = arquivo.stat().st_mtime_ns

    with pytest.MonkeyPatch.context() as mp:
        _com_caminhos(executada["caminhos"], mp)
        raw.particionar()

    assert arquivo.stat().st_mtime_ns == antes


def test_clean_le_a_particao_e_nao_o_dump(executada):
    with pytest.MonkeyPatch.context() as mp:
        _com_caminhos(executada["caminhos"], mp)
        caminho = raw.localizar("aneel")

    assert caminho.parent.name != "bruto"       # está numa partição AAAA-MM-DD
    assert caminho.parent.parent.name == "bruto"


# ---------------------------------------------------------------- camada clean
def test_clean_grava_as_cinco_tabelas(executada):
    gravados = {p.stem for p in executada["caminhos"]["clean"].glob("*.parquet")}

    assert gravados == {"ons_geracao", "nasa_clima_horario", "nasa_clima_diario",
                        "aneel_usinas", "manutencao_simulada"}


def test_ons_remove_a_duplicata_e_completa_a_grade(clean):
    ons = clean["ons_geracao"]

    # 5 unidades x 72 horas: a duplicata saiu e a hora ausente voltou pela grade
    assert len(ons) == len(brinquedo.UNIDADES) * brinquedo.HORAS
    assert not ons.duplicated(["chave_unidade", "data_hora_utc"]).any()
    assert 99.0 not in set(ons["geracao_mwh"])   # o valor antigo da duplicata


def test_ons_marca_cada_tipo_de_sujeira(clean):
    """A sujeira plantada no bruto chega ao clean com a flag certa."""
    ons = clean["ons_geracao"]
    flags = ons["flag_qualidade"].value_counts().to_dict()

    assert flags["negativo_zerado"] == 1
    assert flags["interpolado"] == 1              # a hora recriada pela grade
    assert flags["faltante"] == len(brinquedo.GAP_LONGO)
    assert ons.loc[ons["flag_qualidade"] == "negativo_zerado", "geracao_mwh"].iat[0] == 0.0
    assert ons.loc[ons["flag_qualidade"] == "faltante", "geracao_mwh"].isna().all()
    assert flags["original"] == len(ons) - 2 - len(brinquedo.GAP_LONGO)


def test_ons_converte_para_utc(clean):
    """Horário de Brasília (UTC−3) -> UTC: a primeira hora vira 03:00Z."""
    ons = clean["ons_geracao"]

    assert ons["data_hora_utc"].min() == pd.Timestamp("2026-07-01 03:00", tz="UTC")
    assert str(ons["data_hora_brasilia"].min()) == "2026-07-01 00:00:00-03:00"


def test_ons_usa_o_nome_mais_recente_da_unidade(clean):
    """A UFV_A aparece renomeada no meio do período; vale o último nome."""
    ufv = clean["ons_geracao"].query("chave_unidade == 'UFVA'")

    assert set(ufv["nome_usina"]) == {"UFV BOM SOL I"}
    assert set(ufv["nome_usina_normalizado"]) == {"ufv bom sol i"}


def test_ons_identifica_pequenas_usinas_sem_id_ons(clean):
    """Sem `id_ons`, a identidade é nome + UF."""
    ons = clean["ons_geracao"]
    pqu = ons[ons["chave_unidade"] == "PQU|PQU MMAM MMGD|AM"]

    assert len(pqu) == brinquedo.HORAS
    assert set(pqu["tipo_unidade"]) == {"pequenas_usinas"}


def test_ons_mantem_as_outras_fontes_no_clean(clean):
    """O clean é fiel ao ONS; o recorte solar/eólico é decisão da curated."""
    assert set(clean["ons_geracao"]["fonte"]) == {"solar", "eolica", "hidraulica"}


def test_aneel_converte_numeros_brasileiros(clean):
    aneel = clean["aneel_usinas"]
    bom_sol = aneel[aneel["ceg"] == "UFV.RS.BA.000001-0.1"].iloc[0]

    assert bom_sol["potencia_outorgada_mw"] == 30.0        # "30.000,00" kW
    assert bom_sol["latitude"] == -10.10                   # "-10,10"
    assert bom_sol["longitude"] == -41.10


def test_aneel_marca_as_sujeiras_do_cadastro(clean):
    aneel = clean["aneel_usinas"]
    ruim = aneel[aneel["ceg"] == "UFV.RS.MG.000040-0.1"].iloc[0]

    assert ruim["flag_coordenada"] == "zerada"
    assert pd.isna(ruim["latitude"]) and pd.isna(ruim["longitude"])
    assert ruim["flag_data_operacao"] == "sentinela_1900"
    assert pd.isna(ruim["data_entrada_operacao"])
    assert pd.isna(ruim["municipio"])                      # "Não Informado"


def test_aneel_extrai_o_municipio_principal(clean):
    aneel = clean["aneel_usinas"]
    duas_cidades = aneel[aneel["ceg"] == "EOL.CV.BA.000011-0.1"].iloc[0]

    assert duas_cidades["municipio"] == "Caetité"
    assert duas_cidades["uf_municipio"] == "BA"
    assert duas_cidades["municipio_normalizado"] == "caetite"


def test_aneel_descarta_a_coluna_de_proprietarios(clean):
    """LGPD: nomes e CNPJs não entram na camada limpa."""
    assert not [c for c in clean["aneel_usinas"].columns if "propri" in c.lower()]


def test_nasa_horario_separa_latencia_por_variavel(clean):
    """A irradiância horária não é publicada no período; vento e temperatura são."""
    horario = clean["nasa_clima_horario"]

    assert horario["irradiancia_wh_m2"].isna().all()
    assert set(horario["medidas_faltantes"].dropna()) == {"irradiancia_wh_m2"}
    assert horario["temperatura_2m_c"].notna().all()
    # as 2 horas de vento perdidas foram interpoladas, não propagadas
    assert horario["vento_50m_ms"].notna().all()


def test_nasa_diario_interpola_o_dia_ausente(clean):
    diario = clean["nasa_clima_diario"]
    serie = diario[diario["local"] == "bom_jesus_ba"].sort_values("data")

    assert len(serie) == brinquedo.DIAS_DIARIO          # o dia voltou pela grade
    ausente = serie.iloc[brinquedo.DIA_AUSENTE]
    assert ausente["flag_qualidade"] == "interpolado"
    # interpolado entre os dias vizinhos (5,2 e 5,6), não copiado de um deles
    assert ausente["irradiancia_kwh_m2_dia"] == pytest.approx(5.4)


def test_nasa_diario_nao_inventa_o_ultimo_dia(clean):
    """A latência fica no fim da série, onde não há valor posterior: fica nula."""
    diario = clean["nasa_clima_diario"]
    # `groupby.last()` pula nulos; aqui o nulo é justamente o que se quer ver
    ultimo = diario.loc[diario.groupby("local")["data"].idxmax()]

    assert ultimo["irradiancia_kwh_m2_dia"].isna().all()
    assert set(ultimo["flag_qualidade"]) == {"faltante"}
    assert set(ultimo["medidas_faltantes"]) == {"irradiancia_kwh_m2_dia"}


def test_manutencao_deriva_tempo_em_dias(clean):
    simulada = clean["manutencao_simulada"]

    assert (simulada["tempo_dias"] >= 1).all()
    assert simulada["simulado"].all()
    esperado = round(3.00 * 365.25)
    assert simulada[simulada["evento"] == 1]["tempo_dias"].iat[0] == esperado


# -------------------------------------------------------------- camada curated
def test_curated_grava_a_estrela_inteira(executada):
    gravados = {p.stem for p in executada["caminhos"]["curated"].glob("*.parquet")}

    assert gravados == {"dim_usina", "fato_geracao", "fato_clima", "fato_manutencao",
                        "ponte_usina_aneel"}


def test_dim_so_tem_solar_e_eolica(curated):
    """A hidrelétrica existe no clean e não entra na dimensão."""
    dim = curated["dim_usina"]

    assert len(dim) == 4
    assert set(dim["fonte"]) == {"solar", "eolica"}
    assert "AMBA" not in set(dim["chave_unidade"])


def test_dim_classifica_os_tres_metodos_de_vinculo(curated):
    dim = curated["dim_usina"]

    assert unidade(dim, "UFVA")["metodo_vinculo"] == "ceg"
    assert unidade(dim, "CJEOL")["metodo_vinculo"] == "nome"
    assert unidade(dim, "CJINC")["metodo_vinculo"] == "nome"
    assert unidade(dim, "PQU|PQU MMAM MMGD|AM")["metodo_vinculo"] == "sem_vinculo"


def test_vinculo_por_ceg_ignora_o_sufixo_de_versao(curated):
    """ONS `...-0.01` e ANEEL `...-0.1` são a mesma usina."""
    ufv = unidade(curated["dim_usina"], "UFVA")

    assert ufv["qualidade_vinculo"] == "exata"
    assert ufv["potencia_mw"] == 30.0
    assert ufv["n_usinas_aneel"] == 1
    assert ufv["municipio"] == "Bom Jesus da Lapa"
    assert ufv["data_operacao"] == pd.Timestamp("2019-05-10").date()


def test_conjunto_coerente_soma_as_potencias_dos_membros(curated):
    """2 × 30 MW com pico de 45 MWh: razão 0,75, dentro da faixa aceita."""
    cj = unidade(curated["dim_usina"], "CJEOL")
    minimo, maximo = FAIXA_RAZAO_PICO_POTENCIA

    assert cj["n_usinas_aneel"] == 2                 # a 3ª está em Construção
    assert cj["potencia_mw"] == 60.0
    assert cj["razao_pico_potencia"] == pytest.approx(45.0 / 60.0)
    assert minimo <= cj["razao_pico_potencia"] <= maximo
    assert cj["qualidade_vinculo"] == "consistente"
    assert cj["lat"] == pytest.approx(-11.10)        # média dos membros
    assert cj["data_operacao"] == pd.Timestamp("2015-03-01").date()  # a mais antiga


def test_conjunto_incoerente_nao_publica_potencia(curated):
    """Pico de 30 MWh para 10 MW vinculados: o vínculo pegou parte do conjunto.

    A potência some da coluna publicada e continua auditável ao lado.
    """
    cj = unidade(curated["dim_usina"], "CJINC")

    assert cj["qualidade_vinculo"] == "inconsistente"
    assert pd.isna(cj["potencia_mw"])
    assert cj["potencia_aneel_vinculada_mw"] == 10.0
    assert cj["razao_pico_potencia"] == pytest.approx(3.0)
    assert pd.notna(cj["lat"])                       # a localização continua útil


def test_unidade_sem_vinculo_fica_sem_cadastro(curated):
    pqu = unidade(curated["dim_usina"], "PQU|PQU MMAM MMGD|AM")

    assert pqu["qualidade_vinculo"] == "sem_vinculo"
    assert pqu["n_usinas_aneel"] == 0
    assert pd.isna(pqu["potencia_mw"]) and pd.isna(pqu["lat"])
    assert pqu["nome"] == "PQU MMAM MMGD"            # o essencial continua lá


def test_ponte_registra_cada_ligacao(curated, executada):
    ponte = pd.read_parquet(executada["caminhos"]["curated"] / "ponte_usina_aneel.parquet")

    assert len(ponte) == 4                           # 1 por CEG + 2 + 1 por nome
    assert ponte["usina_id"].notna().all()
    assert set(ponte["metodo_vinculo"]) == {"ceg", "nome"}
    por_nome = ponte[ponte["metodo_vinculo"] == "nome"]
    assert set(por_nome["nucleo_usado"]) == {"santa eugenia", "morro do chapeu sul ii"}
    # usina em Construção e as inelegíveis não entraram
    assert "EOL.CV.BA.000012-0.1" not in set(ponte["ceg_aneel"])
    assert "UFV.RS.CE.000030-0.1" not in set(ponte["ceg_aneel"])


def test_cada_usina_aneel_entra_em_um_unico_vinculo(curated, executada):
    ponte = pd.read_parquet(executada["caminhos"]["curated"] / "ponte_usina_aneel.parquet")

    assert not ponte["ceg_aneel"].duplicated().any()


def test_fato_geracao_tem_uma_linha_por_unidade_hora(curated):
    fato = curated["fato_geracao"]
    dim = curated["dim_usina"]

    assert len(fato) == len(dim) * brinquedo.HORAS
    assert set(fato["usina_id"]) == set(dim["usina_id"])
    assert not fato.duplicated(["usina_id", "timestamp_utc"]).any()
    assert set(fato["flag_qualidade"]) == {"original", "interpolado", "faltante",
                                           "negativo_zerado"}


def test_fato_geracao_preserva_o_gap_como_nulo(curated):
    """As horas sem medição ficam explícitas, em vez de sumirem."""
    fato = curated["fato_geracao"]
    faltantes = fato[fato["flag_qualidade"] == "faltante"]

    assert len(faltantes) == len(brinquedo.GAP_LONGO)
    assert faltantes["energia_mwh"].isna().all()


def test_fato_clima_usa_o_ponto_mais_proximo(curated):
    """Cada usina recebe o ponto NASA mais próximo, com a distância registrada."""
    clima = curated["fato_clima"]
    dim = curated["dim_usina"]
    ids = dict(zip(dim["chave_unidade"], dim["usina_id"]))
    por_usina = clima.drop_duplicates("usina_id").set_index("usina_id")

    assert por_usina.loc[ids["UFVA"], "local_clima"] == "bom_jesus_ba"
    assert por_usina.loc[ids["CJEOL"], "local_clima"] == "bom_jesus_ba"
    assert por_usina.loc[ids["CJINC"], "local_clima"] == "joao_camara_rn"
    assert por_usina.loc[ids["UFVA"], "distancia_km"] < 20
    assert set(por_usina["metodo_vinculo_clima"]) == {"mais_proximo"}


def test_usina_sem_coordenada_e_sem_ponto_na_regiao_fica_sem_clima(curated):
    """A PQU é do Amazonas; não há ponto NASA na UF nem no subsistema."""
    dim = curated["dim_usina"]
    pqu = unidade(dim, "PQU|PQU MMAM MMGD|AM")["usina_id"]

    assert pqu not in set(curated["fato_clima"]["usina_id"])


def test_fato_clima_renomeia_as_medidas_faltantes(curated):
    """Quem consome a fato não precisa conhecer os nomes da camada clean."""
    clima = curated["fato_clima"]
    faltantes = set(clima["medidas_faltantes"].dropna())

    assert faltantes == {"irradiancia_kwh_m2"}       # e não 'irradiancia_kwh_m2_dia'
    assert clima.loc[clima["medidas_faltantes"].notna(), "irradiancia_kwh_m2"].isna().all()


def test_fato_manutencao_herda_o_primeiro_evento_do_conjunto(curated):
    """O conjunto tem 2 membros simulados; vale o evento mais antigo entre eles."""
    dim = curated["dim_usina"]
    fato = curated["fato_manutencao"].set_index("usina_id")
    cj = fato.loc[unidade(dim, "CJEOL")["usina_id"]]

    assert cj["n_usinas_consideradas"] == 2
    assert cj["evento_ocorreu"]
    assert cj["data_primeiro_evento"] == pd.Timestamp("2018-07-15").date()
    # o relógio começa na operação mais antiga do conjunto, não na da usina do evento
    esperado = (pd.Timestamp("2018-07-15") - pd.Timestamp("2015-03-01")).days
    assert cj["tempo_dias"] == esperado


def test_fato_manutencao_censura_quem_nao_teve_evento(curated):
    dim = curated["dim_usina"]
    fato = curated["fato_manutencao"].set_index("usina_id")
    cj = fato.loc[unidade(dim, "CJINC")["usina_id"]]

    assert not cj["evento_ocorreu"]
    assert pd.isna(cj["data_primeiro_evento"])
    esperado = (brinquedo.DATA_CORTE - pd.Timestamp("2017-01-20")).days
    assert cj["tempo_dias"] == esperado


def test_fato_manutencao_so_cobre_unidades_vinculadas(curated):
    """A unidade sem vínculo não tem como receber evento simulado."""
    dim = curated["dim_usina"]
    fato = curated["fato_manutencao"]

    assert len(fato) == 3
    assert unidade(dim, "PQU|PQU MMAM MMGD|AM")["usina_id"] not in set(fato["usina_id"])
    assert fato["simulado"].all()


def test_todo_fato_aponta_para_uma_usina_da_dimensao(curated):
    """A integridade que a `validacao` cobra, verificada de fora dela."""
    ids = set(curated["dim_usina"]["usina_id"])

    for nome in ("fato_geracao", "fato_clima", "fato_manutencao"):
        assert set(curated[nome]["usina_id"]) <= ids, nome


def test_ids_sobrevivem_a_uma_segunda_execucao(executada):
    """Reprocessar a curated não renumera as unidades já publicadas."""
    antes = executada["curated"]["dim_usina"].set_index("chave_unidade")["usina_id"]

    depois = rodar_curated(executada["caminhos"], executada["clean"])
    depois = depois["dim_usina"].set_index("chave_unidade")["usina_id"]

    pd.testing.assert_series_equal(antes.sort_index(), depois.sort_index())


def test_validacao_barra_a_gravacao(executada, tmp_path):
    """Uma curated inválida não é publicada: nada é gravado.

    Com energia negativa no clean, a faixa `energia_mwh >= 0` falha e a etapa
    levanta antes de escrever — a pasta de saída fica vazia.
    """
    caminhos = {**executada["caminhos"], "curated": tmp_path / "curated_falha"}
    clean = {k: v.copy() for k, v in executada["clean"].items()}
    solar = clean["ons_geracao"].query("chave_unidade == 'UFVA'").index[0]
    clean["ons_geracao"].loc[solar, "geracao_mwh"] = -10.0

    with pytest.raises(ValueError, match="Validação da camada curated falhou"):
        rodar_curated(caminhos, clean)

    assert not caminhos["curated"].exists() or not list(caminhos["curated"].glob("*.parquet"))


# -------------------------------------------- _vincular: casos fora do brinquedo
COLUNAS_UNIDADE = ["chave_unidade", "ceg_ons", "nome_normalizado", "tipo_unidade",
                   "fonte", "id_estado"]
COLUNAS_CADASTRO = ["ceg", "nome_normalizado", "fonte", "id_estado", "fase",
                    "potencia_outorgada_mw", "latitude", "longitude", "municipio",
                    "data_entrada_operacao"]


def unidades(*linhas) -> pd.DataFrame:
    """Unidades no formato que `_unidades_ons` produz (só as colunas usadas)."""
    return pd.DataFrame(
        [dict(zip(COLUNAS_UNIDADE, linha)) for linha in linhas], columns=COLUNAS_UNIDADE)


def cadastro(*linhas) -> pd.DataFrame:
    """Usinas da ANEEL no formato da camada clean (só as colunas usadas)."""
    return pd.DataFrame(
        [dict(zip(COLUNAS_CADASTRO,
                  (*linha, -10.0, -40.0, "Teste", pd.Timestamp("2015-01-01"))))
         for linha in linhas], columns=COLUNAS_CADASTRO)


def test_vinculo_por_ceg_vale_em_qualquer_fase():
    """Há usina gerando no ONS que o cadastro ainda marca como 'Construção'."""
    ponte = _vincular(
        unidades(("U1", "EOL.CV.BA.000001-0.01", "usina um", "usina", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000001-0.1", "usina um", "eolica", "BA", "Construção", 30.0)))

    assert list(ponte["metodo_vinculo"]) == ["ceg"]


def test_conjunto_mais_especifico_escolhe_primeiro():
    """Dois núcleos casam a mesma usina; o mais longo (específico) leva.

    Sem a ordenação, 'santa eugenia' poderia capturar as usinas do 'santa
    eugenia norte' e deixar o conjunto certo sem membros.
    """
    ponte = _vincular(
        unidades(("GERAL", None, "conjunto eolico santa eugenia", "conjunto", "eolica", "BA"),
                 ("ESPEC", None, "conjunto eolico santa eugenia norte", "conjunto", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "santa eugenia norte 01", "eolica", "BA", "Operação", 30.0),
                 ("EOL.CV.BA.000011-0.1", "santa eugenia sul 01", "eolica", "BA", "Operação", 30.0)))

    por_chave = dict(zip(ponte["ceg_aneel"], ponte["chave_unidade"]))
    assert por_chave["EOL.CV.BA.000010-0.1"] == "ESPEC"
    assert por_chave["EOL.CV.BA.000011-0.1"] == "GERAL"


def test_usina_ja_vinculada_nao_entra_em_outro_conjunto():
    ponte = _vincular(
        unidades(("A", None, "conjunto eolico caetite", "conjunto", "eolica", "BA"),
                 ("B", None, "conjunto eolico caetite", "conjunto", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "caetite 01", "eolica", "BA", "Operação", 30.0)))

    assert len(ponte) == 1
    assert not ponte["ceg_aneel"].duplicated().any()


def test_nucleo_perde_palavras_do_fim_ate_casar():
    """'caetite 123' não existe no cadastro; 'caetite' existe."""
    ponte = _vincular(
        unidades(("A", None, "conjunto eolico caetite 123", "conjunto", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "caetite 01", "eolica", "BA", "Operação", 30.0)))

    assert list(ponte["nucleo_usado"]) == ["caetite"]


def test_nucleo_curto_nao_vincula():
    """Um núcleo de menos de 4 caracteres casaria com qualquer coisa."""
    ponte = _vincular(
        unidades(("A", None, "conjunto eolico sul", "conjunto", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "ventos do sul 01", "eolica", "BA", "Operação", 30.0)))

    assert ponte.empty


def test_nucleo_casa_palavra_inteira():
    """'lapa' não pode casar 'lapao': a fronteira é de palavra."""
    ponte = _vincular(
        unidades(("A", None, "conjunto fotovoltaico lapa", "conjunto", "solar", "BA")),
        cadastro(("UFV.RS.BA.000010-0.1", "lapao solar 01", "solar", "BA", "Operação", 30.0)))

    assert ponte.empty


@pytest.mark.parametrize(("fase", "mw", "uf", "fonte"), [
    ("Construção", 30.0, "BA", "eolica"),     # fase: só 'Operação' é elegível
    ("Operação", 0.5, "BA", "eolica"),        # abaixo de POTENCIA_MINIMA_MW_VINCULO
    ("Operação", 30.0, "PE", "eolica"),       # outra UF
    ("Operação", 30.0, "BA", "solar"),        # outra fonte
])
def test_conjunto_nao_vincula_usina_inelegivel(fase, mw, uf, fonte):
    ponte = _vincular(
        unidades(("A", None, "conjunto eolico caetite", "conjunto", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "caetite 01", fonte, uf, fase, mw)))

    assert ponte.empty


def test_unidade_individual_sem_ceg_nao_vincula():
    """Usina (não conjunto) sem CEG no ONS não tem como ser vinculada."""
    ponte = _vincular(
        unidades(("A", None, "usina sem ceg", "usina", "eolica", "BA")),
        cadastro(("EOL.CV.BA.000010-0.1", "usina sem ceg", "eolica", "BA", "Operação", 30.0)))

    assert ponte.empty


def test_vincular_devolve_as_colunas_esperadas_mesmo_vazio():
    """A curated faz `groupby` na ponte: ela precisa das colunas sempre."""
    ponte = _vincular(unidades(), cadastro())

    assert list(ponte.columns)[:4] == ["ceg_aneel", "chave_unidade", "metodo_vinculo",
                                       "nucleo_usado"]
    assert {"potencia_outorgada_mw", "latitude", "municipio"} <= set(ponte.columns)
    assert ponte.empty


def test_apenas_solar_e_eolica_sao_consideradas():
    ponte = _vincular(
        unidades(("H", "UHE.PH.AM.000190-2.01", "balbina", "usina", "hidraulica", "AM")),
        cadastro(("UHE.PH.AM.000190-2.1", "balbina", "hidraulica", "AM", "Operação", 250.0)))

    assert ponte.empty


def test_razao_pico_potencia_detecta_vinculo_parcial():
    """O número que separa 'consistente' de 'inconsistente', em isolamento.

    Uma unidade não gera mais que a potência instalada; passar da faixa é prova
    de que o vínculo pegou menos usinas do que o conjunto tem.
    """
    minimo, maximo = FAIXA_RAZAO_PICO_POTENCIA

    assert minimo < 45.0 / 60.0 < maximo          # vínculo completo
    assert 30.0 / 10.0 > maximo                   # vínculo parcial
    assert 2.0 / 60.0 < minimo                    # vínculo excessivo (ou unidade parada)


def test_dados_de_brinquedo_sao_deterministicos():
    """O conjunto é gerado por código; duas chamadas têm de dar o mesmo dado."""
    pd.testing.assert_frame_equal(brinquedo.ons_bruto(), brinquedo.ons_bruto())
    pd.testing.assert_frame_equal(brinquedo.aneel_bruto(), brinquedo.aneel_bruto())
    assert not np.isnan(brinquedo.ons_bruto()["val_geracao"]).all()
