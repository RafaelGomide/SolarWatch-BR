"""Ingestão da ANEEL — SIGA (Sistema de Informações de Geração da ANEEL).

Consulta a API pública CKAN do portal de dados abertos da ANEEL (endpoint
`datastore_search`) e baixa o cadastro de empreendimentos de geração solar
fotovoltaica (UFV) e eólica (EOL): nome, CEG, UF, município, potência
outorgada/fiscalizada, fase da usina, data de entrada em operação,
coordenadas etc. O resultado é gravado, sem transformação, em
`dados/bruto/dados_aneel_bruto.parquet` (retrato mais recente, que é o que o ETL lê).

Cada execução também **arquiva o retrato datado** em
`dados/bruto/historico_aneel/dados_aneel_bruto_AAAA-MM-DD.parquet`. O recurso da
ANEEL é republicado diariamente e sobrescrito: sem o arquivo, cada coleta apaga
a anterior e se perde o histórico de mudanças de fase (construção não iniciada
-> construção -> operação), que é exatamente o evento que a análise de
sobrevivência precisaria para trabalhar com dados reais em vez de simulados.

A data do retrato vem do próprio dado (`DatGeracaoConjuntoDados`), não do
relógio da máquina: é a data em que a ANEEL gerou o conjunto.

Uso:
    python -m ingestao.aneel.ingestao_aneel
    python -m ingestao.aneel.ingestao_aneel --tipos UFV EOL UHE
    python -m ingestao.aneel.ingestao_aneel --listar-retratos   # não baixa nada
    python -m ingestao.aneel.ingestao_aneel --mudancas-de-fase  # compara os 2 últimos
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from ingestao.armazenamento import gravar_parquet
from ingestao.http import get_com_retry, nova_sessao

API_URL = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search"
# Recurso "siga-empreendimentos-geracao-diario.csv" (atualizado diariamente)
RESOURCE_ID = "2f65a1b0-19b8-4360-8238-b34ab4693d55"
TIPOS_PADRAO = ["UFV", "EOL"]  # UFV = solar fotovoltaica, EOL = eólica
TAMANHO_PAGINA = 5000
PAUSA_ENTRE_CHAMADAS_S = 0.5  # cortesia com a API pública

RAIZ = Path(__file__).resolve().parents[2]
SAIDA = RAIZ / "dados" / "bruto" / "dados_aneel_bruto.parquet"
HISTORICO = RAIZ / "dados" / "bruto" / "historico_aneel"
PADRAO_RETRATO = re.compile(r"dados_aneel_bruto_(\d{4}-\d{2}-\d{2})\.parquet$")
COLUNA_DATA_RETRATO = "DatGeracaoConjuntoDados"
CHAVE_USINA = "CodCEG"
COLUNA_FASE = "DscFaseUsina"

log = logging.getLogger("ingestao_aneel")


def _pagina(sessao: requests.Session, tipos: list[str], offset: int) -> dict:
    resp = get_com_retry(
        sessao,
        API_URL,
        params={
            "resource_id": RESOURCE_ID,
            "filters": json.dumps({"SigTipoGeracao": tipos}),
            "limit": TAMANHO_PAGINA,
            "offset": offset,
            # Ordenação estável: sem ela, a paginação por offset pode repetir/pular linhas
            "sort": "_id asc",
        },
    )
    resp.raise_for_status()
    corpo = resp.json()
    if not corpo.get("success"):
        raise RuntimeError(f"API da ANEEL retornou erro: {corpo.get('error')}")
    return corpo["result"]


def data_do_retrato(bruto: pd.DataFrame) -> date:
    """Data em que a ANEEL gerou o conjunto, lida do próprio dado.

    Usar o relógio da máquina daria a data do download, que não é a mesma coisa:
    baixar o mesmo retrato dois dias seguidos criaria dois arquivos idênticos
    com datas diferentes.
    """
    valores = pd.to_datetime(bruto[COLUNA_DATA_RETRATO], errors="coerce").dropna()
    if valores.empty:
        log.warning("[historico] %s ausente; usando a data de hoje", COLUNA_DATA_RETRATO)
        return date.today()
    if valores.dt.date.nunique() > 1:
        # Não deveria acontecer: o recurso é gerado de uma vez
        log.warning("[historico] retrato com %d datas distintas; usando a maior",
                    valores.dt.date.nunique())
    return valores.max().date()


def arquivar(bruto: pd.DataFrame, historico: Path = HISTORICO) -> Path:
    """Grava o retrato datado. Idempotente: se o arquivo do dia já existe, não regrava."""
    destino = historico / f"dados_aneel_bruto_{data_do_retrato(bruto).isoformat()}.parquet"
    if destino.exists():
        log.info("[historico] %s já arquivado, mantido", destino.name)
        return destino
    return gravar_parquet(bruto, destino)


def retratos(historico: Path = HISTORICO) -> list[tuple[date, Path]]:
    """Retratos arquivados, do mais antigo para o mais recente."""
    if not historico.exists():
        return []
    achados = [(date.fromisoformat(casamento[1]), caminho)
               for caminho in historico.glob("*.parquet")
               if (casamento := PADRAO_RETRATO.search(caminho.name))]
    return sorted(achados)


def mudancas_de_fase(anterior: Path, atual: Path) -> pd.DataFrame:
    """Usinas que mudaram de fase entre dois retratos, mais as que entraram no cadastro.

    É a informação que o retrato único destrói e a razão de arquivar: a data em
    que uma usina passa a "Operação" é um evento observado, não uma data de
    cadastro retroativa.
    """
    colunas = [CHAVE_USINA, "NomEmpreendimento", "SigUFPrincipal", COLUNA_FASE]
    antes = pd.read_parquet(anterior, columns=colunas).drop_duplicates(CHAVE_USINA)
    depois = pd.read_parquet(atual, columns=colunas).drop_duplicates(CHAVE_USINA)

    juntos = depois.merge(antes[[CHAVE_USINA, COLUNA_FASE]], on=CHAVE_USINA,
                          how="left", suffixes=("", "_antes"))
    fase_antes = juntos[f"{COLUNA_FASE}_antes"]
    mudou = fase_antes.isna() | fase_antes.ne(juntos[COLUNA_FASE])
    return (juntos[mudou]
            .assign(**{f"{COLUNA_FASE}_antes": fase_antes[mudou].fillna("(não cadastrada)")})
            .rename(columns={COLUNA_FASE: "fase_depois", f"{COLUNA_FASE}_antes": "fase_antes"})
            [[CHAVE_USINA, "NomEmpreendimento", "SigUFPrincipal", "fase_antes", "fase_depois"]]
            .reset_index(drop=True))


def baixar(tipos: list[str], saida: Path = SAIDA, historico: Path = HISTORICO) -> Path:
    sessao = nova_sessao()

    registros: list[dict] = []
    colunas: list[str] = []
    total = None
    offset = 0
    while total is None or offset < total:
        resultado = _pagina(sessao, tipos, offset)
        if total is None:
            total = resultado["total"]
            colunas = [campo["id"] for campo in resultado["fields"]]
            log.info("%d empreendimentos dos tipos %s", total, ", ".join(tipos))
        elif resultado["total"] != total:
            # O recurso é republicado diariamente; se mudar no meio, o retrato fica inconsistente
            raise RuntimeError(
                f"Total mudou durante a paginação ({total} -> {resultado['total']}); rode de novo."
            )
        lote = resultado["records"]
        if not lote:
            break
        registros.extend(lote)
        offset += len(lote)
        log.info("  %d/%d", len(registros), total)
        time.sleep(PAUSA_ENTRE_CHAMADAS_S)

    bruto = pd.DataFrame(registros, columns=colunas).drop_duplicates(subset="_id")
    if len(bruto) != total:
        raise RuntimeError(f"Esperados {total} registros, recebidos {len(bruto)}.")

    arquivar(bruto, historico)   # retrato datado, preservado
    return gravar_parquet(bruto, saida)   # retrato corrente, lido pelo ETL


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tipos",
        nargs="+",
        default=TIPOS_PADRAO,
        help="Siglas SigTipoGeracao (padrão: UFV EOL)",
    )
    parser.add_argument("--listar-retratos", action="store_true",
                        help="lista os retratos arquivados e sai, sem baixar nada")
    parser.add_argument("--mudancas-de-fase", action="store_true",
                        help="compara os dois retratos mais recentes e sai, sem baixar nada")
    args = parser.parse_args()

    if args.listar_retratos or args.mudancas_de_fase:
        arquivados = retratos()
        if not arquivados:
            raise SystemExit(f"Nenhum retrato em {HISTORICO}. Rode a ingestão primeiro.")
        for dia, caminho in arquivados:
            log.info("%s  %s  (%.1f MB)", dia, caminho.name, caminho.stat().st_size / 1024**2)
        if args.mudancas_de_fase:
            if len(arquivados) < 2:
                raise SystemExit("São necessários 2 retratos para comparar; há só 1.")
            (_, anterior), (_, atual) = arquivados[-2], arquivados[-1]
            mudancas = mudancas_de_fase(anterior, atual)
            log.info("%d usina(s) mudaram de fase entre %s e %s",
                     len(mudancas), arquivados[-2][0], arquivados[-1][0])
            if not mudancas.empty:
                print(mudancas.to_string(index=False))
        return

    baixar([tipo.upper() for tipo in args.tipos])


if __name__ == "__main__":
    main()
