"""Processamento distribuído e transações distribuídas (multi-moeda).

Implementa:
  * Transferência LOCAL (ambas as contas no mesmo fragmento) — transação ACID simples.
  * Transferência DISTRIBUÍDA com TWO-PHASE COMMIT (2PC) usando as transações
    preparadas do PostgreSQL (PREPARE TRANSACTION / COMMIT PREPARED), em que este
    nó da API é o COORDENADOR e cada fragmento é um PARTICIPANTE.
  * CONVERSÃO DE MOEDA dentro da transação: cada conta tem a sua moeda (EUR, USD,
    MZN). A taxa vem da tabela taxas_cambio, REPLICADA em todos os nós; o coordenador
    lê-a nos dois participantes (com bloqueio partilhado) e confirma que é igual.
  * Atualização das taxas de câmbio com 2PC (replicação síncrona da tabela).
  * Ordem interbancária com o PADRÃO SAGA (transação T1 + compensação C1), porque
    o sistema legado do banco parceiro não suporta 2PC.
  * Recuperação automática após falhas (registo durável do coordenador + regra
    "presumed abort").

Garantia das propriedades ACID na transação distribuída
-------------------------------------------------------
  A (Atomicidade)  : ou todos os participantes fazem COMMIT PREPARED, ou todos fazem
                     ROLLBACK PREPARED. A decisão é única e fica registada (WAL).
  C (Consistência) : CHECK (saldo >= 0) em cada fragmento; o valor em meticais é
                     conservado (débito e crédito convertidos com a MESMA taxa,
                     lida com FOR SHARE e verificada nos dois nós); um participante
                     que não cumpra as regras vota NÃO e a transação é abortada.
  I (Isolamento)   : SELECT ... FOR UPDATE nas contas e FOR SHARE nas taxas; os
                     bloqueios são mantidos até ao fim da fase 2 (two-phase locking
                     estrito). Contas sempre bloqueadas por ordem alfabética do ID
                     (previne deadlocks distribuídos).
  D (Durabilidade) : PREPARE TRANSACTION grava o estado em disco no participante
                     (sobrevive a reinícios do PostgreSQL); a decisão do coordenador
                     é gravada com fsync ANTES de ser comunicada aos participantes.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import psycopg

from . import config, legado
from .shards import ContaInexistente, FragmentoIndisponivel, ligar, shard_de


# --------------------------------------------------------------- Exceções
class SaldoInsuficiente(Exception):
    pass


class NaoAutorizado(Exception):
    pass


class TransacaoDuplicada(Exception):
    pass


class TaxasInconsistentes(Exception):
    pass


class FalhaSimulada(Exception):
    pass


FALHAS_2PC = {
    None: "sem falha",
    "participante_indisponivel_fase1":
        "O 2.º participante fica inacessível (falha de rede) durante a votação -> ABORT global",
    "atraso_fase2":
        "O coordenador espera 20s após decidir COMMIT (janela para fazer 'docker stop' de um nó)",
    "crash_coordenador_fase2":
        "O coordenador (este processo) morre após decidir COMMIT e confirmar só 1 participante",
}
FALHAS_SAGA = {
    None: "sem falha",
    "legado_indisponivel": "O mainframe legado não responde -> compensação (estorno)",
}
CENTIMO = Decimal("0.01")


# --------------------------------------------------- Registo durável (WAL)
class RegistoDuravel:
    """Write-Ahead Log do coordenador: uma linha JSON por mudança de estado,
    gravada com fsync. É a 'memória' que permite recuperar após um crash."""

    def __init__(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.caminho = caminho
        self.lock = threading.Lock()

    def escrever(self, chave: str, estado: str, **extra):
        linha = json.dumps({"chave": chave, "estado": estado,
                            "ts": datetime.now(timezone.utc).isoformat(), **extra},
                           ensure_ascii=False, default=str)
        with self.lock, open(self.caminho, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
            f.flush()
            os.fsync(f.fileno())          # durabilidade real: só retorna após ir para disco

    def estados(self) -> dict[str, dict]:
        res: dict[str, dict] = {}
        if not self.caminho.exists():
            return res
        with self.lock, open(self.caminho, encoding="utf-8") as f:
            for l in f:
                try:
                    e = json.loads(l)
                except json.JSONDecodeError:   # linha truncada por crash a meio da escrita
                    continue
                anterior = res.get(e["chave"], {})
                novo = {**anterior, **e}
                if e["estado"] in ("COMMIT", "ABORT"):
                    novo["decisao"] = e["estado"]     # decisão global do 2PC
                res[e["chave"]] = novo
        return res


# ------------------------------------------------------------- Conversão
def converter(valor: Decimal, moeda_origem: str, moeda_destino: str, taxas: dict) -> dict:
    """Converte usando o metical como moeda de referência:
       valor_destino = valor × (MZN por 1 origem) / (MZN por 1 destino)
    Arredondamento bancário (half-even) ao cêntimo da moeda de destino."""
    t_o, t_d = taxas[moeda_origem], taxas[moeda_destino]
    convertido = (valor * t_o / t_d).quantize(CENTIMO, rounding=ROUND_HALF_EVEN)
    return {"valor_origem": valor, "moeda_origem": moeda_origem,
            "valor_destino": convertido, "moeda_destino": moeda_destino,
            "taxa": (t_o / t_d).quantize(Decimal("0.00000001")),
            "mzn_por_origem": t_o, "mzn_por_destino": t_d}


def ler_taxas(c, moedas) -> dict:
    """FOR SHARE: impede que a taxa seja alterada enquanto a transação decorre."""
    linhas = c.execute("SELECT moeda, mzn_por_unidade FROM taxas_cambio WHERE moeda = ANY(%s) "
                       "ORDER BY moeda FOR SHARE", (list(set(moedas)),)).fetchall()
    return {l["moeda"].strip(): l["mzn_por_unidade"] for l in linhas}


# ------------------------------------------------ Operações nos participantes
def bloquear_conta(c, conta: str, papel: str) -> dict:
    linha = c.execute("SELECT id, saldo, moeda, owner_username FROM contas WHERE id = %s "
                      "FOR UPDATE", (conta,)).fetchone()
    if not linha:
        raise ContaInexistente(f"Conta de {papel} '{conta}' não existe")
    linha["moeda"] = linha["moeda"].strip()
    return linha


def verificar_debito(linha: dict, valor: Decimal, utilizador) -> None:
    if not utilizador.is_admin and linha["owner_username"] != utilizador.username:
        raise NaoAutorizado(f"O utilizador '{utilizador.username}' não é titular da conta "
                            f"{linha['id']}")
    if linha["saldo"] < valor:
        raise SaldoInsuficiente(f"Saldo insuficiente em {linha['id']}: disponível "
                                f"{linha['saldo']} {linha['moeda']}, pedido {valor} {linha['moeda']}")


def movimentar(c, conta: str, tipo: str, valor: Decimal, moeda: str, tx_id, contraparte: str,
               hash_tx: str, protocolo: str, conv: dict | None = None, lado: str = "origem"):
    sinal = "-" if tipo in ("DEBITO", "DEBITO_EXTERNO") else "+"
    c.execute(f"UPDATE contas SET saldo = saldo {sinal} %s, versao = versao + 1, "
              f"atualizado_em = now() WHERE id = %s", (valor, conta))
    vc = mc = taxa = None
    if conv and conv["moeda_origem"] != conv["moeda_destino"]:
        if lado == "origem":
            vc, mc, taxa = conv["valor_destino"], conv["moeda_destino"], conv["taxa"]
        else:
            vc, mc, taxa = conv["valor_origem"], conv["moeda_origem"], conv["taxa"]
    try:
        c.execute(
            "INSERT INTO movimentos (tx_id, conta_id, tipo, valor, moeda, valor_contraparte, "
            "moeda_contraparte, taxa_aplicada, contraparte, hash_sha256, protocolo, no_coordenador) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tx_id, conta, tipo, valor, moeda, vc, mc, taxa, contraparte, hash_tx, protocolo,
             config.NODE_ID))
    except psycopg.errors.UniqueViolation:
        raise TransacaoDuplicada(f"A transação {tx_id} já foi processada (proteção anti-replay / "
                                 f"idempotência)")


def _resumo(conv: dict) -> dict:
    return {"debitado": f"{conv['valor_origem']} {conv['moeda_origem']}",
            "creditado": f"{conv['valor_destino']} {conv['moeda_destino']}",
            "taxa": f"1 {conv['moeda_origem']} = {conv['taxa']} {conv['moeda_destino']}",
            "referencia": f"1 {conv['moeda_origem']} = {conv['mzn_por_origem']} MZN; "
                          f"1 {conv['moeda_destino']} = {conv['mzn_por_destino']} MZN"}


# ============================================================== Coordenador
class Coordenador:
    FORMAT_ID = 2026

    def __init__(self, auditoria):
        self.auditoria = auditoria
        self.wal = RegistoDuravel(config.DATA_DIR / f"coordenador-2pc-{config.NODE_ID}.log")
        self.saga_log = RegistoDuravel(config.DATA_DIR / f"saga-{config.NODE_ID}.log")
        self.em_curso: set[str] = set()
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- API
    def transferir(self, tx: dict, utilizador, hash_tx: str, falha: str | None = None) -> dict:
        origem, destino = tx["origem"].upper(), tx["destino"].upper()
        if origem == destino:
            raise ValueError("Origem e destino não podem ser a mesma conta")
        valor = Decimal(tx["valor"])
        if valor <= 0:
            raise ValueError("O valor tem de ser positivo")
        so, sd = shard_de(origem), shard_de(destino)
        if so == sd:
            return self._transferencia_local(tx["id"], origem, destino, valor, so, utilizador,
                                             hash_tx)
        return self._transferencia_2pc(tx["id"], origem, destino, valor, so, sd, utilizador,
                                       hash_tx, falha)

    # --------------------------------------------------- Transação local
    def _transferencia_local(self, tx_id, origem, destino, valor, shard, utilizador, hash_tx):
        with ligar(shard) as c:
            with c.transaction():
                linhas = {conta: bloquear_conta(c, conta, "origem" if conta == origem else "destino")
                          for conta in sorted([origem, destino])}     # ordem global de bloqueio
                lo, ld = linhas[origem], linhas[destino]
                verificar_debito(lo, valor, utilizador)
                conv = converter(valor, lo["moeda"], ld["moeda"], ler_taxas(c, [lo["moeda"], ld["moeda"]]))
                movimentar(c, origem, "DEBITO", valor, lo["moeda"], tx_id, destino, hash_tx,
                           "LOCAL", conv, "origem")
                movimentar(c, destino, "CREDITO", conv["valor_destino"], ld["moeda"], tx_id,
                           origem, hash_tx, "LOCAL", conv, "destino")
        self.auditoria.registar("TRANSFERENCIA_LOCAL", tx_id=tx_id, origem=origem,
                                destino=destino, **{k: str(v) for k, v in conv.items()},
                                hash=hash_tx, utilizador=utilizador.username)
        return {"tx_id": tx_id, "estado": "CONFIRMADA", "protocolo": "LOCAL (transação ACID num "
                "único fragmento)", "fragmento": config.SHARD_NOMES[shard],
                "origem": origem, "destino": destino, "conversao": _resumo(conv)}

    # ------------------------------------------------------- Two-Phase Commit
    def _transferencia_2pc(self, tx_id, origem, destino, valor, so, sd, utilizador, hash_tx,
                           falha):
        gtrid = f"{config.NODE_ID}|{tx_id}"
        rasto: list[str] = []

        def r(msg):
            rasto.append(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")

        papel = {origem: (so, "origem"), destino: (sd, "destino")}
        ordem = sorted([origem, destino])               # ordem global (prevenção de deadlock)
        conexoes: dict[str, psycopg.Connection] = {}
        linhas: dict[str, dict] = {}
        preparados: list[str] = []
        conv = None

        with self.lock:
            self.em_curso.add(gtrid)
        self.wal.escrever(gtrid, "INICIO", participantes=[so, sd], origem=origem,
                          destino=destino, valor=str(valor))
        r(f"COORDENADOR {config.NODE_ID}: início da transação global {gtrid}")
        try:
            # ============================ FASE 1 — PREPARAÇÃO / VOTAÇÃO
            try:
                for i, conta in enumerate(ordem):
                    shard, p = papel[conta]
                    if falha == "participante_indisponivel_fase1" and i == 1:
                        r(f"⚡ FALHA INJETADA: participante {shard} inacessível (timeout de rede)")
                        raise FragmentoIndisponivel(shard, FalhaSimulada("timeout de rede simulado"))
                    c = ligar(shard)
                    conexoes[shard] = c
                    c.tpc_begin(c.xid(self.FORMAT_ID, gtrid, shard))
                    linhas[conta] = bloquear_conta(c, conta, p)
                    r(f"FASE 1: conta {conta} bloqueada em {shard} (moeda {linhas[conta]['moeda']})")

                lo, ld = linhas[origem], linhas[destino]
                verificar_debito(lo, valor, utilizador)

                # Taxas lidas nos DOIS participantes: a tabela é replicada, logo têm de coincidir
                moedas = [lo["moeda"], ld["moeda"]]
                t_o, t_d = ler_taxas(conexoes[so], moedas), ler_taxas(conexoes[sd], moedas)
                if t_o != t_d:
                    raise TaxasInconsistentes(f"Réplicas de taxas_cambio divergem: {so}={t_o} "
                                              f"{sd}={t_d}")
                conv = converter(valor, lo["moeda"], ld["moeda"], t_o)
                if lo["moeda"] != ld["moeda"]:
                    r(f"CONVERSÃO: {valor} {lo['moeda']} × ({conv['mzn_por_origem']} / "
                      f"{conv['mzn_por_destino']}) = {conv['valor_destino']} {ld['moeda']} "
                      f"(taxa igual nas 2 réplicas)")

                for conta in ordem:
                    shard, p = papel[conta]
                    c = conexoes[shard]
                    if p == "origem":
                        movimentar(c, origem, "DEBITO", valor, lo["moeda"], tx_id, destino,
                                   hash_tx, "2PC", conv, "origem")
                        r(f"FASE 1: DEBITO de {valor} {lo['moeda']} em {origem} executado em {shard}")
                    else:
                        movimentar(c, destino, "CREDITO", conv["valor_destino"], ld["moeda"],
                                   tx_id, origem, hash_tx, "2PC", conv, "destino")
                        r(f"FASE 1: CREDITO de {conv['valor_destino']} {ld['moeda']} em {destino} "
                          f"executado em {shard}")
                    c.tpc_prepare()                     # PREPARE TRANSACTION -> voto SIM
                    preparados.append(shard)
                    r(f"FASE 1: participante {shard} respondeu PREPARED (voto SIM, gravado em disco)")
            except Exception as e:
                # Qualquer voto NÃO / falha => decisão global ABORT
                self.wal.escrever(gtrid, "ABORT", motivo=str(e)[:300])
                r(f"DECISÃO: ABORT ({type(e).__name__}: {e})")
                tudo_desfeito = True
                for shard, c in conexoes.items():
                    try:
                        c.tpc_rollback()                # ROLLBACK PREPARED / ROLLBACK
                        r(f"FASE 2: ROLLBACK em {shard} ✓")
                    except Exception as e2:
                        tudo_desfeito = False
                        r(f"FASE 2: ROLLBACK em {shard} falhou ({e2}); a recuperação tratará")
                if tudo_desfeito:
                    self.wal.escrever(gtrid, "CONCLUIDO")
                self.auditoria.registar("2PC_ABORT", tx_id=tx_id, origem=origem, destino=destino,
                                        valor=str(valor), motivo=str(e)[:200], hash=hash_tx)
                e.rasto = rasto  # type: ignore[attr-defined]
                raise

            # ============================ DECISÃO (ponto de não retorno)
            self.wal.escrever(gtrid, "COMMIT")
            r("DECISÃO: COMMIT gravada no registo durável do coordenador (fsync)")

            if falha == "atraso_fase2":
                r("⚡ FALHA INJETADA: coordenador aguarda 20s antes da fase 2")
                time.sleep(20)

            # ============================ FASE 2 — CONFIRMAÇÃO
            pendentes = []
            for i, shard in enumerate(preparados):
                try:
                    conexoes[shard].tpc_commit()        # COMMIT PREPARED
                    r(f"FASE 2: COMMIT PREPARED em {shard} ✓")
                except Exception as e:
                    pendentes.append(shard)
                    r(f"FASE 2: {shard} inacessível ({type(e).__name__}); fica PREPARADA e será "
                      f"confirmada pela recuperação automática")
                if falha == "crash_coordenador_fase2" and i == 0:
                    r("⚡ FALHA INJETADA: o processo coordenador vai terminar abruptamente")
                    self.auditoria.registar("2PC_CRASH_SIMULADO", tx_id=tx_id)
                    os._exit(1)                         # morte súbita (sem limpeza)

            if pendentes:
                estado = "CONFIRMADA_PENDENTE"
            else:
                self.wal.escrever(gtrid, "CONCLUIDO")
                estado = "CONFIRMADA"
            self.auditoria.registar("2PC_COMMIT", tx_id=tx_id, origem=origem, destino=destino,
                                    **{k: str(v) for k, v in conv.items()}, hash=hash_tx,
                                    pendentes=pendentes, utilizador=utilizador.username)
            return {"tx_id": tx_id, "estado": estado, "protocolo": "Two-Phase Commit (2PC)",
                    "coordenador": config.NODE_ID, "participantes": [so, sd],
                    "participantes_pendentes": pendentes, "origem": origem, "destino": destino,
                    "conversao": _resumo(conv), "rasto": rasto}
        finally:
            for c in conexoes.values():
                try:
                    c.close()
                except Exception:
                    pass
            with self.lock:
                self.em_curso.discard(gtrid)

    # ------------------------------------------- Taxas de câmbio (replicação 2PC)
    def taxas(self) -> dict:
        """Lê as taxas em TODAS as réplicas e indica se estão coerentes."""
        por_no = {}
        for shard in config.SHARDS:
            try:
                with ligar(shard, autocommit=True) as c:
                    por_no[config.SHARD_NOMES[shard]] = {
                        l["moeda"].strip(): str(l["mzn_por_unidade"]) for l in c.execute(
                            "SELECT moeda, mzn_por_unidade FROM taxas_cambio ORDER BY moeda")}
            except FragmentoIndisponivel:
                por_no[config.SHARD_NOMES[shard]] = "INDISPONÍVEL"
        valores = [v for v in por_no.values() if isinstance(v, dict)]
        return {"moeda_referencia": "MZN", "mzn_por_unidade": por_no,
                "replicas_coerentes": all(v == valores[0] for v in valores) if valores else None}

    def atualizar_taxa(self, moeda: str, mzn_por_unidade: Decimal, utilizador) -> dict:
        """Atualiza a tabela REPLICADA em todos os nós de forma atómica (2PC):
        ou todas as réplicas ficam com a nova taxa, ou nenhuma fica."""
        moeda = moeda.upper()
        if moeda == "MZN":
            raise ValueError("O metical é a moeda de referência (taxa fixa = 1)")
        if mzn_por_unidade <= 0:
            raise ValueError("A taxa tem de ser positiva")
        gtrid = f"{config.NODE_ID}|taxa-{uuid.uuid4()}"
        participantes = sorted(config.SHARDS)
        conexoes, rasto = {}, []
        with self.lock:
            self.em_curso.add(gtrid)
        self.wal.escrever(gtrid, "INICIO", participantes=participantes, moeda=moeda,
                          valor=str(mzn_por_unidade))
        try:
            try:
                for shard in participantes:
                    c = ligar(shard)
                    conexoes[shard] = c
                    c.tpc_begin(c.xid(self.FORMAT_ID, gtrid, shard))
                    n = c.execute("UPDATE taxas_cambio SET mzn_por_unidade = %s, atualizado_em = now() "
                                  "WHERE moeda = %s", (mzn_por_unidade, moeda)).rowcount
                    if n != 1:
                        raise ValueError(f"Moeda '{moeda}' não existe na tabela de taxas")
                    c.tpc_prepare()
                    rasto.append(f"FASE 1: réplica {shard} PREPARED (1 {moeda} = {mzn_por_unidade} MZN)")
            except Exception as e:
                self.wal.escrever(gtrid, "ABORT", motivo=str(e)[:300])
                for c in conexoes.values():
                    try:
                        c.tpc_rollback()
                    except Exception:
                        pass
                self.wal.escrever(gtrid, "CONCLUIDO")
                e.rasto = rasto + [f"DECISÃO: ABORT — nenhuma réplica foi alterada ({e})"]  # type: ignore[attr-defined]
                raise
            self.wal.escrever(gtrid, "COMMIT")
            rasto.append("DECISÃO: COMMIT (fsync)")
            for shard, c in conexoes.items():
                c.tpc_commit()
                rasto.append(f"FASE 2: COMMIT PREPARED na réplica {shard} ✓")
            self.wal.escrever(gtrid, "CONCLUIDO")
            self.auditoria.registar("TAXA_ATUALIZADA", moeda=moeda, mzn_por_unidade=str(mzn_por_unidade),
                                    utilizador=utilizador.username)
            return {"estado": "ATUALIZADA_EM_TODAS_AS_REPLICAS", "moeda": moeda,
                    "mzn_por_unidade": str(mzn_por_unidade), "protocolo": "2PC (replicação síncrona)",
                    "rasto": rasto}
        finally:
            for c in conexoes.values():
                try:
                    c.close()
                except Exception:
                    pass
            with self.lock:
                self.em_curso.discard(gtrid)

    def totais(self) -> dict:
        """Soma dos saldos por moeda e equivalente total em meticais (invariante de
        conservação do dinheiro, usado para demonstrar a atomicidade)."""
        por_moeda: dict[str, Decimal] = {}
        taxas = None
        falhas = []
        for shard in config.SHARDS:
            try:
                with ligar(shard, autocommit=True) as c:
                    for l in c.execute("SELECT moeda, sum(saldo) AS s FROM contas GROUP BY moeda"):
                        m = l["moeda"].strip()
                        por_moeda[m] = por_moeda.get(m, Decimal(0)) + l["s"]
                    if taxas is None:
                        taxas = {l["moeda"].strip(): l["mzn_por_unidade"] for l in c.execute(
                            "SELECT moeda, mzn_por_unidade FROM taxas_cambio")}
            except FragmentoIndisponivel:
                falhas.append(config.SHARD_NOMES[shard])
        total = sum((v * taxas[m] for m, v in por_moeda.items()), Decimal(0)) if taxas else None
        return {"saldos_por_moeda": {m: str(v) for m, v in sorted(por_moeda.items())},
                "total_equivalente_MZN": str(total.quantize(CENTIMO)) if total is not None else None,
                "fragmentos_indisponiveis": falhas}

    # ------------------------------------------------------------------ SAGA
    def ordem_interbancaria(self, o: dict, utilizador, hash_tx: str, falha=None) -> dict:
        """Saga com 2 passos: T1 = débito local (confirmado de imediato);
        T2 = envio da ordem ao mainframe do banco parceiro, na moeda da conta.
        Se T2 falhar, executa-se a compensação C1 = estorno do débito."""
        tx_id, conta = o["id"], o["conta_origem"].upper()
        valor = Decimal(o["valor"])
        if valor <= 0:
            raise ValueError("O valor tem de ser positivo")
        shard = shard_de(conta)
        rasto = []
        contraparte = f"{o['banco_destino']}:{o['iban_destino']}"

        # ---- T1: débito local (transação ACID local, confirmada)
        with ligar(shard) as c:
            with c.transaction():
                linha = bloquear_conta(c, conta, "origem")
                verificar_debito(linha, valor, utilizador)
                movimentar(c, conta, "DEBITO_EXTERNO", valor, linha["moeda"], tx_id, contraparte,
                           hash_tx, "SAGA")
        moeda = linha["moeda"]
        self.saga_log.escrever(tx_id, "T1_DEBITO_OK", conta=conta, valor=str(valor), moeda=moeda,
                               shard=shard, contraparte=contraparte, hash=hash_tx)
        rasto.append(f"SAGA T1: débito de {valor} {moeda} em {conta} confirmado em {shard}")

        # ---- T2: mensagem XML assinada para o sistema legado (Java)
        motivo = None
        try:
            if falha == "legado_indisponivel":
                raise ConnectionError("⚡ FALHA INJETADA: mainframe legado não responde")
            resp = legado.enviar_ordem({
                "Referencia": tx_id, "ContaOrigem": conta, "BancoDestino": o["banco_destino"],
                "IbanDestino": o["iban_destino"], "Valor": f"{valor:.2f}",
                "Moeda": moeda, "Ordenante": utilizador.username,
                "DataHora": o["timestamp"]})
            rasto.append(f"SAGA T2: legado respondeu {resp.get('Estado')} "
                         f"(XML verificado, assinatura aceite)")
            if resp.get("Estado") == "ACEITE":
                self.saga_log.escrever(tx_id, "CONCLUIDA", autorizacao=resp.get("CodigoAutorizacao"))
                self.auditoria.registar("SAGA_CONCLUIDA", tx_id=tx_id, conta=conta,
                                        valor=str(valor), moeda=moeda, hash=hash_tx,
                                        autorizacao=resp.get("CodigoAutorizacao"))
                return {"tx_id": tx_id, "estado": "CONCLUIDA", "protocolo": "SAGA",
                        "resposta_legado": resp, "rasto": rasto}
            motivo = f"Rejeitada pelo banco parceiro: {resp.get('Motivo')}"
        except Exception as e:
            motivo = f"Falha na comunicação com o legado: {e}"
        rasto.append(f"SAGA T2 falhou -> {motivo}")

        # ---- C1: compensação
        self._compensar(tx_id, conta, valor, shard, contraparte, hash_tx, motivo)
        rasto.append(f"SAGA C1: compensação executada — {valor} {moeda} devolvido a {conta}")
        return {"tx_id": tx_id, "estado": "COMPENSADA", "protocolo": "SAGA", "motivo": motivo,
                "rasto": rasto}

    def _compensar(self, tx_id, conta, valor, shard, contraparte, hash_tx, motivo):
        try:
            with ligar(shard) as c:
                with c.transaction():
                    linha = bloquear_conta(c, conta, "origem")
                    movimentar(c, conta, "COMPENSACAO", valor, linha["moeda"], tx_id, contraparte,
                               hash_tx, "SAGA")
        except TransacaoDuplicada:
            pass                                    # compensação já feita (idempotente)
        self.saga_log.escrever(tx_id, "COMPENSADA", motivo=motivo)
        self.auditoria.registar("SAGA_COMPENSADA", tx_id=tx_id, conta=conta, valor=str(valor),
                                motivo=motivo)

    # ============================================================ RECUPERAÇÃO
    def recuperar(self) -> list[str]:
        """Resolve transações 'em dúvida' (in-doubt) após falhas.

        * Decisão COMMIT no WAL  -> COMMIT PREPARED nos participantes que ficaram preparados
        * Decisão ABORT no WAL   -> ROLLBACK PREPARED
        * Sem decisão e antiga   -> presumed abort: ROLLBACK PREPARED
        """
        acoes: list[str] = []
        estados = self.wal.estados()
        with self.lock:
            em_curso = set(self.em_curso)
        alcancaveis, ainda_preparadas = set(), set()

        for shard in config.SHARDS:
            try:
                c = ligar(shard, autocommit=True)
            except FragmentoIndisponivel:
                acoes.append(f"{shard}: indisponível — nova tentativa no próximo ciclo")
                continue
            alcancaveis.add(shard)
            with c:
                for xid in c.tpc_recover():
                    gtrid = xid.gtrid or ""
                    if not gtrid.startswith(config.NODE_ID + "|") or gtrid in em_curso:
                        continue
                    decisao = estados.get(gtrid, {}).get("decisao")
                    if decisao == "COMMIT":
                        c.tpc_commit(xid)
                        acoes.append(f"{shard}: COMMIT PREPARED de {gtrid} (decisão no WAL = COMMIT)")
                        self.auditoria.registar("2PC_RECUPERACAO_COMMIT", gtrid=gtrid, shard=shard)
                    elif decisao == "ABORT":
                        c.tpc_rollback(xid)
                        acoes.append(f"{shard}: ROLLBACK PREPARED de {gtrid} (decisão = ABORT)")
                        self.auditoria.registar("2PC_RECUPERACAO_ABORT", gtrid=gtrid, shard=shard)
                    else:
                        idade = (datetime.now(timezone.utc) - xid.prepared).total_seconds() \
                            if xid.prepared else 1e9
                        if idade > config.PRESUMED_ABORT_APOS_S:
                            c.tpc_rollback(xid)
                            self.wal.escrever(gtrid, "ABORT", motivo="presumed abort (sem decisão)")
                            acoes.append(f"{shard}: ROLLBACK PREPARED de {gtrid} (presumed abort)")
                            self.auditoria.registar("2PC_PRESUMED_ABORT", gtrid=gtrid, shard=shard)
                        else:
                            ainda_preparadas.add(gtrid)

        # Marcar como CONCLUÍDAS as transações cuja fase 2 terminou em todos os participantes
        for gtrid, e in estados.items():
            if e["estado"] in ("COMMIT", "ABORT") and gtrid not in em_curso \
                    and gtrid not in ainda_preparadas \
                    and set(e.get("participantes", [])) <= alcancaveis:
                self.wal.escrever(gtrid, "CONCLUIDO")
        acoes += self._recuperar_sagas()
        return acoes

    def _recuperar_sagas(self) -> list[str]:
        """Sagas interrompidas a meio (ex: crash depois de T1): pergunta ao legado
        pelo estado real da ordem (consulta idempotente) antes de compensar."""
        acoes = []
        for tx_id, e in self.saga_log.estados().items():
            if e["estado"] != "T1_DEBITO_OK":
                continue
            idade = (datetime.now(timezone.utc) - datetime.fromisoformat(e["ts"])).total_seconds()
            if idade < 30:
                continue
            try:
                estado_legado = legado.consultar_ordem(tx_id)
            except Exception:
                acoes.append(f"SAGA {tx_id}: legado inacessível — a aguardar")
                continue
            if estado_legado and estado_legado.get("Estado") == "ACEITE":
                self.saga_log.escrever(tx_id, "CONCLUIDA", recuperada=True)
                acoes.append(f"SAGA {tx_id}: legado confirma ACEITE -> concluída")
            else:
                try:
                    self._compensar(tx_id, e["conta"], Decimal(e["valor"]), e["shard"],
                                    e["contraparte"], e["hash"], "recuperação: ordem não chegou ao legado")
                    acoes.append(f"SAGA {tx_id}: compensada na recuperação")
                except FragmentoIndisponivel:
                    acoes.append(f"SAGA {tx_id}: fragmento indisponível — a aguardar")
        return acoes

    def ciclo_recuperacao(self, parar: threading.Event):
        while not parar.wait(config.RECUPERACAO_INTERVALO_S):
            try:
                for a in self.recuperar():
                    print(f"[recuperação] {a}", flush=True)
            except Exception as e:
                print(f"[recuperação] erro: {e}", flush=True)
            self.auditoria.tentar_flush()
