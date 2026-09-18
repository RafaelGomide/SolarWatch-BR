"""Utilitários HTTP compartilhados pelas ingestões."""

from __future__ import annotations

import logging
import random
import time

import requests

MAX_TENTATIVAS = 5
TIMEOUT_S = 120

log = logging.getLogger(__name__)


def get_com_retry(sessao: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET com exponential backoff + jitter (GET é idempotente, retry é seguro).

    404 é devolvido ao chamador sem retry, para que ele decida o que fazer.
    """
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = sessao.get(url, timeout=TIMEOUT_S, **kwargs)
            if resp.status_code == 404:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as erro:
            if tentativa == MAX_TENTATIVAS:
                raise
            espera = min(2**tentativa, 60) + random.uniform(0, 1)
            log.warning("Falha em %s (%s). Nova tentativa em %.1fs", url, erro, espera)
            time.sleep(espera)
    raise RuntimeError("inalcançável")


def nova_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = "SolarWatch-BR/ingestao (dados abertos)"
    return sessao
