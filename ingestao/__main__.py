"""Orquestrador da ingestão: roda as três fontes em sequência, na ordem certa.

    python -m ingestao                          # ONS, ANEEL, locais.csv, NASA
    python -m ingestao --inicio 2026-01 --fim 2026-08
    python -m ingestao --fontes ons             # só uma fonte
    python -m ingestao --listar                 # mostra o plano e sai
    python -m ingestao --seguir                 # não para na primeira falha

A ordem **não** é arbitrária: `gerar_locais` lê o cadastro da ANEEL para montar
o `locais.csv`, e a ingestão da NASA consulta exatamente as coordenadas desse
arquivo. Rodar a NASA antes da ANEEL usaria os pontos da coleta anterior.

Cada etapa roda como subprocesso (`python -m <modulo>`), e não por importação:
assim cada script mantém o próprio `argparse`, o próprio logging e o próprio
`.env`, e uma falha de uma fonte não deixa estado pela metade no processo das
outras. O custo é um interpretador por etapa — irrelevante perto do tempo de
rede de cada uma.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

log = logging.getLogger("ingestao")


@dataclass(frozen=True)
class Etapa:
    nome: str
    modulo: str
    descricao: str
    aceita_periodo: bool = False


# A ordem desta lista é a ordem de execução.
ETAPAS: tuple[Etapa, ...] = (
    Etapa("ons", "ingestao.ONS.ingestao_ons",
          "geração horária por usina (um Parquet por mês)", aceita_periodo=True),
    Etapa("aneel", "ingestao.aneel.ingestao_aneel",
          "cadastro SIGA + retrato datado"),
    Etapa("locais", "ingestao.nasa_power.gerar_locais",
          "locais.csv a partir das coordenadas da ANEEL"),
    Etapa("nasa", "ingestao.nasa_power.ingestao_nasa_power",
          "clima horário e diário dos pontos do locais.csv", aceita_periodo=True),
)
NOMES = [etapa.nome for etapa in ETAPAS]


def comando(etapa: Etapa, inicio: str | None, fim: str | None) -> list[str]:
    """Linha de comando da etapa. O período só é repassado a quem o aceita."""
    argumentos = [sys.executable, "-m", etapa.modulo]
    if etapa.aceita_periodo:
        if inicio:
            argumentos += ["--inicio", inicio]
        if fim:
            argumentos += ["--fim", fim]
    return argumentos


def rodar(etapas: list[Etapa], inicio: str | None = None, fim: str | None = None,
          seguir: bool = False) -> dict[str, str]:
    """Executa as etapas em ordem. Devolve {nome: 'ok' | 'falhou' | 'pulada'}.

    Sem `seguir`, para na primeira falha: continuar com a ANEEL quebrada só
    produziria um `locais.csv` desatualizado e um clima dos pontos antigos.
    """
    situacao = {etapa.nome: "pulada" for etapa in etapas}
    duracoes: dict[str, float] = {}

    for i, etapa in enumerate(etapas, start=1):
        log.info("[ingestao] (%d/%d) %s — %s", i, len(etapas), etapa.nome, etapa.descricao)
        marca = time.perf_counter()
        resultado = subprocess.run(comando(etapa, inicio, fim), cwd=RAIZ, check=False)
        duracoes[etapa.nome] = time.perf_counter() - marca

        if resultado.returncode == 0:
            situacao[etapa.nome] = "ok"
            log.info("[ingestao] %s concluída em %.1f s", etapa.nome, duracoes[etapa.nome])
            continue

        situacao[etapa.nome] = "falhou"
        log.error("[ingestao] %s falhou (código %d)", etapa.nome, resultado.returncode)
        if not seguir:
            log.error("[ingestao] interrompido; use --seguir para continuar mesmo assim")
            break

    _resumo(situacao, duracoes)
    return situacao


def _resumo(situacao: dict[str, str], duracoes: dict[str, float]) -> None:
    simbolo = {"ok": "ok     ", "falhou": "FALHOU ", "pulada": "pulada "}
    log.info("[ingestao] resumo:")
    for nome, estado in situacao.items():
        tempo = f"{duracoes[nome]:6.1f} s" if nome in duracoes else "       -"
        log.info("  %s %-8s %s", simbolo[estado], nome, tempo)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="python -m ingestao",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--fontes", nargs="+", choices=NOMES, default=NOMES, metavar="FONTE",
                        help=f"etapas a rodar, sempre na ordem canônica ({', '.join(NOMES)})")
    parser.add_argument("--inicio", help="AAAA-MM, repassado a ONS e NASA")
    parser.add_argument("--fim", help="AAAA-MM, repassado a ONS e NASA")
    parser.add_argument("--seguir", action="store_true",
                        help="continua nas demais etapas mesmo se uma falhar")
    parser.add_argument("--listar", action="store_true", help="mostra o plano e sai")
    args = parser.parse_args()

    # A ordem é a de ETAPAS, não a que o usuário digitou
    escolhidas = [etapa for etapa in ETAPAS if etapa.nome in args.fontes]

    if args.listar:
        for i, etapa in enumerate(escolhidas, start=1):
            print(f"{i}. {etapa.nome:8} {' '.join(comando(etapa, args.inicio, args.fim)[2:])}")
            print(f"{'':11}{etapa.descricao}")
        return

    inicio = time.perf_counter()
    situacao = rodar(escolhidas, args.inicio, args.fim, args.seguir)
    log.info("[ingestao] total: %.1f s", time.perf_counter() - inicio)

    if any(estado != "ok" for estado in situacao.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
