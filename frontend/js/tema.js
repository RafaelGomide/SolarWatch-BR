/* Alternância de tema. O escuro é o padrão; a escolha fica no localStorage. */

const CHAVE = "solarwatch:tema";

export function temaAtual() {
  return document.documentElement.dataset.tema || "escuro";
}

export function aplicarTema(tema) {
  document.documentElement.dataset.tema = tema;
  try { localStorage.setItem(CHAVE, tema); } catch { /* modo privado: segue com o padrão */ }
  const botao = document.querySelector("[data-alternar-tema]");
  if (botao) {
    const escuro = tema === "escuro";
    botao.textContent = escuro ? "☾ Escuro" : "☀ Claro";
    botao.setAttribute("aria-label", `Tema ${escuro ? "escuro" : "claro"}; clique para alternar`);
    botao.setAttribute("aria-pressed", String(escuro));
  }
  document.dispatchEvent(new CustomEvent("tema-alterado", { detail: { tema } }));
}

export function iniciarTema() {
  let salvo = null;
  try { salvo = localStorage.getItem(CHAVE); } catch { /* ignora */ }
  aplicarTema(salvo === "claro" ? "claro" : "escuro");   // escuro é o padrão

  document.querySelector("[data-alternar-tema]")
    ?.addEventListener("click", () => aplicarTema(temaAtual() === "escuro" ? "claro" : "escuro"));
}
