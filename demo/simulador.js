/* =============================================================================
 * Global-Scale System — SIMULADOR para a demonstração no GitHub Pages
 *
 * O GitHub Pages só serve ficheiros estáticos: não há PostgreSQL, Java, HDFS nem
 * Docker. Este ficheiro substitui o `fetch` do browser e responde aos pedidos da
 * interface com um modelo em memória que segue as MESMAS regras do sistema real
 * (services/core/app): fragmentos por região, conversão de moeda com a tabela
 * replicada, Two-Phase Commit, Saga com compensação, sentinela com failover,
 * fencing e reintegração, Nó Central com espelho e reconciliação, JWT, SHA-256,
 * anti-replay e cifragem AES-256-GCM (Web Crypto).
 *
 * É uma SIMULAÇÃO: os dados vivem só nesta página e recomeçam ao recarregar.
 * O sistema real corre com `docker compose up -d --build` (ver o README).
 * ============================================================================= */
(() => {
  "use strict";

  // ------------------------------------------------------------ utilitários
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const enc = new TextEncoder();
  const hexDe = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const sha256 = async (t) => hexDe(await crypto.subtle.digest("SHA-256", enc.encode(t)));
  const canonico = (o) => JSON.stringify(Object.fromEntries(Object.keys(o).sort().map((k) => [k, o[k]])));
  const relogio = () => { const d = new Date(); return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0"); };
  const horaCurta = () => new Date().toTimeString().slice(0, 8);
  const iso = (t = Date.now()) => new Date(t).toISOString();
  const uuid = () => crypto.randomUUID();
  const b64url = (s) => btoa(String.fromCharCode(...enc.encode(s))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const deB64url = (s) => { s = s.replace(/-/g, "+").replace(/_/g, "/"); const bin = atob(s + "===".slice((s.length + 3) % 4));
    return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))); };
  const c2s = (c) => (c / 100).toFixed(2);                      // cêntimos -> "123.45"
  const s2c = (s) => Math.round(Number(s) * 100);
  function arredondarPar(x) {                                    // arredondamento bancário (half-even)
    const f = Math.floor(x), d = x - f;
    if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
    return Math.round(x);
  }

  // ------------------------------------------------------------ catálogo
  const NOMES = { EU: "db-europa (Nó A — Europa)", AM: "db-america (Nó B — América)", AF: "db-africa (Nó C — África)", CT: "db-central (Nó Central)" };
  const REGIAO = { EU: "Europa", AM: "América", AF: "África" };
  const FRAG = {
    EU: { nome: "europa", nos: ["db-europa", "db-europa-r"] }, AM: { nome: "america", nos: ["db-america", "db-america-r"] },
    AF: { nome: "africa", nos: ["db-africa", "db-africa-r"] }, CT: { nome: "central", nos: ["db-central", "db-central-r"] },
  };
  for (const f of Object.values(FRAG)) Object.assign(f, { primario: f.nos[0], falhas: 0, sincrona: true, vedados: new Set(), aRecuperar: new Set(), avisos: new Set() });
  const SERVICOS = ["db-europa", "db-europa-r", "db-america", "db-america-r", "db-africa", "db-africa-r", "db-central", "db-central-r",
    "api-1", "api-2", "auth", "legacy", "namenode", "datanode-1", "datanode-2", "sentinela", "gateway"];
  const NOS = Object.fromEntries(SERVICOS.map((s) => [s, { estado: "running", desde: Date.now() }]));
  const DESATIVADAS = new Set();
  const LOGS = Object.fromEntries(SERVICOS.map((s) => [s, []]));
  const log = (svc, l) => { LOGS[svc].push(`[${horaCurta()}] ${l}`); if (LOGS[svc].length > 300) LOGS[svc].shift(); };
  const corre = (s) => NOS[s].estado === "running";
  const app = (no) => no.replace(/-/g, "_");
  const outro = (f, no) => (f.nos[0] === no ? f.nos[1] : f.nos[0]);
  const shardDe = (conta) => { const p = String(conta).split("-")[0].toUpperCase(); return REGIAO[p] ? p : null; };

  const TAXAS = { MZN: 1, EUR: 69.5, USD: 63.9, ZAR: 3.55 };
  const COPIAS_TAXAS = Object.fromEntries(Object.keys(FRAG).map((k) => [k, { ...TAXAS }]));
  const TAXAS_EM = Object.fromEntries(Object.keys(FRAG).map((k) => [k, iso()]));

  const UTILIZADORES = {
    ana: { nome: "Ana Machava", papel: "cliente", pass: "Ana@2026" }, carla: { nome: "Carla Sitoe", papel: "cliente", pass: "Carla@2026" },
    bruno: { nome: "Bruno Cossa", papel: "cliente", pass: "Bruno@2026" }, diego: { nome: "Diego Mondlane", papel: "cliente", pass: "Diego@2026" },
    fatima: { nome: "Fátima Langa", papel: "cliente", pass: "Fatima@2026" }, thabo: { nome: "Thabo Nkosi", papel: "cliente", pass: "Thabo@2026" },
    admin: { nome: "Administrador", papel: "admin", pass: "Admin@2026" },
  };
  const FALHAS_LOGIN = {};
  const CONTAS = {};
  const MOV = { EU: [], AM: [], AF: [] };
  const LSN = { EU: 0x3000000, AM: 0x3000000, AF: 0x3000000, CT: 0x3000000 };
  const LSN_REPLICA = {};
  const ESPELHO = {};                  // conta -> {saldo, versao}
  const PENDENTES = [];                // operações autorizadas pela central
  const POR_VERIFICAR = new Set(), COM_PENDENTES = new Set();
  const PREPARADAS = [];               // {gtrid, shard, desde, dono, aplicar()}
  const WAL = {};                      // gtrid -> {decisao, dono, participantes}
  const PROCESSADAS = new Set();       // tx_id já com movimentos (UNIQUE -> anti-replay)
  const CARTEIRAS = { "841234567": "Joana Mabunda", "851112233": "Carlos Tembe", "861234567": "Luísa Cumbe",
    "871112233": "Rui Macuácua", "821234567": "Sónia Bila", "831112233": "Pedro Nhaca" };
  const PREFIXOS = { "M-PESA": ["84", "85"], "E-MOLA": ["86", "87"], MKESH: ["82", "83"] };
  const RELATORIOS = [];
  const AUDITORIA = { "api-1": [], "api-2": [] };
  let autorizacao = 700000;

  // ------------------------------------------------------------ cifragem AES-256-GCM (Web Crypto)
  let CHAVE = null;
  async function cifrar(texto, contexto) {
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce, additionalData: enc.encode(contexto) }, CHAVE, enc.encode(texto));
    return "01" + hexDe(nonce) + hexDe(ct);
  }
  async function novaConta(id, titular, owner, moeda, saldo) {
    const reg = shardDe(id);
    CONTAS[id] = { id, titular, owner, regiao: reg, moeda, saldo, versao: 0, cif: await cifrar(titular, `contas:${id}:titular`) };
    ESPELHO[id] = { saldo, versao: 0, em: iso() };
    LSN[reg] += 0x120;
  }
  const pronto = (async () => {
    CHAVE = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    await novaConta("EU-1001", "Ana Machava", "ana", "EUR", 500000);
    await novaConta("EU-1002", "Carla Sitoe", "carla", "EUR", 300000);
    await novaConta("EU-1003", "Ana Machava", "ana", "MZN", 25000000);
    await novaConta("AM-2001", "Bruno Cossa", "bruno", "USD", 400000);
    await novaConta("AM-2002", "Diego Mondlane", "diego", "MZN", 18000000);
    await novaConta("AF-3001", "Fátima Langa", "fatima", "MZN", 15000000);
    await novaConta("AF-3002", "Thabo Nkosi", "thabo", "ZAR", 2500000);
    for (const k of Object.keys(LSN)) LSN_REPLICA[k] = LSN[k];
    log("sentinela", "[sentinela] a vigiar: " + Object.values(FRAG).map((f) => `${f.nome} (${f.nos[0]} e ${f.nos[1]})`).join(", "));
    for (const f of Object.values(FRAG)) log("sentinela", `[sentinela] ${f.nome}: réplica ${f.nos[1]} em streaming -> replicação SÍNCRONA ativada`);
    for (const a of ["api-1", "api-2"]) { log(a, "INFO:     Application startup complete."); novoFicheiroAuditoria(a); }
    for (const n of Object.values(FRAG).flatMap((f) => f.nos)) log(n, "LOG:  database system is ready to accept connections");
  })();

  // ------------------------------------------------------------ JWT (assinatura simulada)
  const SEGREDO = "gss-demo-chave-do-emissor";    // no sistema real: RSA-2048 (RS256), só no nó auth
  async function emitirToken(user) {
    const u = UTILIZADORES[user], agora = Math.floor(Date.now() / 1000);
    const h = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid: "demo" }));
    const p = b64url(JSON.stringify({ iss: "global-scale-auth", aud: "global-scale-api", sub: user, nome: u.nome, papel: u.papel,
      iat: agora, nbf: agora, exp: agora + 900, jti: uuid() }));
    return `${h}.${p}.${(await sha256(SEGREDO + h + "." + p)).slice(0, 43)}`;
  }
  async function validarToken(auth) {
    if (!auth || !auth.startsWith("Bearer ")) return [null, respG(401, { detail: "Token JWT em falta (cabeçalho Authorization: Bearer ...)" })];
    const [h, p, s] = auth.slice(7).split(".");
    if (!h || !p || !s || (await sha256(SEGREDO + h + "." + p)).slice(0, 43) !== s)
      return [null, respG(401, { detail: "Token inválido: Signature verification failed" })];
    const c = JSON.parse(deB64url(p));
    if (c.exp * 1000 < Date.now()) return [null, respG(401, { detail: "Token expirado" })];
    return [{ username: c.sub, papel: c.papel, admin: c.papel === "admin" }, null];
  }

  // ------------------------------------------------------------ respostas
  let rr = 0;
  function apiQueResponde(via) {
    const vivas = ["api-1", "api-2"].filter((a) => corre(a) && !DESATIVADAS.has(a));
    if (via) return vivas.includes(via) ? via : null;
    if (!vivas.length) return null;
    return vivas[rr++ % vivas.length];
  }
  function respG(status, corpo, extra = {}, no = null) {
    const texto = typeof corpo === "string" ? corpo : JSON.stringify(corpo);
    return new Response(texto, { status, headers: { "Content-Type": typeof corpo === "string" ? "text/plain" : "application/json",
      ...(no ? { "X-No-Servidor": no } : {}), ...extra } });
  }
  class Falha extends Error { constructor(status, codigo, detalhe, extra = {}) { super(detalhe); Object.assign(this, { status, codigo, extra }); } }
  const indisponivel = (s, motivo) => new Falha(503, "FRAGMENTO_INDISPONIVEL", `Fragmento ${NOMES[s]} indisponível: ${motivo}`,
    { estado: "ABORTADA", nota: "Nenhum dinheiro foi movimentado: a transação foi abortada de forma atómica." });

  // ------------------------------------------------------------ estado dos fragmentos
  const reintegrando = (no) => Object.values(FRAG).some((f) => f.aRecuperar.has(no));
  const primarioVivo = (s) => { const f = FRAG[s]; return corre(f.primario) && !reintegrando(f.primario); };
  const regiaoEmBaixo = (s) => FRAG[s].nos.every((n) => !corre(n));
  async function esperarPrimario(s, maxMs = 25000) {
    const fim = Date.now() + maxMs;
    while (!primarioVivo(s)) {
      if (Date.now() > fim || regiaoEmBaixo(s)) throw indisponivel(s, "nenhum nó aceita escritas");
      await sleep(400);
    }
  }
  function replicaSincrona(s) { const f = FRAG[s], r = outro(f, f.primario); return corre(r) && !reintegrando(r) && f.sincrona; }
  function escrever(s) {
    LSN[s] += 0x1a8 + Math.floor(Math.random() * 0x60);
    if (replicaSincrona(s)) LSN_REPLICA[s] = LSN[s];
  }
  const lsnTexto = (v) => "0/" + v.toString(16).toUpperCase();

  // ------------------------------------------------------------ sentinela (a cada segundo)
  function sentinela() {
    if (!corre("sentinela")) return;
    for (const [s, f] of Object.entries(FRAG)) {
      const rep = outro(f, f.primario);
      if (primarioVivo(s) || (corre(f.primario) && f.aRecuperar.has(f.primario))) {
        f.falhas = 0; f.avisos.delete("baixo");
        const sinc = corre(rep) && !reintegrando(rep);
        if (sinc && !f.sincrona) { f.sincrona = true; LSN_REPLICA[s] = LSN[s]; log("sentinela", `[sentinela] ${f.nome}: réplica ${rep} em streaming -> replicação SÍNCRONA ativada`); }
        if (!sinc && f.sincrona) { f.sincrona = false; log("sentinela", `[sentinela] ${f.nome}: réplica ${rep} em baixo -> primário passa a ASSÍNCRONO (não bloqueia as escritas)`); }
        continue;
      }
      if (f.falhas >= 3) continue;
      f.falhas++;
      log("sentinela", `[sentinela] ${f.nome}: primário ${f.primario} não responde (${f.falhas}/3)`);
      if (f.falhas < 3) continue;
      if (!corre(rep) || reintegrando(rep)) { log("sentinela", `[sentinela] ${f.nome}: réplica ${rep} também inacessível — fragmento em baixo`); continue; }
      if (!f.sincrona) { log("sentinela", `[sentinela] ${f.nome}: NÃO promove ${rep}: não estava síncrona (risco de perder transações confirmadas). Intervenção manual necessária.`); continue; }
      f.vedados.add(f.primario);
      log("sentinela", `[sentinela] ${f.nome}: FENCING — ${f.primario} vedado (não poderá voltar como primário; volta como réplica)`);
      f.primario = rep; f.falhas = 0; f.sincrona = false;
      log("sentinela", `[sentinela] ${f.nome}: FAILOVER — ${rep} PROMOVIDA a primário`);
      log(rep, "LOG:  received promote request"); log(rep, "LOG:  database system is ready to accept connections");
    }
  }

  // ------------------------------------------------------------ controlo dos contentores
  function arrancarDB(no) {
    const [s, f] = Object.entries(FRAG).find(([, x]) => x.nos.includes(no));
    const par = outro(f, no);
    if (f.vedados.has(no)) {
      f.aRecuperar.add(no);
      log(no, `[fencing] ${no} foi substituído por ${par}: não volta a arrancar como primário (evita split-brain).`);
      log(no, `[reintegração] ${no} volta como RÉPLICA de ${par} (cópia base nova; a replicação era síncrona, nada se perde).`);
      const tentar = () => {
        if (!corre(no)) { f.aRecuperar.delete(no); return; }
        if (!primarioVivo(s)) return setTimeout(tentar, 1000);
        f.vedados.delete(no); f.aRecuperar.delete(no); LSN_REPLICA[s] = LSN[s];
        log(no, `[reintegração] ${no} é agora réplica de ${par} (streaming replication).`);
      };
      setTimeout(tentar, 3500);
      return;
    }
    if (no !== f.primario && !corre(f.primario) && !f.vedados.size) {
      // os dois estavam em baixo e liga primeiro a réplica: espera pelo primário
      log(no, `[réplica] À espera do primário ${f.primario}...`);
    }
    log(no, "LOG:  database system is ready to accept connections");
    if (no !== f.primario) { f.aRecuperar.add(no); setTimeout(() => { f.aRecuperar.delete(no); LSN_REPLICA[s] = LSN[s]; }, 1500); }
  }
  function acaoNo(svc, acao) {
    const n = NOS[svc];
    if (acao === "parar" || acao === "matar") {
      if (n.estado !== "running") return;
      n.estado = "exited"; n.desde = Date.now();
      log(svc, acao === "matar" ? "(processo terminado com SIGKILL)" : "LOG:  received fast shutdown request");
    } else if (acao === "ligar") {
      if (n.estado === "running") return;
      n.estado = "running"; n.desde = Date.now();
      if (svc.startsWith("db-")) arrancarDB(svc);
      else if (svc.startsWith("api-")) arrancarApi(svc);
      else log(svc, "started");
    } else if (acao === "reiniciar") {
      acaoNo(svc, "parar"); setTimeout(() => acaoNo(svc, "ligar"), 1200);
    }
  }
  function arrancarApi(a) {
    if (DESATIVADAS.has(a)) { log(a, `[arranque] ${a} está desativada (falha permanente simulada): não arranca.`); NOS[a].estado = "exited"; return; }
    log(a, "INFO:     Started server process"); log(a, "INFO:     Application startup complete.");
    novoFicheiroAuditoria(a);
    for (const l of recuperar(a)) log(a, `[recuperação/arranque] ${l}`);
  }
  function crashApi(a, permanente) {
    NOS[a].estado = "exited"; NOS[a].desde = Date.now();
    log(a, "⚡ FALHA INJETADA: o processo coordenador vai terminar abruptamente");
    if (permanente) { DESATIVADAS.add(a); return; }
    setTimeout(() => { NOS[a].estado = "running"; NOS[a].desde = Date.now(); arrancarApi(a); }, 3000);   // restart: unless-stopped
  }

  // ------------------------------------------------------------ auditoria (cadeia de hashes no HDFS)
  function novoFicheiroAuditoria(a) {
    AUDITORIA[a].push({ nome: new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + ".log", entradas: [], anterior: "0".repeat(64) });
  }
  async function auditar(a, evento, dados) {
    const f = AUDITORIA[a][AUDITORIA[a].length - 1];
    const e = { ts: iso(), no: a, evento, ...dados, hash_anterior: f.anterior };
    e.hash = await sha256(canonico(e));
    f.anterior = e.hash; f.entradas.push(e); f.modificado = iso();
  }

  // ------------------------------------------------------------ movimentos
  function contraparteDe(m) { return m.contraparte; }
  async function movimentar(s, conta, tipo, valor, moeda, txId, contraparte, hash, protocolo, coord, conv, lado, quando) {
    const k = CONTAS[conta];
    const sinal = ["DEBITO", "DEBITO_EXTERNO"].includes(tipo) ? -1 : 1;
    k.saldo += sinal * valor; k.versao++;
    let vc = null, mc = null, taxa = null;
    if (conv && conv.mo !== conv.md) [vc, mc] = lado === "origem" ? [conv.vd, conv.md] : [conv.vo, conv.mo];
    if (conv && conv.mo !== conv.md) taxa = conv.taxa;
    MOV[s].push({ id: MOV[s].length + 1, tx_id: txId, conta, tipo, valor, moeda, vc, mc, taxa, contraparte, hash, protocolo, coord,
      cif: await cifrar(contraparte, `movimentos:${txId}:${conta}:${tipo}:contraparte`), criado_em: quando || iso() });
    escrever(s);
    publicar(conta);
  }
  function publicar(conta) {                     // espelho do Nó Central (UPDATE condicional por versão)
    const k = CONTAS[conta];
    if (!primarioVivo("CT")) { POR_VERIFICAR.add(k.regiao); return; }
    if (PENDENTES.some((p) => p.conta === conta && p.estado === "PENDENTE")) return;
    const e = ESPELHO[conta];
    if (!e || e.versao < k.versao) { ESPELHO[conta] = { saldo: k.saldo, versao: k.versao, em: iso() }; escrever("CT"); }
  }
  function converter(valor, mo, md, t) {
    const vd = arredondarPar(valor * t[mo] / t[md]);
    return { vo: valor, mo, vd, md, taxa: (t[mo] / t[md]).toFixed(8), to: t[mo], td: t[md] };
  }
  const resumo = (c) => ({ debitado: `${c2s(c.vo)} ${c.mo}`, creditado: `${c2s(c.vd)} ${c.md}`, taxa: `1 ${c.mo} = ${c.taxa} ${c.md}`,
    referencia: `1 ${c.mo} = ${c.to.toFixed(6)} MZN; 1 ${c.md} = ${c.td.toFixed(6)} MZN` });
  function verificarDebito(k, valor, u) {
    if (k.owner !== u.username) throw new Falha(403, "NAO_AUTORIZADO", `O utilizador '${u.username}' não é titular da conta ${k.id}` +
      (u.admin ? " (o administrador não movimenta contas de clientes)" : ""), { estado: "ABORTADA" });
    if (k.saldo < valor) throw new Falha(409, "SALDO_INSUFICIENTE", `Saldo insuficiente em ${k.id}: disponível ${c2s(k.saldo)} ${k.moeda}, pedido ${c2s(valor)} ${k.moeda}`, { estado: "ABORTADA" });
  }
  function existe(conta, papel) { if (!CONTAS[conta]) throw new Falha(404, "CONTA_INEXISTENTE", `Conta de ${papel} '${conta}' não existe`); return CONTAS[conta]; }

  // ------------------------------------------------------------ Nó Central
  function podeSubstituir(s) {
    if (!primarioVivo("CT")) throw indisponivel(s, `a região ${REGIAO[s]} e o Nó Central estão os dois em baixo`);
    if (POR_VERIFICAR.has(s)) throw indisponivel(s, "a região está em baixo e o Nó Central não tem a certeza do saldo atual destas contas");
  }
  async function reconciliar(s, a) {
    if (!primarioVivo(s) || !primarioVivo("CT")) return [];
    const feitas = [];
    for (const p of PENDENTES.filter((x) => x.regiao === s && x.estado === "PENDENTE")) {
      const k = CONTAS[p.conta];
      k.saldo += (["DEBITO", "DEBITO_EXTERNO"].includes(p.tipo) ? -1 : 1) * p.valor; k.versao++;
      MOV[s].push({ id: MOV[s].length + 1, tx_id: p.tx_id, conta: p.conta, tipo: p.tipo, valor: p.valor, moeda: p.moeda, vc: p.vc, mc: p.mc,
        taxa: p.taxa, contraparte: p.contraparte, hash: p.hash, protocolo: "CENTRAL", coord: p.coord, cif: p.cif, criado_em: p.criado_em });
      escrever(s);
      p.estado = "APLICADA"; p.aplicada_em = iso(); p.aplicada_por = a;
      ESPELHO[p.conta].versao = k.versao;
      const l = `${s}: ${p.tipo} de ${c2s(p.valor)} ${p.moeda} em ${p.conta} (tx ${p.tx_id}) aplicada`;
      log(a, `[central] ${l}`); feitas.push(l);
    }
    COM_PENDENTES.delete(s);
    if (feitas.length) log(a, `[central] reconciliação de ${REGIAO[s]}: ${feitas.length} operação(ões) aplicada(s)`);
    return feitas;
  }
  async function garantirReconciliada(s, no) {
    if (!COM_PENDENTES.has(s)) return;
    await reconciliar(s, no);
    if (COM_PENDENTES.has(s)) throw indisponivel(s, "a região voltou, mas ainda está a receber as operações feitas pela central");
  }
  async function registarPendente(no, conta, tipo, valor, moeda, txId, contraparte, hash, conv, lado) {
    const k = CONTAS[conta], e = ESPELHO[conta];
    e.saldo += (["DEBITO", "DEBITO_EXTERNO"].includes(tipo) ? -1 : 1) * valor; e.em = iso();
    let vc = null, mc = null, taxa = null;
    if (conv && conv.mo !== conv.md) { [vc, mc] = lado === "origem" ? [conv.vd, conv.md] : [conv.vo, conv.mo]; taxa = conv.taxa; }
    PENDENTES.push({ id: PENDENTES.length + 1, tx_id: txId, conta, regiao: k.regiao, tipo, valor, moeda, vc, mc, taxa, contraparte, hash,
      coord: no, cif: await cifrar(contraparte, `movimentos:${txId}:${conta}:${tipo}:contraparte`), criado_em: iso(), estado: "PENDENTE" });
    COM_PENDENTES.add(k.regiao); escrever("CT");
  }
  const saldoVisto = (k, viaCentral) => (viaCentral ? ESPELHO[k.id].saldo : k.saldo);

  // ------------------------------------------------------------ recuperação (a cada 3 s, por API)
  function recuperar(a) {
    const acoes = [];
    const outra = a === "api-1" ? "api-2" : "api-1";
    const outraEmBaixo = !corre(outra) || DESATIVADAS.has(outra);
    for (const p of [...PREPARADAS]) {
      if (!primarioVivo(p.shard)) continue;
      const w = WAL[p.gtrid] || {};
      let dono = p.dono, assumiu = false;
      if (dono !== a) { if (!outraEmBaixo) continue; assumiu = true; }
      if (w.decisao === "COMMIT") {
        p.aplicar(); PREPARADAS.splice(PREPARADAS.indexOf(p), 1);
        acoes.push(assumiu ? `${p.shard}: ${a} assumiu ${p.gtrid} do coordenador ${dono} (em baixo): COMMIT PREPARED (decisão no WAL de ${dono} = COMMIT)`
          : `${p.shard}: COMMIT PREPARED de ${p.gtrid} (decisão no WAL = COMMIT)`);
      } else if (w.decisao === "ABORT" || Date.now() - p.desde > 60000) {
        PREPARADAS.splice(PREPARADAS.indexOf(p), 1);
        acoes.push(`${p.shard}: ROLLBACK PREPARED de ${p.gtrid}`);
      }
    }
    return acoes;
  }
  setInterval(async () => {
    await pronto;
    sentinela();
  }, 1000);
  setInterval(async () => {
    await pronto;
    for (const a of ["api-1", "api-2"]) {
      if (!corre(a) || DESATIVADAS.has(a)) continue;
      for (const l of recuperar(a)) log(a, `[recuperação] ${l}`);
      for (const s of ["EU", "AM", "AF"]) {
        if (COM_PENDENTES.has(s) || PENDENTES.some((p) => p.regiao === s && p.estado === "PENDENTE")) await reconciliar(s, a);
        if (POR_VERIFICAR.has(s) && primarioVivo(s) && primarioVivo("CT")) {
          for (const k of Object.values(CONTAS).filter((x) => x.regiao === s)) publicar(k.id);
          POR_VERIFICAR.delete(s); log(a, `[central] espelho de ${REGIAO[s]} confirmado igual à região`);
        }
      }
    }
  }, 3000);

  // ------------------------------------------------------------ transferências
  function validarMensagem(campos, hashRecebido) {
    return sha256(canonico(campos)).then((h) => {
      if (h !== String(hashRecebido || "").toLowerCase())
        throw new Falha(400, null, null, { detail: { erro: "INTEGRIDADE_VIOLADA", detalhe: "O hash SHA-256 não corresponde ao conteúdo: a mensagem foi adulterada.",
          hash_recebido: hashRecebido, hash_calculado: h } });
      const desvio = Math.abs(Date.now() - Date.parse(campos.timestamp)) / 1000;
      if (!(desvio <= 300)) throw new Falha(400, null, null, { detail: { erro: "MENSAGEM_EXPIRADA",
        detalhe: `Desvio de ${Math.floor(desvio)}s excede o máximo de 300s (possível replay).` } });
      return h;
    });
  }
  function duplicada(txId) {
    if (PROCESSADAS.has(txId)) throw new Falha(409, "TRANSACAO_DUPLICADA", `A transação ${txId} já foi processada (proteção anti-replay / idempotência)`);
  }

  async function transferir(u, tx, hash, falha) {
    const origem = tx.origem.toUpperCase(), destino = tx.destino.toUpperCase();
    if (origem === destino) throw new Falha(400, "PEDIDO_INVALIDO", "Origem e destino não podem ser a mesma conta");
    const valor = s2c(tx.valor);
    if (!(valor > 0)) throw new Falha(400, "PEDIDO_INVALIDO", "O valor tem de ser positivo");
    const so = shardDe(origem), sd = shardDe(destino);
    if (!so) throw new Falha(404, "CONTA_INEXISTENTE", `Conta '${origem}': região desconhecida`);
    if (!sd) throw new Falha(404, "CONTA_INEXISTENTE", `Conta '${destino}': região desconhecida`);
    duplicada(tx.id);
    const emBaixo = { [so]: regiaoEmBaixo(so), [sd]: regiaoEmBaixo(sd) };
    if (Object.values(emBaixo).some(Boolean)) return viaCentral(u, tx.id, origem, destino, valor, so, sd, emBaixo, hash);
    for (const s of new Set([so, sd])) await garantirReconciliada(s, u.no);
    if (so === sd) {
      await esperarPrimario(so);
      const lo = existe(origem, "origem"), ld = existe(destino, "destino");
      verificarDebito(lo, valor, u);
      const conv = converter(valor, lo.moeda, ld.moeda, COPIAS_TAXAS[so]);
      PROCESSADAS.add(tx.id);
      await movimentar(so, origem, "DEBITO", valor, lo.moeda, tx.id, destino, hash, "LOCAL", u.no, conv, "origem");
      await movimentar(so, destino, "CREDITO", conv.vd, ld.moeda, tx.id, origem, hash, "LOCAL", u.no, conv, "destino");
      await auditar(u.no, "TRANSFERENCIA_LOCAL", { tx_id: tx.id, origem, destino, hash });
      return { tx_id: tx.id, estado: "CONFIRMADA", protocolo: "LOCAL (transação ACID num único fragmento)", fragmento: NOMES[so],
        origem, destino, conversao: resumo(conv) };
    }
    return doisPC(u, tx.id, origem, destino, valor, so, sd, hash, falha);
  }

  async function doisPC(u, txId, origem, destino, valor, so, sd, hash, falha) {
    const coord = u.no, gtrid = `${coord}|${txId}`, rasto = [];
    const r = (m) => rasto.push(`[${relogio()}] ${m}`);
    const papel = { [origem]: [so, "origem"], [destino]: [sd, "destino"] };
    const ordem = [origem, destino].sort();
    WAL[gtrid] = { estado: "INICIO", participantes: [so, sd], dono: coord };
    r(`COORDENADOR ${coord}: início da transação global ${gtrid}`);
    let conv;
    try {
      for (const [i, conta] of ordem.entries()) {
        const [s, p] = papel[conta];
        if (falha === "participante_indisponivel_fase1" && i === 1) {
          r(`⚡ FALHA INJETADA: participante ${s} inacessível (timeout de rede)`);
          throw indisponivel(s, "timeout de rede simulado");
        }
        await esperarPrimario(s);
        existe(conta, p);
        r(`FASE 1: conta ${conta} bloqueada em ${s} (moeda ${CONTAS[conta].moeda})`);
      }
      const lo = CONTAS[origem], ld = CONTAS[destino];
      verificarDebito(lo, valor, u);
      conv = converter(valor, lo.moeda, ld.moeda, COPIAS_TAXAS[so]);
      if (lo.moeda !== ld.moeda) r(`CONVERSÃO: ${c2s(valor)} ${lo.moeda} × (${conv.to.toFixed(6)} / ${conv.td.toFixed(6)}) = ${c2s(conv.vd)} ${ld.moeda} (taxa igual nas 2 réplicas)`);
      for (const conta of ordem) {
        const [s, p] = papel[conta];
        if (!primarioVivo(s)) throw indisponivel(s, "a ligação caiu durante a preparação");
        const fazer = p === "origem"
          ? () => movimentar(so, origem, "DEBITO", valor, lo.moeda, txId, destino, hash, "2PC", coord, conv, "origem")
          : () => movimentar(sd, destino, "CREDITO", conv.vd, ld.moeda, txId, origem, hash, "2PC", coord, conv, "destino");
        r(`FASE 1: ${p === "origem" ? "DEBITO" : "CREDITO"} de ${p === "origem" ? c2s(valor) + " " + lo.moeda : c2s(conv.vd) + " " + ld.moeda} em ${conta} executado em ${s}`);
        if (p === "origem") lo.saldo -= valor;                     // bloqueado na transação preparada
        PREPARADAS.push({ gtrid, shard: s, desde: Date.now(), dono: coord, aplicar: async () => { if (p === "origem") lo.saldo += valor; await fazer(); } });
        escrever(s);
        r(`FASE 1: participante ${s} respondeu PREPARED (voto SIM, gravado em disco)`);
      }
    } catch (e) {
      WAL[gtrid] = { ...WAL[gtrid], estado: "ABORT", decisao: "ABORT" };
      r(`DECISÃO: ABORT (${e.codigo || e.name}: ${e.message})`);
      for (const p of PREPARADAS.filter((x) => x.gtrid === gtrid)) {
        if (p.shard === so && CONTAS[origem]) CONTAS[origem].saldo += valor;
        PREPARADAS.splice(PREPARADAS.indexOf(p), 1);
        r(`FASE 2: ROLLBACK em ${p.shard} ✓`);
      }
      await auditar(coord, "2PC_ABORT", { tx_id: txId, origem, destino, motivo: String(e.message).slice(0, 200), hash });
      e.extra = { ...e.extra, rasto };
      throw e;
    }
    PROCESSADAS.add(txId);
    WAL[gtrid] = { ...WAL[gtrid], estado: "COMMIT", decisao: "COMMIT" };
    r("DECISÃO: COMMIT gravada no registo durável do coordenador (fsync)");
    if (falha === "atraso_fase2") { r("⚡ FALHA INJETADA: coordenador aguarda 20s antes da fase 2"); await sleep(20000); }
    const pendentes = [];
    const minhas = PREPARADAS.filter((x) => x.gtrid === gtrid).sort((a, b) => (a.shard < b.shard ? -1 : 1));
    for (const [i, p] of minhas.entries()) {
      if (primarioVivo(p.shard)) {
        await p.aplicar(); PREPARADAS.splice(PREPARADAS.indexOf(p), 1);
        r(`FASE 2: COMMIT PREPARED em ${p.shard} ✓`);
      } else {
        pendentes.push(p.shard);
        r(`FASE 2: ${p.shard} inacessível (OperationalError); fica PREPARADA e será confirmada pela recuperação automática`);
      }
      if ((falha === "crash_coordenador_fase2" || falha === "crash_permanente_coordenador") && i === 0) {
        r("⚡ FALHA INJETADA: o processo coordenador vai terminar abruptamente");
        await auditar(coord, "2PC_CRASH_SIMULADO", { tx_id: txId, falha });
        crashApi(coord, falha === "crash_permanente_coordenador");
        throw new Falha(502, "SEM_RESPOSTA", "A ligação foi cortada antes de haver resposta.");
      }
    }
    await auditar(coord, "2PC_COMMIT", { tx_id: txId, origem, destino, hash, pendentes });
    return { tx_id: txId, estado: pendentes.length ? "CONFIRMADA_PENDENTE" : "CONFIRMADA", protocolo: "Two-Phase Commit (2PC)",
      coordenador: coord, participantes: [so, sd], participantes_pendentes: pendentes, origem, destino, conversao: resumo(conv), rasto };
  }

  async function viaCentral(u, txId, origem, destino, valor, so, sd, emBaixo, hash) {
    const rasto = [], r = (m) => rasto.push(`[${relogio()}] ${m}`);
    for (const [s, baixo] of Object.entries(emBaixo)) {
      if (baixo) { r(`Região ${REGIAO[s]}: os dois nós (${FRAG[s].nos.join(" e ")}) não respondem -> a parte desta região segue pelo NÓ CENTRAL`); podeSubstituir(s); }
      else await garantirReconciliada(s, u.no);
    }
    const onde = { [origem]: emBaixo[so] ? "CT" : so, [destino]: emBaixo[sd] ? "CT" : sd };
    const regiao = { [origem]: so, [destino]: sd };
    const participantes = [...new Set(Object.values(onde))].sort();
    const dois = participantes.length > 1;
    r(`COORDENADOR ${u.no}: participantes ${participantes.map((p) => NOMES[p]).join(", ")}` + (dois ? " (Two-Phase Commit)" : " (transação local na central)"));
    for (const p of participantes) await esperarPrimario(p);
    for (const conta of [origem, destino].sort()) {
      const k = existe(conta, conta === origem ? "origem" : "destino");
      r(`conta ${conta} bloqueada ` + (onde[conta] === "CT" ? `no ESPELHO da central (saldo ${c2s(ESPELHO[conta].saldo)} ${k.moeda})` : `em ${onde[conta]}`));
    }
    const lo = CONTAS[origem], ld = CONTAS[destino];
    verificarDebito({ ...lo, saldo: saldoVisto(lo, onde[origem] === "CT") }, valor, u);
    const conv = converter(valor, lo.moeda, ld.moeda, COPIAS_TAXAS.CT);
    PROCESSADAS.add(txId);
    for (const [conta, tipo, v, m, contra, lado] of [[origem, "DEBITO", valor, lo.moeda, destino, "origem"], [destino, "CREDITO", conv.vd, ld.moeda, origem, "destino"]]) {
      if (onde[conta] === "CT") {
        await registarPendente(u.no, conta, tipo, v, m, txId, contra, hash, conv, lado);
        r(`CENTRAL: ${tipo} de ${c2s(v)} ${m} em ${conta} autorizado e guardado como PENDENTE (aplicado quando a ${REGIAO[regiao[conta]]} voltar)`);
      } else {
        await movimentar(onde[conta], conta, tipo, v, m, txId, contra, hash, "2PC", u.no, conv, lado);
        r(`${onde[conta]}: ${tipo} de ${c2s(v)} ${m} em ${conta} executado`);
      }
    }
    if (dois) for (const p of participantes) r(`FASE 1: ${NOMES[p]} respondeu PREPARED (voto SIM)`);
    r("DECISÃO: COMMIT gravada no registo durável do coordenador (fsync)");
    for (const p of participantes) r((dois ? "FASE 2: COMMIT PREPARED em " : "COMMIT em ") + `${NOMES[p]} ✓`);
    await auditar(u.no, "TRANSFERENCIA_VIA_CENTRAL", { tx_id: txId, origem, destino, hash });
    const espera = [origem, destino].filter((k) => onde[k] === "CT").map((k) => `${k}: ${k === origem ? "débito" : "crédito"} de ${k === origem ? c2s(valor) : c2s(conv.vd)} ${k === origem ? lo.moeda : ld.moeda}, aplicado quando a ${REGIAO[regiao[k]]} voltar`);
    return { tx_id: txId, estado: "CONFIRMADA", protocolo: "Via NÓ CENTRAL" + (dois ? " (Two-Phase Commit central + região)" : " (transação local na central)"),
      via_central: true, regioes_em_baixo: Object.keys(emBaixo).filter((s) => emBaixo[s]), coordenador: u.no, participantes,
      origem, destino, conversao: resumo(conv), a_aguardar_regiao: espera, rasto };
  }

  // ------------------------------------------------------------ Saga (outro banco ou carteira móvel)
  async function ordem(u, o, hash, falha) {
    const txId = o.id, conta = o.conta_origem.toUpperCase(), valor = s2c(o.valor);
    if (!(valor > 0)) throw new Falha(400, "PEDIDO_INVALIDO", "O valor tem de ser positivo");
    const s = shardDe(conta); if (!s) throw new Falha(404, "CONTA_INEXISTENTE", `Conta '${conta}' não existe`);
    duplicada(txId);
    const carteira = (o.tipo_destino || "IBAN").toUpperCase() === "CARTEIRA";
    const contraparte = `${o.banco_destino}:${o.iban_destino}`, rasto = [];
    const central = regiaoEmBaixo(s);
    if (central) { podeSubstituir(s); rasto.push(`Região ${REGIAO[s]} em baixo: o débito é autorizado pelo NÓ CENTRAL`); }
    else { await garantirReconciliada(s, u.no); await esperarPrimario(s); }
    const k = existe(conta, "origem");
    verificarDebito({ ...k, saldo: saldoVisto(k, central) }, valor, u);
    const conv = carteira && k.moeda !== "MZN" ? converter(valor, k.moeda, "MZN", COPIAS_TAXAS[central ? "CT" : s]) : null;
    PROCESSADAS.add(txId);
    if (central) await registarPendente(u.no, conta, "DEBITO_EXTERNO", valor, k.moeda, txId, contraparte, hash, conv, "origem");
    else await movimentar(s, conta, "DEBITO_EXTERNO", valor, k.moeda, txId, contraparte, hash, "SAGA", u.no, conv, "origem");
    const [vEnvio, mEnvio] = conv ? [conv.vd, "MZN"] : [valor, k.moeda];
    rasto.push(`SAGA T1: débito de ${c2s(valor)} ${k.moeda} em ${conta} confirmado em ${central ? "db-central (pendente)" : s}` + (conv ? ` (convertido para ${c2s(vEnvio)} MZN)` : ""));
    let motivo = null, resposta = null;
    if (falha === "legado_indisponivel" || !corre("legacy")) motivo = `Falha na comunicação com o legado: ${falha ? "⚡ FALHA INJETADA: mainframe legado não responde" : "[Errno 111] Connection refused"}`;
    else {
      await sleep(80);
      const num = String(o.iban_destino).replace(/\s|\+258/g, "");
      if (carteira) {
        const op = String(o.banco_destino).toUpperCase();
        if (!(PREFIXOS[op] || []).includes(num.slice(0, 2)) || !CARTEIRAS[num]) resposta = { Estado: "REJEITADA", Motivo: `O número ${num} não tem carteira ${op}` };
        else if (vEnvio > 25000000) resposta = { Estado: "REJEITADA", Motivo: "Excede o limite de 250 000 MZN por operação de carteira" };
        else resposta = { Estado: "ACEITE", CodigoAutorizacao: "MF" + (++autorizacao), Titular: CARTEIRAS[num] };
      } else if (!/^[A-Z]{2}\d{2}[0-9A-Z]{10,30}$/.test(num)) resposta = { Estado: "REJEITADA", Motivo: "IBAN inválido" };
      else resposta = { Estado: "ACEITE", CodigoAutorizacao: "MF" + (++autorizacao) };
      log("legacy", `ORDEM ${txId} ${resposta.Estado} ${c2s(vEnvio)} ${mEnvio}`);
      rasto.push(`SAGA T2: legado respondeu ${resposta.Estado} (XML verificado, assinatura aceite)`);
      if (resposta.Estado === "ACEITE") {
        await auditar(u.no, "SAGA_CONCLUIDA", { tx_id: txId, conta, hash, autorizacao: resposta.CodigoAutorizacao });
        return { tx_id: txId, estado: "CONCLUIDA", protocolo: "SAGA", via_central: central, destino_tipo: carteira ? "CARTEIRA" : "IBAN",
          titular_destino: resposta.Titular || null, enviado: `${c2s(vEnvio)} ${mEnvio}`, resposta_legado: { Referencia: txId, ...resposta }, rasto };
      }
      motivo = `Rejeitada pelo banco parceiro: ${resposta.Motivo}`;
    }
    rasto.push(`SAGA T2 falhou -> ${motivo}`);
    if (regiaoEmBaixo(s)) await registarPendente(u.no, conta, "COMPENSACAO", valor, k.moeda, txId, contraparte, hash);
    else { await garantirReconciliada(s, u.no); await movimentar(s, conta, "COMPENSACAO", valor, k.moeda, txId, contraparte, hash, "SAGA", u.no); }
    rasto.push(`SAGA C1: compensação executada — ${c2s(valor)} ${k.moeda} devolvido a ${conta}`);
    await auditar(u.no, "SAGA_COMPENSADA", { tx_id: txId, conta, motivo });
    return { tx_id: txId, estado: "COMPENSADA", protocolo: "SAGA", motivo, rasto };
  }

  // ------------------------------------------------------------ leituras
  function contasVisiveis(filtro) {
    const linhas = [], falhas = [], central = [];
    for (const s of ["EU", "AM", "AF"]) {
      const ks = Object.values(CONTAS).filter((k) => k.regiao === s && filtro(k));
      if (!regiaoEmBaixo(s) && primarioVivo(s)) { linhas.push(...ks.map((k) => ({ k, via: false }))); continue; }
      if (regiaoEmBaixo(s) && primarioVivo("CT")) { linhas.push(...ks.map((k) => ({ k, via: true }))); central.push(NOMES[s]); continue; }
      falhas.push(NOMES[s]);
    }
    return { linhas, falhas, central };
  }
  function nomesTitulares(ids) { return Object.fromEntries(ids.filter((i) => CONTAS[i]).map((i) => [i, CONTAS[i].titular])); }
  const OPERADORAS = { "M-PESA": "M-Pesa", "E-MOLA": "e-Mola", MKESH: "mKesh" };
  function descrever(tipo, contraparte, nomes) {
    if (tipo === "DEPOSITO") { const n = contraparte.split(":").slice(1).join(":"); return "Depósito" + (n ? ` (${n})` : ""); }
    if (tipo === "DEBITO_EXTERNO" || tipo === "COMPENSACAO") {
      const [dest, ref] = [contraparte.split(":")[0], contraparte.split(":").slice(1).join(":")];
      const alvo = OPERADORAS[dest] ? `carteira ${OPERADORAS[dest]} ${ref}` : `${dest.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\B\w+/g, (w) => w.toLowerCase())} (${ref})`;
      return tipo === "DEBITO_EXTERNO" ? `Envio para ${alvo}` : `Devolução: envio para ${alvo} recusado`;
    }
    const quem = nomes[contraparte] || contraparte;
    return tipo === "DEBITO" ? `Transferência para ${quem}` : `Transferência de ${quem}`;
  }
  function extracto(conta, de, ate) {
    const k = CONTAS[conta]; if (!k) throw new Falha(404, "CONTA_INEXISTENTE", `Conta '${conta}' não existe`);
    const s = k.regiao;
    const sinal = (m) => (["DEBITO", "DEBITO_EXTERNO"].includes(m.tipo) ? -m.valor : m.valor);
    if (regiaoEmBaixo(s)) {
      if (!primarioVivo("CT")) throw indisponivel(s, "os dois nós estão em baixo");
      const pend = PENDENTES.filter((p) => p.conta === conta && p.estado === "PENDENTE");
      const nomes = nomesTitulares(pend.map((p) => p.contraparte));
      let saldo = ESPELHO[conta].saldo - pend.reduce((a, p) => a + sinal(p), 0); const ini = saldo; let deb = 0, cred = 0;
      const movs = pend.map((p) => { saldo += sinal(p); if (sinal(p) < 0) deb += p.valor; else cred += p.valor;
        return { data: p.criado_em, descricao: descrever(p.tipo, p.contraparte, nomes) + " (via Nó Central)", tipo: p.tipo,
          debito: sinal(p) < 0 ? c2s(p.valor) : null, credito: sinal(p) > 0 ? c2s(p.valor) : null, saldo_apos: c2s(saldo),
          valor_contraparte: p.vc != null ? c2s(p.vc) : null, moeda_contraparte: p.mc, referencia: p.tx_id, protocolo: "CENTRAL (a aguardar a região)" }; });
      return { conta, titular: k.titular, owner_username: k.owner, moeda: k.moeda, regiao: s, fragmento: NOMES.CT, via_central: true,
        nota: "A região desta conta está em baixo. O saldo vem do Nó Central e só aparecem as operações feitas através dele; o histórico completo volta quando a região voltar.",
        periodo: { de: null, ate: null }, saldo_inicial: c2s(ini), total_debitos: c2s(deb), total_creditos: c2s(cred), saldo_final: c2s(saldo),
        saldo_atual: c2s(ESPELHO[conta].saldo), movimentos: movs };
    }
    if (!primarioVivo(s)) throw indisponivel(s, "nenhum nó aceita escritas");
    const movs = MOV[s].filter((m) => m.conta === conta);
    const nomes = nomesTitulares(movs.map((m) => m.contraparte));
    let saldo = k.saldo - movs.reduce((a, m) => a + sinal(m), 0);
    let ini = null, deb = 0, cred = 0; const linhas = [];
    for (const m of movs) {
      const dia = m.criado_em.slice(0, 10), antes = saldo; saldo += sinal(m);
      if ((de && dia < de) || (ate && dia > ate)) continue;
      if (ini === null) ini = antes;
      if (sinal(m) < 0) deb += m.valor; else cred += m.valor;
      linhas.push({ data: m.criado_em, descricao: descrever(m.tipo, m.contraparte, nomes), tipo: m.tipo,
        debito: sinal(m) < 0 ? c2s(m.valor) : null, credito: sinal(m) > 0 ? c2s(m.valor) : null, saldo_apos: c2s(saldo),
        valor_contraparte: m.vc != null ? c2s(m.vc) : null, moeda_contraparte: m.mc, referencia: m.tx_id, protocolo: m.protocolo });
    }
    if (ini === null) ini = saldo;
    return { conta, titular: k.titular, owner_username: k.owner, moeda: k.moeda, regiao: s, fragmento: NOMES[s], periodo: { de, ate },
      saldo_inicial: c2s(ini), total_debitos: c2s(deb), total_creditos: c2s(cred), saldo_final: c2s(ini + cred - deb), saldo_atual: c2s(k.saldo), movimentos: linhas };
  }
  function totais() {
    const por = {}, falhas = [], central = [];
    const somar = (m, v) => { por[m] = (por[m] || 0) + v; };
    for (const s of ["EU", "AM", "AF"]) {
      const ks = Object.values(CONTAS).filter((k) => k.regiao === s);
      if (!regiaoEmBaixo(s) && primarioVivo(s)) {
        ks.forEach((k) => somar(k.moeda, k.saldo));
        PENDENTES.filter((p) => p.regiao === s && p.estado === "PENDENTE").forEach((p) => somar(p.moeda, (["DEBITO", "DEBITO_EXTERNO"].includes(p.tipo) ? -1 : 1) * p.valor));
        PREPARADAS.filter((p) => p.shard === s).forEach(() => {});
      } else if (primarioVivo("CT")) { ks.forEach((k) => somar(k.moeda, ESPELHO[k.id].saldo)); central.push(NOMES[s]); }
      else falhas.push(NOMES[s]);
    }
    const total = Object.entries(por).reduce((a, [m, v]) => a + v * TAXAS[m], 0);
    return { saldos_por_moeda: Object.fromEntries(Object.entries(por).sort().map(([m, v]) => [m, c2s(v)])),
      total_equivalente_MZN: (Math.round(total) / 100).toFixed(2), fragmentos_indisponiveis: falhas, servidos_pela_central: central };
  }
  function saude(no) {
    const fragmentos = {};
    for (const [s, f] of Object.entries(FRAG)) {
      if (!primarioVivo(s)) { fragmentos[NOMES[s]] = { estado: "INDISPONÍVEL" }; continue; }
      const rep = outro(f, f.primario);
      fragmentos[NOMES[s]] = { estado: "OK", primario_atual: f.primario,
        replicas: corre(rep) && !reintegrando(rep) ? [{ nome: app(rep), estado: "streaming", modo: f.sincrona ? "sync" : "async" }] : "nenhuma ligada" };
    }
    const vivos = ["datanode-1", "datanode-2"].filter(corre).length;
    return { no, hora: iso(), fragmentos,
      dfs: corre("namenode") ? { backend: "HDFS", estado: "Operational", datanodes_vivos: vivos, datanodes_mortos: 2 - vivos } : { estado: "INDISPONÍVEL", erro: "namenode não responde" },
      dfs_auditoria_pendente: 0, legado: corre("legacy") ? { Estado: "OK", Sistema: "MAINFRAME-JAVA" } : { Estado: "INDISPONÍVEL", erro: "[Errno 111] Connection refused" } };
  }
  function inspecionar(no, detalhe) {
    const [s, f] = Object.entries(FRAG).find(([, x]) => x.nos.includes(no));
    const base = { no, regiao: s, fragmento: NOMES[s] };
    if (!corre(no)) return { ...base, estado: "INDISPONÍVEL", erro: "Não responde: o servidor está desligado." };
    const primario = f.primario === no && !reintegrando(no);
    const lsn = lsnTexto(primario ? LSN[s] : LSN_REPLICA[s]);
    const prep = PREPARADAS.filter((p) => p.shard === s);
    if (s === "CT") {
      const esp = Object.entries(ESPELHO).map(([id, e]) => ({ id, ...e, k: CONTAS[id] })).sort((a, b) => (a.id < b.id ? -1 : 1));
      const out = { ...base, estado: "OK", papel: primario ? "primário" : "réplica", lsn, n_contas: esp.length, n_movimentos: PENDENTES.length,
        n_pendentes: PENDENTES.filter((p) => p.estado === "PENDENTE").length, n_preparadas: prep.length, saldos_por_moeda: {} };
      esp.forEach((e) => { out.saldos_por_moeda[e.k.moeda] = c2s(s2c(out.saldos_por_moeda[e.k.moeda] || 0) + e.saldo); });
      if (!detalhe) return out;
      return { ...out, espelho: esp.map((e) => ({ id: e.id, regiao: e.k.regiao, owner_username: e.k.owner, moeda: e.k.moeda, saldo: c2s(e.saldo), versao: e.versao,
          titular_cifrado: e.k.cif, titular_decifrado: e.k.titular, atualizado_em: e.em })),
        pendentes: PENDENTES.slice().reverse().slice(0, 25).map((p) => ({ id: p.id, tx_id: p.tx_id, conta: p.conta, regiao: p.regiao, tipo: p.tipo, valor: c2s(p.valor),
          moeda: p.moeda, estado: p.estado, coordenador: p.coord, contraparte_cifrada: p.cif, contraparte_decifrada: p.contraparte, criado_em: p.criado_em,
          aplicada_em: p.aplicada_em || null, aplicada_por: p.aplicada_por || null })),
        taxas: Object.entries(COPIAS_TAXAS.CT).map(([m, v]) => ({ moeda: m, mzn_por_unidade: v.toFixed(6), atualizado_em: TAXAS_EM.CT })),
        preparadas: prep.map((p) => ({ gid: p.gtrid, desde: iso(p.desde) })),
        replicacao: primario && replicaSincrona(s) ? [{ replica: app(outro(f, no)), estado: "streaming", modo: "sync", lsn }] : [],
        estado_central: { por_verificar: Object.fromEntries([...POR_VERIFICAR].map((x) => [x, iso()])), com_pendentes: Object.fromEntries([...COM_PENDENTES].map((x) => [x, iso()])) } };
    }
    const ks = Object.values(CONTAS).filter((k) => k.regiao === s).sort((a, b) => (a.id < b.id ? -1 : 1));
    const out = { ...base, estado: "OK", papel: primario ? "primário" : "réplica", lsn, n_contas: ks.length, n_movimentos: MOV[s].length,
      n_preparadas: prep.length, saldos_por_moeda: {} };
    ks.forEach((k) => { out.saldos_por_moeda[k.moeda] = c2s(s2c(out.saldos_por_moeda[k.moeda] || 0) + k.saldo); });
    if (!detalhe) return out;
    return { ...out,
      contas: ks.map((k) => ({ id: k.id, owner_username: k.owner, regiao: k.regiao, moeda: k.moeda, saldo: c2s(k.saldo), versao: k.versao,
        titular_em_claro: null, titular_cifrado: k.cif, titular_decifrado: k.titular })),
      movimentos: MOV[s].slice().reverse().slice(0, 25).map((m) => ({ tx_id: m.tx_id, conta: m.conta, tipo: m.tipo, valor: c2s(m.valor), moeda: m.moeda,
        protocolo: m.protocolo, coordenador: m.coord, hash: m.hash, contraparte_cifrada: m.cif, contraparte_decifrada: m.contraparte, criado_em: m.criado_em })),
      taxas: Object.entries(COPIAS_TAXAS[s]).map(([m, v]) => ({ moeda: m, mzn_por_unidade: v.toFixed(6), atualizado_em: TAXAS_EM[s] })),
      preparadas: prep.map((p) => ({ gid: p.gtrid, desde: iso(p.desde) })),
      replicacao: primario && replicaSincrona(s) ? [{ replica: app(outro(f, no)), estado: "streaming", modo: "sync", lsn }] : [] };
  }

  // ------------------------------------------------------------ encaminhamento dos pedidos
  const exigirAdmin = (u) => { if (!u.admin) throw new Falha(403, null, null, { detail: "Operação reservada a administradores" }); };
  async function tratar(metodo, url, cab, corpo) {
    const ctx = { no: null };
    try {
      return await rotas(ctx, metodo, url, cab, corpo);
    } catch (e) {
      if (!(e instanceof Falha)) throw e;
      if (e.status === 502) return new Response("", { status: 502 });
      if (!e.codigo) return respG(e.status, e.extra, {}, ctx.no);
      return respG(e.status, { erro: e.codigo, detalhe: e.message, no: ctx.no, ...e.extra }, {}, ctx.no);
    }
  }
  async function rotas(ctx, metodo, url, cab, corpo) {
    const resp = (st, c, ex = {}) => respG(st, c, ex, ctx.no);
    const p = url.pathname.replace(/\/+$/, "") || "/";
    // serviço de autenticação (atrás do gateway)
    if (p === "/auth/login") {
      if (!corre("auth")) return resp(502, "502 Bad Gateway");
      const { username: un = "", password: pw = "" } = corpo || {};
      const f = FALHAS_LOGIN[un] || { n: 0, ate: 0 };
      if (f.ate > Date.now()) return resp(429, { detail: "Demasiadas tentativas falhadas: conta bloqueada durante 60 s" });
      const uu = UTILIZADORES[un];
      if (!uu || uu.pass !== pw) {
        f.n++; if (f.n >= 5) { f.ate = Date.now() + 60000; f.n = 0; } FALHAS_LOGIN[un] = f;
        return resp(401, { detail: "Credenciais inválidas" });
      }
      FALHAS_LOGIN[un] = { n: 0, ate: 0 };
      return resp(200, { access_token: await emitirToken(un), token_type: "Bearer", expires_in: 900, emitido_por: "auth", algoritmo: "RS256", papel: uu.papel });
    }
    if (p === "/auth/utilizadores") {
      if (!corre("auth")) return resp(502, "502 Bad Gateway");
      const [u, e] = await validarToken(cab.authorization); if (e) return e;
      if (!u.admin) return resp(403, { detail: "Só o administrador gere utilizadores" });
      if (metodo === "GET") return resp(200, { utilizadores: Object.entries(UTILIZADORES).map(([k, v]) => ({ username: k, nome: v.nome, papel: v.papel })) });
      const { username, nome, password } = corpo || {};
      if (!/^[a-z][a-z0-9._-]{2,30}$/.test(username || "") || !nome || !password || password.length < 8) return resp(422, { detail: "Dados inválidos" });
      if (UTILIZADORES[username]) return resp(409, { detail: `Já existe o utilizador ${username}` });
      UTILIZADORES[username] = { nome, papel: "cliente", pass: password };
      return resp(201, { username, nome, papel: "cliente" });
    }
    // controlo dos contentores (só admin)
    if (p.startsWith("/controlo/")) {
      const [u, e] = await validarToken(cab.authorization); if (e) return e;
      if (!u.admin) return resp(403, { detail: "Operação reservada a administradores" });
      if (p === "/controlo/nos") return resp(200, { nos: SERVICOS.map((s) => ({ servico: s, estado: NOS[s].estado,
        detalhe: corre(s) ? `Up ${Math.max(1, Math.round((Date.now() - NOS[s].desde) / 1000))} seconds${s.startsWith("db-") ? " (healthy)" : ""}` : `Exited (0) ${Math.round((Date.now() - NOS[s].desde) / 1000)} seconds ago` })) });
      let m;
      if ((m = p.match(/^\/controlo\/nos\/([^/]+)\/(parar|ligar|matar|reiniciar)$/))) {
        if (!NOS[m[1]] || m[1] === "gateway") return resp(404, { detail: "Serviço desconhecido" });
        acaoNo(m[1], m[2]); return resp(200, { servico: m[1], acao: m[2], estado: NOS[m[1]].estado });
      }
      if ((m = p.match(/^\/controlo\/logs\/([^/]+)$/))) {
        if (!LOGS[m[1]]) return resp(404, { detail: "Serviço desconhecido" });
        return resp(200, { servico: m[1], linhas: LOGS[m[1]].slice(-Number(url.searchParams.get("linhas") || 60)) });
      }
      if ((m = p.match(/^\/controlo\/reativar\/(api-[12])$/))) {
        DESATIVADAS.delete(m[1]); log(m[1], `[controlo] ${m[1]} reativada`); acaoNo(m[1], "ligar");
        return resp(200, { servico: m[1], reativado: true });
      }
      return resp(404, { detail: "Not Found" });
    }
    // API: o gateway escolhe uma réplica viva (ou a pedida em /no/api-N/...)
    let via = null, caminho = p;
    const mv = p.match(/^\/no\/(api-[12])(\/.*)$/); if (mv) { via = mv[1]; caminho = mv[2]; }
    const api = apiQueResponde(via);
    if (!api) return new Response("502 Bad Gateway", { status: 502 });
    ctx.no = api;
    await sleep(25 + Math.random() * 60);
    if (caminho === "/saude") return resp(200, saude(ctx.no));
    const [u, e] = await validarToken(cab.authorization); if (e) return e;
    u.no = ctx.no;
    let m;
    if (caminho === "/contas" && metodo === "GET") {
      const { linhas, falhas, central } = contasVisiveis((k) => u.admin || k.owner === u.username);
      return resp(200, { utilizador: u.username, contas: linhas.sort((a, b) => (a.k.id < b.k.id ? -1 : 1)).map(({ k, via: vc }) => ({ id: k.id, titular: k.titular,
        ...(u.admin ? { owner_username: k.owner } : {}), regiao: k.regiao, moeda: k.moeda, saldo: c2s(saldoVisto(k, vc)), versao: k.versao,
        fragmento: vc ? NOMES.CT : NOMES[k.regiao], ...(vc ? { via_central: true } : {}) })), fragmentos_indisponiveis: falhas,
        servidos_pela_central: central, resultado_parcial: falhas.length > 0 });
    }
    if (caminho === "/beneficiarios") {
      const { linhas, falhas, central } = contasVisiveis((k) => k.owner !== u.username);
      return resp(200, { beneficiarios: linhas.map(({ k }) => ({ conta: k.id, titular: k.titular, moeda: k.moeda, regiao: k.regiao })),
        fragmentos_indisponiveis: falhas, servidos_pela_central: central });
    }
    if ((m = caminho.match(/^\/contas\/([^/]+)\/extracto$/))) {
      const ex = extracto(m[1].toUpperCase(), url.searchParams.get("de"), url.searchParams.get("ate"));
      if (!u.admin && ex.owner_username !== u.username) return resp(403, { detail: "Não é titular desta conta" });
      return resp(200, ex);
    }
    if (caminho === "/transferencias" && metodo === "POST") {
      const campos = { id: corpo.id, origem: corpo.origem, destino: corpo.destino, valor: corpo.valor, timestamp: corpo.timestamp };
      const h = await validarMensagem(campos, corpo.hash_sha256);
      const r = await transferir(u, campos, h, url.searchParams.get("falha"));
      return resp(200, { ...r, integridade: { algoritmo: "SHA-256", hash_verificado: h } });
    }
    if ((m = caminho.match(/^\/carteiras\/([^/]+)\/([^/]+)$/))) {
      if (!corre("legacy")) return resp(503, { detail: "A rede interbancária não respondeu: [Errno 111] Connection refused" });
      const op = m[1].toUpperCase(), num = m[2].replace(/\D/g, "");
      if (!PREFIXOS[op] || !/^\d+$/.test(num)) return resp(400, { detail: "Operadora ou número inválido" });
      if (!PREFIXOS[op].includes(num.slice(0, 2)) || num.length !== 9) return resp(200, { operadora: op, numero: num, estado: "INVALIDA", titular: null, motivo: `O número ${num} não pertence à ${OPERADORAS[op]}` });
      return resp(200, CARTEIRAS[num] ? { operadora: op, numero: num, estado: "ATIVA", titular: CARTEIRAS[num], motivo: null }
        : { operadora: op, numero: num, estado: "INVALIDA", titular: null, motivo: `O número ${num} não tem carteira ${op}` });
    }
    if (caminho === "/interbancario/ordens" && metodo === "POST") {
      const campos = { id: corpo.id, conta_origem: corpo.conta_origem, banco_destino: corpo.banco_destino, iban_destino: corpo.iban_destino,
        valor: corpo.valor, timestamp: corpo.timestamp, ...(corpo.tipo_destino ? { tipo_destino: corpo.tipo_destino } : {}) };
      const h = await validarMensagem(campos, corpo.hash_sha256);
      return resp(200, await ordem(u, campos, h, url.searchParams.get("falha")));
    }
    if (caminho === "/taxas" && metodo === "GET") {
      const por = {};
      for (const s of Object.keys(FRAG)) por[NOMES[s]] = primarioVivo(s) ? Object.fromEntries(Object.entries(COPIAS_TAXAS[s]).map(([k, v]) => [k, v.toFixed(6)])) : "INDISPONÍVEL";
      const vals = Object.values(por).filter((v) => typeof v === "object");
      return resp(200, { moeda_referencia: "MZN", mzn_por_unidade: por, replicas_coerentes: vals.every((v) => JSON.stringify(v) === JSON.stringify(vals[0])) });
    }
    // ---- administração
    if (caminho.startsWith("/admin/") || caminho.startsWith("/relatorios") || caminho.startsWith("/auditoria")) {
      if (!(caminho.startsWith("/relatorios") && metodo === "GET")) exigirAdmin(u);
    }
    if (caminho === "/admin/totais") return resp(200, totais());
    if ((m = caminho.match(/^\/admin\/taxas\/([A-Z]{3})$/)) && metodo === "PUT") {
      const moeda = m[1], v = Number(corpo.mzn_por_unidade), gtrid = `${ctx.no}|taxa-${uuid()}`, rasto = [];
      if (moeda === "MZN") throw new Falha(400, "PEDIDO_INVALIDO", "O metical é a moeda de referência (taxa fixa = 1)");
      if (!TAXAS[moeda]) throw new Falha(400, "PEDIDO_INVALIDO", `Moeda '${moeda}' não existe na tabela de taxas`);
      for (const s of Object.keys(FRAG).sort()) {
        if (!primarioVivo(s)) { await sleep(1500);
          throw new Falha(503, "FRAGMENTO_INDISPONIVEL", `Fragmento ${NOMES[s]} indisponível`, { estado: "ABORTADA",
            rasto: [...rasto, `DECISÃO: ABORT — nenhuma réplica foi alterada (Fragmento ${NOMES[s]} indisponível)`] }); }
        rasto.push(`FASE 1: réplica ${s} PREPARED (1 ${moeda} = ${v.toFixed(6)} MZN)`);
      }
      rasto.push("DECISÃO: COMMIT (fsync)");
      for (const s of Object.keys(FRAG).sort()) { COPIAS_TAXAS[s][moeda] = v; TAXAS_EM[s] = iso(); escrever(s); rasto.push(`FASE 2: COMMIT PREPARED na réplica ${s} ✓`); }
      TAXAS[moeda] = v; WAL[gtrid] = { decisao: "COMMIT" };
      return resp(200, { estado: "ATUALIZADA_EM_TODAS_AS_REPLICAS", moeda, mzn_por_unidade: v.toFixed(6), protocolo: "2PC (replicação síncrona)", rasto });
    }
    if (caminho === "/admin/contas" && metodo === "POST") {
      const { owner_username: owner, titular, regiao, moeda } = corpo;
      if (!REGIAO[regiao] || !TAXAS[moeda] || !owner || !titular) throw new Falha(400, "PEDIDO_INVALIDO", "Dados inválidos");
      await esperarPrimario(regiao, 6000);
      const base = { EU: 1000, AM: 2000, AF: 3000 }[regiao];
      const n = Math.max(base, ...Object.keys(CONTAS).filter((c) => c.startsWith(regiao)).map((c) => Number(c.split("-")[1]))) + 1;
      const conta = `${regiao}-${n}`;
      await novaConta(conta, titular, owner, moeda, 0); escrever(regiao); publicar(conta);
      return resp(200, { conta, titular, owner_username: owner, regiao, moeda, saldo: "0.00", fragmento: NOMES[regiao] });
    }
    if (caminho === "/admin/depositos" && metodo === "POST") {
      const conta = String(corpo.conta).toUpperCase(), valor = s2c(corpo.valor), s = shardDe(conta), txId = corpo.id;
      if (!s || !CONTAS[conta]) throw new Falha(404, "CONTA_INEXISTENTE", `Conta '${conta}' não existe`);
      duplicada(txId);
      const hash = await sha256(canonico({ id: txId, conta, valor: c2s(valor), descricao: corpo.descricao, por: u.username }));
      const central = regiaoEmBaixo(s);
      if (central) { podeSubstituir(s); PROCESSADAS.add(txId); await registarPendente(u.no, conta, "DEPOSITO", valor, CONTAS[conta].moeda, txId, `DEPOSITO:${corpo.descricao}`, hash); }
      else { await garantirReconciliada(s, u.no); await esperarPrimario(s); PROCESSADAS.add(txId);
        await movimentar(s, conta, "DEPOSITO", valor, CONTAS[conta].moeda, txId, `DEPOSITO:${corpo.descricao}`, hash, "LOCAL", ctx.no); }
      return resp(200, { tx_id: txId, estado: "CONFIRMADA", protocolo: central ? "Via NÓ CENTRAL (depósito pendente até a região voltar)" : "LOCAL (depósito)",
        via_central: central, conta, valor: c2s(valor), moeda: CONTAS[conta].moeda, saldo_atual: c2s(central ? ESPELHO[conta].saldo : CONTAS[conta].saldo),
        fragmento: NOMES[central ? "CT" : s] });
    }
    if (caminho === "/admin/bd") return resp(200, { lido_por: ctx.no, nos: Object.values(FRAG).flatMap((f) => f.nos).map((n) => inspecionar(n, false)) });
    if ((m = caminho.match(/^\/admin\/bd\/([^/]+)$/))) {
      if (!Object.values(FRAG).some((f) => f.nos.includes(m[1]))) return resp(404, { detail: "Nó desconhecido" });
      return resp(200, { lido_por: ctx.no, ...inspecionar(m[1], true) });
    }
    if (caminho === "/admin/2pc") {
      const vis = PREPARADAS.filter((x) => primarioVivo(x.shard));
      return resp(200, { coordenador: ctx.no, total_registadas: Object.keys(WAL).length,
        nao_concluidas: Object.fromEntries(Object.entries(WAL).filter(([g]) => PREPARADAS.some((x) => x.gtrid === g)).map(([g, w]) => [g, { estado: w.decisao, decisao: w.decisao, participantes: w.participantes }])),
        transacoes_preparadas_nos_participantes: vis.map((x) => ({ gid: `2026_${btoa(x.gtrid)}_${btoa(x.shard)}`, prepared: iso(x.desde), database: "banco", gtrid: x.gtrid })),
        fragmentos_indisponiveis: Object.keys(FRAG).filter((s) => !primarioVivo(s)).map((s) => NOMES[s]) });
    }
    if (caminho === "/admin/recuperar") return resp(200, { no: ctx.no, acoes: recuperar(ctx.no) });
    if (caminho === "/relatorios" && metodo === "POST") {
      if (!corre("namenode") || !["datanode-1", "datanode-2"].some(corre)) return resp(503, { erro: "DFS_INDISPONIVEL", detalhe: "HDFS indisponível", no: ctx.no });
      const { linhas, falhas, central } = contasVisiveis(() => true);
      const csv = ["conta,titular,regiao,moeda,saldo,fragmento", ...linhas.map(({ k, via: vc }) => `${k.id},${k.titular},${k.regiao},${k.moeda},${c2s(saldoVisto(k, vc))},${vc ? NOMES.CT : NOMES[k.regiao]}`)].join("\r\n") + "\r\n";
      const nome = `saldos-${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}-${ctx.no}.csv`, sha = await sha256(csv);
      RELATORIOS.push({ nome, conteudo: csv, sha, modificado: iso() }, { nome: nome + ".sha256", conteudo: sha, sha: "", modificado: iso() });
      await auditar(ctx.no, "RELATORIO_GERADO", { ficheiro: nome, sha256: sha });
      return resp(200, { ficheiro: `/relatorios/${nome}`, gerado_por: ctx.no, sha256: sha, fragmentos_indisponiveis: falhas, servidos_pela_central: central });
    }
    if (caminho === "/relatorios") {
      if (!corre("namenode")) return resp(503, { erro: "DFS_INDISPONIVEL", detalhe: "namenode não responde", no: ctx.no });
      return resp(200, { lido_por: ctx.no, ficheiros: RELATORIOS.map((r) => ({ nome: r.nome, tipo: "FILE", tamanho: r.conteudo.length, replicacao: 2, modificado: r.modificado })) });
    }
    if ((m = caminho.match(/^\/relatorios\/([^/]+)$/))) {
      const r = RELATORIOS.find((x) => x.nome === decodeURIComponent(m[1]));
      if (!r) return resp(404, { detail: "Relatório não encontrado" });
      if (!corre("namenode") || !["datanode-1", "datanode-2"].some(corre)) return resp(503, { erro: "DFS_INDISPONIVEL", detalhe: "sem DataNodes vivos", no: ctx.no });
      return resp(200, r.conteudo, { "X-SHA256": await sha256(r.conteudo) });
    }
    if (caminho === "/auditoria") {
      const nos = Object.fromEntries(Object.entries(AUDITORIA).map(([a, fs]) => [a, fs.map((f) => ({ nome: f.nome, tipo: "FILE",
        tamanho: f.entradas.reduce((t, e) => t + JSON.stringify(e).length + 1, 0), replicacao: 2, modificado: f.modificado || iso() }))]));
      return resp(200, { ficheiro_atual_deste_no: `/auditoria/${ctx.no}/${AUDITORIA[ctx.no].slice(-1)[0].nome}`, nos });
    }
    if (caminho === "/auditoria/verificar") {
      const fich = url.searchParams.get("ficheiro") || "", [, , no, nome] = fich.split("/");
      const f = (AUDITORIA[no] || []).find((x) => x.nome === nome);
      if (!f) return resp(404, { detail: "Ficheiro não encontrado" });
      let anterior = "0".repeat(64);
      for (const [i, e] of f.entradas.entries()) {
        const { hash, ...resto } = e;
        if (resto.hash_anterior !== anterior) return resp(200, { ficheiro: fich, verificado_por: ctx.no, integro: false, linha: i + 1, motivo: "encadeamento quebrado" });
        if (await sha256(canonico(resto)) !== hash) return resp(200, { ficheiro: fich, verificado_por: ctx.no, integro: false, linha: i + 1, motivo: "conteúdo da entrada foi alterado" });
        anterior = hash;
      }
      return resp(200, { ficheiro: fich, verificado_por: ctx.no, integro: true, entradas: f.entradas.length, ultimo_hash: anterior });
    }
    return resp(404, { detail: "Not Found" });
  }

  // ------------------------------------------------------------ substituir o fetch
  const ROTAS = /^\/(auth|saude|contas|beneficiarios|transferencias|no|interbancario|carteiras|taxas|admin|relatorios|auditoria|controlo)(\/|$)/;
  const fetchReal = window.fetch.bind(window);
  window.fetch = async (entrada, init = {}) => {
    const url = new URL(entrada instanceof URL ? entrada.href : typeof entrada === "string" ? entrada : entrada.url, location.href);
    if (url.origin !== location.origin || !ROTAS.test(url.pathname)) return fetchReal(entrada, init);
    await pronto;
    const cab = {}; for (const [k, v] of Object.entries(init.headers || {})) cab[k.toLowerCase()] = v;
    let corpo = null; try { corpo = init.body ? JSON.parse(init.body) : null; } catch (_) { return respG(422, { detail: "JSON inválido" }); }
    try {
      return await tratar((init.method || "GET").toUpperCase(), url, cab, corpo);
    } catch (e) {
      console.error(e);
      return respG(500, { detail: "Erro interno do simulador: " + e.message });
    }
  };
  window.GSS_DEMO = { estado: () => ({ NOS, FRAG, CONTAS, PENDENTES, PREPARADAS, ESPELHO }) };
})();
