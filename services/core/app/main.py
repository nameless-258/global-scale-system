"""Global-Scale System — Núcleo da plataforma (Serviços Web REST/JSON sobre HTTPS).

Cada instância (api-1, api-2) é stateless do ponto de vista do cliente e
coordena transações distribuídas sobre os fragmentos da base de dados.
"""
import csv
import io
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import config

# Simulação de falha PERMANENTE de um coordenador: enquanto existir a marca, este
# nó não arranca (como uma máquina perdida). A outra réplica assume a recuperação.
_marca = config.DATA_DIR / f"{config.NODE_ID}.desativado"
if _marca.exists():
    import time as _t
    print(f"[{config.NODE_ID}] DESATIVADO (simulação de falha permanente). Para reativar: "
          f"apague {_marca} e reinicie o contentor.", flush=True)
    while _marca.exists():
        _t.sleep(5)

from . import legado  # noqa: E402
from .dfs import AuditoriaEncadeada, DFSErro, criar_dfs, verificar_cadeia
from .security import (Utilizador, exigir_admin, sha256_hex, utilizador_atual,
                       verificar_frescura, verificar_integridade)
from .shards import (ContaInexistente, FragmentoIndisponivel, cifrar_titulares, com_titular,
                     estado_fragmento, movimentos_de, obter_conta, para_json, scatter_gather)
from .transacoes import (FALHAS_2PC, FALHAS_SAGA, Coordenador, NaoAutorizado,
                         SaldoInsuficiente, TaxasInconsistentes, TransacaoDuplicada)

dfs = criar_dfs()
auditoria = AuditoriaEncadeada(dfs, config.NODE_ID)
coordenador = Coordenador(auditoria)
_parar = threading.Event()


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    auditoria.registar("NO_ARRANCOU", no=config.NODE_ID)
    for a in cifrar_titulares():
        print(f"[cifragem] {a}", flush=True)
    # Recuperação imediata no arranque (essencial após um crash do coordenador)
    try:
        for a in coordenador.recuperar():
            print(f"[recuperação/arranque] {a}", flush=True)
    except Exception as e:
        print(f"[recuperação/arranque] erro: {e}", flush=True)
    t = threading.Thread(target=coordenador.ciclo_recuperacao, args=(_parar,), daemon=True)
    t.start()
    yield
    _parar.set()


app = FastAPI(
    title="Global-Scale System — API Financeira Distribuída",
    version="1.0.0",
    description="Núcleo REST/JSON: contas fragmentadas (Europa/América), transferências com "
                "Two-Phase Commit, ordens interbancárias com Saga, JWT RS256, SHA-256 e HDFS.",
    lifespan=ciclo_de_vida,
)


# ----------------------------------------------------------- Middleware
@app.middleware("http")
async def identificar_no(request: Request, call_next):
    resposta = await call_next(request)
    resposta.headers["X-No-Servidor"] = config.NODE_ID      # mostra qual réplica respondeu
    resposta.headers["Strict-Transport-Security"] = "max-age=31536000"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


# ------------------------------------------------- Mapeamento de erros
def _erro(status: int, codigo: str, e: Exception, **extra):
    corpo = {"erro": codigo, "detalhe": str(e), "no": config.NODE_ID, **extra}
    if getattr(e, "rasto", None):
        corpo["rasto"] = e.rasto
    return JSONResponse(status_code=status, content=corpo)


app.add_exception_handler(ContaInexistente, lambda r, e: _erro(404, "CONTA_INEXISTENTE", e))
app.add_exception_handler(SaldoInsuficiente, lambda r, e: _erro(409, "SALDO_INSUFICIENTE", e,
                                                                 estado="ABORTADA"))
app.add_exception_handler(NaoAutorizado, lambda r, e: _erro(403, "NAO_AUTORIZADO", e,
                                                             estado="ABORTADA"))
app.add_exception_handler(TaxasInconsistentes, lambda r, e: _erro(409, "TAXAS_INCONSISTENTES", e,
                                                                   estado="ABORTADA"))
app.add_exception_handler(TransacaoDuplicada, lambda r, e: _erro(409, "TRANSACAO_DUPLICADA", e))
app.add_exception_handler(FragmentoIndisponivel, lambda r, e: _erro(
    503, "FRAGMENTO_INDISPONIVEL", e, estado="ABORTADA",
    nota="Nenhum dinheiro foi movimentado: a transação foi abortada de forma atómica."))
app.add_exception_handler(DFSErro, lambda r, e: _erro(503, "DFS_INDISPONIVEL", e))
app.add_exception_handler(ValueError, lambda r, e: _erro(400, "PEDIDO_INVALIDO", e))


# ------------------------------------------------------------- Modelos
VALOR = Field(pattern=r"^\d{1,12}(\.\d{1,2})?$", examples=["150.00"])


class PedidoTransferencia(BaseModel):
    id: UUID = Field(description="UUID gerado pelo cliente (idempotência / anti-replay)")
    origem: str = Field(examples=["EU-1001"])
    destino: str = Field(examples=["AM-2001"])
    valor: str = Field(pattern=r"^\d{1,12}(\.\d{1,2})?$", examples=["150.00"],
                       description="Valor na moeda da conta de ORIGEM (é convertido para a moeda do destino)")
    timestamp: str = Field(examples=["2026-11-18T10:00:00Z"])
    hash_sha256: str = Field(min_length=64, max_length=64,
                             description="SHA-256 da forma canónica de {id,origem,destino,valor,timestamp}")

    def campos(self) -> dict:
        return {"id": str(self.id), "origem": self.origem, "destino": self.destino,
                "valor": self.valor, "timestamp": self.timestamp}


class PedidoOrdemExterna(BaseModel):
    id: UUID
    conta_origem: str = Field(examples=["EU-1001"])
    banco_destino: str = Field(examples=["BANCO-PARCEIRO-MZ"])
    iban_destino: str = Field(examples=["MZ59000100000012345678901"])
    valor: str = Field(pattern=r"^\d{1,12}(\.\d{1,2})?$", examples=["150.00"],
                       description="Valor na moeda da conta de origem")
    timestamp: str
    hash_sha256: str = Field(min_length=64, max_length=64)

    def campos(self) -> dict:
        return {"id": str(self.id), "conta_origem": self.conta_origem,
                "banco_destino": self.banco_destino, "iban_destino": self.iban_destino,
                "valor": self.valor, "timestamp": self.timestamp}


# ============================================================ Endpoints
@app.get("/", tags=["sistema"])
def raiz():
    return {"sistema": "Global-Scale System", "no": config.NODE_ID,
            "documentacao": "/docs", "falhas_injetaveis_2pc": FALHAS_2PC,
            "falhas_injetaveis_saga": FALHAS_SAGA}


@app.get("/saude", tags=["sistema"])
def saude():
    estado = {"no": config.NODE_ID, "hora": datetime.now(timezone.utc).isoformat(),
              "fragmentos": {}, "dfs": None, "legado": None}
    for s, nome in config.SHARD_NOMES.items():
        estado["fragmentos"][nome] = estado_fragmento(s)
    try:
        estado["dfs"] = dfs.saude()
    except Exception as e:
        estado["dfs"] = {"estado": "INDISPONÍVEL", "erro": str(e)[:150]}
    estado["dfs_auditoria_pendente"] = len(auditoria.pendentes)
    try:
        estado["legado"] = legado.saude()
    except Exception as e:
        estado["legado"] = {"Estado": "INDISPONÍVEL", "erro": str(e)[:150]}
    return estado


# ------------------------------------------------------------------ Contas
@app.get("/contas", tags=["contas"])
def listar_contas(u: Utilizador = Depends(utilizador_atual)):
    """Consulta GLOBAL (scatter-gather paralelo sobre todos os fragmentos)."""
    if u.is_admin:
        linhas, falhas = scatter_gather("SELECT id, titular, titular_cifrado, regiao, moeda, saldo, "
                                        "versao FROM contas ORDER BY id")
    else:
        linhas, falhas = scatter_gather("SELECT id, titular, titular_cifrado, regiao, moeda, saldo, "
                                        "versao FROM contas WHERE owner_username = %s ORDER BY id",
                                        (u.username,))
    com_titular(linhas)
    return {"utilizador": u.username, "contas": [{k: para_json(v) for k, v in l.items()}
                                                  for l in linhas],
            "fragmentos_indisponiveis": falhas,
            "resultado_parcial": bool(falhas)}


@app.get("/contas/{conta_id}", tags=["contas"])
def detalhe_conta(conta_id: str, u: Utilizador = Depends(utilizador_atual)):
    conta = obter_conta(conta_id.upper())
    if not u.is_admin and conta["owner_username"] != u.username:
        raise HTTPException(403, "Não é titular desta conta")
    return {"conta": {k: para_json(v) for k, v in conta.items()},
            "movimentos": [{k: para_json(v) for k, v in m.items()}
                           for m in movimentos_de(conta_id.upper())]}


# ----------------------------------------------------------- Transferências
@app.post("/transferencias", tags=["transações"])
def transferir(pedido: PedidoTransferencia,
               falha: Literal["participante_indisponivel_fase1", "atraso_fase2",
                              "crash_coordenador_fase2", "crash_permanente_coordenador"] | None = Query(
                   default=None, description="Injeção de falha (apenas para demonstração)"),
               u: Utilizador = Depends(utilizador_atual)):
    hash_ok = verificar_integridade(pedido.campos(), pedido.hash_sha256)
    verificar_frescura(pedido.timestamp)
    if falha and not config.PERMITIR_INJECAO_FALHAS:
        raise HTTPException(403, "Injeção de falhas desativada")
    resultado = coordenador.transferir(pedido.campos(), u, hash_ok, falha)
    resultado["integridade"] = {"algoritmo": "SHA-256", "hash_verificado": hash_ok}
    return resultado


@app.get("/transferencias/{tx_id}", tags=["transações"])
def estado_transferencia(tx_id: UUID, u: Utilizador = Depends(utilizador_atual)):
    """Estado global de uma transação em todos os fragmentos."""
    movs, falhas = scatter_gather(
        "SELECT conta_id, tipo, valor, moeda, valor_contraparte, moeda_contraparte, taxa_aplicada, "
        "protocolo, no_coordenador, hash_sha256, criado_em FROM movimentos WHERE tx_id = %s",
        (str(tx_id),))
    preparadas, _ = scatter_gather(
        "SELECT gid, prepared FROM pg_prepared_xacts")
    em_duvida = [p for p in preparadas if str(tx_id) in _decodificar_gid(p["gid"])]
    return {"tx_id": str(tx_id),
            "movimentos": [{k: para_json(v) for k, v in m.items()} for m in movs],
            "transacoes_preparadas_em_duvida": [{k: para_json(v) for k, v in p.items()}
                                                for p in em_duvida],
            "fragmentos_indisponiveis": falhas}


def _decodificar_gid(gid: str) -> str:
    import base64
    try:
        _, g, _ = gid.split("_")
        return base64.b64decode(g).decode()
    except Exception:
        return gid


# --------------------------------------------- Interoperabilidade (Saga)
@app.post("/interbancario/ordens", tags=["interoperabilidade"])
def ordem_interbancaria(pedido: PedidoOrdemExterna,
                        falha: Literal["legado_indisponivel"] | None = Query(default=None),
                        u: Utilizador = Depends(utilizador_atual)):
    hash_ok = verificar_integridade(pedido.campos(), pedido.hash_sha256)
    verificar_frescura(pedido.timestamp)
    return coordenador.ordem_interbancaria(pedido.campos(), u, hash_ok, falha)


# ------------------------------------------------- Taxas de câmbio (replicadas)
class NovaTaxa(BaseModel):
    mzn_por_unidade: str = Field(pattern=r"^\d{1,9}(\.\d{1,6})?$", examples=["70.25"])


@app.get("/taxas", tags=["câmbio"])
def ver_taxas(u: Utilizador = Depends(utilizador_atual)):
    """Taxas de câmbio lidas em TODAS as réplicas (tabela totalmente replicada)."""
    return coordenador.taxas()


@app.put("/admin/taxas/{moeda}", tags=["câmbio"])
def atualizar_taxa(moeda: str, nova: NovaTaxa, u: Utilizador = Depends(utilizador_atual)):
    """Atualiza a taxa em todas as réplicas de forma atómica (Two-Phase Commit)."""
    exigir_admin(u)
    from decimal import Decimal
    return coordenador.atualizar_taxa(moeda, Decimal(nova.mzn_por_unidade), u)


@app.get("/admin/totais", tags=["admin"])
def totais(u: Utilizador = Depends(utilizador_atual)):
    """Soma dos saldos por moeda e o equivalente total em meticais."""
    exigir_admin(u)
    return coordenador.totais()


# --------------------------------------------------------- DFS: relatórios
@app.post("/relatorios", tags=["dfs"])
def gerar_relatorio(u: Utilizador = Depends(utilizador_atual)):
    exigir_admin(u)
    linhas, falhas = scatter_gather("SELECT id, titular, titular_cifrado, regiao, moeda, saldo "
                                    "FROM contas ORDER BY id")
    com_titular(linhas)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["conta", "titular", "regiao", "moeda", "saldo", "fragmento"])
    for l in linhas:
        w.writerow([l["id"], l["titular"], l["regiao"], l["moeda"], l["saldo"], l["fragmento"]])
    dados = buf.getvalue().encode()
    nome = f"saldos-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{config.NODE_ID}.csv"
    dfs.mkdirs("/relatorios")
    dfs.escrever(f"/relatorios/{nome}", dados)
    dfs.escrever(f"/relatorios/{nome}.sha256", sha256_hex(dados).encode())
    auditoria.registar("RELATORIO_GERADO", ficheiro=nome, sha256=sha256_hex(dados))
    return {"ficheiro": f"/relatorios/{nome}", "gerado_por": config.NODE_ID,
            "sha256": sha256_hex(dados), "fragmentos_indisponiveis": falhas}


@app.get("/relatorios", tags=["dfs"])
def listar_relatorios(u: Utilizador = Depends(utilizador_atual)):
    return {"lido_por": config.NODE_ID, "ficheiros": dfs.listar("/relatorios")}


@app.get("/relatorios/{nome}", tags=["dfs"], response_class=PlainTextResponse)
def ler_relatorio(nome: str, u: Utilizador = Depends(utilizador_atual)):
    if "/" in nome or ".." in nome:
        raise HTTPException(400, "Nome inválido")
    try:
        dados = dfs.ler(f"/relatorios/{nome}")
    except FileNotFoundError:
        raise HTTPException(404, "Relatório não encontrado")
    return PlainTextResponse(dados.decode(), headers={"X-SHA256": sha256_hex(dados)})


# --------------------------------------------------------- DFS: auditoria
@app.get("/auditoria", tags=["dfs"])
def listar_auditoria(u: Utilizador = Depends(utilizador_atual)):
    exigir_admin(u)
    nos = dfs.listar("/auditoria")
    return {"ficheiro_atual_deste_no": auditoria.caminho,
            "nos": {n["nome"]: dfs.listar(f"/auditoria/{n['nome']}") for n in nos}}


@app.get("/auditoria/verificar", tags=["dfs"])
def verificar_auditoria(ficheiro: str, u: Utilizador = Depends(utilizador_atual)):
    """Verifica a cadeia de hashes SHA-256 de um ficheiro de auditoria."""
    exigir_admin(u)
    if not ficheiro.startswith("/auditoria/") or ".." in ficheiro:
        raise HTTPException(400, "Caminho inválido")
    try:
        conteudo = dfs.ler(ficheiro)
    except FileNotFoundError:
        raise HTTPException(404, "Ficheiro não encontrado")
    return {"ficheiro": ficheiro, "verificado_por": config.NODE_ID, **verificar_cadeia(conteudo)}


# ------------------------------------------------------------- Admin 2PC
@app.get("/admin/2pc", tags=["admin"])
def estado_2pc(u: Utilizador = Depends(utilizador_atual)):
    exigir_admin(u)
    estados = coordenador.wal.estados()
    nao_concluidas = {k: v for k, v in estados.items() if v["estado"] != "CONCLUIDO"}
    preparadas, falhas = scatter_gather("SELECT gid, prepared, database FROM pg_prepared_xacts")
    return {"coordenador": config.NODE_ID, "total_registadas": len(estados),
            "nao_concluidas": nao_concluidas,
            "transacoes_preparadas_nos_participantes": [
                {**{k: para_json(v) for k, v in p.items()}, "gtrid": _decodificar_gid(p["gid"])}
                for p in preparadas],
            "fragmentos_indisponiveis": falhas}


@app.post("/admin/recuperar", tags=["admin"])
def forcar_recuperacao(u: Utilizador = Depends(utilizador_atual)):
    exigir_admin(u)
    return {"no": config.NODE_ID, "acoes": coordenador.recuperar()}
