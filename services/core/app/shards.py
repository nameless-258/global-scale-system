"""Base de Dados Distribuída: encaminhamento para fragmentos e consultas globais.

Fragmentação HORIZONTAL por região:
    db-europa  (Nó A) : contas com prefixo "EU-"
    db-america (Nó B) : contas com prefixo "AM-"

As consultas globais (ex: listar todas as contas de um utilizador) usam o padrão
SCATTER-GATHER: a mesma consulta é enviada em PARALELO a todos os fragmentos
(threads) e os resultados são reunidos. Se um fragmento estiver em baixo, o
sistema devolve resultados parciais e indica o fragmento indisponível
(degradação graciosa em vez de falha total).
"""
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


def ligar(shard: str, autocommit: bool = False) -> psycopg.Connection:
    try:
        return psycopg.connect(
            config.SHARDS[shard], autocommit=autocommit, row_factory=dict_row,
            # lock_timeout: evita esperas infinitas (e deadlocks distribuídos não
            # detetáveis localmente) — a transação aborta e o 2PC faz rollback.
            options="-c lock_timeout=5000 -c statement_timeout=15000",
        )
    except psycopg.OperationalError as e:
        raise FragmentoIndisponivel(shard, e) from e


def _consultar(shard: str, sql: str, params: tuple) -> list[dict]:
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
                        c.execute("UPDATE contas SET titular_cifrado = %s, titular = NULL WHERE id = %s",
                                  (cifrar(l["titular"], ctx_titular(l["id"])), l["id"]))
            if linhas:
                acoes.append(f"{s}: {len(linhas)} titulares cifrados (AES-256-GCM)")
        except FragmentoIndisponivel:
            acoes.append(f"{s}: indisponível — cifragem inicial adiada")
    return acoes


def estado_fragmento(shard: str) -> dict:
    """Que nó é o primário atual do fragmento e em que estado está a réplica."""
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
