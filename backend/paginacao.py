"""Paginação por cursor (system design §5.5).

O cursor é `base64(usina_id do último item)`. Comparado a offset, custa o mesmo
em FastAPI e não sofre com deslocamento de página quando a base muda entre
requisições — além de ser o padrão que se espera de uma API hoje.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import HTTPException

CODIGO_VALIDACAO = 422


def codificar(usina_id: int) -> str:
    return base64.urlsafe_b64encode(str(usina_id).encode()).decode()


def decodificar(cursor: str | None) -> int:
    """Devolve o último usina_id da página anterior (0 quando não há cursor)."""
    if not cursor:
        return 0
    try:
        valor = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError) as erro:
        raise HTTPException(
            CODIGO_VALIDACAO, f"cursor inválido: {cursor!r}"
        ) from erro
    # O decodificador do base64 ignora lixo depois do padding: 'MTI=qualquercoisa'
    # decodifica para 12 em silêncio. Exigir a forma canônica (o cursor tem de ser
    # exatamente o que `codificar` produziria) recusa isso e qualquer cursor
    # inventado à mão, em vez de paginar a partir de um id que ninguém pediu.
    if codificar(valor) != cursor:
        raise HTTPException(CODIGO_VALIDACAO, f"cursor inválido: {cursor!r}")
    return valor
