/* Cliente da API SolarWatch BR + utilitários de formatação. */

// Servido pelo backend em /app -> mesma origem. Aberto direto do disco -> localhost:8000.
export const BASE_API = location.protocol === "file:"
  ? "http://localhost:8000/api/v1"
  : `${location.origin}/api/v1`;

/** GET com tratamento do Problem Details (RFC 9457) da API. */
export async function buscar(caminho, parametros = {}) {
  const url = new URL(BASE_API + caminho, location.origin);
  for (const [chave, valor] of Object.entries(parametros)) {
    if (valor !== null && valor !== undefined && valor !== "") url.searchParams.set(chave, valor);
  }

  let resposta;
  try {
    resposta = await fetch(url, { headers: { Accept: "application/json" } });
  } catch {
    throw new ErroApi("Não foi possível falar com a API. Ela está rodando?", 0);
  }

  const corpo = await resposta.json().catch(() => null);
  if (!resposta.ok) {
    const detalhe = corpo?.detail || `Erro ${resposta.status}`;
    throw new ErroApi(detalhe, resposta.status, corpo?.title);
  }
  return corpo;
}

export class ErroApi extends Error {
  constructor(mensagem, status, titulo) {
    super(mensagem);
    this.status = status;
    this.titulo = titulo || (status === 0 ? "API indisponível" : "Erro");
  }
}

/* ------------------------------- formatação ------------------------------- */
const NUM = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const NUM1 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
// Contagens pequenas mantêm sempre uma casa: "2,0 manutenções" ao lado de "0,5"
// lê-se como a mesma grandeza; "2" ao lado de "0,5" parece outra coisa.
const NUM_FIXO1 = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

export const fmt = {
  inteiro: (v) => (v == null ? "—" : NUM.format(v)),
  decimal: (v) => (v == null ? "—" : NUM1.format(v)),
  contagem: (v) => (v == null ? "—" : NUM_FIXO1.format(v)),
  /** MWh -> escala automática (MWh / GWh / TWh). */
  energia(mwh) {
    if (mwh == null) return { valor: "—", unidade: "" };
    if (Math.abs(mwh) >= 1e6) return { valor: NUM1.format(mwh / 1e6), unidade: "TWh" };
    if (Math.abs(mwh) >= 1e3) return { valor: NUM1.format(mwh / 1e3), unidade: "GWh" };
    return { valor: NUM.format(mwh), unidade: "MWh" };
  },
  potencia: (mw) => (mw == null ? "—" : mw >= 1000 ? `${NUM1.format(mw / 1000)} GW` : `${NUM1.format(mw)} MW`),
  percentual: (fracao, casas = 0) =>
    fracao == null ? "—" : `${fracao.toLocaleString("pt-BR", { style: "percent", maximumFractionDigits: casas })}`,
  data: (iso) => (iso ? new Date(iso).toLocaleDateString("pt-BR") : "—"),
  dataHora: (iso) =>
    iso ? new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—",
  fonte: (f) => (f === "solar" ? "Solar" : f === "eolica" ? "Eólica" : f ?? "—"),
  tipoUnidade: (t) => TIPOS_UNIDADE[t]?.rotulo ?? t ?? "—",
};

/* Os três grãos que o ONS mistura na mesma lista. O agregado estadual não é
   uma usina: é a soma da geração distribuída de um estado, e por isso não tem
   cadastro na ANEEL — nem vai ter. */
export const TIPOS_UNIDADE = {
  usina: { rotulo: "usina", ajuda: "Usina individual medida pelo ONS" },
  conjunto: { rotulo: "conjunto", ajuda: "Conjunto de usinas: o ONS mede a soma do complexo" },
  pequenas_usinas: {
    rotulo: "agregado estadual",
    ajuda: "Soma da geração distribuída (MMGD / Tipo III) de um estado. Não é uma usina, "
         + "então não tem cadastro na ANEEL: sem potência, coordenadas nem data de operação.",
  },
};

/** Etiqueta do grão da unidade, com a explicação no title. */
export function etiquetaTipo(tipo) {
  const { rotulo, ajuda } = TIPOS_UNIDADE[tipo] ?? { rotulo: tipo, ajuda: "" };
  return `<span class="etiqueta etiqueta--neutra" title="${escapar(ajuda)}">${escapar(rotulo)}</span>`;
}

/** Etiqueta colorida por fonte de energia. */
export function etiquetaFonte(fonte) {
  return `<span class="etiqueta etiqueta--${fonte === "solar" ? "solar" : "eolica"}">${fmt.fonte(fonte)}</span>`;
}

/** Escapa texto vindo da API antes de interpolar em innerHTML. */
export function escapar(texto) {
  return String(texto ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/** Mostra o erro dentro de um contêiner, no lugar do conteúdo. */
export function mostrarErro(elemento, erro) {
  elemento.innerHTML =
    `<div class="erro" role="alert"><strong>${escapar(erro.titulo)}</strong><br>${escapar(erro.message)}</div>`;
}
