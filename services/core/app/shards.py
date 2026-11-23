"""Base de Dados Distribuída: encaminhamento para fragmentos e consultas globais.

Fragmentação HORIZONTAL por região:
    db-europa  (Nó A) : contas com prefixo "EU-"
    db-america (Nó B) : contas com prefixo "AM-"
    db-africa  (Nó C) : contas com prefixo "AF-"

As consultas globais (ex: listar todas as contas de um utilizador) usam o padrão
SCATTER-GATHER: a mesma consulta é enviada em PARALELO a todos os fragmentos
(threads) e os resultados são reunidos. Se um fragmento estiver em baixo, o
sistema devolve resultados parciais e indica o fragmento indisponível
(degradação graciosa em vez de falha total).
"""
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from . import config
from .cripto import cifrar, ctx_contraparte, ctx_titular, decifrar


class ContaInexistente(Exception):
    pass


class FragmentoIndisponivel(Exception):
    def __init__(self, shard: str, causa: Exception):
        super().__init__(f"Fragmento {config.SHARD_NOMES.get(shard, shard)} indisponível: {causa}")
        self.shard = shard


def shard_de(conta_id: str) -> str:
    prefixo = conta_id.split("-", 1)[0].upper()
    if prefixo not in config.SHARDS:
        raise ContaInexistente(f"Conta '{conta_id}': região desconhecida "
                               f"(prefixos válidos: {', '.join(config.SHARDS)})")
    return prefixo


ESPERA_FAILOVER_S = float(os.getenv("ESPERA_FAILOVER_S", "25"))


def _porta_aberta(no: str) -> bool:
    try:
        socket.create_connection((config.DB_IPS[no], 5432), timeout=0.5).close()
        return True
    except OSError:
        return False


def regiao_em_baixo(shard: str) -> bool:
    """Verdadeiro se NENHUM dos dois nós do fragmento aceita ligações (0,5 s no máximo).
    Se um deles responde (por exemplo a réplica durante um failover), o fragmento não
    está 'em baixo': o caminho normal espera pela promoção."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        return not any(ex.map(_porta_aberta, config.NOS_FRAGMENTO[shard]))


def ligar(shard: str, autocommit: bool = False, esperar: float = 0.0) -> psycopg.Connection:
    """Liga ao nó que aceita escritas no fragmento (primário ou réplica promovida).

    esperar > 0: se nenhum nó aceitar escritas (por exemplo, o primário acabou de
    morrer e a sentinela ainda está a promover a réplica), tenta de novo durante
    até `esperar` segundos antes de desistir. Assim, uma transferência feita no
    momento da falha é concluída no novo primário em vez de ser recusada."""
    limite = time.monotonic() + esperar
    while True:
        try:
            return psycopg.connect(
                config.DSN[shard], autocommit=autocommit, row_factory=dict_row,
                # lock_timeout: evita esperas infinitas (e deadlocks distribuídos não
                # detetáveis localmente) — a transação aborta e o 2PC faz rollback.
                options="-c lock_timeout=5000 -c statement_timeout=15000",
            )
        except psycopg.OperationalError as e:
            if time.monotonic() >= limite:
                raise FragmentoIndisponivel(shard, e) from e
            time.sleep(1)


def _consultar(shard: str, sql: str, params: tuple) -> list[dict]:
    if regiao_em_baixo(shard):          # falha em 0,5 s em vez de esperar pelos timeouts
        raise FragmentoIndisponivel(shard, RuntimeError("os dois nós estão em baixo"))
    with ligar(shard, autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def scatter_gather(sql: str, params: tuple = ()) -> tuple[list[dict], list[str]]:
    resultados, indisponiveis = [], []
    with ThreadPoolExecutor(max_workers=len(config.SHARDS)) as ex:
        futuros = {s: ex.submit(_consultar, s, sql, params) for s in config.SHARDS}
        for s, f in futuros.items():
            try:
                for linha in f.result():
                    linha["fragmento"] = config.SHARD_NOMES[s]
                    resultados.append(linha)
            except Exception:
                indisponiveis.append(config.SHARD_NOMES[s])
    return resultados, indisponiveis


def com_titular(linhas: list[dict]) -> list[dict]:
    """Decifra o titular (AES-256-GCM) das linhas lidas da BD."""
    for l in linhas:
        cif = l.pop("titular_cifrado", None)
        if l.get("titular") is None and cif is not None:
            l["titular"] = decifrar(cif, ctx_titular(l["id"]))
        if isinstance(l.get("moeda"), str):
            l["moeda"] = l["moeda"].strip()
    return linhas


def obter_conta(conta_id: str) -> dict:
    s = shard_de(conta_id)
    linhas = _consultar_seguro(s, "SELECT * FROM contas WHERE id = %s", (conta_id,))
    if not linhas:
        raise ContaInexistente(f"Conta '{conta_id}' não existe")
    linhas[0]["fragmento"] = config.SHARD_NOMES[s]
    return com_titular(linhas)[0]


def movimentos_de(conta_id: str) -> list[dict]:
    s = shard_de(conta_id)
    linhas = _consultar_seguro(
        s, "SELECT tx_id, conta_id, tipo, valor, moeda, valor_contraparte, moeda_contraparte, "
           "taxa_aplicada, contraparte_cifrada, protocolo, no_coordenador, hash_sha256, criado_em "
           "FROM movimentos WHERE conta_id = %s ORDER BY id DESC LIMIT 50", (conta_id,))
    for l in linhas:
        l["contraparte"] = decifrar(l.pop("contraparte_cifrada"),
                                    ctx_contraparte(l["tx_id"], l.pop("conta_id"), l["tipo"]))
    return linhas


OPERADORAS = {"M-PESA": "M-Pesa", "E-MOLA": "e-Mola", "MKESH": "mKesh"}


def _descrever(tipo: str, contraparte: str, nomes: dict) -> str:
    """Texto do extrato, como num banco: 'Transferência para Bruno Cossa', 'Depósito', ..."""
    if tipo == "DEPOSITO":
        nota = contraparte.split(":", 1)[1] if ":" in contraparte else ""
        return "Depósito" + (f" ({nota})" if nota else "")
    if tipo in ("DEBITO_EXTERNO", "COMPENSACAO"):
        destino, _, ref = contraparte.partition(":")
        alvo = (f"carteira {OPERADORAS[destino]} {ref}" if destino in OPERADORAS
                else f"{destino.replace('-', ' ').title()} ({ref})")
        return (f"Envio para {alvo}" if tipo == "DEBITO_EXTERNO"
                else f"Devolução: envio para {alvo} recusado")
    quem = nomes.get(contraparte, contraparte)
    return f"Transferência para {quem}" if tipo == "DEBITO" else f"Transferência de {quem}"


def extracto(conta_id: str, de: str | None = None, ate: str | None = None) -> dict:
    """Extrato bancário: movimentos a débito e a crédito com o SALDO APÓS cada um.

    O saldo atual e os movimentos são lidos na MESMA transação (REPEATABLE READ),
    por isso o extrato é coerente mesmo com transferências a decorrer. O saldo de
    abertura é o saldo atual menos a soma de todos os movimentos."""
    s = shard_de(conta_id)
    if regiao_em_baixo(s):
        raise FragmentoIndisponivel(s, RuntimeError("os dois nós estão em baixo"))
    try:
        with ligar(s) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conta = c.execute("SELECT id, titular, titular_cifrado, owner_username, regiao, moeda, saldo "
                              "FROM contas WHERE id = %s", (conta_id,)).fetchone()
            if not conta:
                raise ContaInexistente(f"Conta '{conta_id}' não existe")
            movs = c.execute(
                "SELECT tx_id, conta_id, tipo, valor, moeda, valor_contraparte, moeda_contraparte, "
                "taxa_aplicada, contraparte_cifrada, protocolo, criado_em FROM movimentos "
                "WHERE conta_id = %s ORDER BY id", (conta_id,)).fetchall()
            c.rollback()
    except psycopg.OperationalError as e:
        raise FragmentoIndisponivel(s, e) from e
    com_titular([conta])
    for m in movs:
        m["contraparte"] = decifrar(m.pop("contraparte_cifrada"),
                                    ctx_contraparte(m["tx_id"], m.pop("conta_id"), m["tipo"]))
    # nomes dos titulares das contas que aparecem como contraparte (consulta global)
    ids = sorted({m["contraparte"] for m in movs if m["tipo"] in ("DEBITO", "CREDITO")})
    nomes = {}
    if ids:
        linhas, _ = scatter_gather("SELECT id, titular, titular_cifrado FROM contas WHERE id = ANY(%s)", (ids,))
        nomes = {l["id"]: l["titular"] for l in com_titular(linhas)}
    sinal = lambda m: -m["valor"] if m["tipo"] in ("DEBITO", "DEBITO_EXTERNO") else m["valor"]
    saldo = conta["saldo"] - sum((sinal(m) for m in movs), Decimal("0"))
    abertura_total = saldo
    linhas, saldo_inicial, deb, cred = [], None, Decimal("0"), Decimal("0")
    for m in movs:
        dia = m["criado_em"].date().isoformat()
        antes = saldo
        saldo += sinal(m)
        if (de and dia < de) or (ate and dia > ate):
            continue
        if saldo_inicial is None:
            saldo_inicial = antes
        if sinal(m) < 0:
            deb += m["valor"]
        else:
            cred += m["valor"]
        linhas.append({"data": m["criado_em"].isoformat(), "descricao": _descrever(m["tipo"], m["contraparte"], nomes),
                       "tipo": m["tipo"], "debito": str(m["valor"]) if sinal(m) < 0 else None,
                       "credito": str(m["valor"]) if sinal(m) > 0 else None, "saldo_apos": str(saldo),
                       "valor_contraparte": para_json(m["valor_contraparte"]),
                       "moeda_contraparte": m["moeda_contraparte"], "referencia": str(m["tx_id"]),
                       "protocolo": m["protocolo"]})
    if saldo_inicial is None:      # período sem movimentos: saldo no fim do período
        saldo_inicial = saldo if not ate else abertura_total + sum(
            (sinal(m) for m in movs if m["criado_em"].date().isoformat() <= ate), Decimal("0"))
    saldo_final = saldo_inicial + cred - deb
    return {"conta": conta["id"], "titular": conta["titular"], "owner_username": conta["owner_username"],
            "moeda": conta["moeda"], "regiao": conta["regiao"], "fragmento": config.SHARD_NOMES[s],
            "periodo": {"de": de, "ate": ate}, "saldo_inicial": str(saldo_inicial),
            "total_debitos": str(deb), "total_creditos": str(cred), "saldo_final": str(saldo_final),
            "saldo_atual": str(conta["saldo"]), "movimentos": linhas}


def inspecionar_no(no: str, detalhe: bool = True) -> dict:
    """Lê diretamente UM nó (primário ou réplica) e devolve o que lá está guardado,
    tal como está no disco: os dados pessoais aparecem cifrados (AES-256-GCM) e,
    ao lado, decifrados pela API (só ela tem a chave)."""
    if config.NOS_BD[no] == config.CENTRAL:
        from .central import inspecionar_central      # o nó central tem outras tabelas
        return inspecionar_central(no, detalhe)
    if not _porta_aberta(no):
        return {"no": no, "regiao": config.NOS_BD[no], "fragmento": config.SHARD_NOMES[config.NOS_BD[no]],
                "estado": "INDISPONÍVEL", "erro": "Não responde: o servidor está desligado."}
    base = {"no": no, "regiao": config.NOS_BD[no], "fragmento": config.SHARD_NOMES[config.NOS_BD[no]]}
    try:
        with psycopg.connect(config.dsn_no(no), autocommit=True, row_factory=dict_row,
                             options="-c statement_timeout=5000") as c:
            replica = c.execute("SELECT pg_is_in_recovery() AS r").fetchone()["r"]
            lsn = c.execute("SELECT " + ("pg_last_wal_replay_lsn()" if replica else "pg_current_wal_lsn()")
                            + "::text AS lsn").fetchone()["lsn"]
            contas = c.execute("SELECT id, owner_username, regiao, moeda, saldo, versao, titular, "
                               "titular_cifrado FROM contas ORDER BY id").fetchall()
            n_mov = c.execute("SELECT count(*) AS n FROM movimentos").fetchone()["n"]
            prep = c.execute("SELECT gid, prepared FROM pg_prepared_xacts ORDER BY prepared").fetchall()
            out = {**base, "estado": "OK", "papel": "réplica" if replica else "primário", "lsn": lsn,
                   "n_contas": len(contas), "n_movimentos": n_mov, "n_preparadas": len(prep),
                   "saldos_por_moeda": {}}
            for k in contas:
                m = k["moeda"].strip()
                out["saldos_por_moeda"][m] = str(Decimal(out["saldos_por_moeda"].get(m, "0")) + k["saldo"])
            if not detalhe:
                return out
            def hexa(b):
                return bytes(b).hex() if b is not None else None
            out["contas"] = [{"id": k["id"], "owner_username": k["owner_username"], "regiao": k["regiao"],
                              "moeda": k["moeda"].strip(), "saldo": str(k["saldo"]), "versao": k["versao"],
                              "titular_em_claro": k["titular"], "titular_cifrado": hexa(k["titular_cifrado"]),
                              "titular_decifrado": decifrar(k["titular_cifrado"], ctx_titular(k["id"]))
                              if k["titular_cifrado"] is not None else k["titular"]} for k in contas]
            movs = c.execute("SELECT tx_id, conta_id, tipo, valor, moeda, protocolo, no_coordenador, "
                             "hash_sha256, contraparte_cifrada, criado_em FROM movimentos "
                             "ORDER BY id DESC LIMIT 25").fetchall()
            out["movimentos"] = [{"tx_id": str(m["tx_id"]), "conta": m["conta_id"], "tipo": m["tipo"],
                                  "valor": str(m["valor"]), "moeda": m["moeda"].strip(), "protocolo": m["protocolo"],
                                  "coordenador": m["no_coordenador"], "hash": m["hash_sha256"],
                                  "contraparte_cifrada": hexa(m["contraparte_cifrada"]),
                                  "contraparte_decifrada": decifrar(m["contraparte_cifrada"], ctx_contraparte(
                                      m["tx_id"], m["conta_id"], m["tipo"])),
                                  "criado_em": m["criado_em"].isoformat()} for m in movs]
            out["taxas"] = [{"moeda": t["moeda"].strip(), "mzn_por_unidade": str(t["mzn_por_unidade"]),
                             "atualizado_em": t["atualizado_em"].isoformat()} for t in
                            c.execute("SELECT moeda, mzn_por_unidade, atualizado_em FROM taxas_cambio "
                                      "ORDER BY moeda").fetchall()]
            out["preparadas"] = [{"gid": p["gid"], "desde": p["prepared"].isoformat()} for p in prep]
            out["replicacao"] = [] if replica else [
                {"replica": r["application_name"], "estado": r["state"], "modo": r["sync_state"],
                 "lsn": r["replay_lsn"]} for r in c.execute(
                    "SELECT application_name, state, sync_state, replay_lsn::text FROM pg_stat_replication")]
            return out
    except psycopg.OperationalError as e:
        return {**base, "estado": "INDISPONÍVEL", "erro": str(e).strip().splitlines()[0][:160]}


def inspecionar_todos() -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(config.NOS_BD)) as ex:
        return list(ex.map(lambda n: inspecionar_no(n, detalhe=False), config.NOS_BD))


def cifrar_titulares() -> list[str]:
    """Carga inicial: os titulares chegam em claro pelos scripts SQL de arranque;
    no 1.º arranque a aplicação cifra-os e apaga o texto em claro (idempotente)."""
    acoes = []
    for s in config.SHARDS:
        try:
            with ligar(s) as c:
                with c.transaction():
                    linhas = c.execute("SELECT id, titular FROM contas WHERE titular IS NOT NULL "
                                       "FOR UPDATE").fetchall()
                    for l in linhas:
                        c.execute("UPDATE contas SET titular_cifrado = %s, titular = NULL, versao = versao + 1 WHERE id = %s",
                                  (cifrar(l["titular"], ctx_titular(l["id"])), l["id"]))
            if linhas:
                acoes.append(f"{s}: {len(linhas)} titulares cifrados (AES-256-GCM)")
        except FragmentoIndisponivel:
            acoes.append(f"{s}: indisponível — cifragem inicial adiada")
    return acoes


def estado_fragmento(shard: str) -> dict:
    """Que nó é o primário atual do fragmento e em que estado está a réplica."""
    if regiao_em_baixo(shard):
        return {"estado": "INDISPONÍVEL", "nos_em_baixo": list(config.NOS_FRAGMENTO[shard])}
    try:
        with ligar(shard, autocommit=True) as c:
            rep = c.execute("SELECT application_name, state, sync_state FROM pg_stat_replication"
                            ).fetchall()
            return {"estado": "OK", "primario_atual": c.info.host,
                    "replicas": [{"nome": r["application_name"], "estado": r["state"],
                                  "modo": r["sync_state"]} for r in rep] or "nenhuma ligada"}
    except FragmentoIndisponivel:
        return {"estado": "INDISPONÍVEL"}


def _consultar_seguro(shard, sql, params):
    try:
        return _consultar(shard, sql, params)
    except FragmentoIndisponivel:
        raise
    except psycopg.OperationalError as e:
        raise FragmentoIndisponivel(shard, e) from e


def para_json(v):
    if isinstance(v, Decimal):
        return str(v)
    return v
