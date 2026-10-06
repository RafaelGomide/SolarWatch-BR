"""Gravação da camada bruta: escrita atômica (build-then-swap).

Toda a ingestão e todo o ETL gravam por aqui. O que se protege é a promessa de
que **ninguém nunca lê um Parquet pela metade**: se o processo cair no meio de
uma escrita, o arquivo anterior continua íntegro e o que sobra é um `.tmp` que o
leitor ignora por construção (`ler_parquet` faz glob em `*.parquet`).
"""

from __future__ import annotations

import pandas as pd
import pytest

from ingestao.armazenamento import COMPRESSAO, gravar_parquet


def test_grava_e_le_de_volta(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})

    caminho = gravar_parquet(df, tmp_path / "dados.parquet")

    assert caminho.exists()
    pd.testing.assert_frame_equal(pd.read_parquet(caminho), df)


def test_cria_as_pastas_do_caminho(tmp_path):
    caminho = gravar_parquet(pd.DataFrame({"a": [1]}),
                             tmp_path / "bruto" / "2026-07-04" / "x.parquet")

    assert caminho.exists()


def test_nao_deixa_temporario_para_tras(tmp_path):
    gravar_parquet(pd.DataFrame({"a": [1]}), tmp_path / "x.parquet")

    assert not list(tmp_path.glob("*.tmp"))


def test_indice_nao_vai_para_o_arquivo(tmp_path):
    """Índice gravado vira coluna fantasma na leitura."""
    df = pd.DataFrame({"a": [1, 2]}, index=["i", "j"])

    lido = pd.read_parquet(gravar_parquet(df, tmp_path / "x.parquet"))

    assert list(lido.columns) == ["a"]
    assert list(lido.index) == [0, 1]


def test_arquivo_anterior_sobrevive_a_falha_na_escrita(tmp_path, monkeypatch):
    """O ponto do build-then-swap: a escrita falha e o dado antigo fica intacto."""
    caminho = tmp_path / "x.parquet"
    gravar_parquet(pd.DataFrame({"a": [1]}), caminho)

    def explodir(*args, **kwargs):
        raise OSError("disco cheio")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", explodir)
    with pytest.raises(OSError, match="disco cheio"):
        gravar_parquet(pd.DataFrame({"a": [99]}), caminho)

    assert pd.read_parquet(caminho)["a"].tolist() == [1]


def test_regravar_substitui_o_conteudo(tmp_path):
    caminho = tmp_path / "x.parquet"
    gravar_parquet(pd.DataFrame({"a": [1, 2, 3]}), caminho)

    gravar_parquet(pd.DataFrame({"a": [9]}), caminho)

    assert pd.read_parquet(caminho)["a"].tolist() == [9]


def test_usa_compressao_declarada(tmp_path):
    import pyarrow.parquet as pq

    caminho = gravar_parquet(pd.DataFrame({"a": list(range(1000))}), tmp_path / "x.parquet")

    metadados = pq.ParquetFile(caminho).metadata.row_group(0).column(0)
    assert metadados.compression.lower() == COMPRESSAO


def test_dataframe_vazio_tambem_e_gravado(tmp_path):
    """Um mês sem dado publicado não deve derrubar a ingestão."""
    caminho = gravar_parquet(pd.DataFrame({"a": pd.Series(dtype="float64")}),
                             tmp_path / "vazio.parquet")

    assert caminho.exists()
    assert pd.read_parquet(caminho).empty
