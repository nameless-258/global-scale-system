/* Global-Scale System — consola de operação (interface web).
 * Fala só com o gateway, na mesma origem: /auth/*, a API REST e /controlo/* (admin).
 * Tudo o que vem do servidor é inserido com textContent (nunca innerHTML). */
"use strict";

const $ = (id) => document.getElementById(id);
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
const hora = () => new Date().toLocaleTimeString("pt-PT");
const SVGNS = "http://www.w3.org/2000/svg";
const semMovimento = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const fmt = (v) => Number(v).toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const CLIENTES_TESTE = { ana: "Ana@2026", diego: "Diego@2026" };   // usados só pelos cenários automáticos

// ------------------------------------------------------------------ DOM
function el(tag, attrs = {}, ...filhos) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const f of filhos.flat()) {
    if (f === null || f === undefined || f === false) continue;
    e.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return e;
}
function svg(tag, attrs = {}, ...filhos) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  for (const f of filhos) e.append(f instanceof Node ? f : document.createTextNode(String(f)));
  return e;
}
const limpar = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

// ------------------------------------------------------------------ sessão (uma só: cliente OU administrador)
let sessao = null;
try { sessao = JSON.parse(sessionStorage.getItem("gss-sessao") || "null"); } catch (_) {}
function guardarSessao() { try { sessionStorage.setItem("gss-sessao", JSON.stringify(sessao)); } catch (_) {} }
function cargaJWT(token) {
  const p = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(p + "===".slice((p.length + 3) % 4));
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
}
const sessaoValida = () => !!(sessao && sessao.token && sessao.exp * 1000 > Date.now() + 5000);
const ehAdmin = () => sessaoValida() && sessao.papel === "admin";
const ehCliente = () => sessaoValida() && sessao.papel !== "admin";
const tokenAdmin = () => (ehAdmin() ? sessao.token : null);
const tokenCliente = () => (ehCliente() ? sessao.token : null);
const tokenQualquer = () => (sessaoValida() ? sessao.token : null);
const iniciais = (nome) => nome.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]).join("").toUpperCase();

function aplicarSessao() {
  const ok = sessaoValida();
  const papel = ok ? sessao.papel : null;
  $("pil-ini").textContent = ok ? iniciais(sessao.nome) : "?";
  $("pil-nome").textContent = ok ? sessao.nome : "Entrar";
  $("pil-papel").textContent = ok ? (papel === "admin" ? "Supervisão do sistema" : "Cliente") : "ninguém com sessão";
  $("pilula").classList.toggle("ligada", ok);
  $("pilula").classList.toggle("admin", ok && papel === "admin");
  $("pil-menu").hidden = true;
  document.querySelectorAll("#menu [data-papel]").forEach((e) => {
    const p = e.dataset.papel;
    e.hidden = !ok || !(p === "ambos" || (p === "admin") === (papel === "admin"));
  });
  $("form-taxa").hidden = !ehAdmin();
  $("mapa-dica").textContent = ehAdmin() ? "Clique num nó para o parar, ligar ou matar."
    : "Só o administrador pode derrubar nós.";
  dadosCliente = null; totalInicial = null; depositosSessao = 0; clientesAdmin = [];
  mostrarLivroVazio();
  irPara(!ok ? "entrada" : papel === "admin" ? "clientes" : "inicio");
  definirModo("interna");
  if (ehCliente()) carregarDadosCliente();
  if (ehAdmin()) atualizarAdmin();
}
$("pilula").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!sessaoValida()) return abrirLogin();
  const m = $("pil-menu");
  m.hidden = !m.hidden;
  $("pilula").setAttribute("aria-expanded", String(!m.hidden));
  $("pil-menu-info").textContent = `Sessão de ${sessao.user}, válida até às ${new Date(sessao.exp * 1000).toLocaleTimeString("pt-PT").slice(0, 5)}.`;
});
document.addEventListener("click", (e) => { if (!$("pil-menu").hidden && !$("utilizador").contains(e.target)) $("pil-menu").hidden = true; });
$("btn-sair").addEventListener("click", () => {
  evento(`${sessao.nome} terminou a sessão`);
  sessao = null; guardarSessao(); aplicarSessao(); abrirLogin();
});

// ------------------------------------------------------------------ HTTP
async function chamar(metodo, caminho, { token, corpo, consulta } = {}) {
  const url = new URL(caminho, location.origin);
  for (const [k, v] of Object.entries(consulta || {})) if (v) url.searchParams.set(k, v);
  const cab = {};
  if (token) cab.Authorization = "Bearer " + token;
  if (corpo !== undefined) cab["Content-Type"] = "application/json";
  const t0 = performance.now();
  try {
    const r = await fetch(url, { method: metodo, headers: cab, cache: "no-store",
      body: corpo !== undefined ? JSON.stringify(corpo) : undefined });
    const texto = await r.text();
    let dados = texto;
    try { dados = JSON.parse(texto); } catch (_) { /* CSV ou página de erro do gateway */ }
    return { status: r.status, ok: r.ok, no: r.headers.get("X-No-Servidor"), sha: r.headers.get("X-SHA256"),
      dados, ms: Math.round(performance.now() - t0), metodo, caminho: url.pathname + url.search };
  } catch (e) {
    return { status: 0, ok: false, no: null, dados: { erro: "SEM_RESPOSTA", detalhe: "A ligação foi cortada antes de haver resposta." },
      ms: Math.round(performance.now() - t0), metodo, caminho: url.pathname };
  }
}

// ------------------------------------------------------------------ mensagens com hash
async function sha256Hex(texto) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(texto));
  return Array.from(new Uint8Array(b), (x) => x.toString(16).padStart(2, "0")).join("");
}
/* Igual ao servidor: json.dumps(sort_keys=True, separators=(",", ":")) */
function canonico(obj) { const o = {}; for (const k of Object.keys(obj).sort()) o[k] = obj[k]; return JSON.stringify(o); }
const agoraISO = (d = 0) => new Date(Date.now() + d).toISOString().replace(/\.\d{3}Z$/, "Z");
function formatarValor(v) {
  const n = Number(String(v).replace(",", "."));
  if (!isFinite(n) || n <= 0) throw new Error("Escreva um valor maior do que zero.");
  return n.toFixed(2);
}
async function msgTransferencia(origem, destino, valor, d = 0) {
  const c = { id: crypto.randomUUID(), origem: origem.trim().toUpperCase(), destino: destino.trim().toUpperCase(),
    valor: formatarValor(valor), timestamp: agoraISO(d) };
  return { ...c, hash_sha256: await sha256Hex(canonico(c)) };
}
async function msgOrdem(conta, banco, destino, valor, tipo = "IBAN") {
  const c = { id: crypto.randomUUID(), conta_origem: conta, banco_destino: banco,
    iban_destino: String(destino).trim().toUpperCase(), valor: formatarValor(valor), timestamp: agoraISO(), tipo_destino: tipo };
  return { ...c, hash_sha256: await sha256Hex(canonico(c)) };
}

// ------------------------------------------------------------------ recibos (respostas)
const ESTADOS = { CONFIRMADA: ["Confirmada", "ok"], CONFIRMADA_PENDENTE: ["Confirmada, fase 2 pendente", "aviso"],
  ABORTADA: ["Abortada", "erro"], COMPENSADA: ["Compensada", "aviso"], CONCLUIDA: ["Concluída", "ok"],
  ATUALIZADA_EM_TODAS_AS_REPLICAS: ["Alterada em todas as cópias", "ok"] };
const ROTULOS = { tx_id: "Transação", protocolo: "Protocolo", coordenador: "Coordenador", fragmento: "Fragmento",
  erro: "Código", detalhe: "Motivo", motivo: "Motivo", nota: "Nota", participantes_pendentes: "Participantes pendentes",
  moeda: "Moeda", mzn_por_unidade: "Meticais por unidade", ficheiro: "Ficheiro", gerado_por: "Gerado por",
  sha256: "SHA-256", integro: "Cadeia íntegra", linha: "Linha adulterada", total_linhas: "Linhas verificadas",
  total_equivalente_MZN: "Total em meticais" };
const MONO = new Set(["tx_id", "sha256", "ficheiro"]);

function selo(r) {
  const d = r.dados && typeof r.dados === "object" ? r.dados : {};
  if (d.estado && ESTADOS[d.estado]) return ESTADOS[d.estado];
  if (r.status === 0) return ["Sem resposta", "erro"];
  if (!r.ok) return [`Recusado (HTTP ${r.status})`, "erro"];
  return [`Aceite (HTTP ${r.status})`, "ok"];
}
function classeRasto(l) {
  if (/abort|rollback|falh|inacess|erro|rejeit/i.test(l)) return "mau";
  if (/decis/i.test(l)) return "decisao";
  if (/fase 2|commit prepared|compensa/i.test(l)) return "fase2";
  if (/fase 1|prepare|bloque|débito|debito|T1|T2/i.test(l)) return "fase1";
  return "";
}
function recibo(r, titulo) {
  const d = r.dados;
  const [txt, cls] = selo(r);
  const meta = [`${r.metodo} ${r.caminho}`, `${r.ms} ms`, r.no ? `respondido por ${r.no}` : null].filter(Boolean).join(", ");
  const caixa = el("article", { class: "recibo" },
    el("div", { class: "recibo-cab" }, el("h4", {}, titulo || "Resposta"), el("span", { class: "carimbo " + cls }, txt)));
  const corpo = el("div", { class: "recibo-corpo" }, el("div", { class: "recibo-meta" }, meta));
  if (d && typeof d === "object") {
    if (d.conversao) {
      corpo.append(el("div", { class: "recibo-num" },
        el("div", {}, el("small", {}, "Debitado"), el("strong", {}, d.conversao.debitado)),
        el("div", {}, el("small", {}, "Creditado"), el("strong", {}, d.conversao.creditado)),
        el("div", {}, el("small", {}, "Taxa aplicada"), el("strong", {}, d.conversao.taxa.replace(/^1 /, "1 ")))));
    }
    const dl = el("dl");
    for (const [k, rot] of Object.entries(ROTULOS)) {
      const v = d[k];
      if (v === undefined || v === null || (Array.isArray(v) && !v.length)) continue;
      dl.append(el("dt", {}, rot), el("dd", { class: MONO.has(k) ? "mono" : "" }, typeof v === "object" ? JSON.stringify(v) : String(v)));
    }
    if (dl.childNodes.length) corpo.append(dl);
    if (Array.isArray(d.rasto) && d.rasto.length) corpo.append(el("ol", { class: "rasto" }, d.rasto.map((l) => el("li", { class: classeRasto(l) }, l))));
    corpo.append(el("details", { class: "json" }, el("summary", {}, "Ver a resposta completa"), el("pre", { class: "json" }, JSON.stringify(d, null, 2))));
  } else if (d) {
    corpo.append(el("pre", { class: "json" }, String(d).slice(0, 5000)));
  }
  caixa.append(corpo);
  return caixa;
}
function mostrar(destino, r, titulo, manter = false) {
  const alvo = $(destino);
  if (!manter) limpar(alvo);
  alvo.prepend(recibo(r, titulo));
}
function avisar(destino, texto, acao) {
  limpar($(destino)).append(el("div", { class: "vazio-msg" }, el("div", {}, texto),
    acao ? el("button", { class: "discreto", type: "button", onclick: acao[1] }, acao[0]) : null));
}
const pedirLogin = ["Entrar", () => abrirLogin()];

// ------------------------------------------------------------------ menu lateral
document.querySelectorAll("#menu button").forEach((b) => b.addEventListener("click", () => irPara(b.dataset.sep)));
function irPara(sep) {
  document.querySelectorAll("#menu button").forEach((x) => x.classList.toggle("ativo", x.dataset.sep === sep));
  document.querySelectorAll("section.painel").forEach((s) => { s.hidden = s.id !== "sep-" + sep; });
  if (sep === "cambio") carregarTaxas();
  if (sep === "inicio" || sep === "transferir") carregarDadosCliente().then(() => sep === "inicio" && mostrarUltimos());
  if (sep === "extrato") carregarExtrato();
  if (sep === "clientes" || sep === "depositar") carregarClientes();
  if (sep === "bd") { carregarBD(); if (noBD) verNoBD(noBD); }
}
$("btn-entrar-grande").addEventListener("click", () => abrirLogin());

// ------------------------------------------------------------------ entrar
const dlg = $("dlg-login");
function abrirLogin(msg = "") {
  $("login-erro").textContent = msg;
  if (!dlg.open) dlg.showModal();
  $("login-user").focus();
}
$("btn-fechar-login").addEventListener("click", () => dlg.close());
$("btn-olho").addEventListener("click", () => {
  const p = $("login-pass"), ver = p.type === "password";
  p.type = ver ? "text" : "password";
  $("btn-olho").textContent = ver ? "Esconder" : "Mostrar";
  $("btn-olho").setAttribute("aria-label", ver ? "Esconder a palavra-passe" : "Mostrar a palavra-passe");
});
$("form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const user = $("login-user").value.trim(), pass = $("login-pass").value;
  const erro = $("login-erro");
  if (!user || !pass) { erro.textContent = "Escreva o utilizador e a palavra-passe."; return; }
  const b = $("btn-login"); b.disabled = true; b.textContent = "A verificar…"; erro.textContent = "";
  const r = await chamar("POST", "/auth/login", { corpo: { username: user, password: pass } });
  b.disabled = false; b.textContent = "Entrar";
  if (r.ok && r.dados.access_token) {
    const c = cargaJWT(r.dados.access_token);
    sessao = { user: c.sub, nome: c.nome || c.sub, papel: c.papel, token: r.dados.access_token, exp: c.exp };
    guardarSessao();
    $("login-pass").value = ""; $("login-pass").type = "password"; $("btn-olho").textContent = "Mostrar";
    dlg.close();
    evento(`${sessao.nome} entrou como ${c.papel === "admin" ? "administrador" : "cliente"}`, "ok");
    aplicarSessao();
    return;
  }
  $("login-pass").value = ""; $("login-pass").focus();
  erro.textContent = r.status === 401 ? "Utilizador ou palavra-passe incorretos."
    : r.status === 429 ? "Demasiadas tentativas erradas. Esta conta fica bloqueada durante 60 segundos."
    : r.status === 0 ? "Sem ligação ao servidor. Confirme que o sistema está ligado."
    : `O serviço de autenticação respondeu com erro (HTTP ${r.status}).`;
});

// ------------------------------------------------------------------ eventos (painel escuro)
function evento(msg, cls = "") {
  const lista = $("eventos");
  const vazio = lista.querySelector(".vazio"); if (vazio) vazio.remove();
  lista.prepend(el("li", { class: cls }, el("time", {}, hora().slice(0, 8)), el("span", {}, msg)));
  while (lista.children.length > 40) lista.lastChild.remove();
}

// ------------------------------------------------------------------ mapa da topologia
const NOS = {
  navegador: { x: 70, y: 220, nome: "Navegador", papel: "você" },
  gateway: { x: 215, y: 220, nome: "gateway", papel: "HTTPS, balanceia" },
  auth: { x: 215, y: 350, nome: "auth", papel: "emite JWT" },
  "api-1": { x: 395, y: 145, nome: "api-1", papel: "coordenador 2PC" },
  "api-2": { x: 395, y: 295, nome: "api-2", papel: "coordenador 2PC" },
  legacy: { x: 395, y: 430, nome: "legacy", papel: "mainframe Java" },
  "db-europa": { x: 625, y: 50, nome: "db-europa", papel: "" },
  "db-europa-r": { x: 865, y: 50, nome: "db-europa-r", papel: "" },
  sentinela: { x: 745, y: 110, nome: "sentinela", papel: "vigia primários" },
  "db-america": { x: 625, y: 170, nome: "db-america", papel: "" },
  "db-america-r": { x: 865, y: 170, nome: "db-america-r", papel: "" },
  "db-africa": { x: 625, y: 290, nome: "db-africa", papel: "" },
  "db-africa-r": { x: 865, y: 290, nome: "db-africa-r", papel: "" },
  "db-central": { x: 625, y: 362, nome: "db-central", papel: "" },
  "db-central-r": { x: 865, y: 362, nome: "db-central-r", papel: "" },
  namenode: { x: 625, y: 430, nome: "namenode", papel: "HDFS, metadados" },
  "datanode-1": { x: 775, y: 430, nome: "datanode-1", papel: "blocos" },
  "datanode-2": { x: 915, y: 430, nome: "datanode-2", papel: "blocos" },
};
const W = 128, H = 46;
const LIGACOES = [
  ["navegador", "gateway"], ["gateway", "auth"], ["gateway", "api-1"], ["gateway", "api-2"],
  ["api-1", "db-europa", "dados"], ["api-2", "db-europa", "dados"], ["api-1", "db-america", "dados"], ["api-2", "db-america", "dados"],
  ["api-1", "db-africa", "dados"], ["api-2", "db-africa", "dados"],
  ["api-1", "db-europa-r", "dados-r"], ["api-2", "db-europa-r", "dados-r"], ["api-1", "db-america-r", "dados-r"], ["api-2", "db-america-r", "dados-r"],
  ["api-1", "db-africa-r", "dados-r"], ["api-2", "db-africa-r", "dados-r"],
  ["api-1", "db-central", "dados"], ["api-2", "db-central", "dados"], ["api-1", "db-central-r", "dados-r"], ["api-2", "db-central-r", "dados-r"],
  ["db-europa", "db-europa-r", "replica"], ["db-america", "db-america-r", "replica"], ["db-africa", "db-africa-r", "replica"],
  ["db-central", "db-central-r", "replica"], ["sentinela", "db-central", "vigia"], ["sentinela", "db-central-r", "vigia"],
  ["sentinela", "db-europa", "vigia"], ["sentinela", "db-america", "vigia"], ["sentinela", "db-africa", "vigia"],
  ["sentinela", "db-europa-r", "vigia"], ["sentinela", "db-america-r", "vigia"], ["sentinela", "db-africa-r", "vigia"],
  ["api-1", "legacy"], ["api-2", "legacy"], ["api-2", "namenode"],
  ["namenode", "datanode-1"], ["namenode", "datanode-2"],
];
const mapa = $("mapa");
const elsLig = [], elsNo = {};
function construirMapa() {
  const gL = svg("g"), gN = svg("g"), gP = svg("g", { id: "pacotes" });
  for (const [a, b, tipo] of LIGACOES) {
    const A = NOS[a], B = NOS[b];
    const ln = svg("line", { x1: A.x, y1: A.y, x2: B.x, y2: B.y, class: "ligacao" + (tipo === "replica" ? " replica" : tipo === "vigia" ? " vigia" : "") });
    elsLig.push({ a, b, tipo, ln });
    gL.append(ln);
    if (tipo === "replica") gL.append(svg("text", { x: (A.x + B.x) / 2, y: A.y - 9, "text-anchor": "middle", class: "rot-ligacao" }, "WAL síncrono"));
  }
  for (const [id, n] of Object.entries(NOS)) {
    const g = svg("g", { class: "no desconhecido", transform: `translate(${n.x - W / 2},${n.y - H / 2})`, "data-no": id });
    g.append(svg("rect", { class: "corpo", width: W, height: H, rx: 7 }),
      svg("rect", { class: "coroa", x: 10, y: -3, width: W - 20, height: 4, rx: 2 }),
      svg("text", { class: "nome", x: 12, y: 20 }, n.nome),
      svg("text", { class: "papel", x: 12, y: 36 }, n.papel));
    if (id !== "navegador" && id !== "gateway") {
      g.setAttribute("tabindex", "0"); g.setAttribute("role", "button"); g.setAttribute("aria-label", `Nó ${n.nome}`);
      g.addEventListener("click", (ev) => abrirMenuNo(id, ev));
      g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); abrirMenuNo(id, ev); } });
    }
    elsNo[id] = g; gN.append(g);
  }
  mapa.append(gL, gN, gP);
}
function definirNo(id, cls, papel, primario = false) {
  const g = elsNo[id];
  g.setAttribute("class", `no ${cls}${primario ? " primario" : ""}${id !== "navegador" && id !== "gateway" && tokenAdmin() ? " clicavel" : ""}`);
  g.querySelector("text.papel").textContent = papel;
}
let saudeAtual = null, contentores = null;
const REGIOES = [["Europa", "db-europa", "db-europa-r"], ["América", "db-america", "db-america-r"], ["África", "db-africa", "db-africa-r"],
  ["Central", "db-central", "db-central-r"]];
const regiaoDoNo = (id) => (id.includes("europa") ? "Europa" : id.includes("africa") ? "África" : id.includes("central") ? "Central" : "América");
const regiaoDaConta = (c) => (c.startsWith("EU") ? "Europa" : c.startsWith("AF") ? "África" : "América");
function porRegiao(obj, regiao) { if (!obj) return null; const k = Object.keys(obj).find((x) => x.includes(regiao)); return k ? obj[k] : null; }
function primarioDe(regiao) { const f = saudeAtual && porRegiao(saudeAtual.fragmentos, regiao); return f && f.estado === "OK" ? f.primario_atual : null; }

function atualizarMapa() {
  const s = saudeAtual;
  const corre = (id) => (contentores ? (contentores[id] ? contentores[id].estado === "running" : null) : null);
  const base = (id, papel, okSaude) => {
    const c = corre(id);
    if (c === false) return definirNo(id, "falha", "em baixo");
    if (okSaude === false) return definirNo(id, "falha", "não responde");
    definirNo(id, c === null && okSaude === undefined ? "desconhecido" : "ok", papel);
  };
  definirNo("navegador", "ok", "você");
  definirNo("gateway", s ? "ok" : "aviso", s ? "HTTPS, balanceia" : "sem resposta da API");
  base("auth", "emite JWT");
  for (const a of ["api-1", "api-2"]) base(a, "coordenador 2PC", s && s.no === a ? true : undefined);
  base("sentinela", "vigia primários");
  base("legacy", "mainframe Java", s ? !(s.legado && s.legado.erro) : undefined);
  const dfs = s && s.dfs;
  base("namenode", "HDFS, metadados", dfs ? dfs.estado === "Operational" : undefined);
  for (const d of ["datanode-1", "datanode-2"]) base(d, "blocos");
  if (dfs && dfs.datanodes_mortos && contentores === null) for (const d of ["datanode-1", "datanode-2"]) definirNo(d, "aviso", "um DataNode em baixo");

  for (const [regiao, p, r] of REGIOES) {
    const f = s && porRegiao(s.fragmentos, regiao);
    const prim = f && f.estado === "OK" ? f.primario_atual : null;
    const reps = f && Array.isArray(f.replicas) ? f.replicas : [];
    for (const id of [p, r]) {
      const c = corre(id);
      const rep = reps.find((x) => String(x.nome).replace(/_/g, "-") === id);   // application_name usa "_"
      if (c === false) definirNo(id, "falha", "em baixo");
      else if (prim === id) definirNo(id, reps.some((x) => x.modo === "sync") ? "ok" : "aviso",
        reps.some((x) => x.modo === "sync") ? "primário" : "primário, sem réplica", true);
      else if (rep) definirNo(id, rep.modo === "sync" ? "ok" : "aviso", rep.modo === "sync" ? "réplica síncrona" : `réplica ${rep.modo}`);
      else if (prim && prim !== id) definirNo(id, c ? "vedado" : "falha", c ? "vedado, a voltar como réplica" : "em baixo");
      else if (!f) definirNo(id, "desconhecido", "");
      else if (f.estado !== "OK") definirNo(id, c ? "aviso" : "falha", c ? "não responde" : "em baixo");
      else definirNo(id, c ? "aviso" : "desconhecido", c ? "a recuperar" : "");
    }
  }
  for (const { a, b, tipo, ln } of elsLig) {
    let inativa = false;
    const morto = (id) => elsNo[id].classList.contains("falha");
    if (tipo === "dados" || tipo === "dados-r") {
      const regiao = regiaoDoNo(b);
      inativa = primarioDe(regiao) !== b;
    } else if (tipo === "replica") {
      inativa = ![a, b].some((id) => elsNo[id].querySelector("text.papel").textContent.startsWith("réplica"));
    } else inativa = morto(a) || morto(b);
    ln.classList.toggle("inativa", inativa);
    ln.classList.toggle("oculta", inativa && tipo === "dados-r");
  }
  $("mapa-legenda").textContent = !s ? "A API não está a responder."
    : REGIOES.map(([reg]) => { const f = porRegiao(s.fragmentos, reg); const ok = f && f.estado === "OK";
        const central = porRegiao(s.fragmentos, "Central");
        return `${reg === "Central" ? "Nó Central" : reg}: ${ok ? f.primario_atual : reg !== "Central" && central && central.estado === "OK" ? "em baixo, servida pelo Nó Central" : "indisponível"}.`; }).join(" ")
      + ` HDFS com ${dfs && dfs.datanodes_vivos != null ? dfs.datanodes_vivos : "?"} DataNodes vivos.`;
}

// pacotes animados ao longo das ligações
const centro = (id) => [NOS[id].x, NOS[id].y];
function animarCaminho(ids, cls, msPorSalto = 260) {
  if (semMovimento) return Promise.resolve();
  const pts = ids.map(centro);
  const c = svg("circle", { r: 6, class: "pacote " + cls, cx: pts[0][0], cy: pts[0][1] });
  $("pacotes").append(c);
  const total = (pts.length - 1) * msPorSalto;
  return new Promise((resolve) => {
    const t0 = performance.now();
    function passo(t) {
      const k = Math.max(0, Math.min(1, (t - t0) / total));   // o rAF pode dar um t ligeiramente anterior a t0
      const seg = Math.min(pts.length - 2, Math.floor(k * (pts.length - 1)));
      const f = k * (pts.length - 1) - seg;
      const [x1, y1] = pts[seg], [x2, y2] = pts[seg + 1];
      c.setAttribute("cx", x1 + (x2 - x1) * f); c.setAttribute("cy", y1 + (y2 - y1) * f);
      if (k < 1) requestAnimationFrame(passo); else { c.remove(); resolve(); }
    }
    requestAnimationFrame(passo);
  });
}
async function animarTransferencia(r, origem, destino) {
  const d = r.dados || {};
  const coord = ["api-1", "api-2"].includes(d.coordenador) ? d.coordenador : (["api-1", "api-2"].includes(r.no) ? r.no : null);
  if (!coord) return;
  const pC = d.via_central ? primarioDe("Central") : null;
  const pO = primarioDe(regiaoDaConta(origem)) || pC, pD = primarioDe(regiaoDaConta(destino)) || pC;
  await animarCaminho(["navegador", "gateway", coord], "");
  const parts = [...new Set([pO, pD].filter(Boolean))];
  const abort = !r.ok || d.estado === "ABORTADA";
  await Promise.all(parts.map((p) => animarCaminho([coord, p, coord], "")));
  if (!abort && parts.length > 1) await Promise.all(parts.map((p) => animarCaminho([coord, p, coord], "f2")));
  await animarCaminho([coord, "gateway", "navegador"], abort ? "aborto" : "f2");
}

// menu de um nó (parar, ligar, matar)
const menuNo = $("menu-no");
let noAberto = null;
async function abrirMenuNo(id, ev) {
  ev.stopPropagation();
  if (!tokenAdmin()) return;   // só o administrador controla os nós
  noAberto = id;
  const g = elsNo[id];
  const caixa = g.getBoundingClientRect();
  menuNo.hidden = false;
  menuNo.style.left = Math.max(8, Math.min(window.scrollX + caixa.left, window.scrollX + document.documentElement.clientWidth - 316)) + "px";
  menuNo.style.top = (window.scrollY + caixa.bottom + 8) + "px";
  $("menu-no-nome").textContent = NOS[id].nome;
  const c = contentores && contentores[id];
  $("menu-no-estado").textContent = c ? `Contentor: ${c.detalhe || c.estado}` : "Estado do contentor desconhecido.";
  const acoes = limpar($("menu-no-acoes"));
  const corre = c && c.estado === "running";
  const botao = (acao, rot, cls) => el("button", { class: cls, type: "button", onclick: async (e) => {
    e.target.disabled = true; await noAcao(id, acao); fecharMenuNo(); atualizarAdmin();
  } }, rot);
  if (corre) acoes.append(botao("parar", "Parar", "perigo"), botao("matar", "Matar (kill)", "perigo"), botao("reiniciar", "Reiniciar", "discreto"));
  else acoes.append(botao("ligar", "Ligar", "acao"));
  if (id.startsWith("api-")) acoes.append(el("button", { class: "discreto", type: "button", title: "Apaga a marca de desativado e reinicia", onclick: async () => {
    await chamar("POST", `/controlo/reativar/${id}`, { token: tokenAdmin() }); evento(`${id} reativada`, "ok"); fecharMenuNo();
  } }, "Reativar"));
  $("menu-no-log").textContent = "";
  const lg = await chamar("GET", `/controlo/logs/${id}`, { token: tokenAdmin(), consulta: { linhas: "8" } });
  if (noAberto === id && lg.ok) $("menu-no-log").textContent = lg.dados.linhas.map((l) => l.slice(0, 220)).join("\n");
}
function fecharMenuNo() { menuNo.hidden = true; noAberto = null; }
document.addEventListener("click", (e) => { if (!menuNo.hidden && !menuNo.contains(e.target)) fecharMenuNo(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") fecharMenuNo(); });
async function noAcao(servico, acao) {
  const r = await chamar("POST", `/controlo/nos/${servico}/${acao}`, { token: tokenAdmin() });
  const verbo = { parar: "parado", ligar: "ligado", matar: "morto (kill)", reiniciar: "reiniciado" }[acao];
  evento(r.ok ? `${servico} ${verbo}` : `Não foi possível: ${acao} ${servico} (HTTP ${r.status})`, r.ok ? (acao === "ligar" ? "ok" : "falha") : "falha");
  return r;
}

// ------------------------------------------------------------------ livro (painel escuro à direita)
let totalInicial = null;
function mostrarLivroVazio() {
  $("livro-titulo").textContent = ehCliente() ? "O seu dinheiro" : "Dinheiro no sistema";
  const t = $("livro-total");
  t.className = "livro-total vazio";
  t.textContent = sessaoValida() ? "A ler…" : "Entre para ver saldos.";
  $("livro-moeda").hidden = true;
  limpar($("livro-moedas")); $("livro-conservacao").textContent = "";
}
function mostrarLivro(d) {
  $("livro-titulo").textContent = "Dinheiro no sistema";
  const t = $("livro-total");
  t.className = "livro-total";
  $("livro-moeda").hidden = false; $("livro-moeda").textContent = "equivalente em meticais";
  t.textContent = fmt(d.total_equivalente_MZN) + " MZN";
  const dl = limpar($("livro-moedas"));
  for (const [m, v] of Object.entries(d.saldos_por_moeda)) dl.append(el("dt", {}, NOME_MOEDA[m] ? `Em ${NOME_MOEDA[m]}` : m), el("dd", {}, fmtMoeda(v, m)));
  const cons = $("livro-conservacao");
  if (d.fragmentos_indisponiveis && d.fragmentos_indisponiveis.length) {
    cons.className = "livro-conservacao mudou"; cons.textContent = "Total parcial: há um servidor de dados indisponível.";
    return;
  }
  if (totalInicial === null) totalInicial = d.total_equivalente_MZN;
  const pelaCentral = (d.servidos_pela_central || []).length ? ` ${d.servidos_pela_central.map((x) => x.split(" (")[0]).join(", ")} em baixo: contado pelo Nó Central.` : "";
  const dif = Number(d.total_equivalente_MZN) - Number(totalInicial) - depositosSessao;
  cons.className = "livro-conservacao" + (Math.abs(dif) >= 0.005 ? " mudou" : "");
  const deps = depositosSessao > 0.005 ? ` Entraram ${fmt(depositosSessao)} MZN em depósitos.` : "";
  cons.textContent = (Math.abs(dif) < 0.005 ? `Sem dinheiro criado nem perdido desde que entrou.${deps}`
    : `${dif > 0 ? "+" : ""}${fmt(dif)} MZN desde que entrou (envios para fora do banco ou arredondamentos de câmbio).${deps}`) + pelaCentral;
}
function mostrarLivroCliente() {
  if (!dadosCliente) return;
  const { contas, taxas } = dadosCliente;
  $("livro-titulo").textContent = "O seu dinheiro";
  const total = contas.reduce((s, c) => s + Number(c.saldo) * (taxas[c.moeda] || 0), 0);
  const t = $("livro-total");
  t.className = "livro-total";
  t.textContent = fmt(total) + " MZN";
  $("livro-moeda").hidden = false; $("livro-moeda").textContent = "total das suas contas, em meticais";
  const dl = limpar($("livro-moedas"));
  for (const c of contas) dl.append(el("dt", {}, `Conta em ${NOME_MOEDA[c.moeda] || c.moeda}`), el("dd", {}, fmtMoeda(c.saldo, c.moeda)));
  $("livro-conservacao").textContent = "";
}

// ------------------------------------------------------------------ ciclo ao vivo
const sentinelaVista = new Set();
async function atualizarSaude() {
  const r = await chamar("GET", "/saude");
  saudeAtual = r.ok && typeof r.dados === "object" ? r.dados : null;
}
async function atualizarAdmin() {
  const token = tokenAdmin();
  if (!token) { contentores = null; return; }
  const [t, n, s] = await Promise.all([
    chamar("GET", "/admin/totais", { token }),
    chamar("GET", "/controlo/nos", { token }),
    chamar("GET", "/controlo/logs/sentinela", { token, consulta: { linhas: "6" } })]);
  if (t.ok) mostrarLivro(t.dados);
  contentores = n.ok ? Object.fromEntries(n.dados.nos.map((x) => [x.servico, x])) : null;
  if (s.ok) for (const l of s.dados.linhas) {
    if (sentinelaVista.has(l) || !/\[sentinela\]/.test(l)) continue;
    sentinelaVista.add(l);
    if (sentinelaVista.size > 1 && !/a vigiar/.test(l))
      evento(l.replace(/^\[[^\]]*\]\s*\[sentinela\]\s*/, "Sentinela: "), /FAILOVER|PROMOVIDA|SÍNCRONA ativada/.test(l) ? "ok" : /não responde|FENCING/.test(l) ? "falha" : "aviso");
  }
}
let ocupado = false, voltas = 0;
async function ciclo() {
  if (sessao && !sessaoValida()) {
    evento("A sessão expirou (o token só é válido 15 minutos)", "aviso");
    sessao = null; guardarSessao(); aplicarSessao(); abrirLogin("A sua sessão expirou. Entre de novo.");
  }
  if (!ocupado) {
    ocupado = true;
    try {
      await Promise.all([atualizarSaude(), atualizarAdmin()]); atualizarMapa();
      if (ehCliente() && ++voltas % 4 === 0) await carregarDadosCliente();
    } finally { ocupado = false; }
  }
  setTimeout(ciclo, 2500);
}

// ------------------------------------------------------------------ dados do cliente
const NOME_MOEDA = { EUR: "euros", USD: "dólares", MZN: "meticais", ZAR: "randes" };
const SIMBOLO = { EUR: "€", USD: "US$", MZN: "MT", ZAR: "R" };
const fmtMoeda = (v, m) => `${fmt(v)} ${SIMBOLO[m] || m}`;
const nomeRegiao = (r) => (r === "AM" ? "América" : r === "AF" ? "África" : r === "CT" ? "Nó Central" : "Europa");
const classeRegiao = (r) => (r === "AM" ? " am" : r === "AF" ? " af" : r === "CT" ? " ct" : "");
const OPERADORA = { "M-PESA": "M-Pesa", "E-MOLA": "e-Mola", MKESH: "mKesh" };
const PREFIXOS = { "M-PESA": ["84", "85"], "E-MOLA": ["86", "87"], MKESH: ["82", "83"] };
const BANCOS = { BCI: "BCI", "MILLENNIUM-BIM": "Millennium bim", "STANDARD-BANK": "Standard Bank", ABSA: "Absa", MOZA: "Moza Banco", "BANCO-PARCEIRO-MZ": "Banco Parceiro" };
let dadosCliente = null;   // { contas, beneficiarios, taxas }

async function carregarDadosCliente() {
  const token = tokenCliente();
  if (!token) return;
  const [c, b, t] = await Promise.all([chamar("GET", "/contas", { token }), chamar("GET", "/beneficiarios", { token }),
    chamar("GET", "/taxas", { token })]);
  if (!c.ok) { limpar($("cartoes-contas")).append(recibo(c, "Não foi possível ler as suas contas")); return; }
  const porNo = t.ok ? Object.values(t.dados.mzn_por_unidade).find((v) => typeof v === "object") : null;
  const taxas = porNo ? Object.fromEntries(Object.entries(porNo).map(([m, v]) => [m, Number(v)])) : { MZN: 1, EUR: 69.5, USD: 63.9, ZAR: 3.55 };
  dadosCliente = { contas: c.dados.contas, beneficiarios: b.ok ? b.dados.beneficiarios : [], taxas,
    parcial: c.dados.resultado_parcial, falta: c.dados.fragmentos_indisponiveis || [] };
  mostrarContasCliente(); preencherFormularios(); mostrarLivroCliente();
  if (!$("sep-inicio").hidden) mostrarUltimos();
}
function nomeDaConta(id) {
  if (!dadosCliente) return id;
  if (dadosCliente.contas.some((c) => c.id === id)) return sessao.nome;
  const b = dadosCliente.beneficiarios.find((x) => x.conta === id);
  return b ? b.titular : id;
}
function mostrarContasCliente() {
  const caixa = limpar($("cartoes-contas"));
  const h = new Date().getHours();
  $("ola").textContent = `${h < 12 ? "Bom dia" : h < 19 ? "Boa tarde" : "Boa noite"}, ${sessao.nome.split(" ")[0]}`;
  for (const c of dadosCliente.contas) {
    caixa.append(el("button", { type: "button", class: "cartao-conta" + classeRegiao(c.regiao), title: "Ver o extrato desta conta",
      onclick: () => { $("ex-conta").value = c.id; irPara("extrato"); } },
      el("span", { class: "cc-topo" }, el("span", { class: "cc-tipo" }, `Conta em ${NOME_MOEDA[c.moeda] || c.moeda}`),
        el("span", { class: "regiao" + classeRegiao(c.regiao) }, nomeRegiao(c.regiao))),
      el("span", { class: "cc-saldo" }, fmtMoeda(c.saldo, c.moeda)),
      el("span", { class: "cc-num" }, `N.º ${c.id}`),
      c.via_central ? el("span", { class: "cc-central" }, "Região em baixo: saldo do Nó Central") : null));
  }
  if (!dadosCliente.contas.length) caixa.append(el("div", { class: "vazio-msg" }, "Ainda não tem contas. Peça ao banco para abrir uma."));
  if (dadosCliente.parcial) caixa.append(el("div", { class: "vazio-msg" },
    `Algumas contas não aparecem: o servidor ${dadosCliente.falta.join(", ")} não respondeu.`));
}
async function mostrarUltimos() {
  const caixa = $("ultimos");
  if (!dadosCliente || !dadosCliente.contas.length) { limpar(caixa); return; }
  const rs = await Promise.all(dadosCliente.contas.map((c) => chamar("GET", `/contas/${c.id}/extracto`, { token: tokenCliente() })));
  const todos = rs.filter((r) => r.ok).flatMap((r) => r.dados.movimentos.map((m) => ({ ...m, conta: r.dados.conta, moeda: r.dados.moeda })));
  todos.sort((a, b) => (a.data < b.data ? 1 : -1));
  limpar(caixa);
  if (!todos.length) return caixa.append(el("div", { class: "vazio-msg" }, "Ainda não há movimentos."));
  caixa.append(el("ul", { class: "lista-mov" }, todos.slice(0, 6).map((m) => el("li", {},
    el("span", { class: "lm-data" }, new Date(m.data).toLocaleDateString("pt-PT", { day: "2-digit", month: "short" })),
    el("span", { class: "lm-desc" }, m.descricao, el("small", {}, `Conta ${m.conta}`)),
    el("span", { class: "lm-valor " + (m.credito ? "entra" : "sai") }, `${m.credito ? "+" : "−"}${fmtMoeda(m.credito || m.debito, m.moeda)}`)))));
}
document.querySelectorAll(".atalho").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.modo) definirModo(b.dataset.modo);
  irPara(b.dataset.ir);
}));

const rotuloConta = (c) => `Conta em ${NOME_MOEDA[c.moeda] || c.moeda} (${c.id}), ${fmtMoeda(c.saldo, c.moeda)}`;
function preencherFormularios() {
  for (const sel of [$("tr-origem"), $("ex-conta")]) {
    const atual = sel.value;
    limpar(sel);
    for (const c of dadosCliente.contas) sel.append(el("option", { value: c.id }, sel.id === "ex-conta" ? `Conta em ${NOME_MOEDA[c.moeda]} (${c.id})` : rotuloConta(c)));
    if (dadosCliente.contas.some((c) => c.id === atual)) sel.value = atual;
  }
  preencherDestinos(); atualizarMoedas(); atualizarPrevisao();
}
function preencherDestinos() {
  const sel = $("tr-destino"), atual = sel.value, origem = $("tr-origem").value;
  limpar(sel);
  sel.append(el("option", { value: "" }, "Escolha o destinatário"));
  const proprias = dadosCliente.contas.filter((c) => c.id !== origem);
  if (proprias.length) sel.append(el("optgroup", { label: "As suas outras contas" },
    proprias.map((c) => el("option", { value: c.id }, `${sessao.nome}, conta em ${NOME_MOEDA[c.moeda]} (${c.id})`))));
  if (dadosCliente.beneficiarios.length) sel.append(el("optgroup", { label: "Outros clientes do Global-Scale" },
    dadosCliente.beneficiarios.map((b) => el("option", { value: b.conta }, `${b.titular}, conta em ${NOME_MOEDA[b.moeda] || b.moeda} (${nomeRegiao(b.regiao)})`))));
  if ([...sel.options].some((o) => o.value === atual)) sel.value = atual;
}
const contaPropria = (id) => dadosCliente && dadosCliente.contas.find((c) => c.id === id);
function contaDestino(id) {
  if (!dadosCliente) return null;
  const p = contaPropria(id);
  if (p) return { conta: p.id, titular: sessao.nome, moeda: p.moeda, regiao: p.regiao };
  return dadosCliente.beneficiarios.find((b) => b.conta === id) || null;
}
function atualizarMoedas() { const o = contaPropria($("tr-origem").value); $("tr-moeda").textContent = o ? SIMBOLO[o.moeda] : ""; }
function lerMontante(txt) {
  const n = Number(String(txt).replace(/\s/g, "").replace(",", "."));
  return isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : null;
}
function converter(valor, de, para) { const t = dadosCliente.taxas; return valor * t[de] / t[para]; }

// ---- modo da transferência: interna, carteira ou banco
let modo = "interna", operadora = "M-PESA", carteiraConfirmada = null;
function definirModo(m) {
  modo = m;
  document.querySelectorAll(".segmentos [data-modo]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.modo === m)));
  document.querySelectorAll("#form-transf [data-modo]").forEach((e) => {
    e.hidden = !(e.dataset.modo === m || (e.dataset.modo === "externo" && m !== "interna"));
  });
  $("tr-erro").textContent = "";
  $("confirmacao").hidden = true; $("form-transf").hidden = false;
  $("tr-nota").textContent = m === "interna" ? "Entre regiões diferentes, a transferência é protegida por Two-Phase Commit."
    : "Enviada pela rede interbancária (mainframe Java). Se for recusada, o dinheiro volta à sua conta (Saga).";
  atualizarPrevisao();
}
document.querySelectorAll(".segmentos [data-modo]").forEach((b) => b.addEventListener("click", () => definirModo(b.dataset.modo)));
document.querySelectorAll("#operadoras .op").forEach((b) => b.addEventListener("click", () => {
  operadora = b.dataset.op;
  document.querySelectorAll("#operadoras .op").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
  verificarCarteira();
}));
const numeroLimpo = () => $("tr-numero").value.replace(/\D/g, "").replace(/^258(?=\d{9}$)/, "");
let temporizador = null;
$("tr-numero").addEventListener("input", () => { clearTimeout(temporizador); temporizador = setTimeout(verificarCarteira, 350); });
async function verificarCarteira() {
  const caixa = limpar($("tr-titular")), n = numeroLimpo();
  carteiraConfirmada = null;
  if (n.length < 9) { caixa.append(el("span", { class: "nota" }, "Escreva os 9 dígitos do número para confirmarmos o nome do titular.")); atualizarPrevisao(); return; }
  if (!PREFIXOS[operadora].includes(n.slice(0, 2))) {
    const certa = Object.keys(PREFIXOS).find((op) => PREFIXOS[op].includes(n.slice(0, 2)));
    caixa.append(el("span", { class: "mau" }, certa ? `Este número é da ${OPERADORA[certa]}, não da ${OPERADORA[operadora]}.` : "Número de telemóvel inválido."));
    return;
  }
  caixa.append(el("span", { class: "nota" }, "A confirmar o titular…"));
  const r = await chamar("GET", `/carteiras/${operadora}/${n}`, { token: tokenCliente() });
  if (numeroLimpo() !== n) return;
  limpar(caixa);
  if (r.ok && r.dados.estado === "ATIVA") {
    carteiraConfirmada = { numero: n, titular: r.dados.titular, operadora };
    caixa.append(el("span", { class: "bom" }, `${OPERADORA[operadora]} de ${r.dados.titular}`));
  } else caixa.append(el("span", { class: "mau" }, (r.dados && (r.dados.motivo || r.dados.detail)) || "Não foi possível confirmar o número."));
  atualizarPrevisao();
}
function atualizarPrevisao() {
  const caixa = limpar($("tr-previsao"));
  if (!dadosCliente) return;
  const o = contaPropria($("tr-origem").value), v = lerMontante($("tr-valor").value);
  if (!o) return;
  if (modo === "interna") {
    const d = contaDestino($("tr-destino").value);
    if (!d) return;
    if (v) caixa.append(el("strong", {}, `${d.titular.split(" ")[0]} recebe ${o.moeda === d.moeda ? "" : "cerca de "}${fmtMoeda(converter(v, o.moeda, d.moeda), d.moeda)}`));
    if (o.moeda !== d.moeda) caixa.append(el("span", {}, `Câmbio: 1 ${SIMBOLO[o.moeda]} = ${(dadosCliente.taxas[o.moeda] / dadosCliente.taxas[d.moeda]).toFixed(4).replace(".", ",")} ${SIMBOLO[d.moeda]}`));
    caixa.append(el("span", {}, o.regiao === d.regiao ? "Na mesma região: transação local." : `${nomeRegiao(o.regiao)} para ${nomeRegiao(d.regiao)}: Two-Phase Commit.`));
  } else if (modo === "carteira") {
    const quem = carteiraConfirmada ? carteiraConfirmada.titular.split(" ")[0] : "A carteira";
    if (v) caixa.append(el("strong", {}, `${quem} recebe ${o.moeda === "MZN" ? "" : "cerca de "}${fmtMoeda(converter(v, o.moeda, "MZN"), "MZN")}`));
    caixa.append(el("span", {}, o.moeda === "MZN" ? "As carteiras móveis recebem em meticais." : `As carteiras só recebem meticais: o valor é convertido (1 ${SIMBOLO[o.moeda]} = ${fmt(dadosCliente.taxas[o.moeda])} MT).`));
  } else {
    if (v) caixa.append(el("strong", {}, `Envia ${fmtMoeda(v, o.moeda)} para ${BANCOS[$("tr-banco").value]}`));
    caixa.append(el("span", {}, "Na moeda da sua conta, sem conversão."));
  }
}
$("tr-origem").addEventListener("change", () => { preencherDestinos(); atualizarMoedas(); atualizarPrevisao(); });
for (const id of ["tr-destino", "tr-banco"]) $(id).addEventListener("change", atualizarPrevisao);
$("tr-valor").addEventListener("input", atualizarPrevisao);

// ------------------------------------------------------------------ transferir (com confirmação)
const MOTIVOS = {
  SALDO_INSUFICIENTE: "Saldo insuficiente. Nenhum dinheiro foi movimentado.",
  FRAGMENTO_INDISPONIVEL: "Um dos servidores de dados está indisponível. A transferência foi cancelada e nenhum dinheiro foi movimentado.",
  NAO_AUTORIZADO: "Não tem autorização para movimentar esta conta.",
  INTEGRIDADE_VIOLADA: "A mensagem foi alterada pelo caminho e o servidor recusou-a.",
  TRANSACAO_DUPLICADA: "Esta transferência já tinha sido processada; a repetição foi recusada.",
  MENSAGEM_EXPIRADA: "A mensagem chegou fora de tempo e foi recusada.",
  CONTA_INEXISTENTE: "A conta de destino não existe.",
  TAXAS_INCONSISTENTES: "As taxas de câmbio dos servidores não coincidem; a transferência foi cancelada.",
  SEM_RESPOSTA: "O servidor deixou de responder a meio (falha simulada). O sistema termina a operação sozinho: veja o extrato daqui a alguns segundos.",
};
let pedidoPendente = null;
$("form-transf").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const erro = $("tr-erro"); erro.textContent = "";
  if (!dadosCliente) return;
  const o = contaPropria($("tr-origem").value), v = lerMontante($("tr-valor").value);
  let p = { modo, o, v };
  if (modo === "interna") {
    const d = contaDestino($("tr-destino").value);
    if (!d) { erro.textContent = "Escolha para quem quer transferir."; return; }
    p = { ...p, d, nomeDestino: d.titular, refDestino: d.conta, falha: $("tr-falha").value, via: $("tr-via").value };
  } else if (modo === "carteira") {
    if (!carteiraConfirmada || carteiraConfirmada.numero !== numeroLimpo() || carteiraConfirmada.operadora !== operadora) {
      erro.textContent = "Escreva um número de carteira válido e espere pela confirmação do titular."; return;
    }
    p = { ...p, nomeDestino: carteiraConfirmada.titular, refDestino: `${OPERADORA[operadora]} +258 ${numeroLimpo()}`,
      banco: operadora, destino: numeroLimpo(), tipo: "CARTEIRA", falhaLegado: $("tr-legado-falha").checked };
  } else {
    const iban = $("tr-iban").value.replace(/\s/g, "").toUpperCase(), nome = $("tr-nome-banco").value.trim();
    if (!/^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$/.test(iban)) { erro.textContent = "O IBAN não parece válido. Exemplo: MZ59 0001 0000 0012 3456 7890 1."; return; }
    if (nome.length < 3) { erro.textContent = "Escreva o nome do destinatário."; return; }
    p = { ...p, nomeDestino: nome, refDestino: `${BANCOS[$("tr-banco").value]}, IBAN ${iban}`, banco: $("tr-banco").value,
      destino: iban, tipo: "IBAN", falhaLegado: $("tr-legado-falha").checked };
  }
  if (!v) { erro.textContent = "Escreva um montante maior do que zero, por exemplo 150 ou 150,50."; return; }
  if (v > Number(o.saldo)) { erro.textContent = `O montante é maior do que o saldo disponível (${fmtMoeda(o.saldo, o.moeda)}).`; return; }
  pedidoPendente = p;
  const linha = (k, val, cls) => el("div", { class: "resumo-linha " + (cls || "") }, el("span", {}, k), el("strong", {}, val));
  const recebe = modo === "interna" ? fmtMoeda(converter(v, o.moeda, p.d.moeda), p.d.moeda)
    : modo === "carteira" ? fmtMoeda(converter(v, o.moeda, "MZN"), "MZN") : fmtMoeda(v, o.moeda);
  const aprox = (modo === "interna" && o.moeda !== p.d.moeda) || (modo === "carteira" && o.moeda !== "MZN") ? "cerca de " : "";
  limpar($("resumo")).append(
    linha("De", `${sessao.nome}, conta em ${NOME_MOEDA[o.moeda]} (${o.id})`),
    linha("Para", `${p.nomeDestino}`), linha(modo === "interna" ? "Conta" : modo === "carteira" ? "Carteira" : "Banco", p.refDestino),
    linha("Montante", fmtMoeda(v, o.moeda), "grande"),
    linha("Chega ao destino", aprox + recebe),
    ...(p.falha || p.falhaLegado ? [linha("Simulação", p.falha ? $("tr-falha").selectedOptions[0].textContent : "Rede interbancária em baixo", "aviso")] : []));
  $("resumo-nota").textContent = modo !== "interna"
    ? "A transferência sai do Global-Scale pela rede interbancária. Se o destino a recusar, o dinheiro é-lhe devolvido automaticamente."
    : o.regiao === p.d.regiao ? "As duas contas estão no mesmo servidor: a transferência é uma transação local."
    : `As contas estão em servidores diferentes (${nomeRegiao(o.regiao)} e ${nomeRegiao(p.d.regiao)}): a transferência usa Two-Phase Commit, para o dinheiro nunca sair de um lado sem entrar no outro.`;
  $("form-transf").hidden = true; $("confirmacao").hidden = false;
  $("btn-confirmar").focus();
});
$("btn-voltar").addEventListener("click", () => { $("confirmacao").hidden = true; $("form-transf").hidden = false; });
$("btn-confirmar").addEventListener("click", async () => {
  const p = pedidoPendente; if (!p) return;
  const b = $("btn-confirmar"); b.disabled = true; b.textContent = p.falha === "atraso_fase2" ? "A transferir (espera de 20 s)…" : "A transferir…";
  evento(`${sessao.nome.split(" ")[0]} → ${p.nomeDestino.split(" ")[0]}: ${fmtMoeda(p.v, p.o.moeda)} enviado`);
  let r;
  if (p.modo === "interna") {
    const corpo = await msgTransferencia(p.o.id, p.d.conta, p.v);
    r = await chamar("POST", p.via ? `/no/${p.via}/transferencias` : "/transferencias", { token: tokenCliente(), corpo, consulta: { falha: p.falha } });
    animarTransferencia(r, p.o.id, p.d.conta);
  } else {
    const corpo = await msgOrdem(p.o.id, p.banco, p.destino, p.v, p.tipo);
    r = await chamar("POST", "/interbancario/ordens", { token: tokenCliente(), corpo, consulta: { falha: p.falhaLegado ? "legado_indisponivel" : "" } });
    if (r.no) animarCaminho(["navegador", "gateway", r.no, "legacy", r.no, "gateway", "navegador"], r.ok && r.dados.estado === "CONCLUIDA" ? "f2" : "aborto");
  }
  b.disabled = false; b.textContent = "Confirmar e transferir";
  $("confirmacao").hidden = true; $("form-transf").hidden = false;
  limpar($("res-transf")).append(comprovativo(r, p));
  const [txt, cls] = selo(r);
  evento(`${sessao.nome.split(" ")[0]} → ${p.nomeDestino.split(" ")[0]}: ${txt}`, cls === "ok" ? "ok" : cls === "erro" ? "falha" : "aviso");
  if (r.ok && r.dados.estado !== "COMPENSADA") { $("tr-valor").value = ""; $("tr-falha").value = ""; $("tr-via").value = ""; $("tr-legado-falha").checked = false; }
  carregarDadosCliente();
  pedidoPendente = null;
});
function comprovativo(r, p) {
  const d = r.dados && typeof r.dados === "object" ? r.dados : {};
  const [, cls] = selo(r);
  const titulo = d.estado === "CONFIRMADA" ? "Transferência concluída"
    : d.estado === "CONCLUIDA" ? (p.modo === "carteira" ? "Dinheiro enviado para a carteira" : "Transferência enviada para o outro banco")
    : d.estado === "CONFIRMADA_PENDENTE" ? "Transferência confirmada; um servidor ainda está a terminar"
    : d.estado === "COMPENSADA" ? "Não foi possível enviar: o dinheiro foi devolvido"
    : "Transferência não realizada";
  const conv = d.conversao || {};
  const numeros = d.estado === "CONFIRMADA" || d.estado === "CONFIRMADA_PENDENTE"
    ? [["Debitado", conv.debitado ? fmtMoeda(conv.debitado.split(" ")[0], p.o.moeda) : fmtMoeda(p.v, p.o.moeda)],
       ["Creditado", conv.creditado ? fmtMoeda(conv.creditado.split(" ")[0], p.d.moeda) : "—"]]
    : d.estado === "CONCLUIDA" ? [["Debitado", fmtMoeda(p.v, p.o.moeda)], ["Enviado", d.enviado ? fmtMoeda(d.enviado.split(" ")[0], d.enviado.split(" ")[1]) : fmtMoeda(p.v, p.o.moeda)],
       ["Autorização", (d.resposta_legado && d.resposta_legado.CodigoAutorizacao) || "—"]] : null;
  return el("article", { class: "comprovativo " + cls },
    el("div", { class: "comp-cab" }, el("span", { class: "carimbo " + cls }, selo(r)[0]), el("h3", {}, titulo)),
    el("div", { class: "comp-partes" },
      el("div", {}, el("small", {}, "De"), el("strong", {}, sessao.nome), el("span", {}, p.o.id)),
      el("div", { class: "seta", "aria-hidden": "true" }, "→"),
      el("div", {}, el("small", {}, "Para"), el("strong", {}, d.titular_destino || p.nomeDestino), el("span", {}, p.refDestino))),
    numeros ? el("div", { class: "recibo-num" }, numeros.map(([k, v]) => el("div", {}, el("small", {}, k), el("strong", {}, v))),
      d.tx_id ? el("div", {}, el("small", {}, "Referência"), el("strong", {}, String(d.tx_id).slice(0, 8))) : null) : null,
    !r.ok || d.estado === "COMPENSADA" ? el("p", { class: "motivo" }, d.estado === "COMPENSADA"
      ? `${(d.motivo || "").replace(/^Rejeitada pelo banco parceiro: /, "Recusada pelo destino: ")} O valor voltou à sua conta.`
      : MOTIVOS[d.erro] || d.detalhe || "O servidor recusou a transferência.") : null,
    d.estado === "CONFIRMADA_PENDENTE" ? el("p", { class: "motivo" }, "A decisão de confirmar já foi tomada e gravada. Quando o servidor que falhou voltar, a recuperação automática termina a operação.") : null,
    d.via_central ? el("p", { class: "nota-central" }, `Os servidores da ${(d.regioes_em_baixo || []).map(nomeRegiao).join(" e ")} estão em baixo. `
      + "A operação foi autorizada pelo Nó Central e fica registada; o banco aplica-a nessa região assim que ela voltar.") : null,
    el("details", { class: "json" }, el("summary", {}, "Detalhes técnicos (protocolo, fases e mensagens)"), recibo(r, "Resposta do servidor")));
}

// ------------------------------------------------------------------ extrato
let extratoAtual = null;
function periodo() {
  const p = $("ex-periodo").value, hoje = new Date(), iso = (d) => d.toISOString().slice(0, 10);
  document.querySelectorAll("[data-datas]").forEach((e) => { e.hidden = p !== "outro"; });
  if (p === "tudo") return [null, null];
  if (p === "outro") return [$("ex-de").value || null, $("ex-ate").value || null];
  if (p === "mes") return [iso(new Date(hoje.getFullYear(), hoje.getMonth(), 1)), iso(hoje)];
  const d = new Date(hoje); d.setDate(d.getDate() - Number(p)); return [iso(d), iso(hoje)];
}
async function carregarExtrato() {
  if (!dadosCliente) await carregarDadosCliente();
  const conta = $("ex-conta").value, caixa = $("extrato");
  if (!conta) return;
  const [de, ate] = periodo();
  const r = await chamar("GET", `/contas/${conta}/extracto`, { token: tokenCliente(), consulta: { de, ate } });
  limpar(caixa);
  if (!r.ok) return caixa.append(recibo(r, "Não foi possível obter o extrato"));
  const e = extratoAtual = r.dados, m = e.moeda;
  const desc = (d) => (d ? new Date(d + "T00:00").toLocaleDateString("pt-PT") : null);
  caixa.append(
    el("div", { class: "ex-cab" },
      el("div", {}, el("strong", {}, e.titular), el("span", {}, `Conta em ${NOME_MOEDA[m]} N.º ${e.conta}, ${nomeRegiao(e.regiao)}`)),
      el("div", { class: "ex-periodo" }, e.periodo.de || e.periodo.ate ? `De ${desc(e.periodo.de) || "a abertura"} a ${desc(e.periodo.ate) || "hoje"}` : "Desde a abertura da conta",
        el("span", {}, `Emitido a ${new Date().toLocaleString("pt-PT")}`))),
    ...(e.nota ? [el("p", { class: "nota-central" }, e.nota)] : []),
    el("div", { class: "ex-resumo" },
      [["Saldo inicial", e.saldo_inicial], ["Créditos", e.total_creditos, "entra"], ["Débitos", e.total_debitos, "sai"], ["Saldo final", e.saldo_final, "forte"]]
        .map(([k, v, c]) => el("div", { class: c || "" }, el("small", {}, k), el("strong", {}, `${c === "entra" ? "+" : c === "sai" ? "−" : ""}${fmtMoeda(v, m)}`)))),
    e.movimentos.length ? el("table", { class: "quadro extrato" },
      el("thead", {}, el("tr", {}, ["Data", "Descrição", "Débito", "Crédito", "Saldo"].map((h) => el("th", {}, h)))),
      el("tbody", {}, e.movimentos.map((x) => el("tr", {},
        el("td", { class: "data" }, new Date(x.data).toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "short" })),
        el("td", {}, el("div", {}, x.descricao), x.valor_contraparte ? el("small", { class: "sub" }, `Equivalente: ${fmtMoeda(x.valor_contraparte, x.moeda_contraparte)}`) : null,
          el("small", { class: "sub ref" }, `Ref. ${x.referencia.slice(0, 8)}`)),
        el("td", { class: "num sai" }, x.debito ? fmt(x.debito) : ""),
        el("td", { class: "num entra" }, x.credito ? fmt(x.credito) : ""),
        el("td", { class: "num" }, fmt(x.saldo_apos))))))
      : el("div", { class: "vazio-msg" }, "Não há movimentos neste período."));
}
for (const id of ["ex-conta", "ex-periodo", "ex-de", "ex-ate"]) $(id).addEventListener("change", carregarExtrato);
$("btn-imprimir").addEventListener("click", () => window.print());
$("btn-csv").addEventListener("click", () => {
  const e = extratoAtual; if (!e) return;
  const q = (s) => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const linhas = [["Data", "Descrição", "Débito", "Crédito", "Saldo", "Referência"].map(q).join(";")]
    .concat(e.movimentos.map((x) => [new Date(x.data).toLocaleString("pt-PT"), x.descricao, x.debito ? fmt(x.debito) : "",
      x.credito ? fmt(x.credito) : "", fmt(x.saldo_apos), x.referencia].map(q).join(";")));
  const blob = new Blob(["﻿" + linhas.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `extrato-${e.conta}.csv` });
  document.body.append(a); a.click(); a.remove();
});

// ------------------------------------------------------------------ administrador: clientes e contas
let clientesAdmin = [];      // [{username, nome, contas: [...]}]
let depositosSessao = 0;     // depósitos feitos nesta sessão, em MZN (para o painel do dinheiro)
let taxasAdmin = { MZN: 1, EUR: 69.5, USD: 63.9, ZAR: 3.55 };
async function carregarClientes() {
  const token = tokenAdmin();
  if (!token) return;
  const [u, c, t] = await Promise.all([chamar("GET", "/auth/utilizadores", { token }), chamar("GET", "/contas", { token }),
    chamar("GET", "/taxas", { token })]);
  const caixa = limpar($("lista-clientes"));
  if (!u.ok || !c.ok) return caixa.append(recibo(!u.ok ? u : c, "Não foi possível ler os clientes"));
  const porNo = t.ok ? Object.values(t.dados.mzn_por_unidade).find((v) => typeof v === "object") : null;
  if (porNo) taxasAdmin = Object.fromEntries(Object.entries(porNo).map(([m, v]) => [m, Number(v)]));
  clientesAdmin = u.dados.utilizadores.filter((x) => x.papel !== "admin").map((x) => ({ ...x,
    contas: c.dados.contas.filter((k) => k.owner_username === x.username) }));
  preencherContasDeposito();
  if (c.dados.resultado_parcial) caixa.append(el("p", { class: "parcial" }, `Resultado parcial: ${c.dados.fragmentos_indisponiveis.join(", ")} não respondeu.`));
  mostrarClientes();
}
let filtroRegiao = "";
document.querySelectorAll("#filtro-regiao [data-reg]").forEach((b) => b.addEventListener("click", () => {
  filtroRegiao = b.dataset.reg;
  document.querySelectorAll("#filtro-regiao [data-reg]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  mostrarClientes();
}));
function mostrarClientes() {
  const caixa = $("lista-clientes");
  caixa.querySelectorAll("table, .vazio-msg").forEach((n) => n.remove());
  const lista = clientesAdmin.map((cl) => ({ ...cl, contas: cl.contas.filter((k) => !filtroRegiao || k.regiao === filtroRegiao) }))
    .filter((cl) => !filtroRegiao || cl.contas.length);
  const nContas = lista.reduce((n, cl) => n + cl.contas.length, 0);
  const porMoeda = {};
  for (const cl of lista) for (const k of cl.contas) porMoeda[k.moeda] = (porMoeda[k.moeda] || 0) + Number(k.saldo);
  $("filtro-resumo").textContent = `${lista.length} ${lista.length === 1 ? "cliente" : "clientes"}, ${nContas} ${nContas === 1 ? "conta" : "contas"}`
    + (nContas ? `: ${Object.entries(porMoeda).map(([m, v]) => fmtMoeda(v, m)).join(", ")}` : "")
    + (filtroRegiao ? ` no servidor ${nomeRegiao(filtroRegiao) === "África" ? "de África" : "da " + nomeRegiao(filtroRegiao)}` : "");
  if (!lista.length) { caixa.append(el("div", { class: "vazio-msg" }, `Não há contas ${filtroRegiao ? "nesta região" : ""}.`)); return; }
  const depositarEm = (id) => { irPara("depositar"); $("dp-conta").value = id; atualizarDeposito(); $("dp-valor").focus(); };
  if (filtroRegiao) {
    // Vista por região: uma linha por conta guardada no servidor dessa região.
    const contas = lista.flatMap((cl) => cl.contas.map((k) => ({ ...k, cliente: cl })))
      .sort((x, y) => x.id.localeCompare(y.id, "pt", { numeric: true }));
    caixa.append(el("table", { class: "quadro contas-regiao" },
      el("thead", {}, el("tr", {}, ["Conta", "Titular", "Utilizador", "Moeda", "Saldo", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, contas.map((k) => el("tr", {},
        el("td", { class: "id" }, el("span", { class: "regiao" + classeRegiao(k.regiao) }, nomeRegiao(k.regiao)), " ", k.id),
        el("td", {}, el("strong", {}, k.titular || k.cliente.nome)),
        el("td", { class: "ref" }, k.cliente.username),
        el("td", {}, k.moeda),
        el("td", { class: "num" }, fmtMoeda(k.saldo, k.moeda)),
        el("td", { class: "acoes-linha" }, el("button", { class: "discreto", type: "button", onclick: () => depositarEm(k.id) }, "Depositar")))))));
    return;
  }
  caixa.append(el("table", { class: "quadro clientes" },
    el("thead", {}, el("tr", {}, ["Cliente", "Contas", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, lista.map((cl) => el("tr", {},
      el("td", {}, el("div", { class: "cl-nome" }, el("span", { class: "pil-ini pequeno" }, iniciais(cl.nome)), el("span", {}, el("strong", {}, cl.nome), el("small", {}, cl.username)))),
      el("td", {}, cl.contas.length ? el("ul", { class: "cl-contas" }, cl.contas.map((k) => el("li", {},
        el("span", { class: "regiao" + classeRegiao(k.regiao) }, nomeRegiao(k.regiao)), el("span", {}, k.id), el("strong", {}, fmtMoeda(k.saldo, k.moeda)))))
        : el("span", { class: "nota" }, "Sem contas")),
      el("td", { class: "acoes-linha" },
        el("button", { class: "discreto", type: "button", disabled: !cl.contas.length, onclick: () => depositarEm(cl.contas[0].id) }, "Depositar"),
        el("button", { class: "discreto", type: "button", onclick: () => abrirFormConta(clientesAdmin.find((x) => x.username === cl.username)) }, "Abrir conta")))))));
}
$("btn-clientes").addEventListener("click", carregarClientes);

// ---- novo cliente
$("btn-novo-cliente").addEventListener("click", () => {
  $("form-conta").hidden = true; $("form-cliente").hidden = false; $("nc-erro").textContent = ""; $("nc-nome").focus();
});
$("btn-cancelar-cliente").addEventListener("click", () => { $("form-cliente").hidden = true; });
$("nc-nome").addEventListener("input", () => {
  if ($("nc-user").dataset.mexido) return;
  const n = $("nc-nome").value.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().split(/\s+/).filter(Boolean);
  $("nc-user").value = n.length ? (n[0] + (n.length > 1 ? "." + n[n.length - 1] : "")).replace(/[^a-z0-9.]/g, "").slice(0, 30) : "";
});
$("nc-user").addEventListener("input", () => { $("nc-user").dataset.mexido = "1"; });
function gerarPassword() {
  const letras = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ", nums = "23456789", todos = letras + nums;
  const r = crypto.getRandomValues(new Uint32Array(10));
  let p = letras[r[0] % letras.length] + nums[r[1] % nums.length];
  for (let i = 2; i < 10; i++) p += todos[r[i] % todos.length];
  return p;
}
$("btn-gerar").addEventListener("click", () => { $("nc-pass").value = gerarPassword(); });
$("form-cliente").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("nc-erro"); erro.textContent = "";
  const nome = $("nc-nome").value.trim().replace(/\s+/g, " "), user = $("nc-user").value.trim().toLowerCase(), pass = $("nc-pass").value;
  const regiao = $("nc-regiao").value, moeda = $("nc-moeda").value, dep = $("nc-deposito").value.trim() ? lerMontante($("nc-deposito").value) : 0;
  if (nome.split(" ").length < 2) { erro.textContent = "Escreva o nome completo (nome e apelido)."; return; }
  if (!/^[a-z][a-z0-9._-]{2,30}$/.test(user)) { erro.textContent = "O utilizador deve ter 3 a 31 caracteres: letras minúsculas, números, ponto ou hífen, e começar por uma letra."; return; }
  if (pass.length < 8 || !/[A-Za-z]/.test(pass) || !/\d/.test(pass)) { erro.textContent = "A palavra-passe precisa de pelo menos 8 caracteres, com letras e números. Use o botão Gerar."; return; }
  if (dep === null) { erro.textContent = "O depósito inicial não é um valor válido."; return; }
  const b = ev.submitter; if (b) { b.disabled = true; b.textContent = "A criar…"; }
  const passos = [];
  const fim = () => { if (b) { b.disabled = false; b.textContent = "Criar cliente"; } };
  const u = await chamar("POST", "/auth/utilizadores", { token: tokenAdmin(), corpo: { username: user, nome, password: pass } });
  if (!u.ok) { fim(); erro.textContent = u.status === 409 ? `Já existe um utilizador "${user}". Escolha outro.` : (u.dados && u.dados.detail) || `O serviço de autenticação recusou (HTTP ${u.status}).`; return; }
  passos.push(`Utilizador ${user} criado no serviço de autenticação (palavra-passe guardada com PBKDF2).`);
  const c = await chamar("POST", "/admin/contas", { token: tokenAdmin(), corpo: { owner_username: user, titular: nome, regiao, moeda } });
  if (!c.ok) { fim(); erro.textContent = `O utilizador foi criado, mas a conta não (HTTP ${c.status}). Use "Abrir conta" na lista.`; carregarClientes(); return; }
  passos.push(`Conta ${c.dados.conta} aberta no servidor ${c.dados.fragmento}.`);
  if (dep > 0) {
    const d = await chamar("POST", "/admin/depositos", { token: tokenAdmin(), corpo: { id: crypto.randomUUID(), conta: c.dados.conta, valor: dep.toFixed(2), descricao: "Depósito de abertura" } });
    if (d.ok) { passos.push(`Depósito de abertura de ${fmtMoeda(dep, moeda)}.`); depositosSessao += dep * (taxasAdmin[moeda] || 0); }
    else passos.push(`O depósito de abertura falhou (HTTP ${d.status}); faça-o em Depositar.`);
  }
  fim();
  evento(`Novo cliente: ${nome} (${c.dados.conta})`, "ok");
  limpar($("res-clientes")).append(el("article", { class: "comprovativo ok" },
    el("div", { class: "comp-cab" }, el("span", { class: "carimbo ok" }, "Criado"), el("h3", {}, `${nome} já é cliente`)),
    el("ul", { class: "passos" }, passos.map((x) => el("li", {}, x))),
    el("div", { class: "credenciais" }, el("span", {}, "Entregue ao cliente:"), el("code", {}, `utilizador ${user}`), el("code", {}, `palavra-passe ${pass}`))));
  $("form-cliente").reset(); delete $("nc-user").dataset.mexido; $("form-cliente").hidden = true;
  carregarClientes(); atualizarAdmin();
});

// ---- abrir conta para um cliente existente
let clienteConta = null;
function abrirFormConta(cl) {
  clienteConta = cl;
  $("form-cliente").hidden = true; $("form-conta").hidden = false; $("na-erro").textContent = "";
  $("na-titulo").textContent = `Abrir conta para ${cl.nome}`;
  $("form-conta").scrollIntoView({ block: "nearest" });
}
$("btn-cancelar-conta").addEventListener("click", () => { $("form-conta").hidden = true; });
$("form-conta").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!clienteConta) return;
  const r = await chamar("POST", "/admin/contas", { token: tokenAdmin(), corpo: { owner_username: clienteConta.username, titular: clienteConta.nome, regiao: $("na-regiao").value, moeda: $("na-moeda").value } });
  if (!r.ok) { $("na-erro").textContent = (r.dados && (r.dados.detalhe || r.dados.detail)) || `Recusado (HTTP ${r.status}).`; return; }
  evento(`Conta ${r.dados.conta} aberta para ${clienteConta.nome}`, "ok");
  limpar($("res-clientes")).append(el("article", { class: "comprovativo ok" },
    el("div", { class: "comp-cab" }, el("span", { class: "carimbo ok" }, "Aberta"), el("h3", {}, `Conta ${r.dados.conta} aberta para ${clienteConta.nome}`)),
    el("p", { class: "motivo" }, `Conta em ${NOME_MOEDA[r.dados.moeda]}, guardada no servidor ${r.dados.fragmento}. Saldo inicial: ${fmtMoeda(0, r.dados.moeda)}.`)));
  $("form-conta").hidden = true; carregarClientes();
});

// ------------------------------------------------------------------ administrador: bases de dados por nó
let noBD = null;
const curtoHex = (h) => (h ? `${h.slice(0, 16)}…${h.slice(-8)}` : "—");
async function carregarBD() {
  const r = await chamar("GET", "/admin/bd", { token: tokenAdmin() });
  const caixa = limpar($("bd-resumo"));
  if (!r.ok) return caixa.append(recibo(r, "Não foi possível ler as bases de dados"));
  const nos = r.dados.nos;
  const grupos = [["EU", "Europa"], ["AM", "América"], ["AF", "África"], ["CT", "Nó Central"]];
  caixa.append(el("div", { class: "bd-grelha" }, grupos.map(([reg, nome]) => {
    const par = nos.filter((n) => n.regiao === reg);
    const prim = par.find((n) => n.papel === "primário");
    return el("section", { class: "bd-regiao" + classeRegiao(reg) },
      el("h3", {}, `${nome}`, el("small", {}, reg === "CT" ? " espelho + pendentes" : ` contas ${reg}-*`)),
      par.map((n) => {
        const emDia = prim && n.papel === "réplica" && n.lsn === prim.lsn;
        return el("button", { type: "button", class: "bd-no" + (n.estado !== "OK" ? " em-baixo" : "") + (noBD === n.no ? " escolhido" : ""),
          onclick: () => { noBD = n.no; carregarBD(); verNoBD(n.no); } },
          el("span", { class: "bd-nome" }, n.no, el("span", { class: "bd-papel" + (n.papel === "primário" ? " prim" : "") }, n.estado === "OK" ? n.papel : "em baixo")),
          n.estado === "OK" ? [
            el("span", { class: "bd-linha" }, el("span", {}, reg === "CT" ? "Contas no espelho" : "Contas"), el("b", {}, String(n.n_contas))),
            reg === "CT" ? el("span", { class: "bd-linha" }, el("span", {}, "Operações por aplicar"), el("b", {}, String(n.n_pendentes)))
              : el("span", { class: "bd-linha" }, el("span", {}, "Movimentos"), el("b", {}, String(n.n_movimentos))),
            el("span", { class: "bd-linha" }, el("span", {}, "Preparadas (2PC)"), el("b", {}, String(n.n_preparadas))),
            el("span", { class: "bd-linha" }, el("span", {}, "Saldos"), el("b", {}, Object.entries(n.saldos_por_moeda).map(([m, v]) => fmtMoeda(v, m)).join(", ") || "—")),
            el("span", { class: "bd-linha" }, el("span", {}, "WAL (LSN)"), el("b", { class: "mono" }, n.lsn)),
            n.papel === "réplica" ? el("span", { class: "bd-sync " + (emDia ? "ok" : "") }, emDia ? "Igual ao primário" : "A receber o WAL…") : null,
          ] : el("span", { class: "bd-linha" }, n.erro || "Não responde"));
      }));
  })), el("p", { class: "nota" }, `Lido por ${r.dados.lido_por} em ${r.ms} ms. Clique num nó para ver o que tem guardado.`));
}
async function verNoBD(no) {
  const caixa = limpar($("bd-detalhe"));
  caixa.append(el("p", { class: "nota" }, `A ler ${no}…`));
  const r = await chamar("GET", `/admin/bd/${no}`, { token: tokenAdmin() });
  limpar(caixa);
  if (!r.ok) return caixa.append(recibo(r));
  const d = r.dados;
  if (d.estado !== "OK") return caixa.append(el("div", { class: "vazio-msg" }, `${no} não responde: ${d.erro || ""}`));
  if (d.regiao === "CT") return verCentralBD(caixa, d);
  const tabela = (cols, linhas) => el("div", { class: "rolar" }, el("table", { class: "quadro bd" }, el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c)))), el("tbody", {}, linhas)));
  caixa.append(
    el("div", { class: "bd-cab" }, el("h3", {}, `${d.no}`), el("span", { class: "bd-papel" + (d.papel === "primário" ? " prim" : "") }, d.papel),
      el("span", { class: "nota" }, `${d.fragmento}. WAL na posição ${d.lsn}.`)),
    d.papel === "primário" ? el("p", { class: "nota" }, d.replicacao.length
      ? `Réplica ligada: ${d.replicacao.map((x) => `${x.replica.replace(/_/g, "-")} (${x.estado}, ${x.modo === "sync" ? "síncrona" : x.modo}, recebeu até ${x.lsn})`).join("; ")}.`
      : "Sem réplica ligada neste momento.") : el("p", { class: "nota" }, "Réplica só de leitura (hot standby): recebe o WAL do primário e aplica-o."),
    el("h4", { class: "bd-tit" }, `Tabela contas (${d.contas.length} ${d.contas.length === 1 ? "linha" : "linhas"})`),
    el("p", { class: "nota" }, "A coluna titular em claro está vazia: o nome só é guardado cifrado (AES-256-GCM). A última coluna é o mesmo valor decifrado pela API, que é a única que tem a chave."),
    tabela(["Conta", "Dono", "Moeda", "Saldo", "Versão", "titular (em claro)", "titular_cifrado (bytes)", "Decifrado pela API"],
      d.contas.map((k) => el("tr", {}, el("td", { class: "id" }, k.id), el("td", {}, k.owner_username), el("td", {}, k.moeda),
        el("td", { class: "num" }, fmt(k.saldo)), el("td", { class: "num" }, String(k.versao)),
        el("td", { class: "vazio" }, k.titular_em_claro ?? "NULL"), el("td", { class: "mono" }, curtoHex(k.titular_cifrado)),
        el("td", {}, k.titular_decifrado || "—")))),
    el("h4", { class: "bd-tit" }, "Tabela movimentos (últimos 25)"),
    d.movimentos.length ? tabela(["Quando", "Conta", "Tipo", "Valor", "Protocolo", "Coordenador", "contraparte_cifrada", "Decifrada", "hash SHA-256"],
      d.movimentos.map((m) => el("tr", {}, el("td", {}, new Date(m.criado_em).toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "medium" })),
        el("td", {}, m.conta), el("td", {}, m.tipo), el("td", { class: "num" }, `${fmt(m.valor)} ${m.moeda}`), el("td", {}, m.protocolo),
        el("td", {}, m.coordenador), el("td", { class: "mono" }, curtoHex(m.contraparte_cifrada)), el("td", {}, m.contraparte_decifrada),
        el("td", { class: "mono" }, m.hash.slice(0, 12) + "…"))))
      : el("div", { class: "vazio-msg" }, "Sem movimentos."),
    el("h4", { class: "bd-tit" }, "Tabela taxas_cambio (replicada em todos os nós)"),
    tabela(["Moeda", "Meticais por unidade", "Atualizada em"], d.taxas.map((t) => el("tr", {}, el("td", { class: "id" }, t.moeda),
      el("td", { class: "num" }, t.mzn_por_unidade), el("td", {}, new Date(t.atualizado_em).toLocaleString("pt-PT"))))),
    el("h4", { class: "bd-tit" }, `Transações preparadas do 2PC (${d.preparadas.length})`),
    d.preparadas.length ? tabela(["Identificador global (gid)", "Preparada desde"], d.preparadas.map((p) => el("tr", {}, el("td", { class: "mono" }, p.gid), el("td", {}, new Date(p.desde).toLocaleString("pt-PT")))))
      : el("p", { class: "nota" }, "Nenhuma: todas as transações distribuídas estão concluídas."));
}
function verCentralBD(caixa, d) {
  const tabela = (cols, linhas) => el("div", { class: "rolar" }, el("table", { class: "quadro bd" }, el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c)))), el("tbody", {}, linhas)));
  const quando = (t) => (t ? new Date(t).toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "medium" }) : "—");
  const est = d.estado_central || { por_verificar: {}, com_pendentes: {} };
  const regs = (o) => Object.keys(o).map(nomeRegiao).join(", ");
  caixa.append(
    el("div", { class: "bd-cab" }, el("h3", {}, d.no), el("span", { class: "bd-papel" + (d.papel === "primário" ? " prim" : "") }, d.papel),
      el("span", { class: "nota" }, `Nó Central. WAL na posição ${d.lsn}.`)),
    el("p", { class: "nota" }, d.papel === "primário" ? (d.replicacao.length
      ? `Réplica ligada: ${d.replicacao.map((x) => `${x.replica.replace(/_/g, "-")} (${x.estado}, ${x.modo === "sync" ? "síncrona" : x.modo})`).join("; ")}.`
      : "Sem réplica ligada neste momento.") : "Réplica só de leitura: recebe o WAL do primário da central."),
    el("p", { class: "nota" }, `Regiões com operações por aplicar: ${regs(est.com_pendentes) || "nenhuma"}. `
      + `Regiões cujo espelho ainda não foi confirmado: ${regs(est.por_verificar) || "nenhuma"}.`),
    el("h4", { class: "bd-tit" }, `Tabela espelho (${d.espelho.length} contas, cópia dos saldos das três regiões)`),
    tabela(["Conta", "Região", "Dono", "Moeda", "Saldo", "Versão", "titular_cifrado (bytes)", "Decifrado pela API", "Atualizado"],
      d.espelho.map((k) => el("tr", {}, el("td", { class: "id" }, k.id), el("td", {}, nomeRegiao(k.regiao)), el("td", {}, k.owner_username),
        el("td", {}, k.moeda), el("td", { class: "num" }, fmt(k.saldo)), el("td", { class: "num" }, String(k.versao)),
        el("td", { class: "mono" }, curtoHex(k.titular_cifrado)), el("td", {}, k.titular_decifrado || "—"), el("td", {}, quando(k.atualizado_em))))),
    el("h4", { class: "bd-tit" }, "Tabela pendentes (últimas 25 operações autorizadas pela central)"),
    d.pendentes.length ? tabela(["N.º", "Quando", "Conta", "Tipo", "Valor", "Estado", "Contraparte (decifrada)", "Aplicada"],
      d.pendentes.map((p) => el("tr", {}, el("td", { class: "num" }, String(p.id)), el("td", {}, quando(p.criado_em)), el("td", { class: "id" }, p.conta),
        el("td", {}, p.tipo), el("td", { class: "num" }, `${fmt(p.valor)} ${p.moeda}`),
        el("td", {}, el("span", { class: "bd-papel" + (p.estado === "APLICADA" ? " prim" : "") }, p.estado === "APLICADA" ? "aplicada" : "à espera")),
        el("td", {}, p.contraparte_decifrada), el("td", {}, p.aplicada_em ? `${quando(p.aplicada_em)} por ${p.aplicada_por}` : "—"))))
      : el("p", { class: "nota" }, "Nenhuma: ainda nenhuma região precisou do caminho alternativo."),
    el("h4", { class: "bd-tit" }, "Tabela taxas_cambio (cópia da tabela replicada)"),
    tabela(["Moeda", "Meticais por unidade", "Atualizada em"], d.taxas.map((t) => el("tr", {}, el("td", { class: "id" }, t.moeda),
      el("td", { class: "num" }, t.mzn_por_unidade), el("td", {}, quando(t.atualizado_em))))));
}
$("btn-bd").addEventListener("click", () => { carregarBD(); if (noBD) verNoBD(noBD); });

// ------------------------------------------------------------------ administrador: depositar
function preencherContasDeposito() {
  const sel = $("dp-conta"), atual = sel.value;
  limpar(sel);
  for (const cl of clientesAdmin) if (cl.contas.length) sel.append(el("optgroup", { label: cl.nome },
    cl.contas.map((k) => el("option", { value: k.id }, `${k.id}, conta em ${NOME_MOEDA[k.moeda]}, saldo ${fmtMoeda(k.saldo, k.moeda)}`))));
  if ([...sel.options].some((o) => o.value === atual)) sel.value = atual;
  atualizarDeposito();
}
const contaAdmin = (id) => { for (const cl of clientesAdmin) { const k = cl.contas.find((x) => x.id === id); if (k) return { ...k, cliente: cl }; } return null; };
let depositoArmado = false;
function atualizarDeposito() {
  const k = contaAdmin($("dp-conta").value), v = lerMontante($("dp-valor").value), caixa = limpar($("dp-previsao"));
  depositoArmado = false; $("form-deposito").querySelector("button[type=submit]").textContent = "Depositar";
  $("dp-moeda").textContent = k ? SIMBOLO[k.moeda] : "";
  if (k && v) caixa.append(el("strong", {}, `Saldo depois: ${fmtMoeda(Number(k.saldo) + v, k.moeda)}`), el("span", {}, `${k.cliente.nome}, conta ${k.id}`));
}
for (const id of ["dp-conta", "dp-valor"]) $(id).addEventListener("input", atualizarDeposito);
$("form-deposito").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("dp-erro"); erro.textContent = "";
  const k = contaAdmin($("dp-conta").value), v = lerMontante($("dp-valor").value), b = ev.submitter;
  if (!k) { erro.textContent = "Escolha a conta."; return; }
  if (!v) { erro.textContent = "Escreva um montante maior do que zero."; return; }
  if (!depositoArmado) {          // segundo clique confirma
    depositoArmado = true;
    b.textContent = `Confirmar depósito de ${fmtMoeda(v, k.moeda)}`;
    return;
  }
  b.disabled = true;
  const r = await chamar("POST", "/admin/depositos", { token: tokenAdmin(), corpo: { id: crypto.randomUUID(), conta: k.id, valor: v.toFixed(2), descricao: $("dp-desc").value.trim() || "Depósito" } });
  b.disabled = false;
  if (r.ok) {
    depositosSessao += v * (taxasAdmin[k.moeda] || 0);
    evento(`Depósito de ${fmtMoeda(v, k.moeda)} na conta ${k.id} (${k.cliente.nome})`, "ok");
    limpar($("res-deposito")).append(el("article", { class: "comprovativo ok" },
      el("div", { class: "comp-cab" }, el("span", { class: "carimbo ok" }, "Depositado"), el("h3", {}, `${fmtMoeda(v, k.moeda)} na conta de ${k.cliente.nome}`)),
      el("div", { class: "recibo-num" }, el("div", {}, el("small", {}, "Conta"), el("strong", {}, k.id)),
        el("div", {}, el("small", {}, "Saldo atual"), el("strong", {}, fmtMoeda(r.dados.saldo_atual, k.moeda))),
        el("div", {}, el("small", {}, "Referência"), el("strong", {}, String(r.dados.tx_id).slice(0, 8)))),
      el("details", { class: "json" }, el("summary", {}, "Detalhes técnicos"), recibo(r, "Resposta do servidor"))));
    $("dp-valor").value = "";
    if (r.no) animarCaminho(["navegador", "gateway", r.no, primarioDe(nomeRegiao(k.regiao)) || "gateway", r.no, "gateway", "navegador"], "f2");
    await carregarClientes(); atualizarAdmin();
  } else {
    limpar($("res-deposito")).append(recibo(r, "Depósito recusado"));
  }
  atualizarDeposito();
});

// ------------------------------------------------------------------ ataques
const minhasContas = () => (dadosCliente ? dadosCliente.contas.map((c) => c.id) : []);
const origemPropria = () => $("tr-origem").value || minhasContas()[0] || "EU-1001";
const contaAlheia = () => ["AM-2001", "EU-1002", "AF-3001", "AM-2002", "EU-1001"].find((c) => !minhasContas().includes(c));
const destinoAlheio = () => (origemPropria().startsWith("EU") ? "AM-2002" : "EU-1003");
const ATAQUES = [
  { t: "Mensagem adulterada", d: "O valor é multiplicado por 100 depois de calculado o hash, como faria um intermediário.", f: async (tk) => {
    const c = await msgTransferencia(origemPropria(), "EU-1003", "1"); c.valor = (Number(c.valor) * 100).toFixed(2);
    return [["valor alterado depois do hash", await chamar("POST", "/transferencias", { token: tk, corpo: c })]];
  } },
  { t: "Repetição (replay)", d: "A mesma mensagem, com o mesmo identificador e hash, é enviada duas vezes.", f: async (tk) => {
    const c = await msgTransferencia(origemPropria(), destinoAlheio(), "1");
    const a = await chamar("POST", "/transferencias", { token: tk, corpo: c });
    return [["primeiro envio", a], ["segundo envio, igual", await chamar("POST", "/transferencias", { token: tk, corpo: c })]];
  } },
  { t: "Token forjado", d: "O papel no token passa a admin, sem a chave privada do serviço de autenticação.", f: async (tk) => {
    const [h, , s] = tk.split(".");
    const carga = cargaJWT(tk); carga.papel = "admin";
    const bytes = new TextEncoder().encode(JSON.stringify(carga));
    const nova = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return [["token com papel de admin", await chamar("GET", "/admin/2pc", { token: `${h}.${nova}.${s}` })]];
  } },
  { t: "Pedido sem token", d: "Pede a lista de contas sem se identificar.", sem: true, f: async () => [["sem autenticação", await chamar("GET", "/contas")]] },
  { t: "Mensagem expirada", d: "Hash correto, mas com hora de há 10 minutos. A janela aceite é de 5.", f: async (tk) => {
    const c = await msgTransferencia(origemPropria(), "EU-1003", "1", -600000);
    return [["mensagem antiga", await chamar("POST", "/transferencias", { token: tk, corpo: c })]];
  } },
  { t: "Conta de outra pessoa", d: "Tenta debitar uma conta de que não é titular.", f: async (tk) => {
    const c = await msgTransferencia(contaAlheia(), origemPropria(), "1");
    return [["débito de conta alheia", await chamar("POST", "/transferencias", { token: tk, corpo: c })]];
  } },
  { t: "Saldo insuficiente", d: "Pede muito mais do que o saldo: o participante vota não e nada se move.", f: async (tk) => {
    const c = await msgTransferencia(origemPropria(), destinoAlheio(), "999999999");
    return [["valor acima do saldo", await chamar("POST", "/transferencias", { token: tk, corpo: c })]];
  } },
  { t: "Adivinhar a palavra-passe", d: "Seis palavras-passe erradas para a Carla. À sexta, a conta fica bloqueada 60 s.", sem: true, f: async () => {
    const out = [];
    for (let i = 1; i <= 6; i++) out.push([`tentativa ${i}`, await chamar("POST", "/auth/login", { corpo: { username: "carla", password: "errada" + i } })]);
    return out.slice(-2);
  } },
];
for (const a of ATAQUES) {
  $("lista-ataques").append(el("div", { class: "cen" }, el("h3", {}, a.t), el("p", {}, a.d),
    el("button", { class: "perigo", type: "button", onclick: async (ev) => {
      const tk = tokenCliente();
      if (!a.sem && !tk) return avisar("res-seg", "Este ataque usa a sessão de um cliente.", pedirLogin);
      if (tk && !dadosCliente) await carregarDadosCliente();
      ev.target.disabled = true;
      const res = await a.f(tk);
      ev.target.disabled = false;
      limpar($("res-seg"));
      for (const [t, r] of res.reverse()) $("res-seg").prepend(recibo(r, `${a.t}: ${t}`));
      const ultimo = res[0][1];
      evento(`Ataque "${a.t}": ${selo(ultimo)[0]}`, ultimo.ok ? "falha" : "ok");
    } }, "Lançar ataque")));
}

// ------------------------------------------------------------------ câmbio
async function carregarTaxas() {
  const token = tokenQualquer();
  if (!token) return avisar("tabela-taxas", "Entre para ver as taxas.", pedirLogin);
  const r = await chamar("GET", "/taxas", { token });
  const caixa = limpar($("tabela-taxas"));
  if (!r.ok) return caixa.append(recibo(r));
  const por = r.dados.mzn_por_unidade, frags = Object.keys(por);
  const moedas = [...new Set(frags.flatMap((f) => (typeof por[f] === "object" ? Object.keys(por[f]) : [])))];
  caixa.append(el("div", { class: "cabeca-tabela" }, el("span", {}, `Lido por ${r.no}`),
    el("span", { class: r.dados.replicas_coerentes ? "" : "parcial" }, r.dados.replicas_coerentes ? "As cópias são iguais." : "As cópias não coincidem ou falta uma.")),
  el("table", { class: "quadro" },
    el("thead", {}, el("tr", {}, el("th", {}, "Moeda"), frags.map((f) => el("th", {}, `Cópia em ${f.split(" ")[0]}`)))),
    el("tbody", {}, moedas.map((m) => el("tr", {}, el("td", { class: "id" }, `1 ${m}`),
      frags.map((f) => el("td", { class: "num" }, typeof por[f] === "object" ? `${fmt(por[f][m])} MZN` : "indisponível")))))));
}
$("btn-taxas").addEventListener("click", carregarTaxas);
$("form-taxa").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!tokenAdmin()) return avisar("res-taxa", "Só o administrador pode alterar taxas.", pedirLogin);
  const r = await chamar("PUT", "/admin/taxas/" + $("tx-moeda").value, { token: tokenAdmin(), corpo: { mzn_por_unidade: $("tx-valor").value.replace(",", ".") } });
  mostrar("res-taxa", r, `Taxa do ${$("tx-moeda").value}`);
  evento(`Taxa ${$("tx-moeda").value}: ${selo(r)[0]}`, r.ok ? "ok" : "falha");
  carregarTaxas();
});

// ------------------------------------------------------------------ HDFS
function tabelaFicheiros(itens, botao) {
  if (!itens || !itens.length) return el("div", { class: "vazio-msg" }, "Ainda não há ficheiros aqui.");
  return el("table", { class: "quadro" },
    el("thead", {}, el("tr", {}, ["Nome", "Tamanho", "Réplicas", "Alterado", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, itens.map((f) => el("tr", {}, el("td", { class: "id" }, f.nome), el("td", { class: "num" }, `${f.tamanho} B`),
      el("td", { class: "num" }, f.replicacao ?? "—"), el("td", {}, f.modificado ? new Date(f.modificado).toLocaleString("pt-PT") : ""),
      el("td", {}, botao(f))))));
}
$("btn-rel-gerar").addEventListener("click", async () => {
  if (!tokenAdmin()) return avisar("res-dfs", "Só o administrador gera relatórios.", pedirLogin);
  const r = await chamar("POST", "/relatorios", { token: tokenAdmin() });
  mostrar("res-dfs", r, "Relatório de saldos");
  evento(`Relatório no HDFS: ${selo(r)[0]}`, r.ok ? "ok" : "falha");
  if (r.no) animarCaminho([r.no, "namenode", "datanode-1", "datanode-2"], "f2");
  listarRelatorios();
});
async function listarRelatorios() {
  const token = tokenQualquer();
  if (!token) return avisar("lista-rel", "Entre para ver os relatórios.", pedirLogin);
  const r = await chamar("GET", "/relatorios", { token });
  const caixa = limpar($("lista-rel"));
  if (!r.ok) return caixa.append(recibo(r));
  caixa.append(el("h3", {}, "Relatórios"), el("div", { class: "cabeca-tabela" }, el("span", {}, `Lidos por ${r.dados.lido_por}`)),
    tabelaFicheiros(r.dados.ficheiros.filter((f) => f.nome.endsWith(".csv")).reverse(), (f) => el("button", { class: "discreto", type: "button", onclick: async () => {
      const x = await chamar("GET", "/relatorios/" + encodeURIComponent(f.nome), { token });
      mostrar("res-dfs", x, f.nome);
    } }, "Abrir")));
}
$("btn-rel-listar").addEventListener("click", listarRelatorios);
$("btn-aud").addEventListener("click", async () => {
  if (!tokenAdmin()) return avisar("lista-aud", "Só o administrador vê a auditoria.", pedirLogin);
  const token = tokenAdmin();
  const r = await chamar("GET", "/auditoria", { token });
  const caixa = limpar($("lista-aud"));
  if (!r.ok) return caixa.append(recibo(r));
  for (const [no, fichs] of Object.entries(r.dados.nos)) {
    caixa.append(el("h3", {}, `Auditoria de ${no}`), tabelaFicheiros(fichs.slice().reverse(), (f) => el("button", { class: "discreto", type: "button", onclick: async () => {
      const x = await chamar("GET", "/auditoria/verificar", { token, consulta: { ficheiro: `/auditoria/${no}/${f.nome}` } });
      mostrar("res-dfs", x, `Cadeia de hashes de ${no}/${f.nome}`);
    } }, "Verificar")));
  }
});

// ------------------------------------------------------------------ coordenador
async function acaoAdmin(metodo, caminho, titulo) {
  if (!tokenAdmin()) return avisar("res-admin", "Precisa da sessão de administrador.", pedirLogin);
  mostrar("res-admin", await chamar(metodo, caminho, { token: tokenAdmin() }), titulo);
}
$("btn-totais").addEventListener("click", () => acaoAdmin("GET", "/admin/totais", "Totais por moeda"));
$("btn-2pc").addEventListener("click", () => acaoAdmin("GET", "/admin/2pc", "Estado do coordenador"));
$("btn-recuperar").addEventListener("click", () => acaoAdmin("POST", "/admin/recuperar", "Ciclo de recuperação"));

// ------------------------------------------------------------------ cenários de falha
const tokensDemo = {};
async function tokenDemo(user) {
  const s = tokensDemo[user];
  if (s && s.exp * 1000 > Date.now() + 60000) return s.token;
  const r = await chamar("POST", "/auth/login", { corpo: { username: user, password: CLIENTES_TESTE[user] } });
  if (!r.ok) throw new Error(`não foi possível entrar como ${user} (HTTP ${r.status})`);
  tokensDemo[user] = { token: r.dados.access_token, exp: cargaJWT(r.dados.access_token).exp };
  return r.dados.access_token;
}
const exec = $("exec");
function passo(msg, cls = "") {
  const v = exec.querySelector(".vazio"); if (v) v.remove();
  exec.append(el("li", { class: cls }, el("time", {}, hora().slice(0, 8)), el("span", {}, msg)));
  exec.scrollTop = exec.scrollHeight;
}
function secao(msg) { const v = exec.querySelector(".vazio"); if (v) v.remove(); exec.append(el("li", { class: "sec" }, msg)); }
const curta = (r) => { const [t] = selo(r); const d = r.dados || {}; return `${t}${d.erro ? `, ${d.erro}` : ""}${r.no ? `, por ${r.no}` : ""}`; };
async function totalMZN() { const r = await chamar("GET", "/admin/totais", { token: tokenAdmin() }); return r.ok ? r.dados.total_equivalente_MZN : null; }
async function no(servico, acao) { const r = await noAcao(servico, acao); passo(`${{ parar: "Parar", ligar: "Ligar", matar: "Matar" }[acao] || acao} ${servico}${r.ok ? "" : ": falhou"}`, r.ok ? "" : "erro"); return r; }
async function esperarAte(descr, fn, maxS = 90) {
  passo(`À espera: ${descr}…`, "det");
  const fim = Date.now() + maxS * 1000;
  while (Date.now() < fim) {
    try { if (await fn()) { passo(descr.charAt(0).toUpperCase() + descr.slice(1) + ".", "ok"); return true; } } catch (_) {}
    await esperar(2000);
  }
  passo(`Não aconteceu em ${maxS} s: ${descr}.`, "erro"); return false;
}
async function transferir(user, origem, destino, valor, falha, via) {
  const corpo = await msgTransferencia(origem, destino, valor);
  const r = await chamar("POST", via ? `/no/${via}/transferencias` : "/transferencias", { token: await tokenDemo(user), corpo, consulta: { falha } });
  animarTransferencia(r, origem, destino);
  return r;
}
async function preparadas() { const r = await chamar("GET", "/admin/2pc", { token: tokenAdmin() }); return r.ok ? r.dados.transacoes_preparadas_nos_participantes.length : null; }
async function saudeAgora() { await atualizarSaude(); atualizarMapa(); return saudeAtual; }
const fragAmerica = () => saudeAtual && porRegiao(saudeAtual.fragmentos, "América");
async function prepararFailover() {
  await saudeAgora();
  const f = fragAmerica();
  if (!f || f.estado !== "OK") { passo("A América não está disponível. Ligue os servidores da América e tente de novo.", "erro"); return null; }
  const atual = f.primario_atual, outro = atual === "db-america" ? "db-america-r" : "db-america";
  const sinc = Array.isArray(f.replicas) && f.replicas.some((x) => x.modo === "sync");
  if (!sinc) { passo(`A América ainda não tem réplica síncrona (${outro} está a juntar-se). Espere que fique verde no mapa.`, "erro"); return null; }
  passo(`Primário da América: ${atual}. Réplica síncrona: ${outro}.`);
  return { atual, outro };
}
async function reintegrar(antigo, novo) {
  secao(`${antigo} volta a ligar-se`);
  await no(antigo, "ligar");
  const ok = await esperarAte(`${antigo} voltou como réplica síncrona de ${novo}`, async () => {
    await saudeAgora(); const g = fragAmerica();
    return g && g.primario_atual === novo && Array.isArray(g.replicas) && g.replicas.some((x) => String(x.nome).replace(/_/g, "-") === antigo && x.modo === "sync");
  }, 150);
  const lg = await chamar("GET", `/controlo/logs/${antigo}`, { token: tokenAdmin(), consulta: { linhas: "40" } });
  for (const l of (lg.dados.linhas || []).filter((x) => /\[fencing\]|\[reintegra/i.test(x)).slice(-3)) passo(l.replace(/^.*?\[(fencing|reintegra)/, "[$1"), "det");
  if (ok) passo(`Nunca houve dois primários. A América voltou a ter redundância: pode repetir o cenário, agora no sentido contrário.`, "ok");
}
async function totalAntes() { const t = await totalMZN(); passo(`Dinheiro no sistema antes: ${fmt(t)} MZN.`); return t; }
async function totalDepois(antes) {
  const d = await totalMZN();
  if (antes === null || d === null) return passo("Não foi possível ler o total.", "aviso");
  passo(antes === d ? `Dinheiro no sistema depois: ${fmt(d)} MZN. Exatamente igual: nada se criou nem perdeu.` : `Depois: ${fmt(d)} MZN (diferença ${fmt(Number(d) - Number(antes))}).`, antes === d ? "ok" : "aviso");
}
function rasto(r, n = 4) { for (const l of (r.dados && r.dados.rasto ? r.dados.rasto : []).slice(-n)) passo(l, "det"); }

const CENARIOS = [
  { tipo: "dados", rot: "Nó Central", t: "A América fica sem os dois servidores", d: "Desliga a réplica e o primário da América. As transferências para e a partir da América continuam pelo Nó Central, que autoriza com base no espelho dos saldos e guarda as operações. Quando a América volta, recebe-as por ordem.", f: async () => {
    const antes = await totalAntes();
    await saudeAgora();
    const fa = fragAmerica(), prim = fa && fa.primario_atual ? fa.primario_atual : "db-america";
    const rep2 = prim === "db-america" ? "db-america-r" : "db-america";
    await no(rep2, "parar"); await no(prim, "parar"); await esperar(2000);   // réplica primeiro: assim não há failover
    passo("A América não tem nenhum servidor ligado. As operações seguem pelo Nó Central.", "aviso");
    secao("Transferências com a América em baixo");
    let r = await transferir("ana", "EU-1003", "AM-2002", "800");
    passo(`Ana envia 800 MZN para o Diego (destino na América): ${curta(r)}.`, r.ok && r.dados.via_central ? "ok" : "erro"); rasto(r, 4);
    r = await transferir("diego", "AM-2002", "EU-1003", "300");
    passo(`Diego envia 300 MZN para a Ana (origem na América): ${curta(r)}.`, r.ok && r.dados.via_central ? "ok" : "erro"); rasto(r, 3);
    r = await transferir("diego", "AM-2002", "EU-1003", "99999999");
    passo(`Diego tenta enviar mais do que tem: ${curta(r)}. A central também verifica o saldo.`, r.status === 409 ? "ok" : "erro");
    r = await chamar("GET", "/contas", { token: await tokenDemo("diego") });
    const cd = (r.dados.contas || []).find((c) => c.id === "AM-2002");
    passo(`Saldo do Diego, lido no espelho do Nó Central: ${cd ? fmtMoeda(cd.saldo, cd.moeda) : "?"}.`, cd && cd.via_central ? "ok" : "aviso");
    const c1 = await chamar("GET", "/admin/bd/db-central", { token: tokenAdmin() });
    passo(`Operações à espera da América no Nó Central: ${c1.ok ? c1.dados.n_pendentes : "?"}.`, "det");
    await totalDepois(antes);
    secao("A América volta");
    await no(prim, "ligar"); await no(rep2, "ligar");
    await esperarAte("a América voltou, com réplica síncrona", async () => { await saudeAgora(); const f = fragAmerica(); return f && f.estado === "OK" && Array.isArray(f.replicas) && f.replicas.some((x) => x.modo === "sync"); }, 120);
    await esperarAte("o Nó Central aplicou na América todas as operações pendentes", async () => {
      const c = await chamar("GET", "/admin/bd/db-central", { token: tokenAdmin() }); return c.ok && c.dados.n_pendentes === 0; }, 60);
    for (const api of ["api-1", "api-2"]) {
      const lg = await chamar("GET", `/controlo/logs/${api}`, { token: tokenAdmin(), consulta: { linhas: "120" } });
      for (const l of (lg.dados.linhas || []).filter((x) => /\[central\] AM:.*aplicada/.test(x)).slice(-4)) passo(l.replace(/^.*?\[central\]\s*/, "Reconciliação: "), "det");
    }
    r = await chamar("GET", "/contas/AM-2002/extracto", { token: await tokenDemo("diego") });
    const ult = r.ok ? r.dados.movimentos.slice(-2) : [];
    for (const m of ult) passo(`Extrato do Diego, já na América: ${m.descricao}, ${m.debito ? "−" + fmt(m.debito) : "+" + fmt(m.credito)} MZN (${m.protocolo}).`, "det");
    await totalDepois(antes);
  } },
  { tipo: "dados", rot: "Base de dados", t: "A América e o Nó Central em baixo", d: "Desliga a América e também o Nó Central. Sem caminho alternativo, a transferência para a América é abortada, a Europa continua e as consultas devolvem um resultado parcial.", f: async () => {
    const antes = await totalAntes();
    await saudeAgora();
    const fa = fragAmerica(), prim = fa && fa.primario_atual ? fa.primario_atual : "db-america";
    const rep2 = prim === "db-america" ? "db-america-r" : "db-america";
    const fc = saudeAtual && porRegiao(saudeAtual.fragmentos, "Central"), pc = fc && fc.primario_atual ? fc.primario_atual : "db-central";
    const rc = pc === "db-central" ? "db-central-r" : "db-central";
    await no(rc, "parar"); await no(pc, "parar"); await no(rep2, "parar"); await no(prim, "parar"); await esperar(2000);
    let r = await transferir("ana", "EU-1001", "AM-2001", "20"); passo(`Transferência EU-1001 → AM-2001: ${curta(r)}.`, r.status === 503 ? "ok" : "erro");
    r = await transferir("ana", "EU-1001", "EU-1003", "5"); passo(`Transferência dentro da Europa: ${curta(r)}.`, r.ok ? "ok" : "erro");
    r = await chamar("GET", "/contas", { token: tokenAdmin() });
    passo(`Consulta global: ${(r.dados.contas || []).length} contas, resultado parcial (${(r.dados.fragmentos_indisponiveis || []).join(", ")} não respondeu).`, r.dados.resultado_parcial ? "ok" : "aviso");
    const tx = await chamar("GET", "/taxas", { token: tokenAdmin() });
    const eu = tx.ok ? porRegiao(tx.dados.mzn_por_unidade, "Europa") : null;
    r = await chamar("PUT", "/admin/taxas/EUR", { token: tokenAdmin(), corpo: { mzn_por_unidade: String(eu && eu.EUR ? eu.EUR : "69.50") } });
    passo(`Alterar a taxa do EUR com uma cópia em baixo: ${curta(r)}. Nenhuma cópia muda.`, r.ok ? "erro" : "ok");
    await no(pc, "ligar"); await no(rc, "ligar"); await no(prim, "ligar"); await no(rep2, "ligar");
    await esperarAte("a América e o Nó Central voltaram, com réplicas síncronas", async () => { await saudeAgora();
      return ["América", "Central"].every((g) => { const f = porRegiao(saudeAtual.fragmentos, g); return f && f.estado === "OK" && Array.isArray(f.replicas) && f.replicas.some((x) => x.modo === "sync"); }); }, 150);
    await totalDepois(antes);
  } },
  { tipo: "2pc", rot: "Two-Phase Commit", t: "Rede falha na fase 1", d: "Um participante fica inacessível durante a preparação. O coordenador decide ABORT e desfaz o que já estava preparado.", f: async () => {
    const antes = await totalAntes();
    const r = await transferir("ana", "EU-1001", "AM-2001", "50", "participante_indisponivel_fase1");
    passo(`Transferência: ${curta(r)}.`, r.ok ? "erro" : "ok"); rasto(r, 6);
    await totalDepois(antes);
  } },
  { tipo: "ha", rot: "Alta disponibilidade", t: "A América perde o servidor principal", d: "O servidor principal da América é desligado e o Diego transfere logo a seguir. A sentinela promove a réplica e a transferência é feita nela. Quando o servidor antigo volta, junta-se como réplica.", f: async () => {
    const ctx = await prepararFailover(); if (!ctx) return;
    const { atual, outro } = ctx;
    const antes = await totalAntes();
    await no(atual, "parar");
    const t0 = Date.now();
    passo(`Diego transfere 50 MZN de AM-2002 para EU-1003, com ${atual} em baixo…`);
    const r = await transferir("diego", "AM-2002", "EU-1003", "50");
    passo(`Resposta ao fim de ${((Date.now() - t0) / 1000).toFixed(1)} s: ${curta(r)}.`, r.ok ? "ok" : "erro");
    await saudeAgora();
    const g = fragAmerica();
    passo(`Primário da América agora: ${g && g.primario_atual}.`, g && g.primario_atual === outro ? "ok" : "aviso");
    await totalDepois(antes);
    await reintegrar(atual, outro);
  } },
  { tipo: "ha", rot: "Alta disponibilidade", t: "O primário morre a meio do 2PC", d: "O servidor principal da América é parado durante a fase 2 de uma transferência. A sentinela promove a réplica, a transação é concluída nela, e o servidor antigo volta como réplica.", f: async () => {
    const ctx = await prepararFailover(); if (!ctx) return;
    const { atual, outro } = ctx;
    const antes = await totalAntes();
    passo("Diego transfere 100 MZN de AM-2002 para EU-1003. O coordenador vai esperar 20 s antes da fase 2.");
    const pend = transferir("diego", "AM-2002", "EU-1003", "100", "atraso_fase2");
    await esperar(5000); await no(atual, "parar");
    const r = await pend; passo(`Resposta: ${curta(r)}.`, r.ok ? "aviso" : "erro"); rasto(r, 3);
    await esperarAte(`a sentinela promoveu ${outro} a primário`, async () => { await saudeAgora(); const g = fragAmerica(); return g && g.primario_atual === outro; }, 120);
    await esperarAte("a recuperação concluiu a transação no novo primário", async () => (await preparadas()) === 0, 90);
    await totalDepois(antes);
    await reintegrar(atual, outro);
  } },
  { tipo: "2pc", rot: "Two-Phase Commit", t: "O coordenador morre e reinicia", d: "A réplica da API que coordena morre depois de decidir COMMIT. O Docker reinicia-a e a recuperação acaba o trabalho.", f: async () => {
    const antes = await totalAntes();
    const r = await transferir("diego", "AM-2002", "EU-1003", "30", "crash_coordenador_fase2");
    passo(`Resposta: ${curta(r)}. O coordenador morreu a meio.`, "aviso");
    await esperar(1500);
    passo(`Transações preparadas à espera de decisão: ${await preparadas()}.`, "det");
    await esperarAte("o coordenador reiniciou e concluiu a transação", async () => (await preparadas()) === 0, 90);
    await totalDepois(antes);
  } },
  { tipo: "2pc", rot: "Two-Phase Commit", t: "O coordenador não volta", d: "A api-1 morre definitivamente na fase 2. A api-2 lê a decisão no registo da api-1 e conclui a transação.", f: async () => {
    const antes = await totalAntes();
    const r = await transferir("diego", "AM-2002", "EU-1003", "20", "crash_permanente_coordenador", "api-1");
    passo(`Enviada à api-1: ${curta(r)}. A api-1 ficou desativada.`, "aviso");
    await esperarAte("a api-2 assumiu e concluiu a transação da api-1", async () => (await preparadas()) === 0, 120);
    const lg = await chamar("GET", "/controlo/logs/api-2", { token: tokenAdmin(), consulta: { linhas: "80" } });
    for (const l of (lg.dados.linhas || []).filter((x) => /assumiu/i.test(x)).slice(-1)) passo(l.replace(/^.*?\[recupera/, "[recupera"), "ok");
    await totalDepois(antes);
    const x = await chamar("POST", "/controlo/reativar/api-1", { token: tokenAdmin() });
    passo(x.ok ? "api-1 reativada." : "Não foi possível reativar a api-1.", x.ok ? "" : "erro");
  } },
  { tipo: "app", rot: "Aplicação", t: "Uma API desliga-se", d: "Desliga a api-1. O gateway passa a enviar tudo para a api-2, sem o cliente notar.", f: async () => {
    await no("api-1", "parar"); await esperar(1000);
    for (let i = 1; i <= 4; i++) { const r = await chamar("GET", "/contas", { token: await tokenDemo("ana") }); passo(`Pedido ${i}: ${curta(r)}.`, r.ok ? "ok" : "erro"); }
    await no("api-1", "ligar");
    await esperarAte("a api-1 voltou ao balanceamento", async () => { const s = new Set(); for (let i = 0; i < 4; i++) s.add((await chamar("GET", "/saude")).no); return s.has("api-1"); }, 60);
  } },
  { tipo: "app", rot: "Saga", t: "O banco parceiro não responde", d: "Desliga o mainframe Java. A ordem é debitada, o envio falha e a compensação devolve o dinheiro.", f: async () => {
    const antes = await totalAntes();
    await no("legacy", "parar"); await esperar(1000);
    const corpo = await msgOrdem("EU-1001", "BANCO-PARCEIRO-MZ", "MZ59000100000012345678901", "70");
    const r = await chamar("POST", "/interbancario/ordens", { token: await tokenDemo("ana"), corpo });
    passo(`Ordem de 70 EUR: ${curta(r)}.`, r.dados.estado === "COMPENSADA" ? "ok" : "erro"); rasto(r, 5);
    await no("legacy", "ligar");
    await esperarAte("o mainframe voltou", async () => { await saudeAgora(); return saudeAtual && saudeAtual.legado && !saudeAtual.legado.erro; }, 60);
    await totalDepois(antes);
  } },
  { tipo: "dados", rot: "HDFS", t: "Um DataNode desliga-se", d: "Grava um relatório no HDFS, desliga o datanode-1 e lê o relatório na mesma, a partir da outra cópia.", f: async () => {
    const g = await chamar("POST", "/relatorios", { token: tokenAdmin() });
    passo(`Relatório gravado por ${g.dados.gerado_por || "?"}: ${g.dados.ficheiro || curta(g)}.`, g.ok ? "ok" : "erro");
    if (!g.ok) return;
    await no("datanode-1", "parar"); await esperar(1500);
    const r = await chamar("GET", "/relatorios/" + encodeURIComponent(g.dados.ficheiro.split("/").pop()), { token: tokenAdmin() });
    passo(`Leitura com o datanode-1 em baixo: ${curta(r)}, ${String(r.dados).trim().split("\n").length - 1} contas.`, r.ok ? "ok" : "erro");
    passo(r.sha === g.dados.sha256 ? "O SHA-256 lido é igual ao gravado." : "O SHA-256 não coincide.", r.sha === g.dados.sha256 ? "ok" : "erro");
    await no("datanode-1", "ligar");
  } },
  { tipo: "2pc", rot: "Concorrência", t: "20 transferências cruzadas", d: "Dez transferências num sentido e dez no outro, ao mesmo tempo, entre a Europa e a América. Sem deadlocks.", f: async () => {
    const antes = await totalAntes();
    await tokenDemo("ana"); await tokenDemo("diego");
    const ps = [];
    for (let i = 0; i < 10; i++) { ps.push(transferir("ana", "EU-1003", "AM-2002", "1")); ps.push(transferir("diego", "AM-2002", "EU-1003", "1")); }
    const rs = await Promise.all(ps);
    const ok = rs.filter((r) => r.dados && r.dados.estado === "CONFIRMADA").length;
    const nos = {}; rs.forEach((r) => { nos[r.no] = (nos[r.no] || 0) + 1; });
    passo(`${ok} de 20 confirmadas. Coordenadas por ${Object.entries(nos).map(([k, v]) => `${k} (${v})`).join(" e ")}.`, ok === 20 ? "ok" : "aviso");
    await totalDepois(antes);
  } },
];
let cenarioAtivo = false;
for (const c of CENARIOS) {
  const cartao = el("div", { class: "cen", "data-tipo": c.tipo }, el("span", { class: "tipo" }, c.rot), el("h3", {}, c.t), el("p", {}, c.d));
  cartao.append(el("button", { class: "acao", type: "button", onclick: async () => {
    if (!tokenAdmin()) { limpar(exec); passo("Os cenários precisam da sessão de administrador.", "erro"); abrirLogin(); return; }
    if (cenarioAtivo) return;
    cenarioAtivo = true; cartao.classList.add("a-correr");
    document.querySelectorAll("#lista-cenarios button").forEach((b) => { b.disabled = true; });
    secao(c.t); evento(`Cenário: ${c.t}`);
    try { await c.f(); passo("Cenário terminado.", "ok"); }
    catch (e) { passo("Erro: " + e.message, "erro"); }
    finally {
      cenarioAtivo = false; cartao.classList.remove("a-correr");
      document.querySelectorAll("#lista-cenarios button").forEach((b) => { b.disabled = false; });
    }
  } }, "Executar"));
  $("lista-cenarios").append(cartao);
}
$("btn-limpar-exec").addEventListener("click", () => { limpar(exec).append(el("li", { class: "vazio" }, "Escolha um cenário para começar.")); });

// ------------------------------------------------------------------ arranque
construirMapa();
$("eventos").append(el("li", { class: "vazio" }, "Os acontecimentos da rede aparecem aqui."));
aplicarSessao();
if (!sessaoValida()) abrirLogin();
ciclo();
