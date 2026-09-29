"""Utilitários HTTP compartilhados pelas ingestões."""

from __future__ import annotations

import logging
import random
import time

import requests

MAX_TENTATIVAS = 5
TIMEOUT_S = 120

log = logging.getLogger(__name__)


def _esperar_ou_levantar(tentativa: int, url: str, erro: Exception) -> None:
    """Dorme antes da próxima tentativa; na última, propaga o erro."""
    if tentativa == MAX_TENTATIVAS:
        raise erro
    espera = min(2**tentativa, 60) + random.uniform(0, 1)
    log.warning("Falha em %s (%s). Nova tentativa em %.1fs", url, erro, espera)
    time.sleep(espera)


def get_com_retry(sessao: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET com exponential backoff + jitter (GET é idempotente, retry é seguro).

    Só falhas **transitórias** são repetidas: erro de conexão, timeout, `429`
    (rate limit) e `5xx`. Os demais `4xx` são erros da própria requisição
    (parâmetro inválido, recurso inexistente, credencial) — repetir não muda a
    resposta, então o `HTTPError` é levantado na primeira tentativa.

    404 é devolvido ao chamador sem retry e sem exceção, para que ele decida o
    que fazer (o ONS usa isso para pular um mês ainda não publicado).
    """
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = sessao.get(url, timeout=TIMEOUT_S, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as erro:
            _esperar_ou_levantar(tentativa, url, erro)
            continue

        if resp.status_code == 404:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            erro = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            _esperar_ou_levantar(tentativa, url, erro)
            continue

        resp.raise_for_status()  # 4xx não-transitório: levanta já na 1ª tentativa
        return resp
    raise RuntimeError("inalcançável")


def nova_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = "SolarWatch-BR/ingestao (dados abertos)"
    return sessao
