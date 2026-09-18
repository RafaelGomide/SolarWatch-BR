# -*- coding: utf-8 -*-
"""
================================================================================
DS TOOLKIT — Caixa de ferramentas para o dia a dia do Cientista de Dados
================================================================================

Autor........: Rafael Gomide
Descrição....: Módulo com funções reutilizáveis e altamente parametrizáveis
               para acelerar fluxos de trabalho de Data Science, organizado em
               5 seções:

               1. INGESTÃO DE DADOS  -> leitura/escrita de qualquer fonte
               2. ETL                -> limpeza, tratamento e transformação
               3. EDA                -> análise exploratória e testes estatísticos
               4. GRÁFICOS           -> visualizações prontas (tema claro/escuro)
               5. ML                 -> preparação, avaliação e comparação de modelos

Uso rápido:
    >>> import ds_toolkit as dst
    >>> df = dst.carregar_dados("dados.csv")
    >>> dst.relatorio_qualidade(df)
    >>> dst.plot_correlacao(df)

Dependências: pandas, numpy, scipy, scikit-learn, matplotlib, seaborn,
              requests (opcional p/ APIs), sqlalchemy (opcional p/ SQL),
              joblib (persistência de modelos).

Convenções:
    - Nenhuma função altera o DataFrame original por padrão (sempre trabalha
      sobre cópia e retorna um novo objeto), a menos que `inplace=True`.
    - `verbose=True` imprime relatórios legíveis; `verbose=False` silencia.
    - Funções de plot retornam `(fig, ax)` para customização posterior.
================================================================================
"""

from __future__ import annotations

import os
import re
import glob
import json
import warnings
import unicodedata
from pathlib import Path
from typing import Iterable, Literal, Sequence, Any, Callable

import numpy as np
import pandas as pd
from scipy import stats

# ------------------------------------------------------------------------------
# Imports opcionais (não quebram o módulo se ausentes)
# ------------------------------------------------------------------------------
try:
    import requests
    _TEM_REQUESTS = True
except ImportError:
    _TEM_REQUESTS = False

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import seaborn as sns
    _TEM_PLOT = True
except ImportError:
    _TEM_PLOT = False

try:
    import joblib
    _TEM_JOBLIB = True
except ImportError:
    _TEM_JOBLIB = False

try:
    import lifelines
    _TEM_LIFELINES = True
except ImportError:
    _TEM_LIFELINES = False


# ==============================================================================
# 1. INGESTÃO DE DADOS
# ==============================================================================

def carregar_dados(
    caminho: str | Path,
    *,
    encodings: Sequence[str] = ("utf-8", "latin-1", "cp1252"),
    detectar_separador: bool = True,
    decimal_brasileiro: bool = False,
    aba: str | int | None = 0,
    verbose: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """Carrega um arquivo de dados detectando o formato pela extensão.

    Resolve os problemas mais comuns de ingestão:
    - Encoding errado (tenta uma lista de encodings em cascata);
    - Separador desconhecido em CSV/TXT (`;`, `,`, `\\t`, `|`);
    - Números no padrão brasileiro (`1.234,56`) via `decimal_brasileiro=True`.

    Formatos suportados: .csv, .txt, .tsv, .xlsx, .xls, .json, .parquet,
    .feather, .pkl/.pickle, .html, .xml.

    Parameters
    ----------
    caminho : str | Path
        Caminho do arquivo.
    encodings : sequence of str
        Encodings testados em ordem para arquivos de texto.
    detectar_separador : bool
        Se True, usa `sep=None, engine='python'` (sniffer do pandas) em CSV/TXT.
    decimal_brasileiro : bool
        Se True, lê CSV/TXT com `decimal=',', thousands='.'`.
    aba : str | int | None
        Aba do Excel (nome ou índice). `None` retorna dict com todas as abas.
    verbose : bool
        Imprime shape e memória após a leitura.
    **kwargs
        Repassados à função `pd.read_*` correspondente
        (ex.: `dtype=`, `usecols=`, `nrows=`, `parse_dates=`).

    Returns
    -------
    pd.DataFrame
        (ou dict de DataFrames, se Excel com `aba=None`).

    Examples
    --------
    >>> df = carregar_dados("vendas.csv", decimal_brasileiro=True)
    >>> df = carregar_dados("relatorio.xlsx", aba="2025")
    >>> df = carregar_dados("dados.parquet", columns=["id", "preco"])
    """
    caminho = Path(caminho)
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")

    ext = caminho.suffix.lower()

    def _ler_texto(func: Callable) -> pd.DataFrame:
        """Tenta múltiplos encodings para leitores baseados em texto."""
        opts = dict(kwargs)
        if detectar_separador and "sep" not in opts:
            opts.update(sep=None, engine="python")
        if decimal_brasileiro:
            opts.setdefault("decimal", ",")
            opts.setdefault("thousands", ".")
        ultimo_erro: Exception | None = None
        for enc in encodings:
            try:
                return func(caminho, encoding=enc, **opts)
            except (UnicodeDecodeError, UnicodeError) as e:
                ultimo_erro = e
        raise UnicodeDecodeError(
            "utf-8", b"", 0, 1,
            f"Nenhum encoding funcionou ({encodings}). Último erro: {ultimo_erro}"
        )

    if ext in (".csv", ".txt", ".tsv"):
        df = _ler_texto(pd.read_csv)
    elif ext in (".xlsx", ".xls", ".xlsm"):
        df = pd.read_excel(caminho, sheet_name=aba, **kwargs)
    elif ext == ".json":
        try:
            df = pd.read_json(caminho, **kwargs)
        except ValueError:
            # JSON aninhado -> tenta normalizar
            with open(caminho, "r", encoding="utf-8") as f:
                df = pd.json_normalize(json.load(f))
    elif ext == ".parquet":
        df = pd.read_parquet(caminho, **kwargs)
    elif ext == ".feather":
        df = pd.read_feather(caminho, **kwargs)
    elif ext in (".pkl", ".pickle"):
        df = pd.read_pickle(caminho, **kwargs)
    elif ext in (".html", ".htm"):
        df = pd.read_html(caminho, **kwargs)[0]
    elif ext == ".xml":
        df = pd.read_xml(caminho, **kwargs)
    else:
        raise ValueError(f"Extensão não suportada: '{ext}'")

    if verbose and isinstance(df, pd.DataFrame):
        mem = df.memory_usage(deep=True).sum() / 1024**2
        print(f"[carregar_dados] {caminho.name}: {df.shape[0]:,} linhas x "
              f"{df.shape[1]} colunas | {mem:.2f} MB")
    return df


def carregar_multiplos(
    pasta: str | Path,
    padrao: str = "*.csv",
    *,
    adicionar_origem: bool = True,
    ignorar_erros: bool = False,
    verbose: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """Lê todos os arquivos que casam com um padrão glob e concatena em um único DataFrame.

    Útil para consolidar exportações mensais, partições de scraping, logs etc.

    Parameters
    ----------
    pasta : str | Path
        Diretório onde procurar.
    padrao : str
        Padrão glob (ex.: "*.csv", "vendas_2025_*.xlsx").
    adicionar_origem : bool
        Se True, cria a coluna `_arquivo_origem` com o nome do arquivo fonte
        (essencial para depurar de onde veio cada linha).
    ignorar_erros : bool
        Se True, arquivos com erro são pulados com aviso, em vez de abortar.
    **kwargs
        Repassados para `carregar_dados` (ex.: `decimal_brasileiro=True`).

    Returns
    -------
    pd.DataFrame concatenado (index resetado).

    Examples
    --------
    >>> df = carregar_multiplos("exports/", "vendas_*.csv", decimal_brasileiro=True)
    """
    arquivos = sorted(glob.glob(str(Path(pasta) / padrao)))
    if not arquivos:
        raise FileNotFoundError(f"Nenhum arquivo casa com '{padrao}' em {pasta}")

    partes: list[pd.DataFrame] = []
    for arq in arquivos:
        try:
            parte = carregar_dados(arq, verbose=False, **kwargs)
            if adicionar_origem:
                parte["_arquivo_origem"] = Path(arq).name
            partes.append(parte)
        except Exception as e:
            if ignorar_erros:
                warnings.warn(f"Pulei '{arq}': {e}")
            else:
                raise

    df = pd.concat(partes, ignore_index=True)
    if verbose:
        print(f"[carregar_multiplos] {len(partes)} arquivos -> "
              f"{df.shape[0]:,} linhas x {df.shape[1]} colunas")
    return df


def carregar_sql(
    query: str,
    conexao: Any,
    *,
    parse_dates: Sequence[str] | None = None,
    chunksize: int | None = None,
    verbose: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """Executa uma query SQL e retorna DataFrame.

    Aceita qualquer conexão compatível com pandas: engine do SQLAlchemy,
    `sqlite3.Connection`, connection string etc.

    Parameters
    ----------
    query : str
        SQL a executar (SELECT).
    conexao : Any
        Engine/conexão/URL. Ex.: `sqlalchemy.create_engine("sqlite:///db.db")`.
    parse_dates : sequence of str, optional
        Colunas a converter para datetime na leitura.
    chunksize : int, optional
        Se definido, lê em blocos e concatena ao final — evita estourar
        memória em tabelas gigantes.

    Examples
    --------
    >>> from sqlalchemy import create_engine
    >>> eng = create_engine("sqlite:///imoveis.db")
    >>> df = carregar_sql("SELECT * FROM anuncios WHERE preco > 0", eng)
    """
    if chunksize:
        partes = pd.read_sql(query, conexao, parse_dates=parse_dates,
                             chunksize=chunksize, **kwargs)
        df = pd.concat(partes, ignore_index=True)
    else:
        df = pd.read_sql(query, conexao, parse_dates=parse_dates, **kwargs)
    if verbose:
        print(f"[carregar_sql] {df.shape[0]:,} linhas x {df.shape[1]} colunas")
    return df


def carregar_api(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    caminho_json: str | None = None,
    tentativas: int = 3,
    timeout: int = 30,
    intervalo_retry: float = 2.0,
    verbose: bool = True,
) -> pd.DataFrame:
    """Consome uma API REST (GET) com retry automático e devolve DataFrame.

    Trata os problemas clássicos: timeout, erro 5xx intermitente, resposta
    aninhada (use `caminho_json` para navegar até a lista de registros).

    Parameters
    ----------
    url : str
        Endpoint da API.
    params : dict, optional
        Query params (ex.: {"page": 1, "limit": 100}).
    headers : dict, optional
        Headers HTTP (ex.: {"Authorization": "Bearer ..."}).
    caminho_json : str, optional
        Caminho separado por pontos até a lista de registros dentro do JSON.
        Ex.: "data.results" para `{"data": {"results": [...]}}`.
    tentativas : int
        Número máximo de tentativas em caso de falha de rede/5xx.
    timeout : int
        Timeout em segundos por requisição.
    intervalo_retry : float
        Espera (com backoff exponencial) entre tentativas.

    Examples
    --------
    >>> df = carregar_api(
    ...     "https://dadosabertos.camara.leg.br/api/v2/proposicoes",
    ...     params={"itens": 100}, caminho_json="dados")
    """
    if not _TEM_REQUESTS:
        raise ImportError("Instale requests: pip install requests")
    import time

    ultimo_erro: Exception | None = None
    for i in range(1, tentativas + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            dados = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            ultimo_erro = e
            if verbose:
                print(f"[carregar_api] tentativa {i}/{tentativas} falhou: {e}")
            time.sleep(intervalo_retry * (2 ** (i - 1)))
    else:
        raise ConnectionError(f"API falhou após {tentativas} tentativas: {ultimo_erro}")

    if caminho_json:
        for chave in caminho_json.split("."):
            dados = dados[chave]

    df = pd.json_normalize(dados) if isinstance(dados, (list, dict)) else pd.DataFrame(dados)
    if verbose:
        print(f"[carregar_api] {df.shape[0]:,} registros de {url}")
    return df


def salvar_dados(
    df: pd.DataFrame,
    caminho: str | Path,
    *,
    criar_pastas: bool = True,
    index: bool = False,
    verbose: bool = True,
    **kwargs: Any,
) -> Path:
    """Salva um DataFrame no formato deduzido pela extensão do caminho.

    Suporta: .csv, .xlsx, .json, .parquet, .feather, .pkl/.pickle, .html.

    Parameters
    ----------
    criar_pastas : bool
        Cria diretórios intermediários automaticamente.
    index : bool
        Se True, grava o índice (padrão False — causa nº 1 de coluna
        'Unnamed: 0' fantasma ao reler CSVs).
    **kwargs
        Repassados ao `df.to_*` (ex.: `sep=';'`, `sheet_name='dados'`).

    Examples
    --------
    >>> salvar_dados(df, "saida/resultado.parquet")
    >>> salvar_dados(df, "saida/relatorio.xlsx", sheet_name="Q3")
    """
    caminho = Path(caminho)
    if criar_pastas:
        caminho.parent.mkdir(parents=True, exist_ok=True)

    ext = caminho.suffix.lower()
    if ext == ".csv":
        df.to_csv(caminho, index=index, **kwargs)
    elif ext in (".xlsx", ".xls"):
        df.to_excel(caminho, index=index, **kwargs)
    elif ext == ".json":
        df.to_json(caminho, orient=kwargs.pop("orient", "records"),
                   force_ascii=False, **kwargs)
    elif ext == ".parquet":
        df.to_parquet(caminho, index=index, **kwargs)
    elif ext == ".feather":
        df.reset_index(drop=not index).to_feather(caminho, **kwargs)
    elif ext in (".pkl", ".pickle"):
        df.to_pickle(caminho, **kwargs)
    elif ext == ".html":
        df.to_html(caminho, index=index, **kwargs)
    else:
        raise ValueError(f"Extensão não suportada para escrita: '{ext}'")

    if verbose:
        tam = caminho.stat().st_size / 1024**2
        print(f"[salvar_dados] gravado em {caminho} ({tam:.2f} MB)")
    return caminho


# ==============================================================================
# 2. ETL — LIMPEZA E TRANSFORMAÇÃO
# ==============================================================================

def relatorio_qualidade(
    df: pd.DataFrame,
    *,
    limite_cardinalidade: int = 50,
    verbose: bool = True,
) -> pd.DataFrame:
    """Gera um diagnóstico completo de qualidade dos dados, coluna a coluna.

    Para cada coluna reporta: dtype, nº e % de nulos, nº de valores únicos,
    % de valor mais frequente (detecta colunas quase-constantes), exemplos,
    e flags de problemas comuns:
        - `constante`        : 1 único valor (inútil para modelagem)
        - `quase_constante`  : valor dominante > 95%
        - `alta_cardinalidade`: nº únicos > limite (cuidado com one-hot!)
        - `possivel_id`      : todos os valores são únicos
        - `muitos_nulos`     : > 50% nulos

    Também reporta linhas duplicadas no DataFrame inteiro.

    Returns
    -------
    pd.DataFrame com o diagnóstico (uma linha por coluna).

    Examples
    --------
    >>> diag = relatorio_qualidade(df)
    >>> diag[diag["flags"] != ""]  # só colunas com problemas
    """
    n = len(df)
    linhas = []
    for col in df.columns:
        s = df[col]
        n_nulos = int(s.isna().sum())
        n_unicos = int(s.nunique(dropna=True))
        try:
            top_freq = s.value_counts(dropna=True).iloc[0] / max(n - n_nulos, 1)
        except (IndexError, TypeError):
            top_freq = np.nan

        flags = []
        if n_unicos <= 1:
            flags.append("constante")
        elif top_freq is not np.nan and top_freq > 0.95:
            flags.append("quase_constante")
        if n_unicos == n - n_nulos and n_unicos > 1:
            flags.append("possivel_id")
        if s.dtype == object and n_unicos > limite_cardinalidade:
            flags.append("alta_cardinalidade")
        if n and n_nulos / n > 0.5:
            flags.append("muitos_nulos")

        exemplos = s.dropna().unique()[:3]
        linhas.append({
            "coluna": col,
            "dtype": str(s.dtype),
            "nulos": n_nulos,
            "%_nulos": round(100 * n_nulos / n, 2) if n else 0,
            "unicos": n_unicos,
            "%_top_valor": round(100 * top_freq, 1) if pd.notna(top_freq) else np.nan,
            "exemplos": ", ".join(map(str, exemplos))[:60],
            "flags": " | ".join(flags),
        })

    diag = pd.DataFrame(linhas)
    if verbose:
        dups = int(df.duplicated().sum())
        mem = df.memory_usage(deep=True).sum() / 1024**2
        print(f"[qualidade] {n:,} linhas x {df.shape[1]} colunas | "
              f"{dups:,} linhas duplicadas ({100*dups/max(n,1):.1f}%) | {mem:.2f} MB")
        problemas = diag[diag["flags"] != ""]
        if not problemas.empty:
            print(f"[qualidade] {len(problemas)} colunas com flags de atenção:")
            print(problemas[["coluna", "flags"]].to_string(index=False))
    return diag


def limpar_nomes_colunas(
    df: pd.DataFrame,
    *,
    minusculas: bool = True,
    remover_acentos: bool = True,
    snake_case: bool = True,
    prefixo_numerico: str = "col_",
    inplace: bool = False,
) -> pd.DataFrame:
    """Padroniza nomes de colunas: remove acentos, espaços e caracteres especiais.

    'Preço do Imóvel (R$)' -> 'preco_do_imovel_r'

    Resolve o clássico problema de colunas com espaço/acento que quebram
    `df.query()`, SQL, e acesso por atributo.

    Parameters
    ----------
    prefixo_numerico : str
        Prefixo adicionado a colunas que começariam com dígito
        ('2024' -> 'col_2024'), pois nomes iniciados por número são inválidos
        em muitos contextos.

    Examples
    --------
    >>> df = limpar_nomes_colunas(df)
    """
    alvo = df if inplace else df.copy()
    novos = []
    vistos: dict[str, int] = {}
    for c in alvo.columns:
        nome = str(c).strip()
        if remover_acentos:
            nome = unicodedata.normalize("NFKD", nome)
            nome = nome.encode("ascii", "ignore").decode("ascii")
        if minusculas:
            nome = nome.lower()
        if snake_case:
            nome = re.sub(r"[^\w]+", "_", nome)
            nome = re.sub(r"_+", "_", nome).strip("_")
        if nome and nome[0].isdigit():
            nome = prefixo_numerico + nome
        if not nome:
            nome = "coluna_sem_nome"
        # resolve duplicatas: preco, preco_2, preco_3...
        if nome in vistos:
            vistos[nome] += 1
            nome = f"{nome}_{vistos[nome]}"
        else:
            vistos[nome] = 1
        novos.append(nome)
    alvo.columns = novos
    return alvo


def converter_tipos(
    df: pd.DataFrame,
    *,
    colunas_data: Sequence[str] | None = None,
    formato_data: str | None = None,
    dayfirst: bool = True,
    colunas_numericas: Sequence[str] | None = None,
    decimal_brasileiro: bool = True,
    colunas_categoricas: Sequence[str] | None = None,
    auto_detectar: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Converte tipos de colunas de forma robusta, com foco em dados brasileiros.

    Problemas que resolve:
    - Datas em dd/mm/aaaa lidas como string (usa `dayfirst=True` por padrão);
    - Números como "R$ 1.234,56" ou "1.234,56" viram float de verdade;
    - Colunas de baixa cardinalidade viram `category` (economia de memória).

    Parameters
    ----------
    colunas_data : sequence of str, optional
        Colunas a converter para datetime. Valores inválidos viram NaT.
    formato_data : str, optional
        Formato explícito (ex.: "%d/%m/%Y"). Mais rápido e seguro que inferir.
    colunas_numericas : sequence of str, optional
        Colunas string a converter para número. Remove "R$", "%", espaços.
    decimal_brasileiro : bool
        Se True, interpreta '.' como milhar e ',' como decimal.
    colunas_categoricas : sequence of str, optional
        Colunas a converter para dtype `category`.
    auto_detectar : bool
        Se True, tenta converter TODAS as colunas object para número/data
        automaticamente (mantém como estava quando >20% viraria nulo).

    Examples
    --------
    >>> df = converter_tipos(df,
    ...     colunas_data=["data_anuncio"], formato_data="%d/%m/%Y",
    ...     colunas_numericas=["preco", "condominio"])
    """
    df = df.copy()

    def _para_numero(s: pd.Series) -> pd.Series:
        limpa = (s.astype(str)
                  .str.replace(r"[R$%\s\u00a0]", "", regex=True))
        if decimal_brasileiro:
            limpa = limpa.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        return pd.to_numeric(limpa, errors="coerce")

    for col in (colunas_numericas or []):
        antes = df[col].notna().sum()
        df[col] = _para_numero(df[col])
        perdidos = antes - df[col].notna().sum()
        if verbose and perdidos:
            print(f"[tipos] '{col}': {perdidos} valores viraram NaN na conversão numérica")

    for col in (colunas_data or []):
        antes = df[col].notna().sum()
        df[col] = pd.to_datetime(df[col], format=formato_data,
                                 dayfirst=dayfirst, errors="coerce")
        perdidos = antes - df[col].notna().sum()
        if verbose and perdidos:
            print(f"[tipos] '{col}': {perdidos} valores viraram NaT na conversão de data")

    for col in (colunas_categoricas or []):
        df[col] = df[col].astype("category")

    if auto_detectar:
        for col in df.select_dtypes(include="object").columns:
            original = df[col]
            num = _para_numero(original)
            if num.notna().sum() >= 0.8 * original.notna().sum():
                df[col] = num
                continue
            dt = pd.to_datetime(original, dayfirst=dayfirst, errors="coerce")
            if dt.notna().sum() >= 0.8 * original.notna().sum():
                df[col] = dt
    return df


def tratar_nulos(
    df: pd.DataFrame,
    estrategia: Literal["media", "mediana", "moda", "constante",
                        "ffill", "bfill", "interpolar", "knn", "drop_linhas",
                        "drop_colunas"] = "mediana",
    *,
    colunas: Sequence[str] | None = None,
    valor_constante: Any = 0,
    limite_drop_coluna: float = 0.5,
    knn_vizinhos: int = 5,
    criar_indicador: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Trata valores nulos com a estratégia escolhida.

    Estratégias
    -----------
    - "media"/"mediana" : só em colunas numéricas (mediana é robusta a outliers).
    - "moda"            : funciona em numéricas e categóricas.
    - "constante"       : preenche com `valor_constante`.
    - "ffill"/"bfill"   : propaga valor anterior/posterior (séries temporais).
    - "interpolar"      : interpolação linear (séries temporais numéricas).
    - "knn"             : KNNImputer do sklearn (usa padrões multivariados).
    - "drop_linhas"     : remove linhas com nulo nas colunas alvo.
    - "drop_colunas"    : remove colunas com fração de nulos > `limite_drop_coluna`.

    Parameters
    ----------
    colunas : sequence of str, optional
        Restringe o tratamento a essas colunas (padrão: todas as aplicáveis).
    criar_indicador : bool
        Se True, cria coluna booleana `<col>_era_nulo` antes de imputar —
        preserva a informação de "faltava" para o modelo.

    Examples
    --------
    >>> df = tratar_nulos(df, "mediana", colunas=["preco", "area"],
    ...                   criar_indicador=True)
    >>> df = tratar_nulos(df, "knn")   # imputação multivariada
    """
    df = df.copy()
    antes = int(df.isna().sum().sum())

    if estrategia == "drop_colunas":
        frac = df.isna().mean()
        remover = frac[frac > limite_drop_coluna].index.tolist()
        df = df.drop(columns=remover)
        if verbose:
            print(f"[nulos] removidas {len(remover)} colunas: {remover}")
        return df

    cols = list(colunas) if colunas else df.columns.tolist()

    if criar_indicador:
        for c in cols:
            if df[c].isna().any():
                df[f"{c}_era_nulo"] = df[c].isna()

    if estrategia == "drop_linhas":
        df = df.dropna(subset=cols)
    elif estrategia in ("ffill", "bfill"):
        df[cols] = df[cols].ffill() if estrategia == "ffill" else df[cols].bfill()
    elif estrategia == "interpolar":
        num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
        df[num] = df[num].interpolate(method="linear", limit_direction="both")
    elif estrategia == "knn":
        from sklearn.impute import KNNImputer
        num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
        if num:
            df[num] = KNNImputer(n_neighbors=knn_vizinhos).fit_transform(df[num])
    elif estrategia == "constante":
        df[cols] = df[cols].fillna(valor_constante)
    else:  # media, mediana, moda
        for c in cols:
            if not df[c].isna().any():
                continue
            if estrategia == "moda":
                moda = df[c].mode(dropna=True)
                if not moda.empty:
                    df[c] = df[c].fillna(moda.iloc[0])
            elif pd.api.types.is_numeric_dtype(df[c]):
                valor = df[c].mean() if estrategia == "media" else df[c].median()
                df[c] = df[c].fillna(valor)

    if verbose:
        depois = int(df.isna().sum().sum())
        print(f"[nulos] '{estrategia}': {antes:,} -> {depois:,} nulos "
              f"| linhas: {len(df):,}")
    return df


def tratar_duplicados(
    df: pd.DataFrame,
    *,
    subset: Sequence[str] | None = None,
    manter: Literal["first", "last", False] = "first",
    verbose: bool = True,
) -> pd.DataFrame:
    """Remove linhas duplicadas com relatório do que foi removido.

    Parameters
    ----------
    subset : sequence of str, optional
        Considera duplicata apenas por essas colunas (ex.: chave de negócio
        como ["id_anuncio"]) em vez da linha inteira.
    manter : "first" | "last" | False
        Qual ocorrência manter. `False` remove todas as ocorrências duplicadas.

    Examples
    --------
    >>> df = tratar_duplicados(df, subset=["url"], manter="last")
    """
    n_antes = len(df)
    df = df.drop_duplicates(subset=subset, keep=manter).reset_index(drop=True)
    if verbose:
        rem = n_antes - len(df)
        chave = f" por {list(subset)}" if subset else ""
        print(f"[duplicados] removidas {rem:,} linhas{chave} ({len(df):,} restantes)")
    return df


def remover_outliers(
    df: pd.DataFrame,
    colunas: Sequence[str] | None = None,
    metodo: Literal["iqr", "zscore", "quantil", "isolation_forest"] = "iqr",
    *,
    fator_iqr: float = 1.5,
    limite_z: float = 3.0,
    quantis: tuple[float, float] = (0.01, 0.99),
    contaminacao: float = 0.05,
    acao: Literal["remover", "limitar", "marcar"] = "remover",
    verbose: bool = True,
) -> pd.DataFrame:
    """Detecta e trata outliers em colunas numéricas.

    Métodos
    -------
    - "iqr"              : fora de [Q1 - k*IQR, Q3 + k*IQR] (robusto, padrão).
    - "zscore"           : |z| > limite_z (assume ~normalidade).
    - "quantil"          : fora dos percentis definidos em `quantis`.
    - "isolation_forest" : multivariado (sklearn) — considera combinações
                           de variáveis, não coluna a coluna.

    Ações
    -----
    - "remover" : exclui as linhas outlier.
    - "limitar" : winsoriza — trunca nos limites (clip). Preserva o nº de linhas.
    - "marcar"  : só adiciona coluna booleana `_outlier` (você decide depois).

    Examples
    --------
    >>> df = remover_outliers(df, ["preco", "area"], metodo="iqr", acao="limitar")
    >>> df = remover_outliers(df, metodo="isolation_forest", acao="marcar")
    """
    df = df.copy()
    if colunas is None:
        colunas = df.select_dtypes(include=np.number).columns.tolist()
    colunas = [c for c in colunas if pd.api.types.is_numeric_dtype(df[c])]
    n_antes = len(df)

    if metodo == "isolation_forest":
        from sklearn.ensemble import IsolationForest
        sub = df[colunas].dropna()
        modelo = IsolationForest(contamination=contaminacao, random_state=42)
        pred = pd.Series(modelo.fit_predict(sub), index=sub.index)
        mascara_outlier = pd.Series(False, index=df.index)
        mascara_outlier.loc[pred[pred == -1].index] = True
    else:
        mascara_outlier = pd.Series(False, index=df.index)
        limites: dict[str, tuple[float, float]] = {}
        for c in colunas:
            s = df[c]
            if metodo == "iqr":
                q1, q3 = s.quantile(0.25), s.quantile(0.75)
                iqr = q3 - q1
                lo, hi = q1 - fator_iqr * iqr, q3 + fator_iqr * iqr
            elif metodo == "zscore":
                mu, sd = s.mean(), s.std()
                lo, hi = mu - limite_z * sd, mu + limite_z * sd
            else:  # quantil
                lo, hi = s.quantile(quantis[0]), s.quantile(quantis[1])
            limites[c] = (lo, hi)
            mascara_outlier |= (s < lo) | (s > hi)

        if acao == "limitar":
            for c, (lo, hi) in limites.items():
                df[c] = df[c].clip(lo, hi)
            if verbose:
                print(f"[outliers] '{metodo}': valores truncados nos limites "
                      f"({int(mascara_outlier.sum()):,} linhas afetadas)")
            return df

    if acao == "marcar":
        df["_outlier"] = mascara_outlier
        if verbose:
            print(f"[outliers] '{metodo}': {int(mascara_outlier.sum()):,} linhas "
                  f"marcadas em '_outlier'")
        return df

    df = df[~mascara_outlier].reset_index(drop=True)
    if verbose:
        print(f"[outliers] '{metodo}': removidas {n_antes - len(df):,} de "
              f"{n_antes:,} linhas ({100*(n_antes-len(df))/max(n_antes,1):.1f}%)")
    return df


def otimizar_memoria(df: pd.DataFrame, *, categorizar_objetos: bool = True,
                     limite_categoria: float = 0.5, verbose: bool = True) -> pd.DataFrame:
    """Reduz o uso de memória do DataFrame com downcast de tipos.

    - int64 -> menor int possível (int8/16/32);
    - float64 -> float32 (atenção: perde precisão além de ~7 dígitos);
    - object -> category quando nº únicos / nº linhas < `limite_categoria`.

    Essencial ao trabalhar com datasets grandes no Colab (RAM limitada).

    Examples
    --------
    >>> df = otimizar_memoria(df)
    """
    df = df.copy()
    mem_antes = df.memory_usage(deep=True).sum() / 1024**2
    for col in df.columns:
        dt = df[col].dtype
        if pd.api.types.is_integer_dtype(dt):
            df[col] = pd.to_numeric(df[col], downcast="integer")
        elif pd.api.types.is_float_dtype(dt):
            df[col] = pd.to_numeric(df[col], downcast="float")
        elif dt == object and categorizar_objetos:
            n = len(df[col])
            if n and df[col].nunique(dropna=True) / n < limite_categoria:
                df[col] = df[col].astype("category")
    mem_depois = df.memory_usage(deep=True).sum() / 1024**2
    if verbose:
        red = 100 * (1 - mem_depois / max(mem_antes, 1e-9))
        print(f"[memoria] {mem_antes:.2f} MB -> {mem_depois:.2f} MB (-{red:.1f}%)")
    return df


def codificar_categoricas(
    df: pd.DataFrame,
    colunas: Sequence[str] | None = None,
    metodo: Literal["onehot", "label", "ordinal", "frequencia", "target"] = "onehot",
    *,
    ordem: dict[str, list] | None = None,
    alvo: str | None = None,
    max_categorias_onehot: int = 20,
    drop_first: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Codifica variáveis categóricas para uso em modelos.

    Métodos
    -------
    - "onehot"     : dummies 0/1. Pula (com aviso) colunas com mais de
                     `max_categorias_onehot` categorias — evita explosão dimensional.
    - "label"      : inteiro arbitrário por categoria (ok p/ modelos de árvore).
    - "ordinal"    : inteiro respeitando ordem fornecida em `ordem`
                     (ex.: {"tamanho": ["P", "M", "G"]}).
    - "frequencia" : substitui pela frequência relativa da categoria.
    - "target"     : média do alvo por categoria (target encoding).
                     ATENÇÃO: aplique só no treino ou com validação cruzada
                     para não vazar informação (data leakage).

    Examples
    --------
    >>> df = codificar_categoricas(df, ["bairro"], metodo="frequencia")
    >>> df = codificar_categoricas(df, ["padrao"], metodo="ordinal",
    ...                            ordem={"padrao": ["baixo", "medio", "alto"]})
    """
    df = df.copy()
    if colunas is None:
        colunas = df.select_dtypes(include=["object", "category"]).columns.tolist()

    if metodo == "onehot":
        aplicaveis, puladas = [], []
        for c in colunas:
            (aplicaveis if df[c].nunique() <= max_categorias_onehot else puladas).append(c)
        if puladas and verbose:
            print(f"[encoding] puladas (alta cardinalidade p/ onehot): {puladas} "
                  f"— considere 'frequencia' ou 'target'")
        df = pd.get_dummies(df, columns=aplicaveis, drop_first=drop_first, dtype=int)
    elif metodo == "label":
        for c in colunas:
            df[c] = df[c].astype("category").cat.codes.replace(-1, np.nan)
    elif metodo == "ordinal":
        if not ordem:
            raise ValueError("Forneça `ordem={coluna: [cat1, cat2, ...]}` para ordinal.")
        for c, cats in ordem.items():
            mapa = {cat: i for i, cat in enumerate(cats)}
            df[c] = df[c].map(mapa)
    elif metodo == "frequencia":
        for c in colunas:
            df[c] = df[c].map(df[c].value_counts(normalize=True))
    elif metodo == "target":
        if not alvo:
            raise ValueError("Forneça `alvo=` para target encoding.")
        for c in colunas:
            df[c] = df[c].map(df.groupby(c, observed=True)[alvo].mean())

    if verbose:
        print(f"[encoding] '{metodo}' aplicado -> {df.shape[1]} colunas")
    return df


def escalar(
    df: pd.DataFrame,
    colunas: Sequence[str] | None = None,
    metodo: Literal["standard", "minmax", "robust", "log", "log1p"] = "standard",
    *,
    verbose: bool = True,
) -> tuple[pd.DataFrame, Any]:
    """Escala/transforma colunas numéricas e retorna também o scaler ajustado.

    Métodos
    -------
    - "standard" : (x - média) / desvio — padrão p/ modelos lineares, SVM, KNN.
    - "minmax"   : [0, 1] — bom p/ redes neurais.
    - "robust"   : usa mediana e IQR — resistente a outliers.
    - "log"      : log natural (exige valores > 0).
    - "log1p"    : log(1 + x) — aceita zeros; ótimo p/ variáveis assimétricas
                   como preço e renda.

    Returns
    -------
    (DataFrame escalado, scaler ajustado ou None)
        Guarde o scaler para aplicar `.transform()` em dados novos/teste —
        NUNCA ajuste o scaler no conjunto de teste (data leakage).

    Examples
    --------
    >>> df_tr, scaler = escalar(df_treino, ["preco", "area"], "robust")
    >>> df_te[["preco", "area"]] = scaler.transform(df_teste[["preco", "area"]])
    """
    from sklearn.preprocessing import StandardScaler, MinMaxScaler, RobustScaler
    df = df.copy()
    if colunas is None:
        colunas = df.select_dtypes(include=np.number).columns.tolist()
    colunas = list(colunas)

    scaler = None
    if metodo in ("log", "log1p"):
        for c in colunas:
            if metodo == "log":
                if (df[c] <= 0).any():
                    warnings.warn(f"'{c}' tem valores <= 0; usando log1p nessa coluna.")
                    df[c] = np.log1p(df[c].clip(lower=0))
                else:
                    df[c] = np.log(df[c])
            else:
                df[c] = np.log1p(df[c].clip(lower=0))
    else:
        scaler = {"standard": StandardScaler(),
                  "minmax": MinMaxScaler(),
                  "robust": RobustScaler()}[metodo]
        df[colunas] = scaler.fit_transform(df[colunas])

    if verbose:
        print(f"[escala] '{metodo}' aplicado em {len(colunas)} colunas")
    return df, scaler


def criar_features_data(
    df: pd.DataFrame,
    coluna: str,
    *,
    componentes: Sequence[str] = ("ano", "mes", "dia", "dia_semana",
                                  "fim_de_semana", "trimestre"),
    ciclicas: bool = False,
    prefixo: str | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Extrai features de uma coluna datetime (feature engineering temporal).

    Componentes disponíveis: "ano", "mes", "dia", "dia_semana" (0=segunda),
    "dia_ano", "semana", "trimestre", "hora", "minuto", "fim_de_semana",
    "inicio_mes", "fim_mes".

    Parameters
    ----------
    ciclicas : bool
        Se True, adiciona codificação seno/cosseno para mês, dia da semana e
        hora — preserva a circularidade (dezembro é vizinho de janeiro),
        importante para modelos lineares e redes neurais.
    prefixo : str, optional
        Prefixo das novas colunas (padrão: nome da coluna original).

    Examples
    --------
    >>> df = criar_features_data(df, "data_anuncio", ciclicas=True)
    """
    df = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(df[coluna]):
        df[coluna] = pd.to_datetime(df[coluna], dayfirst=True, errors="coerce")
    p = prefixo or coluna
    dt = df[coluna].dt

    mapa = {
        "ano": dt.year, "mes": dt.month, "dia": dt.day,
        "dia_semana": dt.dayofweek, "dia_ano": dt.dayofyear,
        "semana": dt.isocalendar().week.astype("Int64"),
        "trimestre": dt.quarter, "hora": dt.hour, "minuto": dt.minute,
        "fim_de_semana": (dt.dayofweek >= 5).astype("Int64"),
        "inicio_mes": dt.is_month_start.astype("Int64"),
        "fim_mes": dt.is_month_end.astype("Int64"),
    }
    for comp in componentes:
        if comp in mapa:
            df[f"{p}_{comp}"] = mapa[comp]

    if ciclicas:
        for comp, periodo in (("mes", 12), ("dia_semana", 7), ("hora", 24)):
            serie = mapa[comp]
            df[f"{p}_{comp}_sen"] = np.sin(2 * np.pi * serie / periodo)
            df[f"{p}_{comp}_cos"] = np.cos(2 * np.pi * serie / periodo)

    if verbose:
        novas = [c for c in df.columns if c.startswith(p + "_")]
        print(f"[features_data] criadas {len(novas)} colunas a partir de '{coluna}'")
    return df


# ==============================================================================
# 3. EDA — ANÁLISE EXPLORATÓRIA E TESTES ESTATÍSTICOS
# ==============================================================================

def resumo_geral(df: pd.DataFrame, *, percentis: Sequence[float] = (0.05, 0.25,
                 0.5, 0.75, 0.95), verbose: bool = True) -> pd.DataFrame:
    """Describe turbinado: estatísticas + assimetria, curtose, CV e nulos.

    Para colunas numéricas adiciona:
    - `assimetria` (skew): |v| > 1 sugere transformação log;
    - `curtose`: caudas pesadas (>3 = mais outliers que a normal);
    - `cv` (coef. de variação = desvio/média): compara dispersão entre
      variáveis de escalas diferentes.

    Examples
    --------
    >>> resumo_geral(df)
    """
    num = df.select_dtypes(include=np.number)
    if num.empty:
        if verbose:
            print("[resumo] sem colunas numéricas — use relatorio_qualidade().")
        return df.describe(include="all").T

    desc = num.describe(percentiles=list(percentis)).T
    desc["assimetria"] = num.skew()
    desc["curtose"] = num.kurtosis()
    with np.errstate(divide="ignore", invalid="ignore"):
        desc["cv"] = (num.std() / num.mean()).replace([np.inf, -np.inf], np.nan)
    desc["nulos"] = num.isna().sum()
    desc["%_nulos"] = (100 * num.isna().mean()).round(2)

    if verbose:
        assimetricas = desc[desc["assimetria"].abs() > 1].index.tolist()
        if assimetricas:
            print(f"[resumo] colunas muito assimétricas (candidatas a log): "
                  f"{assimetricas}")
    return desc.round(4)


def analise_correlacao(
    df: pd.DataFrame,
    metodo: Literal["pearson", "spearman", "kendall"] = "pearson",
    *,
    limite_forte: float = 0.7,
    alvo: str | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Matriz de correlação + relatório dos pares mais correlacionados.

    Dicas de escolha do método:
    - "pearson"  : relação LINEAR, sensível a outliers;
    - "spearman" : relação MONOTÔNICA (por postos), robusta a outliers —
                   preferível quando há assimetria forte;
    - "kendall"  : similar ao spearman, melhor p/ amostras pequenas/empates.

    Parameters
    ----------
    limite_forte : float
        |corr| acima disso entra no relatório de pares fortes — atenção a
        multicolinearidade em modelos lineares.
    alvo : str, optional
        Se fornecido, imprime ranking de correlação de todas as variáveis
        com o alvo.

    Returns
    -------
    Matriz de correlação (DataFrame).

    Examples
    --------
    >>> corr = analise_correlacao(df, "spearman", alvo="preco")
    """
    num = df.select_dtypes(include=np.number)
    corr = num.corr(method=metodo)

    if verbose:
        pares = (corr.where(np.triu(np.ones_like(corr, dtype=bool), k=1))
                     .stack().sort_values(key=lambda s: s.abs(), ascending=False))
        fortes = pares[pares.abs() >= limite_forte]
        if not fortes.empty:
            print(f"[correlacao:{metodo}] pares com |r| >= {limite_forte}:")
            for (a, b), v in fortes.items():
                print(f"    {a} x {b}: {v:+.3f}")
        if alvo and alvo in corr.columns:
            rank = corr[alvo].drop(alvo).sort_values(key=lambda s: s.abs(),
                                                     ascending=False)
            print(f"[correlacao:{metodo}] ranking vs '{alvo}':")
            print(rank.round(3).to_string())
    return corr


def testar_normalidade(
    serie: pd.Series | np.ndarray,
    *,
    alpha: float = 0.05,
    verbose: bool = True,
) -> dict[str, Any]:
    """Testa normalidade com Shapiro-Wilk, D'Agostino e Kolmogorov-Smirnov.

    Regras práticas aplicadas automaticamente:
    - Shapiro-Wilk é o mais poderoso, mas limitado a n <= 5000 (acima disso
      usa-se amostra aleatória de 5000);
    - Com n muito grande, QUALQUER desvio mínimo rejeita H0 — considere
      também skew/curtose e o histograma antes de decidir.

    Returns
    -------
    dict com estatísticas, p-valores e o veredito `normal` (bool) por maioria.

    Examples
    --------
    >>> res = testar_normalidade(df["preco"])
    >>> if not res["normal"]: ...  # use testes não-paramétricos
    """
    x = pd.Series(serie).dropna().astype(float)
    n = len(x)
    if n < 8:
        raise ValueError(f"Amostra muito pequena para testes de normalidade (n={n}).")

    amostra_sw = x.sample(5000, random_state=42) if n > 5000 else x
    sw_stat, sw_p = stats.shapiro(amostra_sw)
    da_stat, da_p = stats.normaltest(x)
    ks_stat, ks_p = stats.kstest((x - x.mean()) / x.std(ddof=1), "norm")

    votos_normal = sum(p > alpha for p in (sw_p, da_p, ks_p))
    resultado = {
        "n": n,
        "shapiro": {"estatistica": sw_stat, "p_valor": sw_p},
        "dagostino": {"estatistica": da_stat, "p_valor": da_p},
        "ks": {"estatistica": ks_stat, "p_valor": ks_p},
        "assimetria": float(x.skew()),
        "curtose": float(x.kurtosis()),
        "normal": votos_normal >= 2,
    }
    if verbose:
        v = "NORMAL" if resultado["normal"] else "NÃO normal"
        print(f"[normalidade] n={n} | shapiro p={sw_p:.4f} | "
              f"dagostino p={da_p:.4f} | ks p={ks_p:.4f} -> {v} "
              f"(skew={resultado['assimetria']:.2f})")
        if n > 5000:
            print("[normalidade] aviso: n grande — p-valores rejeitam com facilidade; "
                  "confie também no histograma/QQ-plot.")
    return resultado


def comparar_grupos(
    df: pd.DataFrame,
    coluna_numerica: str,
    coluna_grupo: str,
    *,
    alpha: float = 0.05,
    forcar_teste: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Compara uma variável numérica entre grupos escolhendo o teste correto.

    Fluxo de decisão automático:
    - 2 grupos, ambos ~normais  -> t de Welch (não assume variâncias iguais);
    - 2 grupos, não normais     -> Mann-Whitney U;
    - 3+ grupos, todos ~normais -> ANOVA one-way;
    - 3+ grupos, não normais    -> Kruskal-Wallis.

    Também reporta tamanho de efeito (Cohen's d para 2 grupos; eta² p/ 3+),
    porque p-valor pequeno não significa efeito relevante.

    Parameters
    ----------
    forcar_teste : {"t", "mannwhitney", "anova", "kruskal"}, optional
        Ignora a decisão automática e usa o teste indicado.

    Returns
    -------
    dict: teste usado, estatística, p-valor, tamanho de efeito, medianas/médias.

    Examples
    --------
    >>> comparar_grupos(df, "preco_m2", "cluster")
    """
    dados = df[[coluna_numerica, coluna_grupo]].dropna()
    grupos = [g[coluna_numerica].values for _, g in dados.groupby(coluna_grupo, observed=True)]
    nomes = [str(k) for k, _ in dados.groupby(coluna_grupo, observed=True)]
    k = len(grupos)
    if k < 2:
        raise ValueError("São necessários pelo menos 2 grupos.")

    def _normalzinho(g: np.ndarray) -> bool:
        if len(g) < 8:
            return False
        amostra = pd.Series(g).sample(min(len(g), 5000), random_state=42)
        return stats.shapiro(amostra)[1] > alpha

    normais = all(_normalzinho(g) for g in grupos)

    if forcar_teste:
        escolha = forcar_teste
    elif k == 2:
        escolha = "t" if normais else "mannwhitney"
    else:
        escolha = "anova" if normais else "kruskal"

    efeito = None
    if escolha == "t":
        stat, p = stats.ttest_ind(grupos[0], grupos[1], equal_var=False)
        s_pool = np.sqrt((np.var(grupos[0], ddof=1) + np.var(grupos[1], ddof=1)) / 2)
        efeito = ("cohen_d", float((np.mean(grupos[0]) - np.mean(grupos[1])) /
                                   s_pool) if s_pool else np.nan)
    elif escolha == "mannwhitney":
        stat, p = stats.mannwhitneyu(grupos[0], grupos[1], alternative="two-sided")
        n1, n2 = len(grupos[0]), len(grupos[1])
        efeito = ("rank_biserial", float(1 - 2 * stat / (n1 * n2)))
    elif escolha == "anova":
        stat, p = stats.f_oneway(*grupos)
        todos = np.concatenate(grupos)
        ss_entre = sum(len(g) * (np.mean(g) - todos.mean()) ** 2 for g in grupos)
        ss_total = float(((todos - todos.mean()) ** 2).sum())
        efeito = ("eta2", ss_entre / ss_total if ss_total else np.nan)
    else:  # kruskal
        stat, p = stats.kruskal(*grupos)
        n_tot = sum(map(len, grupos))
        efeito = ("epsilon2", float((stat - k + 1) / (n_tot - k)) if n_tot > k else np.nan)

    resultado = {
        "teste": escolha, "estatistica": float(stat), "p_valor": float(p),
        "significativo": p < alpha, "tamanho_efeito": efeito,
        "grupos": {nome: {"n": len(g), "media": float(np.mean(g)),
                          "mediana": float(np.median(g))}
                   for nome, g in zip(nomes, grupos)},
    }
    if verbose:
        sig = "SIGNIFICATIVO" if p < alpha else "não significativo"
        print(f"[grupos] teste={escolha} | p={p:.4g} ({sig}) | "
              f"{efeito[0]}={efeito[1]:.3f}")
        for nome, info in resultado["grupos"].items():
            print(f"    {nome}: n={info['n']:,} media={info['media']:.3f} "
                  f"mediana={info['mediana']:.3f}")
    return resultado


def analise_categorica(
    df: pd.DataFrame,
    coluna_a: str,
    coluna_b: str,
    *,
    alpha: float = 0.05,
    verbose: bool = True,
) -> dict[str, Any]:
    """Associação entre duas variáveis categóricas: qui-quadrado + Cramér's V.

    Valida a suposição do qui-quadrado (frequências esperadas >= 5 em pelo
    menos 80% das células); se violada em tabela 2x2, usa Fisher exato.

    Cramér's V interpreta a força: ~0.1 fraca, ~0.3 média, ~0.5 forte.

    Returns
    -------
    dict com tabela de contingência, teste, p-valor e Cramér's V.

    Examples
    --------
    >>> analise_categorica(df, "bairro_cluster", "tipo_imovel")
    """
    tabela = pd.crosstab(df[coluna_a], df[coluna_b])
    chi2, p, dof, esperado = stats.chi2_contingency(tabela)
    frac_ok = (esperado >= 5).mean()

    teste = "qui_quadrado"
    if frac_ok < 0.8 and tabela.shape == (2, 2):
        _, p = stats.fisher_exact(tabela)
        teste = "fisher_exato"
    elif frac_ok < 0.8 and verbose:
        print(f"[categorica] aviso: {100*(1-frac_ok):.0f}% das células com "
              f"esperado < 5 — resultado do qui² pode não ser confiável; "
              f"considere agrupar categorias raras.")

    n = tabela.values.sum()
    v_cramer = float(np.sqrt(chi2 / (n * (min(tabela.shape) - 1)))) if n else np.nan

    resultado = {"tabela": tabela, "teste": teste, "chi2": float(chi2),
                 "p_valor": float(p), "significativo": p < alpha,
                 "cramers_v": v_cramer, "graus_liberdade": int(dof)}
    if verbose:
        sig = "SIGNIFICATIVO" if p < alpha else "não significativo"
        print(f"[categorica] {teste} p={p:.4g} ({sig}) | Cramér's V={v_cramer:.3f}")
    return resultado


def analise_univariada(df: pd.DataFrame, coluna: str, *,
                       top_n: int = 10, verbose: bool = True) -> dict[str, Any]:
    """Perfil completo de uma única coluna, numérica ou categórica.

    Numérica: estatísticas, quartis, outliers via IQR, teste de normalidade.
    Categórica: contagens, proporções, cardinalidade, categorias raras (<1%).

    Examples
    --------
    >>> analise_univariada(df, "preco")
    >>> analise_univariada(df, "bairro")
    """
    s = df[coluna]
    resultado: dict[str, Any] = {"coluna": coluna, "dtype": str(s.dtype),
                                 "n": len(s), "nulos": int(s.isna().sum())}
    if pd.api.types.is_numeric_dtype(s):
        x = s.dropna()
        q1, q3 = x.quantile([0.25, 0.75])
        iqr = q3 - q1
        n_out = int(((x < q1 - 1.5 * iqr) | (x > q3 + 1.5 * iqr)).sum())
        resultado.update({
            "tipo": "numerica",
            "media": float(x.mean()), "mediana": float(x.median()),
            "desvio": float(x.std()), "min": float(x.min()), "max": float(x.max()),
            "assimetria": float(x.skew()), "outliers_iqr": n_out,
        })
        if len(x) >= 8:
            resultado["normal"] = testar_normalidade(x, verbose=False)["normal"]
        if verbose:
            print(f"[univariada] '{coluna}' (numérica): media={x.mean():.3f} "
                  f"mediana={x.median():.3f} std={x.std():.3f} | "
                  f"{n_out} outliers IQR | skew={x.skew():.2f}")
    else:
        contagem = s.value_counts(dropna=True)
        prop = s.value_counts(normalize=True, dropna=True)
        raras = prop[prop < 0.01].index.tolist()
        resultado.update({
            "tipo": "categorica", "cardinalidade": int(s.nunique()),
            "top": contagem.head(top_n).to_dict(),
            "categorias_raras": raras,
        })
        if verbose:
            print(f"[univariada] '{coluna}' (categórica): {s.nunique()} categorias "
                  f"| {len(raras)} raras (<1%)")
            print(contagem.head(top_n).to_string())
    return resultado


# ==============================================================================
# 4. GRÁFICOS
# ==============================================================================

_PALETA_PADRAO = "viridis"


def _checar_plot() -> None:
    if not _TEM_PLOT:
        raise ImportError("Instale matplotlib e seaborn: pip install matplotlib seaborn")


def configurar_estilo(
    tema: Literal["claro", "escuro"] = "escuro",
    *,
    paleta: str = _PALETA_PADRAO,
    tamanho_figura: tuple[float, float] = (10, 6),
    tamanho_fonte: int = 11,
    dpi: int = 100,
) -> None:
    """Configura o estilo global de todos os plots do módulo (e do notebook).

    Chame uma vez no início do notebook. O tema "escuro" combina com o
    dark mode do VSCode/Colab e usa fundo transparente-friendly.

    Examples
    --------
    >>> configurar_estilo("escuro")
    >>> configurar_estilo("claro", paleta="magma", dpi=150)  # p/ relatórios
    """
    _checar_plot()
    if tema == "escuro":
        plt.style.use("dark_background")
        cor_grid = "#3a3a3a"
    else:
        plt.style.use("default")
        sns.set_style("whitegrid")
        cor_grid = "#dddddd"
    sns.set_palette(paleta)
    plt.rcParams.update({
        "figure.figsize": tamanho_figura,
        "figure.dpi": dpi,
        "font.size": tamanho_fonte,
        "axes.titlesize": tamanho_fonte + 2,
        "axes.titleweight": "bold",
        "axes.grid": True,
        "grid.color": cor_grid,
        "grid.alpha": 0.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def plot_distribuicao(
    df: pd.DataFrame,
    coluna: str,
    *,
    bins: int | str = "auto",
    kde: bool = True,
    com_boxplot: bool = True,
    log_x: bool = False,
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Histograma + KDE + boxplot alinhados — visão completa de uma numérica.

    Anota média (linha tracejada) e mediana (linha cheia); a distância entre
    elas denuncia assimetria.

    Parameters
    ----------
    log_x : bool
        Escala log no eixo x — indispensável p/ preço, renda, população.
    salvar_em : str, optional
        Caminho para salvar a figura (ex.: "figs/dist_preco.png").

    Returns
    -------
    (fig, axes)

    Examples
    --------
    >>> fig, ax = plot_distribuicao(df, "preco", log_x=True)
    """
    _checar_plot()
    x = df[coluna].dropna()
    if com_boxplot:
        fig, (ax_box, ax_hist) = plt.subplots(
            2, 1, sharex=True, gridspec_kw={"height_ratios": (1, 4)},
            figsize=plt.rcParams["figure.figsize"])
        sns.boxplot(x=x, ax=ax_box, fliersize=3)
        ax_box.set(xlabel="")
        axes = (ax_box, ax_hist)
    else:
        fig, ax_hist = plt.subplots()
        axes = (ax_hist,)

    sns.histplot(x, bins=bins, kde=kde, ax=ax_hist)
    ax_hist.axvline(x.mean(), ls="--", lw=1.5, color="tomato",
                    label=f"média = {x.mean():,.2f}")
    ax_hist.axvline(x.median(), ls="-", lw=1.5, color="gold",
                    label=f"mediana = {x.median():,.2f}")
    if log_x:
        ax_hist.set_xscale("log")
    ax_hist.legend()
    ax_hist.set_xlabel(coluna)
    fig.suptitle(titulo or f"Distribuição de {coluna}")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, axes


def plot_correlacao(
    df: pd.DataFrame,
    *,
    metodo: Literal["pearson", "spearman", "kendall"] = "pearson",
    anotar: bool = True,
    mascara_superior: bool = True,
    cmap: str = "coolwarm",
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Heatmap de correlação com máscara triangular (sem redundância visual).

    Examples
    --------
    >>> plot_correlacao(df, metodo="spearman")
    """
    _checar_plot()
    corr = df.select_dtypes(include=np.number).corr(method=metodo)
    mask = np.triu(np.ones_like(corr, dtype=bool)) if mascara_superior else None
    lado = max(6, 0.6 * len(corr))
    fig, ax = plt.subplots(figsize=(lado, lado * 0.8))
    sns.heatmap(corr, mask=mask, annot=anotar, fmt=".2f", cmap=cmap,
                vmin=-1, vmax=1, center=0, square=True,
                linewidths=0.5, cbar_kws={"shrink": 0.8}, ax=ax)
    ax.set_title(titulo or f"Correlação ({metodo})")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_categorico(
    df: pd.DataFrame,
    coluna: str,
    *,
    top_n: int = 15,
    horizontal: bool = True,
    mostrar_pct: bool = True,
    ordenar: bool = True,
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Gráfico de barras de contagem com % anotada e limite de categorias.

    Categorias além de `top_n` são agrupadas em "(outras)" — evita o gráfico
    ilegível de 80 bairros.

    Examples
    --------
    >>> plot_categorico(df, "bairro", top_n=12)
    """
    _checar_plot()
    contagem = df[coluna].value_counts()
    if ordenar:
        contagem = contagem.sort_values(ascending=False)
    if len(contagem) > top_n:
        outras = contagem.iloc[top_n:].sum()
        contagem = contagem.iloc[:top_n]
        contagem["(outras)"] = outras
    total = contagem.sum()

    fig, ax = plt.subplots()
    if horizontal:
        contagem[::-1].plot.barh(ax=ax)
        for i, v in enumerate(contagem[::-1]):
            rotulo = f"{v:,}" + (f" ({100*v/total:.1f}%)" if mostrar_pct else "")
            ax.text(v, i, " " + rotulo, va="center", fontsize=9)
        ax.set_xlabel("contagem")
    else:
        contagem.plot.bar(ax=ax)
        for i, v in enumerate(contagem):
            rotulo = f"{v:,}" + (f"\n{100*v/total:.1f}%" if mostrar_pct else "")
            ax.text(i, v, rotulo, ha="center", va="bottom", fontsize=9)
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    ax.set_title(titulo or f"Contagem por {coluna}")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_dispersao(
    df: pd.DataFrame,
    x: str,
    y: str,
    *,
    hue: str | None = None,
    tamanho: str | None = None,
    linha_tendencia: bool = True,
    log_x: bool = False,
    log_y: bool = False,
    alpha: float = 0.6,
    amostra_max: int = 10_000,
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Dispersão com linha de tendência e correlação anotada no título.

    Amostra automaticamente quando há mais de `amostra_max` pontos —
    scatter com 500k pontos trava o notebook e vira uma mancha.

    Examples
    --------
    >>> plot_dispersao(df, "area", "preco", hue="cluster", log_y=True)
    """
    _checar_plot()
    dados = df.dropna(subset=[c for c in (x, y, hue, tamanho) if c])
    if len(dados) > amostra_max:
        dados = dados.sample(amostra_max, random_state=42)

    fig, ax = plt.subplots()
    sns.scatterplot(data=dados, x=x, y=y, hue=hue, size=tamanho,
                    alpha=alpha, ax=ax)
    if linha_tendencia:
        sns.regplot(data=dados, x=x, y=y, scatter=False, ax=ax,
                    line_kws={"color": "tomato", "lw": 2})
    if log_x:
        ax.set_xscale("log")
    if log_y:
        ax.set_yscale("log")
    r = dados[[x, y]].corr().iloc[0, 1]
    ax.set_title(titulo or f"{y} vs {x}  (r = {r:.2f})")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_boxplots_grupo(
    df: pd.DataFrame,
    coluna_numerica: str,
    coluna_grupo: str,
    *,
    top_n: int = 12,
    ordenar_por_mediana: bool = True,
    mostrar_pontos: bool = False,
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Boxplots de uma numérica por grupo, ordenados pela mediana.

    Limita aos `top_n` grupos mais frequentes (os demais poluem o gráfico).
    `mostrar_pontos=True` sobrepõe stripplot — bom p/ grupos pequenos.

    Examples
    --------
    >>> plot_boxplots_grupo(df, "preco_m2", "bairro", top_n=10)
    """
    _checar_plot()
    principais = df[coluna_grupo].value_counts().head(top_n).index
    dados = df[df[coluna_grupo].isin(principais)].dropna(
        subset=[coluna_numerica, coluna_grupo])
    ordem = (dados.groupby(coluna_grupo, observed=True)[coluna_numerica]
             .median().sort_values(ascending=False).index
             if ordenar_por_mediana else None)

    fig, ax = plt.subplots()
    sns.boxplot(data=dados, x=coluna_grupo, y=coluna_numerica, order=ordem,
                fliersize=2, ax=ax)
    if mostrar_pontos:
        sns.stripplot(data=dados, x=coluna_grupo, y=coluna_numerica,
                      order=ordem, color="white", alpha=0.3, size=2, ax=ax)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    ax.set_title(titulo or f"{coluna_numerica} por {coluna_grupo}")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_serie_temporal(
    df: pd.DataFrame,
    coluna_data: str,
    coluna_valor: str,
    *,
    frequencia: str | None = None,
    agregacao: str = "mean",
    media_movel: int | None = None,
    titulo: str | None = None,
    salvar_em: str | None = None,
):
    """Linha temporal com reamostragem e média móvel opcionais.

    Parameters
    ----------
    frequencia : str, optional
        Regra de resample do pandas: "D", "W", "ME" (mês), "QE", "YE".
    agregacao : str
        Função de agregação no resample: "mean", "sum", "median", "count"...
    media_movel : int, optional
        Janela da média móvel sobreposta (suaviza ruído).

    Examples
    --------
    >>> plot_serie_temporal(df, "data", "preco", frequencia="ME",
    ...                     agregacao="median", media_movel=3)
    """
    _checar_plot()
    dados = df[[coluna_data, coluna_valor]].dropna().copy()
    if not pd.api.types.is_datetime64_any_dtype(dados[coluna_data]):
        dados[coluna_data] = pd.to_datetime(dados[coluna_data],
                                            dayfirst=True, errors="coerce")
    dados = dados.dropna().set_index(coluna_data).sort_index()
    serie = (dados[coluna_valor].resample(frequencia).agg(agregacao)
             if frequencia else dados[coluna_valor])

    fig, ax = plt.subplots()
    ax.plot(serie.index, serie.values, lw=1.5, label=coluna_valor)
    if media_movel:
        mm = serie.rolling(media_movel, min_periods=1).mean()
        ax.plot(mm.index, mm.values, lw=2.5, color="tomato",
                label=f"média móvel ({media_movel})")
    ax.legend()
    ax.set_xlabel("")
    ax.set_title(titulo or f"{coluna_valor} ao longo do tempo")
    fig.autofmt_xdate()
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_nulos(df: pd.DataFrame, *, apenas_com_nulos: bool = True,
               titulo: str | None = None, salvar_em: str | None = None):
    """Barra horizontal com % de nulos por coluna (visão rápida de faltantes).

    Examples
    --------
    >>> plot_nulos(df)
    """
    _checar_plot()
    pct = (100 * df.isna().mean()).sort_values(ascending=True)
    if apenas_com_nulos:
        pct = pct[pct > 0]
    if pct.empty:
        print("[plot_nulos] nenhum nulo no DataFrame.")
        return None, None
    fig, ax = plt.subplots(figsize=(9, max(3, 0.35 * len(pct))))
    pct.plot.barh(ax=ax)
    for i, v in enumerate(pct):
        ax.text(v, i, f" {v:.1f}%", va="center", fontsize=9)
    ax.set_xlabel("% de nulos")
    ax.set_title(titulo or "Valores nulos por coluna")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


# ==============================================================================
# 5. ML — MACHINE LEARNING
# ==============================================================================

def preparar_dados(
    df: pd.DataFrame,
    alvo: str,
    *,
    colunas_excluir: Sequence[str] = (),
    test_size: float = 0.2,
    estratificar: bool | None = None,
    random_state: int = 42,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Separa X/y e faz train/test split com estratificação inteligente.

    - Remove automaticamente do X colunas de ID óbvias e as em `colunas_excluir`;
    - `estratificar=None` decide sozinho: estratifica se o alvo for
      categórico/discreto com poucas classes (mantém proporção das classes —
      crucial em bases desbalanceadas).

    Returns
    -------
    (X_train, X_test, y_train, y_test)

    Examples
    --------
    >>> X_tr, X_te, y_tr, y_te = preparar_dados(df, "preco",
    ...     colunas_excluir=["url", "id_anuncio"])
    """
    from sklearn.model_selection import train_test_split
    df = df.copy()
    excluir = set(colunas_excluir) & set(df.columns)
    X = df.drop(columns=[alvo, *excluir])
    y = df[alvo]

    if estratificar is None:
        estratificar = (not pd.api.types.is_float_dtype(y)) and y.nunique() <= 20
    strat = y if estratificar else None

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=strat)
    if verbose:
        print(f"[preparar] treino={len(X_tr):,} teste={len(X_te):,} | "
              f"{X.shape[1]} features | estratificado={bool(estratificar)}")
        if excluir:
            print(f"[preparar] excluídas: {sorted(excluir)}")
    return X_tr, X_te, y_tr, y_te


def pipeline_preprocessamento(
    X: pd.DataFrame,
    *,
    imputacao_numerica: str = "median",
    imputacao_categorica: str = "most_frequent",
    escalar_numericas: bool = True,
    metodo_escala: Literal["standard", "minmax", "robust"] = "standard",
    max_categorias_onehot: int = 20,
) -> Any:
    """Monta um ColumnTransformer sklearn com imputação + escala + one-hot.

    Vantagem sobre pré-processar "na mão": encapsulado num Pipeline, o
    fit acontece SÓ no treino — elimina data leakage por construção, e o
    mesmo objeto serve para produção (FastAPI, por exemplo).

    Colunas categóricas com mais de `max_categorias_onehot` categorias são
    descartadas com aviso (trate-as antes com frequency/target encoding).

    Returns
    -------
    sklearn.compose.ColumnTransformer (não ajustado).

    Examples
    --------
    >>> from sklearn.pipeline import Pipeline
    >>> from sklearn.ensemble import RandomForestRegressor
    >>> pre = pipeline_preprocessamento(X_train)
    >>> modelo = Pipeline([("pre", pre), ("rf", RandomForestRegressor())])
    >>> modelo.fit(X_train, y_train)
    """
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import (StandardScaler, MinMaxScaler,
                                       RobustScaler, OneHotEncoder)

    num_cols = X.select_dtypes(include=np.number).columns.tolist()
    cat_cols = X.select_dtypes(include=["object", "category"]).columns.tolist()

    cat_ok = [c for c in cat_cols if X[c].nunique() <= max_categorias_onehot]
    descartadas = sorted(set(cat_cols) - set(cat_ok))
    if descartadas:
        warnings.warn(f"Colunas categóricas descartadas (alta cardinalidade): "
                      f"{descartadas}")

    passos_num: list = [("imputer", SimpleImputer(strategy=imputacao_numerica))]
    if escalar_numericas:
        passos_num.append(("scaler", {"standard": StandardScaler(),
                                      "minmax": MinMaxScaler(),
                                      "robust": RobustScaler()}[metodo_escala]))

    transformadores = []
    if num_cols:
        transformadores.append(("num", Pipeline(passos_num), num_cols))
    if cat_ok:
        transformadores.append(("cat", Pipeline([
            ("imputer", SimpleImputer(strategy=imputacao_categorica)),
            ("onehot", OneHotEncoder(handle_unknown="ignore",
                                     sparse_output=False)),
        ]), cat_ok))

    return ColumnTransformer(transformadores, remainder="drop",
                             verbose_feature_names_out=False)


def avaliar_classificacao(
    modelo: Any,
    X_test: pd.DataFrame | np.ndarray,
    y_test: pd.Series | np.ndarray,
    *,
    plotar: bool = True,
    salvar_em: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Avaliação completa de um classificador já treinado.

    Reporta accuracy, precision, recall, F1 (weighted), classification_report
    e, se o modelo tiver `predict_proba` e o problema for binário, ROC-AUC.
    Plota matriz de confusão (e curva ROC no caso binário).

    Lembrete: em bases desbalanceadas, olhe F1/recall por classe — accuracy
    engana (o "accuracy paradox").

    Examples
    --------
    >>> metricas = avaliar_classificacao(modelo, X_te, y_te)
    """
    from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                                 f1_score, classification_report,
                                 confusion_matrix, roc_auc_score, roc_curve)
    y_pred = modelo.predict(X_test)
    metricas: dict[str, Any] = {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "precision_w": float(precision_score(y_test, y_pred,
                                             average="weighted", zero_division=0)),
        "recall_w": float(recall_score(y_test, y_pred,
                                       average="weighted", zero_division=0)),
        "f1_w": float(f1_score(y_test, y_pred, average="weighted",
                               zero_division=0)),
    }

    binario = pd.Series(y_test).nunique() == 2
    y_prob = None
    if binario and hasattr(modelo, "predict_proba"):
        y_prob = modelo.predict_proba(X_test)[:, 1]
        metricas["roc_auc"] = float(roc_auc_score(y_test, y_prob))

    if verbose:
        print("[classificacao] " + " | ".join(f"{k}={v:.4f}"
                                              for k, v in metricas.items()))
        print(classification_report(y_test, y_pred, zero_division=0))

    if plotar and _TEM_PLOT:
        cm = confusion_matrix(y_test, y_pred)
        classes = sorted(pd.Series(y_test).unique())
        n_plots = 2 if y_prob is not None else 1
        fig, axes = plt.subplots(1, n_plots, figsize=(6 * n_plots, 5))
        axes = np.atleast_1d(axes)
        sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", cbar=False,
                    xticklabels=classes, yticklabels=classes, ax=axes[0])
        axes[0].set(xlabel="Previsto", ylabel="Real", title="Matriz de confusão")
        if y_prob is not None:
            fpr, tpr, _ = roc_curve(y_test, y_prob)
            axes[1].plot(fpr, tpr, lw=2,
                         label=f"AUC = {metricas['roc_auc']:.3f}")
            axes[1].plot([0, 1], [0, 1], ls="--", lw=1, color="gray")
            axes[1].set(xlabel="Taxa de falsos positivos",
                        ylabel="Taxa de verdadeiros positivos", title="Curva ROC")
            axes[1].legend()
        fig.tight_layout()
        if salvar_em:
            Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(salvar_em, bbox_inches="tight")
        metricas["fig"] = fig
    return metricas


def avaliar_regressao(
    modelo: Any,
    X_test: pd.DataFrame | np.ndarray,
    y_test: pd.Series | np.ndarray,
    *,
    plotar: bool = True,
    salvar_em: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Avaliação completa de um regressor já treinado.

    Métricas: RMSE, MAE, R², MAPE (ignora divisões por ~zero).
    Plots de diagnóstico: previsto vs real e resíduos vs previsto —
    padrão nos resíduos (funil, curva) indica heterocedasticidade ou
    relação não capturada.

    Examples
    --------
    >>> metricas = avaliar_regressao(modelo, X_te, y_te)
    """
    from sklearn.metrics import (mean_squared_error, mean_absolute_error,
                                 r2_score)
    y_test = np.asarray(y_test, dtype=float)
    y_pred = np.asarray(modelo.predict(X_test), dtype=float)
    residuos = y_test - y_pred

    mask = np.abs(y_test) > 1e-9
    mape = float(np.mean(np.abs(residuos[mask] / y_test[mask])) * 100) \
        if mask.any() else np.nan

    metricas = {
        "rmse": float(np.sqrt(mean_squared_error(y_test, y_pred))),
        "mae": float(mean_absolute_error(y_test, y_pred)),
        "r2": float(r2_score(y_test, y_pred)),
        "mape_%": mape,
    }
    if verbose:
        print("[regressao] " + " | ".join(f"{k}={v:.4f}"
                                          for k, v in metricas.items()))

    if plotar and _TEM_PLOT:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        ax1.scatter(y_test, y_pred, alpha=0.4, s=15)
        lim = [min(y_test.min(), y_pred.min()), max(y_test.max(), y_pred.max())]
        ax1.plot(lim, lim, ls="--", color="tomato", lw=2)
        ax1.set(xlabel="Real", ylabel="Previsto",
                title=f"Previsto vs Real (R² = {metricas['r2']:.3f})")
        ax2.scatter(y_pred, residuos, alpha=0.4, s=15)
        ax2.axhline(0, ls="--", color="tomato", lw=2)
        ax2.set(xlabel="Previsto", ylabel="Resíduo", title="Resíduos")
        fig.tight_layout()
        if salvar_em:
            Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(salvar_em, bbox_inches="tight")
        metricas["fig"] = fig
    return metricas


def comparar_modelos(
    X: pd.DataFrame | np.ndarray,
    y: pd.Series | np.ndarray,
    tipo: Literal["classificacao", "regressao"],
    *,
    modelos: dict[str, Any] | None = None,
    cv: int = 5,
    scoring: str | None = None,
    preprocessador: Any = None,
    random_state: int = 42,
    verbose: bool = True,
) -> pd.DataFrame:
    """Compara vários modelos baseline via validação cruzada.

    Sempre comece por aqui antes de otimizar hiperparâmetros: descobrir QUAL
    família de modelo funciona vale mais que ajustar fino o modelo errado.

    Parameters
    ----------
    modelos : dict, optional
        {"nome": estimador}. Se None, usa um conjunto padrão sensato
        (Dummy como piso, linear, árvore, floresta, gradient boosting, KNN).
    scoring : str, optional
        Métrica sklearn (padrão: "f1_weighted" ou "neg_root_mean_squared_error").
    preprocessador : ColumnTransformer, optional
        Se fornecido, cada modelo roda dentro de um Pipeline com ele —
        garante pré-processamento sem leakage dentro de cada fold.

    Returns
    -------
    DataFrame ordenado por desempenho com média, desvio e tempo de fit.

    Examples
    --------
    >>> pre = pipeline_preprocessamento(X)
    >>> ranking = comparar_modelos(X, y, "regressao", preprocessador=pre)
    """
    from sklearn.model_selection import cross_validate
    from sklearn.pipeline import Pipeline

    if modelos is None:
        if tipo == "classificacao":
            from sklearn.dummy import DummyClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.tree import DecisionTreeClassifier
            from sklearn.ensemble import (RandomForestClassifier,
                                          GradientBoostingClassifier)
            from sklearn.neighbors import KNeighborsClassifier
            modelos = {
                "Dummy (baseline)": DummyClassifier(strategy="most_frequent"),
                "Regressão Logística": LogisticRegression(max_iter=2000,
                                                          random_state=random_state),
                "Árvore de Decisão": DecisionTreeClassifier(random_state=random_state),
                "Random Forest": RandomForestClassifier(n_estimators=200,
                                                        random_state=random_state,
                                                        n_jobs=-1),
                "Gradient Boosting": GradientBoostingClassifier(random_state=random_state),
                "KNN": KNeighborsClassifier(),
            }
        else:
            from sklearn.dummy import DummyRegressor
            from sklearn.linear_model import LinearRegression, Ridge, Lasso
            from sklearn.ensemble import (RandomForestRegressor,
                                          GradientBoostingRegressor)
            modelos = {
                "Dummy (baseline)": DummyRegressor(strategy="median"),
                "Regressão Linear": LinearRegression(),
                "Ridge": Ridge(random_state=random_state),
                "Lasso": Lasso(random_state=random_state),
                "Random Forest": RandomForestRegressor(n_estimators=200,
                                                       random_state=random_state,
                                                       n_jobs=-1),
                "Gradient Boosting": GradientBoostingRegressor(random_state=random_state),
            }

    if scoring is None:
        scoring = ("f1_weighted" if tipo == "classificacao"
                   else "neg_root_mean_squared_error")

    linhas = []
    for nome, est in modelos.items():
        pipe = (Pipeline([("pre", preprocessador), ("modelo", est)])
                if preprocessador is not None else est)
        try:
            res = cross_validate(pipe, X, y, cv=cv, scoring=scoring, n_jobs=-1)
            linhas.append({
                "modelo": nome,
                "score_medio": res["test_score"].mean(),
                "score_std": res["test_score"].std(),
                "tempo_fit_s": res["fit_time"].mean(),
            })
        except Exception as e:
            warnings.warn(f"'{nome}' falhou: {e}")

    ranking = (pd.DataFrame(linhas)
               .sort_values("score_medio", ascending=False)
               .reset_index(drop=True))
    if verbose:
        print(f"[comparar] métrica='{scoring}' | cv={cv}")
        print(ranking.round(4).to_string(index=False))
        print("(scoring 'neg_*': mais próximo de 0 é melhor)")
    return ranking


def otimizar_hiperparametros(
    modelo: Any,
    grade: dict[str, list],
    X: pd.DataFrame | np.ndarray,
    y: pd.Series | np.ndarray,
    *,
    metodo: Literal["grid", "random"] = "random",
    n_iter: int = 50,
    cv: int = 5,
    scoring: str | None = None,
    random_state: int = 42,
    verbose: bool = True,
) -> Any:
    """Busca de hiperparâmetros com GridSearchCV ou RandomizedSearchCV.

    Regra prática: "random" com 50 iterações costuma achar resultado quase
    tão bom quanto grid completo em fração do tempo — use "grid" só quando
    a grade é pequena (< ~100 combinações).

    Parameters
    ----------
    grade : dict
        {"param": [valores]}. Se `modelo` for um Pipeline, prefixe com o nome
        do passo: {"modelo__n_estimators": [100, 300]}.

    Returns
    -------
    Objeto de busca ajustado. Use `.best_estimator_`, `.best_params_`,
    `.best_score_`.

    Examples
    --------
    >>> busca = otimizar_hiperparametros(
    ...     RandomForestRegressor(), 
    ...     {"n_estimators": [100, 300, 500], "max_depth": [None, 10, 20]},
    ...     X_tr, y_tr, scoring="neg_root_mean_squared_error")
    >>> melhor = busca.best_estimator_
    """
    from sklearn.model_selection import GridSearchCV, RandomizedSearchCV
    if metodo == "grid":
        busca = GridSearchCV(modelo, grade, cv=cv, scoring=scoring, n_jobs=-1)
    else:
        busca = RandomizedSearchCV(modelo, grade, n_iter=n_iter, cv=cv,
                                   scoring=scoring, n_jobs=-1,
                                   random_state=random_state)
    busca.fit(X, y)
    if verbose:
        print(f"[otimizar:{metodo}] melhor score = {busca.best_score_:.4f}")
        print(f"[otimizar:{metodo}] melhores params = {busca.best_params_}")
    return busca


def importancia_features(
    modelo: Any,
    nomes_features: Sequence[str] | None = None,
    *,
    X_test: pd.DataFrame | np.ndarray | None = None,
    y_test: pd.Series | np.ndarray | None = None,
    metodo: Literal["auto", "permutacao"] = "auto",
    top_n: int = 20,
    plotar: bool = True,
    random_state: int = 42,
    verbose: bool = True,
) -> pd.DataFrame:
    """Ranking de importância de features de um modelo treinado.

    - "auto": usa `feature_importances_` (árvores) ou |coef_| (lineares —
      só comparável se as features estiverem escaladas!);
    - "permutacao": permutation importance no conjunto de TESTE (mais
      confiável e agnóstico ao modelo; exige X_test e y_test).

    Examples
    --------
    >>> imp = importancia_features(modelo, X_tr.columns)
    >>> imp = importancia_features(modelo, X_te.columns, X_test=X_te,
    ...                            y_test=y_te, metodo="permutacao")
    """
    if metodo == "permutacao":
        if X_test is None or y_test is None:
            raise ValueError("Permutação exige X_test e y_test.")
        from sklearn.inspection import permutation_importance
        res = permutation_importance(modelo, X_test, y_test, n_repeats=10,
                                     random_state=random_state, n_jobs=-1)
        valores = res.importances_mean
    elif hasattr(modelo, "feature_importances_"):
        valores = modelo.feature_importances_
    elif hasattr(modelo, "coef_"):
        valores = np.abs(np.ravel(modelo.coef_))
    else:
        raise ValueError("Modelo sem importâncias nativas — use metodo='permutacao'.")

    if nomes_features is None:
        nomes_features = [f"feature_{i}" for i in range(len(valores))]
    imp = (pd.DataFrame({"feature": list(nomes_features), "importancia": valores})
           .sort_values("importancia", ascending=False)
           .reset_index(drop=True))

    if verbose:
        print(imp.head(top_n).round(4).to_string(index=False))
    if plotar and _TEM_PLOT:
        top = imp.head(top_n)[::-1]
        fig, ax = plt.subplots(figsize=(9, max(3, 0.35 * len(top))))
        ax.barh(top["feature"], top["importancia"])
        ax.set_title(f"Importância das features ({metodo})")
        fig.tight_layout()
    return imp


def avaliar_clustering(
    X: pd.DataFrame | np.ndarray,
    *,
    k_min: int = 2,
    k_max: int = 10,
    escalar_antes: bool = True,
    random_state: int = 42,
    plotar: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Ajuda a escolher o k do KMeans: inércia (elbow) + silhouette por k.

    Interpretação do silhouette médio: > 0.5 estrutura razoável; 0.25–0.5
    estrutura fraca (comum em dados reais); < 0.25 provavelmente não há
    clusters bem separados — reporte isso com honestidade.

    Parameters
    ----------
    escalar_antes : bool
        Padroniza X antes (StandardScaler) — KMeans usa distância euclidiana
        e é dominado pela variável de maior escala se não escalar.

    Returns
    -------
    DataFrame com k, inércia e silhouette; melhor k por silhouette impresso.

    Examples
    --------
    >>> resultados = avaliar_clustering(df[["preco_m2", "dist_praia"]], k_max=8)
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    X_arr = np.asarray(pd.DataFrame(X).dropna(), dtype=float)
    if escalar_antes:
        X_arr = StandardScaler().fit_transform(X_arr)

    linhas = []
    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, n_init="auto", random_state=random_state)
        rotulos = km.fit_predict(X_arr)
        linhas.append({"k": k, "inercia": float(km.inertia_),
                       "silhouette": float(silhouette_score(X_arr, rotulos))})
    res = pd.DataFrame(linhas)
    melhor = res.loc[res["silhouette"].idxmax()]

    if verbose:
        print(res.round(4).to_string(index=False))
        print(f"[clustering] melhor k por silhouette: k={int(melhor['k'])} "
              f"(silhouette={melhor['silhouette']:.3f})")

    if plotar and _TEM_PLOT:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
        ax1.plot(res["k"], res["inercia"], marker="o")
        ax1.set(xlabel="k", ylabel="inércia", title="Método do cotovelo")
        ax2.plot(res["k"], res["silhouette"], marker="o", color="tomato")
        ax2.axvline(melhor["k"], ls="--", color="gray")
        ax2.set(xlabel="k", ylabel="silhouette médio", title="Silhouette por k")
        fig.tight_layout()
    return res


def salvar_modelo(modelo: Any, caminho: str | Path, *,
                  metadados: dict | None = None, verbose: bool = True) -> Path:
    """Persiste um modelo (ou pipeline) com joblib + metadados opcionais.

    Boas práticas embutidas:
    - salva junto um dict de metadados (data, métricas, versão de libs) em
      `<caminho>.meta.json` — daqui a 6 meses você vai agradecer;
    - cria pastas automaticamente.

    Examples
    --------
    >>> salvar_modelo(pipeline, "modelos/rf_precos.joblib",
    ...               metadados={"rmse": 45000, "features": list(X.columns)})
    """
    if not _TEM_JOBLIB:
        raise ImportError("Instale joblib: pip install joblib")
    import sklearn
    from datetime import datetime

    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(modelo, caminho)

    meta = {
        "salvo_em": datetime.now().isoformat(timespec="seconds"),
        "classe": type(modelo).__name__,
        "sklearn_versao": sklearn.__version__,
        "pandas_versao": pd.__version__,
        **(metadados or {}),
    }
    with open(f"{caminho}.meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    if verbose:
        print(f"[salvar_modelo] {caminho} ({caminho.stat().st_size/1024:.1f} KB) "
              f"+ metadados")
    return caminho


def carregar_modelo(caminho: str | Path, *, verbose: bool = True) -> Any:
    """Carrega modelo salvo com `salvar_modelo` e avisa se a versão do
    sklearn mudou (causa comum de warnings/incompatibilidades silenciosas).

    Examples
    --------
    >>> modelo = carregar_modelo("modelos/rf_precos.joblib")
    """
    if not _TEM_JOBLIB:
        raise ImportError("Instale joblib: pip install joblib")
    import sklearn

    caminho = Path(caminho)
    modelo = joblib.load(caminho)
    meta_path = Path(f"{caminho}.meta.json")
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if verbose:
            print(f"[carregar_modelo] {meta.get('classe')} salvo em "
                  f"{meta.get('salvo_em')}")
        if meta.get("sklearn_versao") != sklearn.__version__:
            warnings.warn(
                f"Modelo salvo com sklearn {meta.get('sklearn_versao')}, "
                f"ambiente atual tem {sklearn.__version__} — valide previsões.")
    return modelo


# ==============================================================================
# 2b. ETL — FUNÇÕES ADICIONAIS
# ==============================================================================

def padronizar_texto(
    df: pd.DataFrame,
    colunas: Sequence[str] | None = None,
    *,
    minusculas: bool = True,
    remover_acentos: bool = False,
    remover_espacos_extras: bool = True,
    remover_pontuacao: bool = False,
    modo_titulo: bool = False,
    mapa_substituicoes: dict[str, str] | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Padroniza VALORES de colunas de texto (não os nomes — p/ isso use
    `limpar_nomes_colunas`).

    Resolve o clássico "São Paulo" vs "sao paulo " vs "SAO  PAULO" que gera
    categorias duplicadas fantasmas em groupby e value_counts.

    Parameters
    ----------
    colunas : sequence of str, optional
        Padrão: todas as colunas object/category.
    remover_acentos : bool
        "São Paulo" -> "Sao Paulo". Cuidado: irreversível.
    remover_espacos_extras : bool
        Trim nas pontas + colapsa espaços múltiplos internos.
    modo_titulo : bool
        Aplica Title Case ao final ("praia da costa" -> "Praia Da Costa").
    mapa_substituicoes : dict, optional
        Substituições exatas aplicadas por último
        (ex.: {"Vv": "Vila Velha", "N/I": ""}).

    Examples
    --------
    >>> df = padronizar_texto(df, ["bairro", "cidade"], remover_acentos=True)
    """
    df = df.copy()
    if colunas is None:
        colunas = df.select_dtypes(include=["object", "category"]).columns.tolist()

    for c in colunas:
        s = df[c].astype("string")
        n_antes = s.nunique(dropna=True)
        if remover_espacos_extras:
            s = s.str.strip().str.replace(r"\s+", " ", regex=True)
        if minusculas:
            s = s.str.lower()
        if remover_acentos:
            s = (s.str.normalize("NFKD")
                  .str.encode("ascii", "ignore").str.decode("ascii"))
        if remover_pontuacao:
            s = s.str.replace(r"[^\w\s]", "", regex=True)
        if modo_titulo:
            s = s.str.title()
        if mapa_substituicoes:
            s = s.replace(mapa_substituicoes)
        df[c] = s
        if verbose:
            n_depois = df[c].nunique(dropna=True)
            if n_depois < n_antes:
                print(f"[texto] '{c}': {n_antes} -> {n_depois} categorias "
                      f"({n_antes - n_depois} duplicatas de grafia unificadas)")
    return df


def criar_faixas(
    df: pd.DataFrame,
    coluna: str,
    *,
    metodo: Literal["quantil", "largura", "custom"] = "quantil",
    n_faixas: int = 4,
    limites: Sequence[float] | None = None,
    rotulos: Sequence[str] | None = None,
    nova_coluna: str | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Discretiza uma variável numérica em faixas (binning).

    Métodos
    -------
    - "quantil" : faixas com o MESMO nº de observações (qcut) — padrão,
                  robusto a assimetria;
    - "largura" : faixas de mesma amplitude (cut) — interpretável, mas faixas
                  podem ficar vazias em dados assimétricos;
    - "custom"  : você define os cortes em `limites`
                  (ex.: [0, 200_000, 500_000, np.inf]).

    Uso típico: transformar preço em "econômico/médio/alto/luxo" para
    análise, ou discretizar para testes qui-quadrado.

    Examples
    --------
    >>> df = criar_faixas(df, "preco", metodo="custom",
    ...     limites=[0, 300e3, 600e3, np.inf],
    ...     rotulos=["econômico", "médio", "alto padrão"])
    """
    df = df.copy()
    destino = nova_coluna or f"{coluna}_faixa"
    if metodo == "quantil":
        df[destino] = pd.qcut(df[coluna], q=n_faixas, labels=rotulos,
                              duplicates="drop")
    elif metodo == "largura":
        df[destino] = pd.cut(df[coluna], bins=n_faixas, labels=rotulos)
    else:
        if limites is None:
            raise ValueError("metodo='custom' exige `limites=[...]`.")
        df[destino] = pd.cut(df[coluna], bins=list(limites), labels=rotulos,
                             include_lowest=True)
    if verbose:
        print(f"[faixas] '{destino}' criada ({metodo}):")
        print(df[destino].value_counts(dropna=False).sort_index().to_string())
    return df


def mesclar_seguro(
    esquerda: pd.DataFrame,
    direita: pd.DataFrame,
    *,
    on: str | Sequence[str],
    como: Literal["left", "right", "inner", "outer"] = "left",
    sufixos: tuple[str, str] = ("", "_dir"),
    validar: str | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Merge com diagnóstico completo — o antídoto contra joins silenciosamente errados.

    Reporta automaticamente:
    - taxa de match (quantas chaves da esquerda encontraram par);
    - explosão de linhas (merge N:N duplicando dados sem você perceber);
    - chaves duplicadas em cada lado.

    Parameters
    ----------
    validar : str, optional
        Passa ao pandas p/ ABORTAR se a relação for violada:
        "one_to_one", "one_to_many", "many_to_one". Use sempre que souber a
        cardinalidade esperada.

    Examples
    --------
    >>> df = mesclar_seguro(anuncios, bairros, on="bairro",
    ...                     como="left", validar="many_to_one")
    """
    chaves = [on] if isinstance(on, str) else list(on)
    n_esq = len(esquerda)

    if verbose:
        dup_esq = int(esquerda.duplicated(subset=chaves).sum())
        dup_dir = int(direita.duplicated(subset=chaves).sum())
        if dup_esq:
            print(f"[merge] aviso: {dup_esq:,} chaves duplicadas à ESQUERDA")
        if dup_dir:
            print(f"[merge] aviso: {dup_dir:,} chaves duplicadas à DIREITA "
                  f"(left join vai multiplicar linhas!)")

    resultado = esquerda.merge(direita, on=chaves, how=como,
                               suffixes=sufixos, indicator="_merge_status",
                               validate=validar)
    if verbose:
        contagem = resultado["_merge_status"].value_counts()
        so_esq = int(contagem.get("left_only", 0))
        ambos = int(contagem.get("both", 0))
        print(f"[merge] {como}: {n_esq:,} -> {len(resultado):,} linhas | "
              f"match={ambos:,} ({100*ambos/max(len(resultado),1):.1f}%) | "
              f"sem par={so_esq:,}")
        if len(resultado) > n_esq and como == "left":
            print(f"[merge] ATENÇÃO: left join AUMENTOU as linhas "
                  f"(+{len(resultado)-n_esq:,}) — chaves duplicadas à direita.")
    return resultado.drop(columns="_merge_status")


# ==============================================================================
# 3b. EDA — FUNÇÕES ADICIONAIS
# ==============================================================================

def analise_alvo(
    df: pd.DataFrame,
    alvo: str,
    *,
    max_categorias: int = 30,
    verbose: bool = True,
) -> pd.DataFrame:
    """Ranqueia TODAS as features pela força de associação com o alvo,
    escolhendo a medida certa para cada par de tipos:

    - num x num : |correlação de Spearman|;
    - cat x num : eta² da ANOVA (variância do alvo explicada pelos grupos);
    - num x cat (alvo categórico) : eta² invertido (feature por grupo do alvo);
    - cat x cat : Cramér's V.

    Todas as medidas ficam em [0, 1], então o ranking é comparável entre
    tipos — ótimo primeiro passo de seleção de features.

    Returns
    -------
    DataFrame: feature, tipo_relacao, medida, forca (ordenado desc).

    Examples
    --------
    >>> analise_alvo(df, "preco").head(10)
    """
    def _eta2(valores: pd.Series, grupos: pd.Series) -> float:
        dados = pd.DataFrame({"v": valores, "g": grupos}).dropna()
        if dados["g"].nunique() < 2 or len(dados) < 3:
            return np.nan
        media_geral = dados["v"].mean()
        ss_entre = float((dados.groupby("g", observed=True)["v"]
                          .agg(["mean", "size"])
                          .apply(lambda r: r["size"] * (r["mean"] - media_geral) ** 2,
                                 axis=1).sum()))
        ss_total = float(((dados["v"] - media_geral) ** 2).sum())
        return ss_entre / ss_total if ss_total else np.nan

    y = df[alvo]
    alvo_numerico = pd.api.types.is_numeric_dtype(y) and y.nunique() > 20
    linhas = []
    for col in df.columns:
        if col == alvo:
            continue
        x = df[col]
        col_numerica = pd.api.types.is_numeric_dtype(x)
        if not col_numerica and x.nunique() > max_categorias:
            continue  # alta cardinalidade: medida ficaria inflada
        try:
            if alvo_numerico and col_numerica:
                forca = abs(df[[col, alvo]].dropna().corr(method="spearman").iloc[0, 1])
                tipo, medida = "num x num", "|spearman|"
            elif alvo_numerico:
                forca = _eta2(y, x)
                tipo, medida = "cat x num", "eta2"
            elif col_numerica:
                forca = _eta2(x, y)
                tipo, medida = "num x cat", "eta2"
            else:
                tabela = pd.crosstab(x, y)
                chi2 = stats.chi2_contingency(tabela)[0]
                n = tabela.values.sum()
                forca = float(np.sqrt(chi2 / (n * (min(tabela.shape) - 1)))) if n else np.nan
                tipo, medida = "cat x cat", "cramers_v"
            linhas.append({"feature": col, "tipo_relacao": tipo,
                           "medida": medida, "forca": forca})
        except Exception:
            continue

    ranking = (pd.DataFrame(linhas).dropna(subset=["forca"])
               .sort_values("forca", ascending=False).reset_index(drop=True))
    if verbose:
        print(f"[analise_alvo] associação com '{alvo}':")
        print(ranking.round(3).to_string(index=False))
    return ranking


# ==============================================================================
# 4b. GRÁFICOS — FUNÇÕES ADICIONAIS
# ==============================================================================

def plot_qq(serie: pd.Series | np.ndarray, *, titulo: str | None = None,
            salvar_em: str | None = None):
    """QQ-plot contra a normal — o complemento visual de `testar_normalidade`.

    Como ler: pontos na reta = compatível com normal; cauda direita acima
    da reta = assimetria positiva (típico de preço — tente log).

    Examples
    --------
    >>> plot_qq(df["preco"])
    >>> plot_qq(np.log(df["preco"]))  # comparar antes/depois do log
    """
    _checar_plot()
    x = pd.Series(serie).dropna().astype(float)
    fig, ax = plt.subplots(figsize=(6.5, 6))
    stats.probplot(x, dist="norm", plot=ax)
    ax.get_lines()[0].set(markersize=4, alpha=0.6)
    ax.get_lines()[1].set(color="tomato", lw=2)
    ax.set_title(titulo or "QQ-plot vs distribuição normal")
    fig.tight_layout()
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(salvar_em, bbox_inches="tight")
    return fig, ax


def plot_pares(
    df: pd.DataFrame,
    colunas: Sequence[str] | None = None,
    *,
    hue: str | None = None,
    amostra_max: int = 2000,
    diagonal: Literal["kde", "hist"] = "kde",
    salvar_em: str | None = None,
):
    """Pairplot (matriz de dispersões) com amostragem automática.

    Limita a 8 colunas e `amostra_max` linhas — pairplot completo de um
    DataFrame grande demora minutos e sai ilegível.

    Examples
    --------
    >>> plot_pares(df, ["preco", "area", "quartos"], hue="cluster")
    """
    _checar_plot()
    if colunas is None:
        colunas = df.select_dtypes(include=np.number).columns.tolist()[:8]
    colunas = list(colunas)[:8]
    cols_uso = colunas + ([hue] if hue and hue not in colunas else [])
    dados = df[cols_uso].dropna()
    if len(dados) > amostra_max:
        dados = dados.sample(amostra_max, random_state=42)
    g = sns.pairplot(dados, vars=colunas, hue=hue, diag_kind=diagonal,
                     plot_kws={"alpha": 0.5, "s": 15}, corner=True)
    if salvar_em:
        Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
        g.savefig(salvar_em, bbox_inches="tight")
    return g


# ==============================================================================
# 5b. ML — FUNÇÕES ADICIONAIS
# ==============================================================================

def curva_aprendizado(
    modelo: Any,
    X: pd.DataFrame | np.ndarray,
    y: pd.Series | np.ndarray,
    *,
    cv: int = 5,
    scoring: str | None = None,
    n_pontos: int = 8,
    plotar: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Curva de aprendizado — diagnostica overfitting vs underfitting.

    Como interpretar:
    - Curvas distantes (treino alto, validação baixa) -> OVERFITTING:
      regularize, simplifique o modelo ou consiga mais dados;
    - Curvas juntas e baixas -> UNDERFITTING: modelo mais complexo ou
      melhores features;
    - Validação ainda subindo no fim -> mais dados devem ajudar.

    Examples
    --------
    >>> curva_aprendizado(pipeline, X, y, scoring="r2")
    """
    from sklearn.model_selection import learning_curve
    tamanhos, sc_tr, sc_va = learning_curve(
        modelo, X, y, cv=cv, scoring=scoring,
        train_sizes=np.linspace(0.1, 1.0, n_pontos), n_jobs=-1)
    res = pd.DataFrame({
        "n_amostras": tamanhos,
        "score_treino": sc_tr.mean(axis=1),
        "score_validacao": sc_va.mean(axis=1),
        "std_validacao": sc_va.std(axis=1),
    })
    res["gap"] = res["score_treino"] - res["score_validacao"]
    if verbose:
        final = res.iloc[-1]
        print(res.round(4).to_string(index=False))
        if final["gap"] > 0.1:
            print(f"[aprendizado] gap final = {final['gap']:.3f} -> "
                  f"indícios de OVERFITTING")
    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots()
        ax.plot(res["n_amostras"], res["score_treino"], marker="o",
                label="treino")
        ax.plot(res["n_amostras"], res["score_validacao"], marker="o",
                color="tomato", label="validação")
        ax.fill_between(res["n_amostras"],
                        res["score_validacao"] - res["std_validacao"],
                        res["score_validacao"] + res["std_validacao"],
                        alpha=0.2, color="tomato")
        ax.set(xlabel="nº de amostras de treino", ylabel=scoring or "score",
               title="Curva de aprendizado")
        ax.legend()
        fig.tight_layout()
    return res


def selecionar_features(
    X: pd.DataFrame,
    y: pd.Series | np.ndarray,
    *,
    metodo: Literal["kbest", "rfe", "modelo"] = "kbest",
    k: int = 10,
    tipo: Literal["classificacao", "regressao"] = "regressao",
    estimador: Any = None,
    verbose: bool = True,
) -> list[str]:
    """Seleciona as k melhores features (exige X totalmente numérico e sem nulos).

    Métodos
    -------
    - "kbest"  : testes univariados (f_regression/f_classif) — rápido, mas
                 ignora interações;
    - "rfe"    : eliminação recursiva com o estimador — mais caro e mais fiel;
    - "modelo" : importâncias de um RandomForest (capta não-linearidades).

    Returns
    -------
    Lista com os nomes das features selecionadas.

    Examples
    --------
    >>> melhores = selecionar_features(X_num, y, metodo="rfe", k=8)
    >>> X_reduzido = X[melhores]
    """
    from sklearn.feature_selection import SelectKBest, RFE, f_classif, f_regression
    k = min(k, X.shape[1])

    if metodo == "kbest":
        seletor = SelectKBest(f_classif if tipo == "classificacao"
                              else f_regression, k=k).fit(X, y)
        escolhidas = X.columns[seletor.get_support()].tolist()
    elif metodo == "rfe":
        if estimador is None:
            from sklearn.linear_model import LogisticRegression, LinearRegression
            estimador = (LogisticRegression(max_iter=2000)
                         if tipo == "classificacao" else LinearRegression())
        seletor = RFE(estimador, n_features_to_select=k).fit(X, y)
        escolhidas = X.columns[seletor.get_support()].tolist()
    else:  # modelo
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        est = estimador or (RandomForestClassifier(n_estimators=200, random_state=42)
                            if tipo == "classificacao"
                            else RandomForestRegressor(n_estimators=200,
                                                       random_state=42))
        est.fit(X, y)
        imp = pd.Series(est.feature_importances_, index=X.columns)
        escolhidas = imp.nlargest(k).index.tolist()

    if verbose:
        print(f"[selecao:{metodo}] top {k}: {escolhidas}")
    return escolhidas


def reduzir_dimensionalidade(
    X: pd.DataFrame | np.ndarray,
    *,
    n_componentes: int | float = 0.95,
    escalar_antes: bool = True,
    plotar: bool = True,
    verbose: bool = True,
) -> tuple[pd.DataFrame, Any]:
    """PCA com relatório de variância explicada.

    Parameters
    ----------
    n_componentes : int | float
        Inteiro = nº fixo de componentes; float em (0,1) = mínimo de
        variância acumulada desejada (ex.: 0.95 escolhe o nº automaticamente).
    escalar_antes : bool
        Padroniza X primeiro — PCA sem escala é dominado pela variável de
        maior variância. Desligue só se X já estiver escalado.

    Returns
    -------
    (DataFrame com colunas PC1..PCn, objeto PCA ajustado)
        Use `pca.transform(novos_dados_escalados)` em produção.

    Examples
    --------
    >>> X_pca, pca = reduzir_dimensionalidade(X_num, n_componentes=0.9)
    """
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    X_df = pd.DataFrame(X).dropna()
    X_arr = StandardScaler().fit_transform(X_df) if escalar_antes else X_df.values
    pca = PCA(n_components=n_componentes, random_state=42)
    comp = pca.fit_transform(X_arr)
    X_pca = pd.DataFrame(comp, index=X_df.index,
                         columns=[f"PC{i+1}" for i in range(comp.shape[1])])
    var_acum = np.cumsum(pca.explained_variance_ratio_)

    if verbose:
        print(f"[pca] {X_df.shape[1]} -> {comp.shape[1]} dimensões | "
              f"variância explicada acumulada = {var_acum[-1]:.1%}")
    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots()
        ax.bar(range(1, len(var_acum) + 1), pca.explained_variance_ratio_,
               label="individual")
        ax.plot(range(1, len(var_acum) + 1), var_acum, marker="o",
                color="tomato", label="acumulada")
        ax.set(xlabel="componente", ylabel="variância explicada",
               title="Scree plot (PCA)")
        ax.legend()
        fig.tight_layout()
    return X_pca, pca


def balancear_classes(
    X: pd.DataFrame,
    y: pd.Series,
    *,
    metodo: Literal["undersample", "oversample"] = "oversample",
    random_state: int = 42,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.Series]:
    """Balanceia classes por reamostragem simples (sem dependência do imblearn).

    - "oversample"  : duplica aleatoriamente a(s) classe(s) minoritária(s)
                      até igualar a majoritária (não perde dados; risco de
                      overfitting em modelos que decoram);
    - "undersample" : descarta aleatoriamente da majoritária (perde dados;
                      seguro quando a base é grande).

    IMPORTANTE: aplique SOMENTE no conjunto de TREINO, nunca no teste —
    balancear antes do split infla artificialmente as métricas.

    Examples
    --------
    >>> X_tr_bal, y_tr_bal = balancear_classes(X_tr, y_tr, metodo="oversample")
    """
    from sklearn.utils import resample
    dados = pd.concat([pd.DataFrame(X).reset_index(drop=True),
                       pd.Series(y, name="_alvo").reset_index(drop=True)], axis=1)
    contagens = dados["_alvo"].value_counts()
    alvo_n = contagens.max() if metodo == "oversample" else contagens.min()

    partes = []
    for classe, grupo in dados.groupby("_alvo", observed=True):
        if len(grupo) == alvo_n:
            partes.append(grupo)
        else:
            partes.append(resample(grupo, replace=(metodo == "oversample"),
                                   n_samples=alvo_n, random_state=random_state))
    bal = pd.concat(partes).sample(frac=1, random_state=random_state)\
                           .reset_index(drop=True)
    if verbose:
        print(f"[balanceio:{metodo}] antes={contagens.to_dict()} | "
              f"depois={bal['_alvo'].value_counts().to_dict()}")
    return bal.drop(columns="_alvo"), bal["_alvo"]


# ==============================================================================
# 6. MATEMÁTICA / ESTATÍSTICA
# ==============================================================================

def intervalo_confianca(
    serie: pd.Series | np.ndarray,
    *,
    nivel: float = 0.95,
    tipo: Literal["media", "proporcao", "mediana"] = "media",
    verbose: bool = True,
) -> dict[str, float]:
    """Intervalo de confiança para média (t de Student), proporção (Wilson)
    ou mediana (bootstrap).

    - "media"     : IC t — adequado mesmo sem normalidade se n for razoável
                    (TCL), mas confira outliers;
    - "proporcao" : Wilson score — muito melhor que o IC "normal" clássico
                    perto de 0/1 e em amostras pequenas. A série deve conter
                    apenas 0/1 (ou bool);
    - "mediana"   : bootstrap percentil (2000 reamostras).

    Examples
    --------
    >>> intervalo_confianca(df["preco"], tipo="mediana")
    >>> intervalo_confianca(df["convertido"], tipo="proporcao")
    """
    x = pd.Series(serie).dropna().astype(float)
    n = len(x)
    alfa = 1 - nivel

    if tipo == "media":
        centro = float(x.mean())
        ep = float(x.std(ddof=1) / np.sqrt(n))
        t_crit = stats.t.ppf(1 - alfa / 2, df=n - 1)
        lo, hi = centro - t_crit * ep, centro + t_crit * ep
    elif tipo == "proporcao":
        if not set(np.unique(x)).issubset({0.0, 1.0}):
            raise ValueError("Para proporção, a série deve conter apenas 0/1.")
        p = float(x.mean())
        z = stats.norm.ppf(1 - alfa / 2)
        denom = 1 + z**2 / n
        centro_w = (p + z**2 / (2 * n)) / denom
        meia = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
        centro, lo, hi = p, centro_w - meia, centro_w + meia
    else:  # mediana via bootstrap
        rng = np.random.default_rng(42)
        medianas = np.array([np.median(rng.choice(x, n, replace=True))
                             for _ in range(2000)])
        centro = float(x.median())
        lo, hi = np.percentile(medianas, [100 * alfa / 2, 100 * (1 - alfa / 2)])

    resultado = {"estimativa": centro, "ic_inferior": float(lo),
                 "ic_superior": float(hi), "nivel": nivel, "n": n}
    if verbose:
        print(f"[ic:{tipo}] {centro:.4f} | IC{nivel:.0%} = "
              f"[{lo:.4f}, {hi:.4f}] | n={n:,}")
    return resultado


def bootstrap_estatistica(
    serie: pd.Series | np.ndarray,
    estatistica: Callable[[np.ndarray], float] = np.mean,
    *,
    n_reamostras: int = 5000,
    nivel: float = 0.95,
    random_state: int = 42,
    verbose: bool = True,
) -> dict[str, Any]:
    """IC bootstrap (percentil) para QUALQUER estatística que você definir.

    Serve quando não há fórmula fechada: desvio-padrão, quantis, razão de
    médias, coeficiente de variação, trimmed mean...

    Parameters
    ----------
    estatistica : callable
        Função array -> escalar. Ex.: `np.median`,
        `lambda a: np.percentile(a, 90)`, `lambda a: a.std()/a.mean()`.

    Examples
    --------
    >>> bootstrap_estatistica(df["preco"], lambda a: np.percentile(a, 90))
    """
    x = pd.Series(serie).dropna().to_numpy(dtype=float)
    rng = np.random.default_rng(random_state)
    amostras = np.array([estatistica(rng.choice(x, len(x), replace=True))
                         for _ in range(n_reamostras)])
    alfa = 1 - nivel
    lo, hi = np.percentile(amostras, [100 * alfa / 2, 100 * (1 - alfa / 2)])
    resultado = {
        "estimativa": float(estatistica(x)),
        "ic_inferior": float(lo), "ic_superior": float(hi),
        "erro_padrao_boot": float(amostras.std(ddof=1)),
        "nivel": nivel, "n_reamostras": n_reamostras,
        "distribuicao_boot": amostras,
    }
    if verbose:
        print(f"[bootstrap] {resultado['estimativa']:.4f} | "
              f"IC{nivel:.0%} = [{lo:.4f}, {hi:.4f}] | "
              f"EP={resultado['erro_padrao_boot']:.4f}")
    return resultado


def tamanho_amostra(
    *,
    tipo: Literal["media", "proporcao"] = "proporcao",
    margem_erro: float = 0.05,
    nivel: float = 0.95,
    desvio_estimado: float | None = None,
    proporcao_estimada: float = 0.5,
    populacao: int | None = None,
    verbose: bool = True,
) -> int:
    """Calcula o n mínimo para estimar média ou proporção com a margem de
    erro desejada.

    - "proporcao" : usa p=0.5 por padrão (cenário mais conservador);
    - "media"     : exige `desvio_estimado` (de estudo piloto ou literatura);
    - `populacao` : se fornecida, aplica correção para população finita
      (faz diferença quando n passa de ~5% da população).

    Examples
    --------
    >>> tamanho_amostra(tipo="proporcao", margem_erro=0.03)          # pesquisa
    >>> tamanho_amostra(tipo="media", margem_erro=50, desvio_estimado=400)
    """
    z = stats.norm.ppf(1 - (1 - nivel) / 2)
    if tipo == "proporcao":
        p = proporcao_estimada
        n = (z**2 * p * (1 - p)) / margem_erro**2
    else:
        if desvio_estimado is None:
            raise ValueError("tipo='media' exige `desvio_estimado`.")
        n = (z * desvio_estimado / margem_erro) ** 2
    if populacao:
        n = n / (1 + (n - 1) / populacao)
    n_final = int(np.ceil(n))
    if verbose:
        extra = f" (população={populacao:,})" if populacao else ""
        print(f"[amostra] n mínimo = {n_final:,} para margem "
              f"±{margem_erro} com {nivel:.0%} de confiança{extra}")
    return n_final


def teste_ab(
    conversoes_a: int, total_a: int,
    conversoes_b: int, total_b: int,
    *,
    alpha: float = 0.05,
    verbose: bool = True,
) -> dict[str, Any]:
    """Análise completa de um teste A/B de proporções (conversão).

    Reporta: taxas, lift relativo, teste z bilateral de duas proporções,
    IC da diferença e poder estatístico observado — se o poder for baixo
    (<80%), um resultado "não significativo" é inconclusivo, não negativo.

    Examples
    --------
    >>> teste_ab(conversoes_a=120, total_a=2400, conversoes_b=156, total_b=2350)
    """
    p_a, p_b = conversoes_a / total_a, conversoes_b / total_b
    p_pool = (conversoes_a + conversoes_b) / (total_a + total_b)
    ep_pool = np.sqrt(p_pool * (1 - p_pool) * (1 / total_a + 1 / total_b))
    z_stat = (p_b - p_a) / ep_pool if ep_pool else np.nan
    p_valor = float(2 * (1 - stats.norm.cdf(abs(z_stat))))

    ep_dif = np.sqrt(p_a * (1 - p_a) / total_a + p_b * (1 - p_b) / total_b)
    z_crit = stats.norm.ppf(1 - alpha / 2)
    dif = p_b - p_a
    ic = (float(dif - z_crit * ep_dif), float(dif + z_crit * ep_dif))

    # poder observado (aproximação normal para o efeito medido)
    poder = float(stats.norm.cdf(abs(dif) / ep_dif - z_crit)) if ep_dif else np.nan

    resultado = {
        "taxa_a": p_a, "taxa_b": p_b,
        "diferenca_abs": dif,
        "lift_relativo_%": 100 * dif / p_a if p_a else np.nan,
        "z": float(z_stat), "p_valor": p_valor,
        "significativo": p_valor < alpha,
        "ic_diferenca": ic, "poder_observado": poder,
    }
    if verbose:
        sig = "SIGNIFICATIVO" if resultado["significativo"] else "não significativo"
        print(f"[ab] A={p_a:.4f} B={p_b:.4f} | lift="
              f"{resultado['lift_relativo_%']:+.1f}% | p={p_valor:.4g} ({sig})")
        print(f"[ab] IC{1-alpha:.0%} da diferença = "
              f"[{ic[0]:+.4f}, {ic[1]:+.4f}] | poder={poder:.1%}")
        if not resultado["significativo"] and poder < 0.8:
            print("[ab] poder < 80% -> resultado INCONCLUSIVO "
                  "(amostra pequena para detectar esse efeito)")
    return resultado


def correlacao_com_p(
    df: pd.DataFrame,
    *,
    metodo: Literal["pearson", "spearman"] = "pearson",
    corrigir_multiplas: bool = True,
    alpha: float = 0.05,
    verbose: bool = True,
) -> pd.DataFrame:
    """Todos os pares de correlação COM p-valor — o que `df.corr()` não dá.

    Aplica correção de Bonferroni por padrão: testando dezenas de pares,
    ~5% seriam "significativos" por puro acaso sem correção.

    Returns
    -------
    DataFrame longo: var_1, var_2, r, p_valor, p_ajustado, significativo.

    Examples
    --------
    >>> pares = correlacao_com_p(df, metodo="spearman")
    >>> pares[pares["significativo"]]
    """
    num = df.select_dtypes(include=np.number)
    cols = num.columns.tolist()
    func = stats.pearsonr if metodo == "pearson" else stats.spearmanr
    linhas = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            par = num[[a, b]].dropna()
            if len(par) < 3:
                continue
            r, p = func(par[a], par[b])
            linhas.append({"var_1": a, "var_2": b, "r": float(r),
                           "p_valor": float(p), "n": len(par)})
    res = pd.DataFrame(linhas)
    if res.empty:
        return res
    m = len(res)
    res["p_ajustado"] = (res["p_valor"] * m).clip(upper=1.0) \
        if corrigir_multiplas else res["p_valor"]
    res["significativo"] = res["p_ajustado"] < alpha
    res = res.sort_values("r", key=lambda s: s.abs(), ascending=False)\
             .reset_index(drop=True)
    if verbose:
        n_sig = int(res["significativo"].sum())
        corr_txt = " (Bonferroni)" if corrigir_multiplas else ""
        print(f"[corr_p:{metodo}] {m} pares testados | "
              f"{n_sig} significativos{corr_txt}")
    return res


def calcular_vif(X: pd.DataFrame, *, limite_alerta: float = 10.0,
                 verbose: bool = True) -> pd.DataFrame:
    """VIF (Variance Inflation Factor) — diagnóstico de multicolinearidade
    para modelos lineares, sem depender do statsmodels.

    Interpretação usual: VIF < 5 ok; 5–10 atenção; > 10 multicolinearidade
    séria (coeficientes instáveis — remova ou combine variáveis).

    Exige X numérico e sem nulos.

    Examples
    --------
    >>> calcular_vif(df[["area", "quartos", "banheiros", "vagas"]])
    """
    from sklearn.linear_model import LinearRegression
    X_num = pd.DataFrame(X).select_dtypes(include=np.number).dropna()
    linhas = []
    for col in X_num.columns:
        outras = X_num.drop(columns=col)
        if outras.empty:
            linhas.append({"feature": col, "vif": 1.0})
            continue
        r2 = LinearRegression().fit(outras, X_num[col])\
                               .score(outras, X_num[col])
        vif = 1 / (1 - r2) if r2 < 1 else np.inf
        linhas.append({"feature": col, "vif": float(vif)})
    res = pd.DataFrame(linhas).sort_values("vif", ascending=False)\
                              .reset_index(drop=True)
    if verbose:
        print(res.round(2).to_string(index=False))
        graves = res[res["vif"] > limite_alerta]["feature"].tolist()
        if graves:
            print(f"[vif] multicolinearidade séria (>{limite_alerta}): {graves}")
    return res


def ajustar_distribuicao(
    serie: pd.Series | np.ndarray,
    *,
    candidatas: Sequence[str] = ("norm", "lognorm", "expon", "gamma",
                                 "weibull_min", "uniform"),
    plotar: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Ajusta várias distribuições teóricas aos dados e ranqueia pelo
    teste de Kolmogorov-Smirnov (maior p-valor = melhor ajuste).

    Útil para: escolher a distribuição em simulações Monte Carlo, validar
    suposições de modelos, e — no seu caso — famílias paramétricas de
    análise de sobrevivência (Weibull, Log-Normal, Exponencial).

    Parameters
    ----------
    candidatas : sequence of str
        Nomes de distribuições do `scipy.stats`.

    Returns
    -------
    DataFrame: distribuicao, parametros, ks_stat, p_valor (ordenado).

    Examples
    --------
    >>> ajustar_distribuicao(df["tempo_ate_venda"],
    ...                      candidatas=["expon", "weibull_min", "lognorm"])
    """
    x = pd.Series(serie).dropna().to_numpy(dtype=float)
    linhas = []
    for nome in candidatas:
        dist = getattr(stats, nome)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                params = dist.fit(x)
                ks_stat, p = stats.kstest(x, nome, args=params)
            linhas.append({"distribuicao": nome,
                           "parametros": tuple(round(p_, 4) for p_ in params),
                           "ks_stat": float(ks_stat), "p_valor": float(p)})
        except Exception as e:
            if verbose:
                print(f"[distribuicao] '{nome}' falhou: {e}")
    res = pd.DataFrame(linhas).sort_values("p_valor", ascending=False)\
                              .reset_index(drop=True)
    if verbose and not res.empty:
        melhor = res.iloc[0]
        print(f"[distribuicao] melhor ajuste: {melhor['distribuicao']} "
              f"(KS p={melhor['p_valor']:.4f})")
        print(res.to_string(index=False))
    if plotar and _TEM_PLOT and not res.empty:
        fig, ax = plt.subplots()
        ax.hist(x, bins="auto", density=True, alpha=0.5, label="dados")
        grade = np.linspace(x.min(), x.max(), 300)
        for _, linha in res.head(3).iterrows():
            dist = getattr(stats, linha["distribuicao"])
            ax.plot(grade, dist.pdf(grade, *dist.fit(x)), lw=2,
                    label=linha["distribuicao"])
        ax.legend()
        ax.set_title("Ajuste de distribuições (top 3)")
        fig.tight_layout()
    return res


def estatisticas_robustas(serie: pd.Series | np.ndarray, *,
                          proporcao_apara: float = 0.1,
                          verbose: bool = True) -> dict[str, float]:
    """Estatísticas resistentes a outliers, lado a lado com as clássicas.

    - MAD (desvio absoluto mediano, escalado x1.4826 p/ comparar com o desvio
      padrão sob normalidade);
    - média aparada (descarta `proporcao_apara` de cada cauda);
    - média winsorizada (trunca as caudas em vez de descartar).

    Se média ≈ média aparada, os outliers não estão distorcendo; se diferem
    muito, prefira estatísticas robustas nos seus relatórios.

    Examples
    --------
    >>> estatisticas_robustas(df["preco"])
    """
    x = pd.Series(serie).dropna().to_numpy(dtype=float)
    mediana = float(np.median(x))
    mad = float(stats.median_abs_deviation(x, scale="normal"))
    resultado = {
        "media": float(np.mean(x)),
        "mediana": mediana,
        "desvio_padrao": float(np.std(x, ddof=1)),
        "mad_escalado": mad,
        "media_aparada": float(stats.trim_mean(x, proporcao_apara)),
        "media_winsorizada": float(stats.mstats.winsorize(
            x, limits=proporcao_apara).mean()),
        "iqr": float(np.percentile(x, 75) - np.percentile(x, 25)),
    }
    if verbose:
        dif = 100 * abs(resultado["media"] - resultado["media_aparada"]) \
              / max(abs(resultado["media_aparada"]), 1e-12)
        print(f"[robustas] media={resultado['media']:.4f} vs "
              f"aparada={resultado['media_aparada']:.4f} "
              f"(dif {dif:.1f}%) | mediana={mediana:.4f} | "
              f"std={resultado['desvio_padrao']:.4f} vs MAD={mad:.4f}")
        if dif > 10:
            print("[robustas] diferença >10% -> outliers estão puxando a média")
    return resultado


def derivada_numerica(
    funcao: Callable[[float], float],
    x0: float,
    *,
    ordem: int = 1,
    h: float = 1e-5,
) -> float:
    """Derivada numérica de 1ª ou 2ª ordem por diferenças centrais.

    Útil para conferir derivadas calculadas à mão (seus estudos de cálculo)
    e para analisar sensibilidade de funções de custo.

    Examples
    --------
    >>> derivada_numerica(lambda x: x**3, 2.0)        # ~12 (3x²)
    >>> derivada_numerica(np.sin, 0.0, ordem=2)       # ~0  (-sin(0))
    """
    if ordem == 1:
        return float((funcao(x0 + h) - funcao(x0 - h)) / (2 * h))
    if ordem == 2:
        return float((funcao(x0 + h) - 2 * funcao(x0) + funcao(x0 - h)) / h**2)
    raise ValueError("Apenas ordem 1 ou 2.")


def integral_numerica(
    funcao: Callable[[float], float],
    a: float,
    b: float,
    *,
    verbose: bool = True,
) -> dict[str, float]:
    """Integral definida via quadratura adaptativa (scipy.integrate.quad),
    com estimativa de erro.

    Aceita limites infinitos (`np.inf`) — ex.: verificar que uma densidade
    de probabilidade integra 1.

    Examples
    --------
    >>> integral_numerica(lambda x: np.exp(-x**2/2)/np.sqrt(2*np.pi),
    ...                   -np.inf, np.inf)   # ~1.0
    """
    from scipy.integrate import quad
    valor, erro = quad(funcao, a, b)
    if verbose:
        print(f"[integral] ∫f de {a} a {b} = {valor:.6f} (erro ~{erro:.2e})")
    return {"valor": float(valor), "erro_estimado": float(erro)}



# ==============================================================================
# 7. ANÁLISE DE SOBREVIVÊNCIA
# ==============================================================================
# Requer: pip install lifelines
#
# Convenção de dados (formato padrão da área):
#   - coluna de TEMPO   : duração até o evento ou até a censura (T > 0);
#   - coluna de EVENTO  : 1 = evento observado (óbito, churn, venda...),
#                         0 = censura à direita (acompanhamento terminou
#                         sem o evento ocorrer).
# ==============================================================================

def _checar_lifelines() -> None:
    if not _TEM_LIFELINES:
        raise ImportError("Instale a lifelines: pip install lifelines")


def preparar_sobrevivencia(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    *,
    mapa_evento: dict | None = None,
    coluna_inicio: str | None = None,
    coluna_fim: str | None = None,
    unidade: Literal["dias", "semanas", "meses", "anos"] = "dias",
    verbose: bool = True,
) -> pd.DataFrame:
    """Valida e padroniza um DataFrame para análise de sobrevivência.

    Checagens e correções aplicadas:
    - evento convertido para 0/1 (aceita bool, "sim"/"nao" via `mapa_evento`,
      True/False...);
    - tempos <= 0 ou nulos são removidos com aviso (inválidos p/ os modelos);
    - se você só tem DATAS (`coluna_inicio`/`coluna_fim`), calcula a duração
      automaticamente na `unidade` pedida;
    - reporta a taxa de censura — se for altíssima (>90%), estimativas de
      mediana podem nem existir.

    Parameters
    ----------
    mapa_evento : dict, optional
        Mapeamento para 0/1. Ex.: {"obito": 1, "vivo": 0} ou {"Dead": 1,
        "Alive": 0} (padrão TCGA).
    coluna_inicio, coluna_fim : str, optional
        Se fornecidas, `coluna_tempo` é CRIADA a partir da diferença.

    Returns
    -------
    DataFrame limpo com `coluna_tempo` (float > 0) e `coluna_evento` (int 0/1).

    Examples
    --------
    >>> df = preparar_sobrevivencia(df, "tempo_meses", "status",
    ...                             mapa_evento={"Dead": 1, "Alive": 0})
    >>> df = preparar_sobrevivencia(df, "duracao", "vendido",
    ...     coluna_inicio="data_anuncio", coluna_fim="data_venda",
    ...     unidade="semanas")
    """
    df = df.copy()
    divisores = {"dias": 1, "semanas": 7, "meses": 30.44, "anos": 365.25}

    if coluna_inicio and coluna_fim:
        for c in (coluna_inicio, coluna_fim):
            if not pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = pd.to_datetime(df[c], dayfirst=True, errors="coerce")
        df[coluna_tempo] = ((df[coluna_fim] - df[coluna_inicio]).dt.days
                            / divisores[unidade])

    if mapa_evento:
        df[coluna_evento] = df[coluna_evento].map(mapa_evento)
    df[coluna_evento] = pd.to_numeric(df[coluna_evento], errors="coerce")

    invalidos_evento = ~df[coluna_evento].isin([0, 1])
    if invalidos_evento.any():
        if verbose:
            print(f"[sobrevivencia] {int(invalidos_evento.sum())} linhas com "
                  f"evento fora de {{0,1}} removidas — use `mapa_evento` "
                  f"se os valores forem texto.")
        df = df[~invalidos_evento]

    df[coluna_tempo] = pd.to_numeric(df[coluna_tempo], errors="coerce")
    invalidos_tempo = df[coluna_tempo].isna() | (df[coluna_tempo] <= 0)
    if invalidos_tempo.any():
        if verbose:
            print(f"[sobrevivencia] {int(invalidos_tempo.sum())} linhas com "
                  f"tempo nulo ou <= 0 removidas.")
        df = df[~invalidos_tempo]

    df[coluna_evento] = df[coluna_evento].astype(int)
    df = df.reset_index(drop=True)

    if verbose:
        n = len(df)
        n_eventos = int(df[coluna_evento].sum())
        taxa_censura = 1 - n_eventos / max(n, 1)
        print(f"[sobrevivencia] n={n:,} | eventos={n_eventos:,} | "
              f"censura={taxa_censura:.1%} | "
              f"tempo: mediana={df[coluna_tempo].median():.1f}, "
              f"max={df[coluna_tempo].max():.1f}")
        if taxa_censura > 0.9:
            print("[sobrevivencia] censura > 90% — mediana de sobrevivência "
                  "pode não ser estimável; interprete com cautela.")
    return df


def kaplan_meier(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    *,
    coluna_grupo: str | None = None,
    intervalo_confianca: bool = True,
    marcar_censuras: bool = True,
    tabela_risco: bool = False,
    plotar: bool = True,
    salvar_em: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Curva(s) de Kaplan-Meier com medianas, IC e log-rank automático.

    - Sem `coluna_grupo`: uma curva geral;
    - Com `coluna_grupo`: uma curva por grupo + teste de log-rank
      (multivariado se 3+ grupos) comparando as curvas.

    Parameters
    ----------
    marcar_censuras : bool
        Desenha ticks nos tempos de censura (boa prática de publicação).
    tabela_risco : bool
        Adiciona a tabela "at risk" sob o gráfico (padrão em papers médicos).

    Returns
    -------
    dict com: `ajustes` ({grupo: KaplanMeierFitter}), `medianas`
    ({grupo: mediana com IC}), e `logrank` (estatística e p-valor, se grupos).

    Examples
    --------
    >>> res = kaplan_meier(df, "tempo", "evento", coluna_grupo="cluster")
    >>> res["medianas"]
    """
    _checar_lifelines()
    from lifelines import KaplanMeierFitter
    from lifelines.statistics import logrank_test, multivariate_logrank_test
    from lifelines.utils import median_survival_times
    from lifelines.plotting import add_at_risk_counts

    dados = df[[coluna_tempo, coluna_evento] +
               ([coluna_grupo] if coluna_grupo else [])].dropna()

    resultado: dict[str, Any] = {"ajustes": {}, "medianas": {}}
    fig = ax = None
    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots(figsize=(9, 6))

    if coluna_grupo:
        grupos = list(dados.groupby(coluna_grupo, observed=True))
    else:
        grupos = [("geral", dados)]

    for nome, sub in grupos:
        kmf = KaplanMeierFitter(label=str(nome))
        kmf.fit(sub[coluna_tempo], event_observed=sub[coluna_evento])
        resultado["ajustes"][str(nome)] = kmf
        mediana = kmf.median_survival_time_
        ic_med = median_survival_times(kmf.confidence_interval_)
        resultado["medianas"][str(nome)] = {
            "mediana": float(mediana) if np.isfinite(mediana) else np.inf,
            "ic": (float(ic_med.iloc[0, 0]), float(ic_med.iloc[0, 1])),
            "n": len(sub), "eventos": int(sub[coluna_evento].sum()),
        }
        if ax is not None:
            kmf.plot_survival_function(ax=ax, ci_show=intervalo_confianca,
                                       show_censors=marcar_censuras,
                                       censor_styles={"ms": 5, "marker": "|"})

    if coluna_grupo and len(grupos) >= 2:
        if len(grupos) == 2:
            (_, g1), (_, g2) = grupos
            lr = logrank_test(g1[coluna_tempo], g2[coluna_tempo],
                              g1[coluna_evento], g2[coluna_evento])
        else:
            lr = multivariate_logrank_test(dados[coluna_tempo],
                                           dados[coluna_grupo],
                                           dados[coluna_evento])
        resultado["logrank"] = {"estatistica": float(lr.test_statistic),
                                "p_valor": float(lr.p_value)}

    if verbose:
        for nome, info in resultado["medianas"].items():
            med = info["mediana"]
            med_txt = f"{med:.1f}" if np.isfinite(med) else "não atingida"
            print(f"[km] {nome}: n={info['n']:,} eventos={info['eventos']:,} "
                  f"| mediana={med_txt} IC95%=[{info['ic'][0]:.1f}, "
                  f"{info['ic'][1]:.1f}]")
        if "logrank" in resultado:
            p = resultado["logrank"]["p_valor"]
            sig = "SIGNIFICATIVO" if p < 0.05 else "não significativo"
            print(f"[km] log-rank: p={p:.4g} ({sig}) — curvas "
                  f"{'diferem' if p < 0.05 else 'não diferem'} entre grupos")

    if ax is not None:
        ax.set(xlabel=coluna_tempo, ylabel="S(t) — prob. de sobrevivência",
               title="Curvas de Kaplan-Meier", ylim=(0, 1.02))
        if tabela_risco:
            add_at_risk_counts(*resultado["ajustes"].values(), ax=ax)
        fig.tight_layout()
        if salvar_em:
            Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(salvar_em, bbox_inches="tight")
        resultado["fig"] = fig
    return resultado


def risco_acumulado(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    *,
    coluna_grupo: str | None = None,
    plotar: bool = True,
    salvar_em: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Risco acumulado de Nelson-Aalen H(t) — o complemento do Kaplan-Meier.

    Como ler a curva: a INCLINAÇÃO é a taxa de risco instantânea.
    - Reta -> risco constante (compatível com Exponencial);
    - Côncava p/ cima -> risco crescente (Weibull com k > 1, envelhecimento);
    - Côncava p/ baixo -> risco decrescente (Weibull com k < 1).

    Ótimo diagnóstico visual ANTES de escolher o modelo paramétrico.

    Examples
    --------
    >>> risco_acumulado(df, "tempo", "evento", coluna_grupo="tratamento")
    """
    _checar_lifelines()
    from lifelines import NelsonAalenFitter

    dados = df[[coluna_tempo, coluna_evento] +
               ([coluna_grupo] if coluna_grupo else [])].dropna()
    resultado: dict[str, Any] = {"ajustes": {}}
    fig = ax = None
    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots(figsize=(9, 6))

    grupos = (list(dados.groupby(coluna_grupo, observed=True))
              if coluna_grupo else [("geral", dados)])
    for nome, sub in grupos:
        naf = NelsonAalenFitter(label=str(nome))
        naf.fit(sub[coluna_tempo], event_observed=sub[coluna_evento])
        resultado["ajustes"][str(nome)] = naf
        if ax is not None:
            naf.plot_cumulative_hazard(ax=ax)
        if verbose:
            h_final = float(naf.cumulative_hazard_.iloc[-1, 0])
            print(f"[nelson_aalen] {nome}: H(t_max)={h_final:.3f}")

    if ax is not None:
        ax.set(xlabel=coluna_tempo, ylabel="H(t) — risco acumulado",
               title="Risco acumulado (Nelson-Aalen)")
        fig.tight_layout()
        if salvar_em:
            Path(salvar_em).parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(salvar_em, bbox_inches="tight")
        resultado["fig"] = fig
    return resultado


def cox_ph(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    covariaveis: Sequence[str] | None = None,
    *,
    penalizador: float = 0.0,
    verificar_premissas: bool = True,
    plotar: bool = True,
    verbose: bool = True,
) -> Any:
    """Regressão de Cox (riscos proporcionais) com relatório interpretado.

    Para cada covariável reporta o hazard ratio exp(coef) com IC95%:
    - HR > 1 -> aumenta o risco (evento acontece mais cedo);
    - HR < 1 -> protege (evento demora mais);
    - HR = 1.20 lê-se "+20% de risco por unidade da covariável".

    Também reporta o índice de concordância (C-index): 0.5 = aleatório,
    >0.7 = bom poder discriminativo.

    Parameters
    ----------
    covariaveis : sequence of str, optional
        Padrão: todas as colunas numéricas exceto tempo/evento.
        Categóricas devem ser codificadas antes (`codificar_categoricas`).
    penalizador : float
        Regularização L2 leve (ex.: 0.1) estabiliza coeficientes quando há
        covariáveis correlacionadas ou poucos eventos.
    verificar_premissas : bool
        Roda o teste de riscos proporcionais (Schoenfeld). Violações
        indicam usar estratificação ou termos tempo-dependentes.

    Returns
    -------
    lifelines.CoxPHFitter ajustado — use `.summary`, `.predict_median(X)`,
    `.predict_survival_function(X)`.

    Examples
    --------
    >>> cph = cox_ph(df, "tempo", "evento", ["idade", "estagio", "biomarcador"])
    >>> cph.predict_median(df_novos_pacientes)
    """
    _checar_lifelines()
    from lifelines import CoxPHFitter

    if covariaveis is None:
        covariaveis = [c for c in df.select_dtypes(include=np.number).columns
                       if c not in (coluna_tempo, coluna_evento)]
    dados = df[[coluna_tempo, coluna_evento, *covariaveis]].dropna()

    cph = CoxPHFitter(penalizer=penalizador)
    cph.fit(dados, duration_col=coluna_tempo, event_col=coluna_evento)

    if verbose:
        n_eventos = int(dados[coluna_evento].sum())
        epv = n_eventos / max(len(covariaveis), 1)
        print(f"[cox] n={len(dados):,} | eventos={n_eventos:,} | "
              f"C-index={cph.concordance_index_:.3f} | "
              f"eventos/covariável={epv:.1f}")
        if epv < 10:
            print("[cox] aviso: <10 eventos por covariável — risco de "
                  "overfitting; considere penalização ou menos variáveis.")
        resumo = cph.summary[["exp(coef)", "exp(coef) lower 95%",
                              "exp(coef) upper 95%", "p"]]
        resumo.columns = ["HR", "HR_ic_inf", "HR_ic_sup", "p_valor"]
        print(resumo.round(4).to_string())

    if verificar_premissas:
        try:
            from lifelines.statistics import proportional_hazard_test
            teste = proportional_hazard_test(cph, dados, time_transform="rank")
            violacoes = teste.summary[teste.summary["p"] < 0.05].index.tolist()
            if verbose:
                if violacoes:
                    print(f"[cox] PREMISSA VIOLADA (riscos não proporcionais) "
                          f"em: {violacoes} — considere estratificar ou usar "
                          f"efeito tempo-dependente.")
                else:
                    print("[cox] premissa de riscos proporcionais ok "
                          "(Schoenfeld, alpha=0.05)")
        except Exception as e:
            if verbose:
                print(f"[cox] teste de premissas falhou: {e}")

    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots(figsize=(8, max(3, 0.5 * len(covariaveis))))
        cph.plot(ax=ax)  # forest plot dos log(HR)
        ax.set_title("Coeficientes de Cox — log(HR) com IC95%")
        fig.tight_layout()
    return cph


def cox_lasso(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    covariaveis: Sequence[str] | None = None,
    *,
    penalizadores: Sequence[float] | None = None,
    l1_ratio: float = 1.0,
    cv: int = 5,
    escalar_antes: bool = True,
    random_state: int = 42,
    plotar: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """Cox penalizado (LASSO/Elastic-Net) com seleção do penalizador por
    validação cruzada — seleção de variáveis em alta dimensão (p >> n).

    É exatamente o framework Cox-LASSO usado em genômica (ex.: TCGA):
    o L1 zera coeficientes de covariáveis irrelevantes, entregando um
    subconjunto esparso e interpretável.

    Notas metodológicas importantes:
    - as covariáveis são padronizadas por default (obrigatório p/ penalização
      justa entre escalas diferentes);
    - o C-index de validação cruzada é a métrica de seleção — mais honesto
      que o C-index de treino;
    - `l1_ratio=1.0` é LASSO puro; valores em (0,1) dão Elastic-Net, que
      lida melhor com genes/variáveis fortemente correlacionados.

    Parameters
    ----------
    penalizadores : sequence of float, optional
        Grade de penalização testada (padrão: 10^-3 a 10^1, 9 valores).

    Returns
    -------
    dict com:
    - "modelo"       : CoxPHFitter final ajustado com o melhor penalizador;
    - "melhor_penalizador", "cindex_cv" : resultado da busca;
    - "selecionadas" : covariáveis com coeficiente != 0;
    - "trajetoria"   : DataFrame penalizador x C-index médio.

    Examples
    --------
    >>> res = cox_lasso(df, "tempo_meses", "obito", lista_500_genes)
    >>> res["selecionadas"]
    ['gene_BRCA1', 'gene_TP53', 'idade']
    """
    _checar_lifelines()
    from lifelines import CoxPHFitter
    from lifelines.utils import k_fold_cross_validation

    if covariaveis is None:
        covariaveis = [c for c in df.select_dtypes(include=np.number).columns
                       if c not in (coluna_tempo, coluna_evento)]
    dados = df[[coluna_tempo, coluna_evento, *covariaveis]].dropna().copy()

    if escalar_antes:
        from sklearn.preprocessing import StandardScaler
        dados[list(covariaveis)] = StandardScaler().fit_transform(
            dados[list(covariaveis)])

    if penalizadores is None:
        penalizadores = np.logspace(-3, 1, 9)

    linhas = []
    for pen in penalizadores:
        cph = CoxPHFitter(penalizer=pen, l1_ratio=l1_ratio)
        try:
            scores = k_fold_cross_validation(
                cph, dados, duration_col=coluna_tempo,
                event_col=coluna_evento, k=cv,
                scoring_method="concordance_index", seed=random_state)
            linhas.append({"penalizador": float(pen),
                           "cindex_cv": float(np.mean(scores)),
                           "cindex_std": float(np.std(scores))})
        except Exception as e:
            if verbose:
                print(f"[cox_lasso] penalizador={pen:.4g} falhou: {e}")
    trajetoria = pd.DataFrame(linhas)
    if trajetoria.empty:
        raise RuntimeError("Nenhum penalizador convergiu — confira os dados.")

    melhor = trajetoria.loc[trajetoria["cindex_cv"].idxmax()]
    modelo = CoxPHFitter(penalizer=float(melhor["penalizador"]),
                         l1_ratio=l1_ratio)
    modelo.fit(dados, duration_col=coluna_tempo, event_col=coluna_evento)

    coefs = modelo.params_
    selecionadas = coefs[coefs.abs() > 1e-6].index.tolist()

    if verbose:
        tipo = "LASSO" if l1_ratio == 1.0 else f"Elastic-Net (l1={l1_ratio})"
        print(f"[cox_lasso:{tipo}] melhor penalizador="
              f"{melhor['penalizador']:.4g} | C-index CV="
              f"{melhor['cindex_cv']:.3f} (+/-{melhor['cindex_std']:.3f})")
        print(f"[cox_lasso] {len(selecionadas)}/{len(covariaveis)} "
              f"covariáveis selecionadas: {selecionadas}")

    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots()
        ax.errorbar(trajetoria["penalizador"], trajetoria["cindex_cv"],
                    yerr=trajetoria["cindex_std"], marker="o", capsize=3)
        ax.axvline(melhor["penalizador"], ls="--", color="tomato",
                   label=f"melhor = {melhor['penalizador']:.3g}")
        ax.set_xscale("log")
        ax.set(xlabel="penalizador (escala log)", ylabel="C-index (CV)",
               title="Seleção do penalizador — Cox penalizado")
        ax.legend()
        fig.tight_layout()

    return {"modelo": modelo,
            "melhor_penalizador": float(melhor["penalizador"]),
            "cindex_cv": float(melhor["cindex_cv"]),
            "selecionadas": selecionadas,
            "trajetoria": trajetoria}


def modelos_parametricos_sobrevivencia(
    df: pd.DataFrame,
    coluna_tempo: str,
    coluna_evento: str,
    *,
    modelos: Sequence[str] = ("exponencial", "weibull", "lognormal",
                              "loglogistico"),
    plotar: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Ajusta modelos paramétricos de sobrevivência e compara por AIC.

    Modelos e o que cada um assume sobre a taxa de risco h(t):
    - "exponencial"  : risco CONSTANTE no tempo (sem memória);
    - "weibull"      : risco monotônico — crescente (rho>1) ou decrescente
                       (rho<1); generaliza a exponencial;
    - "lognormal"    : risco sobe e depois cai (não monotônico);
    - "loglogistico" : similar à lognormal, caudas mais pesadas.

    Menor AIC = melhor equilíbrio ajuste x complexidade (diferenças < 2
    são empate técnico). Compare também a curva de cada modelo contra o
    Kaplan-Meier no gráfico — AIC bom com curva descolada do KM é alerta.

    Returns
    -------
    DataFrame: modelo, AIC, log_likelihood, parametros, mediana_prevista
    (ordenado por AIC). O objeto ajustado fica na coluna "ajuste".

    Examples
    --------
    >>> ranking = modelos_parametricos_sobrevivencia(df, "tempo", "evento")
    >>> melhor = ranking.iloc[0]["ajuste"]
    >>> melhor.predict(np.array([12, 24, 60]))   # S(t) nesses tempos
    """
    _checar_lifelines()
    from lifelines import (ExponentialFitter, WeibullFitter,
                           LogNormalFitter, LogLogisticFitter,
                           KaplanMeierFitter)

    mapa = {"exponencial": ExponentialFitter, "weibull": WeibullFitter,
            "lognormal": LogNormalFitter, "loglogistico": LogLogisticFitter}
    dados = df[[coluna_tempo, coluna_evento]].dropna()
    T, E = dados[coluna_tempo], dados[coluna_evento]

    linhas = []
    for nome in modelos:
        if nome not in mapa:
            warnings.warn(f"Modelo desconhecido: '{nome}'")
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ajuste = mapa[nome]().fit(T, event_observed=E, label=nome)
            params = {k: round(float(v), 4)
                      for k, v in ajuste.params_.items()}
            linhas.append({"modelo": nome, "AIC": float(ajuste.AIC_),
                           "log_likelihood": float(ajuste.log_likelihood_),
                           "parametros": params,
                           "mediana_prevista": float(ajuste.median_survival_time_),
                           "ajuste": ajuste})
        except Exception as e:
            if verbose:
                print(f"[parametricos] '{nome}' falhou: {e}")

    res = pd.DataFrame(linhas).sort_values("AIC").reset_index(drop=True)
    if verbose and not res.empty:
        print(res.drop(columns="ajuste").round(3).to_string(index=False))
        melhor = res.iloc[0]
        print(f"[parametricos] melhor por AIC: {melhor['modelo']} "
              f"(AIC={melhor['AIC']:.1f})")

    if plotar and _TEM_PLOT and not res.empty:
        fig, ax = plt.subplots(figsize=(9, 6))
        km = KaplanMeierFitter(label="Kaplan-Meier (referência)")
        km.fit(T, event_observed=E)
        km.plot_survival_function(ax=ax, ci_show=False, color="white"
                                  if plt.rcParams["figure.facecolor"] in
                                  ("black", "#000000") else "black",
                                  lw=2.5, ls=":")
        for _, linha in res.iterrows():
            linha["ajuste"].plot_survival_function(ax=ax, ci_show=False)
        ax.set(xlabel=coluna_tempo, ylabel="S(t)",
               title="Modelos paramétricos vs Kaplan-Meier", ylim=(0, 1.02))
        fig.tight_layout()
    return res


def prever_sobrevivencia(
    modelo: Any,
    X_novos: pd.DataFrame,
    *,
    tempos: Sequence[float] | None = None,
    plotar: bool = True,
    max_curvas_plot: int = 12,
    verbose: bool = True,
) -> pd.DataFrame:
    """Prevê curvas de sobrevivência individuais com um modelo de Cox ajustado.

    Recebe novos indivíduos (mesmas covariáveis do treino) e retorna S(t)
    para cada um — a base de aplicações como "probabilidade de o imóvel
    ainda estar à venda em 90 dias" ou "sobrevida em 5 anos do paciente".

    Parameters
    ----------
    modelo : CoxPHFitter
        Modelo ajustado por `cox_ph` ou `cox_lasso(...)['modelo']`.
    tempos : sequence of float, optional
        Tempos específicos de interesse (ex.: [30, 90, 180]). Padrão: grade
        do próprio modelo.

    Returns
    -------
    DataFrame: linhas = tempos, colunas = indivíduos, valores = S(t).

    Examples
    --------
    >>> curvas = prever_sobrevivencia(cph, df_novos, tempos=[30, 90, 180])
    >>> medianas = cph.predict_median(df_novos)   # tempo mediano por indivíduo
    """
    _checar_lifelines()
    curvas = modelo.predict_survival_function(
        X_novos, times=list(tempos) if tempos is not None else None)
    if verbose:
        print(f"[prever] S(t) para {curvas.shape[1]} indivíduos em "
              f"{curvas.shape[0]} tempos")
        if tempos is not None:
            print(curvas.round(3).to_string())
    if plotar and _TEM_PLOT:
        fig, ax = plt.subplots(figsize=(9, 6))
        curvas.iloc[:, :max_curvas_plot].plot(ax=ax, legend=len(
            curvas.columns) <= max_curvas_plot)
        ax.set(xlabel="tempo", ylabel="S(t)",
               title="Curvas de sobrevivência previstas", ylim=(0, 1.02))
        fig.tight_layout()
    return curvas


# ==============================================================================
# __all__ — API pública do módulo
# ==============================================================================
__all__ = [
    # 1. Ingestão
    "carregar_dados", "carregar_multiplos", "carregar_sql", "carregar_api",
    "salvar_dados",
    # 2. ETL
    "relatorio_qualidade", "limpar_nomes_colunas", "converter_tipos",
    "tratar_nulos", "tratar_duplicados", "remover_outliers",
    "otimizar_memoria", "codificar_categoricas", "escalar",
    "criar_features_data", "padronizar_texto", "criar_faixas", "mesclar_seguro",
    # 3. EDA
    "resumo_geral", "analise_correlacao", "testar_normalidade",
    "comparar_grupos", "analise_categorica", "analise_univariada",
    "analise_alvo",
    # 4. Gráficos
    "configurar_estilo", "plot_distribuicao", "plot_correlacao",
    "plot_categorico", "plot_dispersao", "plot_boxplots_grupo",
    "plot_serie_temporal", "plot_nulos", "plot_qq", "plot_pares",
    # 5. ML
    "preparar_dados", "pipeline_preprocessamento", "avaliar_classificacao",
    "avaliar_regressao", "comparar_modelos", "otimizar_hiperparametros",
    "importancia_features", "avaliar_clustering", "salvar_modelo",
    "carregar_modelo", "curva_aprendizado", "selecionar_features",
    "reduzir_dimensionalidade", "balancear_classes",
    # 6. Matemática / Estatística
    "intervalo_confianca", "bootstrap_estatistica", "tamanho_amostra",
    "teste_ab", "correlacao_com_p", "calcular_vif", "ajustar_distribuicao",
    "estatisticas_robustas", "derivada_numerica", "integral_numerica",
    # 7. Análise de Sobrevivência
    "preparar_sobrevivencia", "kaplan_meier", "risco_acumulado",
    "cox_ph", "cox_lasso", "modelos_parametricos_sobrevivencia",
    "prever_sobrevivencia",
]

if __name__ == "__main__":
    print(__doc__)
    print(f"{len(__all__)} funções disponíveis:")
    for nome in __all__:
        print(f"  - {nome}")
