"""Rate limiting por IP — token bucket em memória do processo (system design §5.9).

O design previa `slowapi`, mas a versão instalada dele é **incompatível** com
esta versão do FastAPI: `slowapi` descobre a rota procurando o atributo
`.endpoint` nas rotas do app, e o FastAPI passou a envolver roteadores incluídos
em objetos `_IncludedRouter` que não expõem esse atributo. O resultado é que
`_should_exempt` devolve `True` para **todas** as requisições e o limite nunca é
aplicado — silenciosamente. Testado: 65 requisições seguidas, nenhuma bloqueada.

Como o comportamento pedido é simples e a alternativa era depender de um
comportamento quebrado, o token bucket está implementado aqui:

- capacidade = `limite` tokens, reposição contínua de `limite/periodo` por segundo;
- um balde por IP, em memória do processo (1 instância única, §3.6);
- headers `X-RateLimit-Limit`, `-Remaining`, `-Reset` em toda resposta;
- 429 com `Retry-After` e corpo Problem Details quando estoura;
- `/health` e `/metrics` ficam de fora, para não bloquear monitoramento.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from backend.erros import problema

log = logging.getLogger("backend.limites")

ROTAS_ISENTAS = ("/health", "/metrics")
INATIVIDADE_PARA_DESCARTE_S = 600


@dataclass
class Balde:
    tokens: float
    ultimo_acesso: float = field(default_factory=time.monotonic)


class LimitadorTokenBucket:
    def __init__(self, limite: int, periodo_s: float) -> None:
        self.limite = limite
        self.periodo_s = periodo_s
        self.taxa = limite / periodo_s
        self.baldes: dict[str, Balde] = {}

    def consumir(self, chave: str) -> tuple[bool, int, float]:
        """Tenta gastar 1 token. Retorna (permitido, restantes, segundos até repor)."""
        agora = time.monotonic()
        balde = self.baldes.get(chave)
        if balde is None:
            balde = self.baldes[chave] = Balde(tokens=float(self.limite))
        else:
            balde.tokens = min(self.limite, balde.tokens + (agora - balde.ultimo_acesso) * self.taxa)
        balde.ultimo_acesso = agora

        if balde.tokens >= 1:
            balde.tokens -= 1
            permitido = True
        else:
            permitido = False

        faltando = 1 - balde.tokens
        segundos_para_repor = max(0.0, faltando / self.taxa) if faltando > 0 else 0.0
        self._descartar_inativos(agora)
        return permitido, int(balde.tokens), segundos_para_repor

    def _descartar_inativos(self, agora: float) -> None:
        """Evita crescer sem limite sob scraping com IPs variados."""
        if len(self.baldes) < 1000:
            return
        antigos = [ip for ip, b in self.baldes.items()
                   if agora - b.ultimo_acesso > INATIVIDADE_PARA_DESCARTE_S]
        for ip in antigos:
            del self.baldes[ip]


def parse_limite(expressao: str) -> tuple[int, float]:
    """'60/minute' -> (60, 60.0). Aceita second, minute e hour."""
    quantidade, _, unidade = expressao.partition("/")
    periodos = {"second": 1.0, "minute": 60.0, "hour": 3600.0}
    if unidade.strip() not in periodos:
        raise ValueError(f"limite inválido: {expressao!r} (use N/second, N/minute ou N/hour)")
    return int(quantidade), periodos[unidade.strip()]


class MiddlewareRateLimit(BaseHTTPMiddleware):
    def __init__(self, app, expressao: str) -> None:
        super().__init__(app)
        limite, periodo = parse_limite(expressao)
        self.limitador = LimitadorTokenBucket(limite, periodo)
        log.info("[limites] %s por IP", expressao)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ROTAS_ISENTAS:
            return await call_next(request)

        ip = request.client.host if request.client else "desconhecido"
        permitido, restantes, reset = self.limitador.consumir(ip)

        if not permitido:
            log.warning("[limites] 429 para %s em %s", ip, request.url.path)
            resposta = problema(
                request, 429,
                f"Limite de {self.limitador.limite} requisições por "
                f"{int(self.limitador.periodo_s)}s excedido. Tente de novo em {reset:.0f}s.")
            resposta.headers["Retry-After"] = str(max(1, int(reset)))
        else:
            resposta = await call_next(request)

        resposta.headers["X-RateLimit-Limit"] = str(self.limitador.limite)
        resposta.headers["X-RateLimit-Remaining"] = str(max(0, restantes))
        resposta.headers["X-RateLimit-Reset"] = str(int(reset))
        return resposta
