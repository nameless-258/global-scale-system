"""NÓ CENTRAL — caminho alternativo quando uma região inteira está em baixo.

Cada região tem dois nós (primário e réplica síncrona). Se os DOIS falharem, as
contas dessa região deixariam de poder enviar ou receber dinheiro. O Nó Central
(db-central, com a sua própria réplica db-central-r) resolve isso, à maneira do
"stand-in processing" das redes bancárias:

1. ESPELHO DOS SALDOS
   A central guarda uma cópia do saldo de todas as contas. Depois de cada transação
   confirmada numa região, a API publica no espelho o saldo e a VERSÃO da linha
   (UPDATE condicional: só substitui se a versão for mais recente, por isso a ordem
   de chegada não importa). Um SINCRONIZADOR compara as regiões com o espelho a
   cada 2 s e corrige qualquer diferença.

2. OPERAÇÕES EM SUBSTITUIÇÃO (stand-in)
   Com uma região em baixo, a central autoriza as operações em nome dela, com base
   no espelho: verifica o titular e o saldo, atualiza o saldo no espelho e guarda a
   operação na tabela "pendentes". Uma transferência entre uma região em baixo e
   outra que funciona continua a ser ATÓMICA: é um Two-Phase Commit entre a central
   e a região que está viva.

3. RECONCILIAÇÃO
   Quando a região volta, as operações pendentes são aplicadas nela pela ORDEM em que
   foram feitas, cada uma com o mesmo tx_id (a restrição UNIQUE dos movimentos torna a
   aplicação idempotente: se a API morrer a meio, repetir não duplica nada). Até a
   reconciliação acabar, a região não aceita operações diretas — assim o saldo nunca
   é gasto duas vezes.

Segurança do espelho: se não for possível publicar um saldo (central em baixo,
crash da API), a região fica marcada "por verificar" e a central RECUSA operações em
nome dela até o sincronizador confirmar que o espelho está igual à região.
"""
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from . import config
from .cripto import decifrar, ctx_contraparte, ctx_titular
from .shards import ContaInexistente, FragmentoIndisponivel, _descrever, ligar, para_json, regiao_em_baixo

CT = config.CENTRAL
_ESTADO = config.DATA_DIR / "central-estado.json"
_TRINCO = config.DATA_DIR / "central-estado.lock"
DEBITOS = ("DEBITO", "DEBITO_EXTERNO")
NOMES_REGIAO = {"EU": "Europa", "AM": "América", "AF": "África"}


def log(msg):
    print(f"[central] {msg}", flush=True)


# ------------------------------------------------- estado partilhado pelas APIs
# Ficheiro no volume coordenador-data (partilhado por api-1 e api-2), com trinco
# (flock) para as duas réplicas não o escreverem ao mesmo tempo.
@contextmanager
def _estado(escrever: bool = True):
    _TRINCO.parent.mkdir(parents=True, exist_ok=True)
    with open(_TRINCO, "a+") as t:
        fcntl.flock(t, fcntl.LOCK_EX if escrever else fcntl.LOCK_SH)
        try:
            e = json.loads(_ESTADO.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            e = {}
        e.setdefault("por_verificar", {})      # espelho desta região pode estar desatualizado
        e.setdefault("com_pendentes", {})      # há operações da central por aplicar nesta região
        yield e
        if escrever:
            tmp = _ESTADO.with_suffix(".tmp")
            tmp.write_text(json.dumps(e))
            os.replace(tmp, _ESTADO)


def marcar(chave: str, regioes, valor: bool = True):
    with _estado() as e:
        for r in regioes:
            if valor:
                e[chave].setdefault(r, datetime.now(timezone.utc).isoformat())
            else:
                e[chave].pop(r, None)


def estado() -> dict:
    with _estado(escrever=False) as e:
        return {"por_verificar": dict(e["por_verificar"]), "com_pendentes": dict(e["com_pendentes"])}


def pode_substituir(shard: str) -> None:
    """A central só autoriza operações em nome de uma região se estiver ligada e se o
    espelho dessa região for de confiança."""
    if regiao_em_baixo(CT):
        raise FragmentoIndisponivel(shard, RuntimeError(
            f"a região {NOMES_REGIAO.get(shard, shard)} e o Nó Central estão os dois em baixo"))
    if shard in estado()["por_verificar"]:
        raise FragmentoIndisponivel(shard, RuntimeError(
            "a região está em baixo e o Nó Central não tem a certeza do saldo atual destas contas "
            "(o espelho não foi confirmado depois da última falha), por isso não autoriza a operação"))


def garantir_reconciliada(shard: str) -> None:
    """Antes de usar diretamente uma região que esteve em baixo, aplica nela as
    operações que a central autorizou entretanto (senão o saldo podia ser gasto duas vezes)."""
    if shard not in estado()["com_pendentes"]:
        return
    try:
        reconciliar(shard)
    except Exception as e:
        log(f"reconciliação de {shard} falhou: {e}")
    if shard in estado()["com_pendentes"]:
        raise FragmentoIndisponivel(shard, RuntimeError(
            "a região voltou, mas ainda está a receber as operações feitas pela central enquanto "
            "esteve em baixo; tente de novo dentro de segundos"))


# ------------------------------------------------------------------ espelho
_UPSERT = (
    "INSERT INTO espelho AS e (conta, regiao, moeda, owner_username, titular_cifrado, saldo, versao) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (conta) DO UPDATE SET "
    "saldo = EXCLUDED.saldo, versao = EXCLUDED.versao, moeda = EXCLUDED.moeda, "
    "owner_username = EXCLUDED.owner_username, titular_cifrado = EXCLUDED.titular_cifrado, "
    "atualizado_em = now() "
    "WHERE e.versao < EXCLUDED.versao AND NOT EXISTS ("
    "  SELECT 1 FROM pendentes p WHERE p.conta = e.conta AND p.estado = 'PENDENTE')")


def _valores(l: dict) -> tuple:
    return (l["id"], l["regiao"], l["moeda"].strip(), l["owner_username"], l.get("titular_cifrado"),
            l["saldo"], l["versao"])


def publicar(linhas: list[dict]) -> None:
    """Copia para o espelho o saldo e a versão das contas alteradas por uma transação
    já confirmada. Se não conseguir, marca a região como "por verificar"."""
    linhas = [l for l in linhas if l]
    if not linhas:
        return
    regioes = {l["regiao"].strip() for l in linhas}
    try:
        if regiao_em_baixo(CT):
            raise FragmentoIndisponivel(CT, RuntimeError("Nó Central em baixo"))
        with ligar(CT, autocommit=True) as c:
            for l in linhas:
                c.execute(_UPSERT, _valores(l))
    except Exception as e:
        log(f"espelho não atualizado ({e}); regiões {sorted(regioes)} ficam por verificar")
        marcar("por_verificar", regioes)


def bloquear(c, conta: str, papel: str) -> dict:
    """Equivalente a bloquear_conta(), mas no espelho da central (SELECT ... FOR UPDATE)."""
    l = c.execute("SELECT conta AS id, saldo, moeda, owner_username, regiao FROM espelho "
                  "WHERE conta = %s FOR UPDATE", (conta,)).fetchone()
    if not l:
        raise ContaInexistente(f"Conta de {papel} '{conta}' não existe (ou ainda não está no espelho)")
    l["moeda"], l["regiao"] = l["moeda"].strip(), l["regiao"].strip()
    return l


def registar(c, conta: str, regiao: str, tipo: str, valor: Decimal, moeda: str, tx_id,
             contraparte_cifrada: bytes, hash_tx: str, vc=None, mc=None, taxa=None) -> None:
    """Operação autorizada pela central: atualiza o saldo no espelho e guarda-a em
    'pendentes' para ser aplicada na região quando ela voltar."""
    sinal = "-" if tipo in DEBITOS else "+"
    c.execute(f"UPDATE espelho SET saldo = saldo {sinal} %s, atualizado_em = now() WHERE conta = %s",
              (valor, conta))
    c.execute("INSERT INTO pendentes (tx_id, conta, regiao, tipo, valor, moeda, valor_contraparte, "
              "moeda_contraparte, taxa_aplicada, contraparte_cifrada, hash_sha256, no_coordenador) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
              (tx_id, conta, regiao, tipo, valor, moeda, vc, mc, taxa, contraparte_cifrada, hash_tx,
               config.NODE_ID))


# ------------------------------------------------------------- reconciliação
def reconciliar(shard: str) -> list[str]:
    """Aplica na região, por ordem, as operações que a central autorizou enquanto ela
    esteve em baixo. Idempotente (UNIQUE (tx_id, conta_id, tipo) nos movimentos)."""
    acoes: list[str] = []
    with ligar(CT, autocommit=True) as cc:
        chave = 7_000 + list(config.SHARDS).index(shard)
        if not cc.execute("SELECT pg_try_advisory_lock(%s) AS ok", (chave,)).fetchone()["ok"]:
            return [f"{shard}: reconciliação já a decorrer noutra réplica da API"]
        try:
            pend = cc.execute("SELECT * FROM pendentes WHERE regiao = %s AND estado = 'PENDENTE' "
                              "ORDER BY id", (shard,)).fetchall()
            for p in pend:
                sinal = "-" if p["tipo"] in DEBITOS else "+"
                with ligar(shard) as c:
                    try:
                        with c.transaction():
                            c.execute(f"UPDATE contas SET saldo = saldo {sinal} %s, versao = versao + 1, "
                                      f"atualizado_em = now() WHERE id = %s", (p["valor"], p["conta"]))
                            c.execute(
                                "INSERT INTO movimentos (tx_id, conta_id, tipo, valor, moeda, valor_contraparte, "
                                "moeda_contraparte, taxa_aplicada, contraparte_cifrada, hash_sha256, protocolo, "
                                "no_coordenador, criado_em) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CENTRAL',%s,%s)",
                                (p["tx_id"], p["conta"], p["tipo"], p["valor"], p["moeda"], p["valor_contraparte"],
                                 p["moeda_contraparte"], p["taxa_aplicada"], p["contraparte_cifrada"],
                                 p["hash_sha256"], p["no_coordenador"], p["criado_em"]))
                        nota = "aplicada"
                    except psycopg.errors.UniqueViolation:
                        nota = "já tinha sido aplicada (idempotência)"
                    versao = c.execute("SELECT versao FROM contas WHERE id = %s", (p["conta"],)).fetchone()["versao"]
                cc.execute("UPDATE pendentes SET estado = 'APLICADA', aplicada_em = now(), aplicada_por = %s "
                           "WHERE id = %s", (config.NODE_ID, p["id"]))
                cc.execute("UPDATE espelho SET versao = GREATEST(versao, %s) WHERE conta = %s",
                           (versao, p["conta"]))
                acoes.append(f"{shard}: {p['tipo']} de {p['valor']} {p['moeda'].strip()} em {p['conta']} "
                             f"(tx {p['tx_id']}) {nota}")
            resta = cc.execute("SELECT count(*) AS n FROM pendentes WHERE regiao = %s AND estado = 'PENDENTE'",
                               (shard,)).fetchone()["n"]
            if resta == 0:
                marcar("com_pendentes", [shard], False)
        finally:
            cc.execute("SELECT pg_advisory_unlock(%s)", (chave,))
    if acoes:
        log(f"reconciliação de {NOMES_REGIAO.get(shard, shard)}: {len(acoes)} operação(ões) aplicada(s)")
    return acoes


# ------------------------------------------------------------- sincronizador
def sincronizar() -> list[str]:
    """Para cada região que está a funcionar: aplica as pendentes (se houver), copia
    para o espelho qualquer saldo mais recente e, se tudo coincidir, confirma que o
    espelho dessa região é de confiança."""
    acoes: list[str] = []
    if regiao_em_baixo(CT):
        return acoes
    est = estado()
    for shard in config.SHARDS:
        if regiao_em_baixo(shard):
            continue
        try:
            with ligar(CT, autocommit=True) as cc:
                tem = cc.execute("SELECT 1 FROM pendentes WHERE regiao = %s AND estado = 'PENDENTE' LIMIT 1",
                                 (shard,)).fetchone()
            if tem or shard in est["com_pendentes"]:
                if tem:
                    marcar("com_pendentes", [shard])
                acoes += reconciliar(shard)
                if shard in estado()["com_pendentes"]:
                    continue
            with ligar(shard, autocommit=True) as c:
                contas = c.execute("SELECT id, regiao, moeda, owner_username, titular_cifrado, saldo, versao "
                                   "FROM contas").fetchall()
            with ligar(CT, autocommit=True) as cc:
                for l in contas:
                    if cc.execute(_UPSERT, _valores(l)).rowcount:
                        acoes.append(f"espelho: {l['id']} atualizado para a versão {l['versao']}")
                espelho = {e["conta"]: (e["saldo"], e["versao"]) for e in cc.execute(
                    "SELECT conta, saldo, versao FROM espelho WHERE regiao = %s", (shard,))}
            igual = len(espelho) == len(contas) and all(
                espelho.get(l["id"]) == (l["saldo"], l["versao"]) for l in contas)
            if igual and shard in est["por_verificar"]:
                marcar("por_verificar", [shard], False)
                acoes.append(f"espelho de {NOMES_REGIAO[shard]} confirmado igual à região")
        except Exception as e:
            acoes.append(f"{shard}: sincronização adiada ({type(e).__name__}: {str(e)[:120]})")
    return acoes


def ciclo(parar: threading.Event):
    # No arranque não se sabe se a API morreu entre um COMMIT e a publicação no espelho:
    # todas as regiões ficam "por verificar" até o sincronizador as confirmar.
    marcar("por_verificar", list(config.SHARDS))
    while True:
        try:
            for a in sincronizar():
                log(a)
        except Exception as e:
            log(f"sincronização: {e}")
        if parar.wait(config.CENTRAL_SYNC_S):
            return


# ------------------------------------------------------------------ leituras
def contas_espelho(regioes: list[str], owner: str | None = None, excluir_owner: str | None = None) -> list[dict]:
    """Contas das regiões em baixo, servidas a partir do espelho da central."""
    if not regioes or regiao_em_baixo(CT):
        return []
    sql = ("SELECT conta AS id, regiao, moeda, owner_username, titular_cifrado, saldo, versao "
           "FROM espelho WHERE regiao = ANY(%s)")
    params: list = [regioes]
    if owner:
        sql += " AND owner_username = %s"
        params.append(owner)
    if excluir_owner:
        sql += " AND owner_username <> %s"
        params.append(excluir_owner)
    with ligar(CT, autocommit=True) as c:
        linhas = c.execute(sql + " ORDER BY conta", params).fetchall()
    for l in linhas:
        l["titular"] = decifrar(l.pop("titular_cifrado"), ctx_titular(l["id"]))
        l["moeda"], l["regiao"] = l["moeda"].strip(), l["regiao"].strip()
        l["fragmento"] = config.SHARD_NOMES[CT]
        l["via_central"] = True
    return linhas


def efeitos_pendentes(c, shard: str) -> dict[str, Decimal]:
    """Soma, por moeda, das operações autorizadas pela central e ainda não aplicadas."""
    return {l["moeda"].strip(): l["s"] for l in c.execute(
        "SELECT moeda, sum(CASE WHEN tipo IN ('DEBITO','DEBITO_EXTERNO') THEN -valor ELSE valor END) AS s "
        "FROM pendentes WHERE regiao = %s AND estado = 'PENDENTE' GROUP BY moeda", (shard,))}


def extracto_central(conta_id: str) -> dict:
    """Extrato mínimo de uma conta cuja região está em baixo: saldo do espelho e as
    operações feitas através da central (o histórico completo volta com a região)."""
    with ligar(CT, autocommit=True) as c:
        e = c.execute("SELECT * FROM espelho WHERE conta = %s", (conta_id,)).fetchone()
        if not e:
            raise ContaInexistente(f"Conta '{conta_id}' não existe")
        pend = c.execute("SELECT * FROM pendentes WHERE conta = %s AND estado = 'PENDENTE' ORDER BY id",
                         (conta_id,)).fetchall()
    ids = sorted({decifrar(p["contraparte_cifrada"], ctx_contraparte(p["tx_id"], conta_id, p["tipo"]))
                  for p in pend if p["tipo"] in ("DEBITO", "CREDITO")})
    nomes = {}
    if ids:
        with ligar(CT, autocommit=True) as c:
            nomes = {l["conta"]: decifrar(l["titular_cifrado"], ctx_titular(l["conta"])) for l in c.execute(
                "SELECT conta, titular_cifrado FROM espelho WHERE conta = ANY(%s)", (ids,))}
    sinal = lambda p: -p["valor"] if p["tipo"] in DEBITOS else p["valor"]
    saldo = e["saldo"] - sum((sinal(p) for p in pend), Decimal(0))
    inicial, deb, cred, linhas = saldo, Decimal(0), Decimal(0), []
    for p in pend:
        saldo += sinal(p)
        contraparte = decifrar(p["contraparte_cifrada"], ctx_contraparte(p["tx_id"], conta_id, p["tipo"]))
        if sinal(p) < 0:
            deb += p["valor"]
        else:
            cred += p["valor"]
        linhas.append({"data": p["criado_em"].isoformat(), "tipo": p["tipo"],
                       "descricao": _descrever(p["tipo"], contraparte, nomes) + " (via Nó Central)",
                       "debito": str(p["valor"]) if sinal(p) < 0 else None,
                       "credito": str(p["valor"]) if sinal(p) > 0 else None, "saldo_apos": str(saldo),
                       "valor_contraparte": para_json(p["valor_contraparte"]),
                       "moeda_contraparte": p["moeda_contraparte"], "referencia": str(p["tx_id"]),
                       "protocolo": "CENTRAL (a aguardar a região)"})
    return {"conta": conta_id, "titular": decifrar(e["titular_cifrado"], ctx_titular(conta_id)),
            "owner_username": e["owner_username"], "moeda": e["moeda"].strip(), "regiao": e["regiao"].strip(),
            "fragmento": config.SHARD_NOMES[CT], "via_central": True,
            "nota": "A região desta conta está em baixo. O saldo vem do Nó Central e só aparecem as "
                    "operações feitas através dele; o histórico completo volta quando a região voltar.",
            "periodo": {"de": None, "ate": None}, "saldo_inicial": str(inicial), "total_debitos": str(deb),
            "total_creditos": str(cred), "saldo_final": str(saldo), "saldo_atual": str(e["saldo"]),
            "movimentos": linhas}


def inspecionar_central(no: str, detalhe: bool = True) -> dict:
    """Página 'Bases de dados': o que está guardado num nó da central."""
    base = {"no": no, "regiao": CT, "fragmento": config.SHARD_NOMES[CT]}
    from .shards import _porta_aberta
    if not _porta_aberta(no):
        return {**base, "estado": "INDISPONÍVEL", "erro": "Não responde: o servidor está desligado."}
    try:
        with psycopg.connect(config.dsn_no(no), autocommit=True, row_factory=dict_row,
                             options="-c statement_timeout=5000") as c:
            replica = c.execute("SELECT pg_is_in_recovery() AS r").fetchone()["r"]
            lsn = c.execute("SELECT " + ("pg_last_wal_replay_lsn()" if replica else "pg_current_wal_lsn()")
                            + "::text AS lsn").fetchone()["lsn"]
            esp = c.execute("SELECT * FROM espelho ORDER BY conta").fetchall()
            pend = c.execute("SELECT * FROM pendentes ORDER BY id DESC LIMIT 25").fetchall()
            n_pend = c.execute("SELECT count(*) AS n FROM pendentes WHERE estado = 'PENDENTE'").fetchone()["n"]
            n_tot = c.execute("SELECT count(*) AS n FROM pendentes").fetchone()["n"]
            prep = c.execute("SELECT gid, prepared FROM pg_prepared_xacts ORDER BY prepared").fetchall()
            out = {**base, "estado": "OK", "papel": "réplica" if replica else "primário", "lsn": lsn,
                   "n_contas": len(esp), "n_movimentos": n_tot, "n_pendentes": n_pend, "n_preparadas": len(prep),
                   "saldos_por_moeda": {}}
            for k in esp:
                m = k["moeda"].strip()
                out["saldos_por_moeda"][m] = str(Decimal(out["saldos_por_moeda"].get(m, "0")) + k["saldo"])
            if not detalhe:
                return out
            hexa = lambda b: bytes(b).hex() if b is not None else None
            out["espelho"] = [{"id": k["conta"], "regiao": k["regiao"].strip(), "owner_username": k["owner_username"],
                               "moeda": k["moeda"].strip(), "saldo": str(k["saldo"]), "versao": k["versao"],
                               "titular_cifrado": hexa(k["titular_cifrado"]),
                               "titular_decifrado": decifrar(k["titular_cifrado"], ctx_titular(k["conta"]))
                               if k["titular_cifrado"] else None,
                               "atualizado_em": k["atualizado_em"].isoformat()} for k in esp]
            out["pendentes"] = [{"id": p["id"], "tx_id": str(p["tx_id"]), "conta": p["conta"],
                                 "regiao": p["regiao"].strip(), "tipo": p["tipo"], "valor": str(p["valor"]),
                                 "moeda": p["moeda"].strip(), "estado": p["estado"], "coordenador": p["no_coordenador"],
                                 "contraparte_cifrada": hexa(p["contraparte_cifrada"]),
                                 "contraparte_decifrada": decifrar(p["contraparte_cifrada"], ctx_contraparte(
                                     p["tx_id"], p["conta"], p["tipo"])),
                                 "criado_em": p["criado_em"].isoformat(),
                                 "aplicada_em": p["aplicada_em"].isoformat() if p["aplicada_em"] else None,
                                 "aplicada_por": p["aplicada_por"]} for p in pend]
            out["taxas"] = [{"moeda": t["moeda"].strip(), "mzn_por_unidade": str(t["mzn_por_unidade"]),
                             "atualizado_em": t["atualizado_em"].isoformat()} for t in
                            c.execute("SELECT * FROM taxas_cambio ORDER BY moeda").fetchall()]
            out["preparadas"] = [{"gid": p["gid"], "desde": p["prepared"].isoformat()} for p in prep]
            out["replicacao"] = [] if replica else [
                {"replica": r["application_name"], "estado": r["state"], "modo": r["sync_state"],
                 "lsn": r["replay_lsn"]} for r in c.execute(
                    "SELECT application_name, state, sync_state, replay_lsn::text FROM pg_stat_replication")]
            out["estado_central"] = estado()
            return out
    except psycopg.OperationalError as e:
        return {**base, "estado": "INDISPONÍVEL", "erro": str(e).strip().splitlines()[0][:160]}
