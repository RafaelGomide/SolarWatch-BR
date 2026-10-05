"""`dim_usina`: extração do núcleo do nome de um conjunto e IDs estáveis.

As duas funções sustentam coisas diferentes: `_nucleo` sustenta o vínculo
aproximado conjunto ONS → usinas da ANEEL, e `_ids_estaveis` sustenta a
promessa de que `usina_id` não muda entre execuções — se mudar, todo gráfico,
link e modelo salvo que guarda um `usina_id` passa a apontar para outra usina.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ETL.config import PALAVRAS_GENERICAS_CONJUNTO
from ETL.curated.dim_usina import (TAMANHO_MINIMO_NUCLEO, _ids_estaveis,
                                   _nucleo)


# --------------------------------------------------------------------- _nucleo
@pytest.mark.parametrize(("nome", "esperado"), [
    # o caso do docstring: tira as genéricas e o nível de tensão
    ("conjunto eolico morro do chapeu sul ii 230 kv", "morro do chapeu sul ii"),
    ("conjunto eolico caetite", "caetite"),
    ("complexo fotovoltaico bom jesus da lapa", "bom jesus da lapa"),
    ("cj eolico ventos de santa eugenia", "ventos de santa eugenia"),
    # tensão colada no número e em outra posição
    ("conjunto eolico lagoa 230kv", "lagoa"),
    ("conjunto solar 500 kv assu", "assu"),
    # nome sem nada de genérico passa inteiro
    ("santa eugenia", "santa eugenia"),
])
def test_nucleo_remove_genericas_e_tensao(nome, esperado):
    assert _nucleo(nome) == esperado


def test_nucleo_e_idempotente():
    """Aplicar duas vezes não muda nada: o resultado já está sem genéricas."""
    nome = "conjunto eolico morro do chapeu sul ii 230 kv"

    assert _nucleo(_nucleo(nome)) == _nucleo(nome)


def test_nucleo_nao_remove_numeros_que_nao_sao_tensao():
    """`caetite 2` é o nome da usina; o 2 precisa sobreviver."""
    assert _nucleo("conjunto eolico caetite 2") == "caetite 2"


def test_nucleo_so_vazio_quando_tudo_e_generico():
    """Um conjunto nomeado só com palavras genéricas não dá núcleo utilizável.

    O `_vincular` barra isso com `TAMANHO_MINIMO_NUCLEO`: um núcleo curto casaria
    com qualquer usina da UF e produziria um vínculo errado em vez de nenhum.
    """
    nucleo = _nucleo("conjunto eolico usinas")

    assert nucleo == ""
    assert len(nucleo) < TAMANHO_MINIMO_NUCLEO


def test_palavras_genericas_estao_normalizadas():
    """A lista é comparada com o nome já normalizado: minúsculas e sem acento."""
    assert all(p == p.lower() and p.isascii() for p in PALAVRAS_GENERICAS_CONJUNTO)


# --------------------------------------------------------------- _ids_estaveis
@pytest.fixture
def curated(tmp_path, monkeypatch):
    """Aponta a pasta curated para um diretório temporário vazio."""
    import ETL.curated.dim_usina as modulo

    monkeypatch.setattr(modulo, "CURATED", tmp_path)
    return tmp_path


def dim(*chaves) -> pd.DataFrame:
    return pd.DataFrame({"chave_unidade": list(chaves)})


def gravar_anterior(pasta, mapa: dict[str, int]) -> None:
    pd.DataFrame({"usina_id": list(mapa.values()),
                  "chave_unidade": list(mapa.keys())}).to_parquet(
        pasta / "dim_usina.parquet")


def test_primeira_execucao_numera_de_1(curated):
    ids = _ids_estaveis(dim("a", "b", "c"))

    assert ids.tolist() == [1, 2, 3]
    assert ids.dtype == "uint64"


def test_ids_sao_reaproveitados_pela_chave(curated):
    gravar_anterior(curated, {"a": 1, "b": 2, "c": 3})

    ids = _ids_estaveis(dim("a", "b", "c"))

    assert ids.tolist() == [1, 2, 3]


def test_ids_seguem_a_chave_e_nao_a_posicao(curated):
    """A dim é reordenada por fonte/região/nome; o ID não pode acompanhar."""
    gravar_anterior(curated, {"a": 1, "b": 2, "c": 3})

    ids = _ids_estaveis(dim("c", "a", "b"))

    assert ids.tolist() == [3, 1, 2]


def test_unidade_nova_recebe_o_proximo_id(curated):
    gravar_anterior(curated, {"a": 1, "b": 2})

    ids = _ids_estaveis(dim("a", "b", "nova"))

    assert ids.tolist() == [1, 2, 3]


def test_id_de_unidade_que_saiu_nao_e_reciclado(curated):
    """A usina B saiu do ONS; o ID 2 fica aposentado.

    Reciclar o 2 faria uma usina nova herdar o histórico da antiga em qualquer
    coisa que tenha guardado o ID — gráfico salvo, modelo treinado, link.
    """
    gravar_anterior(curated, {"a": 1, "b": 2, "c": 3})

    ids = _ids_estaveis(dim("a", "c", "nova"))

    assert ids.tolist() == [1, 3, 4]


def test_varias_unidades_novas_recebem_ids_distintos(curated):
    gravar_anterior(curated, {"a": 10})

    ids = _ids_estaveis(dim("a", "x", "y", "z"))

    assert ids.tolist() == [10, 11, 12, 13]


def test_execucao_repetida_devolve_os_mesmos_ids(curated):
    """Rodar a pipeline duas vezes sem dado novo não renumera nada."""
    primeira = _ids_estaveis(dim("a", "b"))
    gravar_anterior(curated, dict(zip(["a", "b"], primeira)))

    segunda = _ids_estaveis(dim("a", "b"))

    assert segunda.tolist() == primeira.tolist()


def test_indice_do_dataframe_e_preservado(curated):
    """A dim entra com índice arbitrário e o `insert` precisa alinhar."""
    entrada = dim("a", "b").set_axis([7, 9])

    ids = _ids_estaveis(entrada)

    assert ids.index.tolist() == [7, 9]
