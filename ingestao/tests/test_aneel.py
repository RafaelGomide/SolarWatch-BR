"""Ingestão da ANEEL: paginação do `datastore_search` e retratos datados."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
import responses

import ingestao.aneel.ingestao_aneel as aneel
from ingestao.aneel.ingestao_aneel import API_URL, baixar, mudancas_de_fase

CAMPOS = ["_id", "CodCEG", "NomEmpreendimento", "SigUFPrincipal", "SigTipoGeracao",
          "DscFaseUsina", "DatGeracaoConjuntoDados"]


@pytest.fixture(autouse=True)
def pagina_pequena(monkeypatch):
    """Páginas de 2 registros: paginar de verdade sem simular 5.000 linhas."""
    monkeypatch.setattr(aneel, "TAMANHO_PAGINA", 2)
    monkeypatch.setattr(aneel.time, "sleep", lambda _: None)


def _registro(i: int, fase: str = "Operação", retrato: str = "2026-09-18") -> dict:
    return {"_id": i, "CodCEG": f"UFV.RS.BA.{i:06d}-0.1", "NomEmpreendimento": f"Usina {i}",
            "SigUFPrincipal": "BA", "SigTipoGeracao": "UFV", "DscFaseUsina": fase,
            "DatGeracaoConjuntoDados": retrato}


def _pagina(registros: list[dict], total: int) -> dict:
    return {"success": True, "result": {"total": total,
                                        "fields": [{"id": c} for c in CAMPOS],
                                        "records": registros}}


def _responder(paginas: list[dict]) -> None:
    for corpo in paginas:
        responses.get(API_URL, json=corpo, status=200)


# ------------------------------------------------------------------ paginação
@responses.activate
def test_paginacao_percorre_todas_as_paginas(tmp_path):
    _responder([
        _pagina([_registro(1), _registro(2)], total=5),
        _pagina([_registro(3), _registro(4)], total=5),
        _pagina([_registro(5)], total=5),
    ])

    saida = baixar(["UFV"], saida=tmp_path / "bruto.parquet", historico=tmp_path / "hist")

    df = pd.read_parquet(saida)
    assert len(df) == 5
    assert df["_id"].tolist() == [1, 2, 3, 4, 5]
    assert len(responses.calls) == 3


@responses.activate
def test_paginacao_avanca_o_offset_e_pede_ordem_estavel(tmp_path):
    """Sem `sort=_id asc`, a paginação por offset pode repetir ou pular linhas."""
    _responder([
        _pagina([_registro(1), _registro(2)], total=3),
        _pagina([_registro(3)], total=3),
    ])

    baixar(["UFV", "EOL"], saida=tmp_path / "bruto.parquet", historico=tmp_path / "hist")

    primeira, segunda = (c.request.params for c in responses.calls)
    assert primeira["offset"] == "0" and segunda["offset"] == "2"
    assert primeira["sort"] == "_id asc"
    assert primeira["limit"] == "2"
    assert json.loads(primeira["filters"]) == {"SigTipoGeracao": ["UFV", "EOL"]}


@responses.activate
def test_total_que_muda_no_meio_aborta(tmp_path):
    """O recurso é republicado diariamente: se mudar durante a coleta, o retrato
    seria uma mistura de duas publicações."""
    _responder([
        _pagina([_registro(1), _registro(2)], total=5),
        _pagina([_registro(3), _registro(4)], total=6),   # republicado no meio
    ])

    with pytest.raises(RuntimeError, match="Total mudou"):
        baixar(["UFV"], saida=tmp_path / "bruto.parquet", historico=tmp_path / "hist")

    assert not (tmp_path / "bruto.parquet").exists()      # nada é gravado pela metade


@responses.activate
def test_contagem_menor_que_o_total_aborta(tmp_path):
    """A API diz 5, entrega 2 e para: um retrato incompleto não pode virar bruto."""
    _responder([
        _pagina([_registro(1), _registro(2)], total=5),
        _pagina([], total=5),
    ])

    with pytest.raises(RuntimeError, match="Esperados 5"):
        baixar(["UFV"], saida=tmp_path / "bruto.parquet", historico=tmp_path / "hist")


@responses.activate
def test_registro_repetido_entre_paginas_e_descartado(tmp_path):
    """`drop_duplicates('_id')` protege contra sobreposição de página; a
    verificação de contagem continua valendo depois dela."""
    _responder([
        _pagina([_registro(1), _registro(2)], total=3),
        _pagina([_registro(2), _registro(3)], total=3),   # _id 2 repetido
    ])

    df = pd.read_parquet(baixar(["UFV"], saida=tmp_path / "bruto.parquet",
                                historico=tmp_path / "hist"))
    assert df["_id"].tolist() == [1, 2, 3]


@responses.activate
def test_success_false_vira_erro(tmp_path):
    responses.get(API_URL, json={"success": False, "error": {"message": "resource not found"}},
                  status=200)

    with pytest.raises(RuntimeError, match="API da ANEEL retornou erro"):
        baixar(["UFV"], saida=tmp_path / "bruto.parquet", historico=tmp_path / "hist")


# ------------------------------------------------------------ retratos datados
@responses.activate
def test_baixar_arquiva_retrato_datado_com_a_data_do_dado(tmp_path):
    _responder([_pagina([_registro(1, retrato="2026-09-18")], total=1)])
    historico = tmp_path / "hist"

    baixar(["UFV"], saida=tmp_path / "bruto.parquet", historico=historico)

    assert [p.name for p in historico.glob("*.parquet")] == ["dados_aneel_bruto_2026-09-18.parquet"]


def test_data_do_retrato_ignora_o_relogio_da_maquina():
    bruto = pd.DataFrame([_registro(1, retrato="2020-01-15")])
    assert aneel.data_do_retrato(bruto) == date(2020, 1, 15)


def test_data_do_retrato_sem_a_coluna_preenchida_cai_para_hoje():
    bruto = pd.DataFrame([{**_registro(1), "DatGeracaoConjuntoDados": None}])
    assert aneel.data_do_retrato(bruto) == date.today()


def test_arquivar_e_idempotente(tmp_path):
    bruto = pd.DataFrame([_registro(1)])
    primeiro = aneel.arquivar(bruto, historico=tmp_path)
    assinatura = primeiro.stat().st_mtime_ns

    segundo = aneel.arquivar(bruto, historico=tmp_path)

    assert segundo == primeiro
    assert segundo.stat().st_mtime_ns == assinatura      # não foi regravado


def test_retratos_vem_ordenados_e_ignoram_arquivos_estranhos(tmp_path):
    for dia in ["2026-09-18", "2026-01-02", "2026-05-09"]:
        aneel.arquivar(pd.DataFrame([_registro(1, retrato=dia)]), historico=tmp_path)
    (tmp_path / "anotacoes.txt").write_text("nada a ver", encoding="utf-8")
    (tmp_path / "dados_aneel_bruto_incompleto.parquet").write_bytes(b"")

    assert [d.isoformat() for d, _ in aneel.retratos(tmp_path)] == \
        ["2026-01-02", "2026-05-09", "2026-09-18"]


def test_retratos_em_pasta_inexistente(tmp_path):
    assert aneel.retratos(tmp_path / "ainda-nao-existe") == []


def test_mudancas_de_fase_detecta_transicao_e_cadastro_novo(tmp_path):
    antes = aneel.arquivar(pd.DataFrame([
        _registro(1, fase="Construção", retrato="2026-09-18"),
        _registro(2, fase="Operação", retrato="2026-09-18"),
    ]), historico=tmp_path)
    depois = aneel.arquivar(pd.DataFrame([
        _registro(1, fase="Operação", retrato="2026-09-29"),    # transição
        _registro(2, fase="Operação", retrato="2026-09-29"),    # sem mudança
        _registro(3, fase="Construção", retrato="2026-09-29"),  # entrou no cadastro
    ]), historico=tmp_path)

    mudancas = mudancas_de_fase(antes, depois)

    assert len(mudancas) == 2
    transicao = mudancas[mudancas["NomEmpreendimento"].eq("Usina 1")].iloc[0]
    assert (transicao["fase_antes"], transicao["fase_depois"]) == ("Construção", "Operação")
    nova = mudancas[mudancas["NomEmpreendimento"].eq("Usina 3")].iloc[0]
    assert nova["fase_antes"] == "(não cadastrada)"


def test_mudancas_de_fase_entre_retratos_iguais_e_vazio(tmp_path):
    igual = pd.DataFrame([_registro(1), _registro(2)])
    a = aneel.arquivar(igual, historico=tmp_path)
    b = aneel.arquivar(igual.assign(DatGeracaoConjuntoDados="2026-09-29"), historico=tmp_path)

    assert mudancas_de_fase(a, b).empty
