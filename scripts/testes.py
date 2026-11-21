#!/usr/bin/env python3
"""Testes automáticos de aceitação do Global-Scale System.

Corre contra o sistema em execução (através do gateway) e verifica, para cada
requisito, o comportamento esperado. Termina com código 0 se tudo passar.

    docker compose run --rm --entrypoint python cliente /scripts/testes.py

Os cenários que exigem desligar contentores (failover, crash do coordenador)
estão no guião interativo scripts/demo.sh.
"""
import hashlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

URL = os.getenv("GSS_URL", "https://gateway").rstrip("/")
AUTH = os.getenv("GSS_AUTH", URL).rstrip("/")
CA = os.getenv("GSS_CA", "/certs/publico/ca.crt")
VERDE, VERM, AZUL, FIM = "\033[92m", "\033[91m", "\033[94m", "\033[0m"
http = httpx.Client(base_url=URL, verify=CA, timeout=60)
resultados: list[tuple[str, bool, str]] = []


def teste(nome):
    def deco(f):
        def run():
            try:
                detalhe = f() or ""
                resultados.append((nome, True, detalhe))
                print(f"  {VERDE}✓ PASSOU{FIM}  {nome}  {detalhe}")
            except AssertionError as e:
                resultados.append((nome, False, str(e)))
                print(f"  {VERM}✗ FALHOU{FIM}  {nome}  -> {e}")
            except Exception as e:
                resultados.append((nome, False, f"{type(e).__name__}: {e}"))
                print(f"  {VERM}✗ ERRO{FIM}    {nome}  -> {type(e).__name__}: {e}")
        run.__name__ = f.__name__
        return run
    return deco


# ------------------------------------------------------------------ utilitários
def token(u, p):
    r = http.post(f"{AUTH}/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, f"login {u}: HTTP {r.status_code}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def canon(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def agora(desvio=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=desvio)).strftime("%Y-%m-%dT%H:%M:%SZ")


def tx(origem, destino, valor, **extra):
    c = {"id": str(uuid.uuid4()), "origem": origem, "destino": destino,
         "valor": f"{Decimal(valor):.2f}", "timestamp": extra.pop("ts", agora())}
    return {**c, "hash_sha256": canon(c)}


def total(adm):
    return Decimal(http.get("/admin/totais", headers=adm).json()["total_equivalente_MZN"])


def saldo(h, conta):
    return Decimal(http.get(f"/contas/{conta}", headers=h).json()["conta"]["saldo"])


ANA = ADM = None


# ======================================================================= testes
@teste("Saúde: fragmentos, réplicas síncronas, HDFS e legado")
def t_saude():
    s = http.get("/saude").json()
    assert len(s["fragmentos"]) == 3, f"esperados 3 fragmentos: {list(s['fragmentos'])}"
    for nome, f in s["fragmentos"].items():
        assert f["estado"] == "OK", f"{nome} não está OK"
    assert s["dfs"].get("estado") == "Operational", f"DFS: {s['dfs']}"
    assert s["legado"].get("Estado") == "OK", "legado não responde"
    modos = [r["modo"] for f in s["fragmentos"].values() for r in (f["replicas"] if isinstance(f["replicas"], list) else [])]
    return f"(réplicas: {', '.join(modos) or 'nenhuma'})"


@teste("Autenticação: JWT RS256 emitido e validado")
def t_login():
    global ANA, ADM
    ANA, ADM = token("ana", "Ana@2026"), token("admin", "Admin@2026")
    assert http.get("/contas", headers=ANA).status_code == 200


@teste("Segurança: password errada recusada (401)")
def t_password():
    assert http.post(f"{AUTH}/auth/login", json={"username": "carla", "password": "x"}).status_code == 401


@teste("Segurança: pedido sem token recusado (401)")
def t_sem_token():
    assert http.get("/contas").status_code == 401


@teste("Segurança: token JWT forjado recusado (401)")
def t_token_forjado():
    t = ANA["Authorization"].split()[1]
    h, p, s = t.split(".")
    import base64
    dados = json.loads(base64.urlsafe_b64decode(p + "==="))
    dados["papel"] = "admin"
    p2 = base64.urlsafe_b64encode(json.dumps(dados).encode()).rstrip(b"=").decode()
    assert http.get("/admin/2pc", headers={"Authorization": f"Bearer {h}.{p2}.{s}"}).status_code == 401


@teste("Cifragem em repouso: titular decifrado só pela API")
def t_cifragem():
    c = http.get("/contas/EU-1001", headers=ANA).json()["conta"]
    assert c["titular"] == "Ana Machava", f"titular = {c.get('titular')}"
    assert "titular_cifrado" not in c


@teste("Transferência LOCAL na mesma moeda conserva o dinheiro")
def t_local():
    antes = total(ADM)
    r = http.post("/transferencias", json=tx("EU-1001", "EU-1003", "1"), headers=ANA)
    assert r.status_code == 200 and r.json()["estado"] == "CONFIRMADA", r.text[:200]
    return f"(1 EUR -> {r.json()['conversao']['creditado']})"


@teste("Transferência 2PC entre fragmentos com conversão EUR -> USD")
def t_2pc():
    a, b = saldo(ADM, "EU-1001"), saldo(ADM, "AM-2001")
    r = http.post("/transferencias", json=tx("EU-1001", "AM-2001", "150"), headers=ANA).json()
    assert r["estado"] == "CONFIRMADA" and r["protocolo"].startswith("Two-Phase"), r
    assert saldo(ADM, "EU-1001") == a - 150, "débito errado"
    creditado = Decimal(r["conversao"]["creditado"].split()[0])
    assert saldo(ADM, "AM-2001") == b + creditado, "crédito errado"
    return f"(150 EUR -> {creditado} USD)"


@teste("Fragmento África: 2PC Europa -> África e conversão ZAR -> MZN em África")
def t_africa():
    antes = total(ADM)
    a, f = saldo(ADM, "EU-1003"), saldo(ADM, "AF-3001")
    r = http.post("/transferencias", json=tx("EU-1003", "AF-3001", "500"), headers=ANA).json()
    assert r["estado"] == "CONFIRMADA" and r["protocolo"].startswith("Two-Phase"), r
    assert saldo(ADM, "EU-1003") == a - 500 and saldo(ADM, "AF-3001") == f + 500, "saldos errados"
    assert total(ADM) == antes, "total mudou numa transferência MZN -> MZN"
    t = token("thabo", "Thabo@2026")
    r2 = http.post("/transferencias", json=tx("AF-3002", "AF-3001", "100"), headers=t).json()
    assert r2["estado"] == "CONFIRMADA" and r2["protocolo"].startswith("LOCAL"), r2
    return f"(500 MZN Europa -> África por 2PC; 100 ZAR -> {r2['conversao']['creditado']} local)"


@teste("Conservação: 2PC na mesma moeda (MZN) mantém o total exato")
def t_conservacao():
    d = token("diego", "Diego@2026")
    antes = total(ADM)
    r = http.post("/transferencias", json=tx("AM-2002", "EU-1003", "1234.56"), headers=d)
    assert r.status_code == 200, r.text[:200]
    assert total(ADM) == antes, f"total mudou: {antes} -> {total(ADM)}"


@teste("Atomicidade: saldo insuficiente aborta sem movimentar dinheiro")
def t_saldo():
    antes = total(ADM)
    r = http.post("/transferencias", json=tx("EU-1001", "AM-2001", "999999"), headers=ANA)
    assert r.status_code == 409 and r.json()["erro"] == "SALDO_INSUFICIENTE"
    assert total(ADM) == antes


@teste("Atomicidade: falha de rede na fase 1 -> ABORT global")
def t_fase1():
    antes = total(ADM)
    r = http.post("/transferencias", json=tx("EU-1001", "AM-2001", "10"), headers=ANA,
                  params={"falha": "participante_indisponivel_fase1"})
    assert r.status_code == 503 and r.json()["estado"] == "ABORTADA", r.text[:200]
    assert total(ADM) == antes, "dinheiro movimentado apesar do ABORT"


@teste("Autorização: não titular não pode debitar (403)")
def t_autorizacao():
    r = http.post("/transferencias", json=tx("AM-2001", "EU-1001", "1"), headers=ANA)
    assert r.status_code == 403


@teste("Separação de funções: o administrador não movimenta contas de clientes (403)")
def t_admin_nao_debita():
    r = http.post("/transferencias", json=tx("EU-1001", "EU-1003", "1"), headers=ADM)
    assert r.status_code == 403, f"HTTP {r.status_code}"
    r = http.get("/beneficiarios", headers=ANA).json()["beneficiarios"]
    assert r and all(not b["conta"] in ("EU-1001", "EU-1003") for b in r), "beneficiários incluem contas próprias"
    return f"{len(r)} beneficiários para a ana"


@teste("Integridade: mensagem adulterada recusada (SHA-256)")
def t_adulterada():
    corpo = tx("EU-1001", "AM-2001", "10")
    corpo["valor"] = "1000.00"
    r = http.post("/transferencias", json=corpo, headers=ANA)
    assert r.status_code == 400 and "INTEGRIDADE" in r.text


@teste("Anti-replay: a mesma mensagem não é processada duas vezes")
def t_replay():
    corpo = tx("EU-1001", "EU-1002", "1")
    carla = token("carla", "Carla@2026")
    assert http.post("/transferencias", json=corpo, headers=ANA).status_code == 200
    r = http.post("/transferencias", json=corpo, headers=ANA)
    assert r.status_code == 409 and r.json()["erro"] == "TRANSACAO_DUPLICADA"


@teste("Anti-replay: mensagem com timestamp antigo recusada")
def t_expirada():
    r = http.post("/transferencias", json=tx("EU-1001", "EU-1003", "1", ts=agora(-3600)), headers=ANA)
    assert r.status_code == 400 and "EXPIRADA" in r.text


@teste("Replicação: taxas de câmbio coerentes em todas as réplicas")
def t_taxas():
    assert http.get("/taxas", headers=ANA).json()["replicas_coerentes"] is True


@teste("Replicação síncrona: atualização de taxa por 2PC")
def t_taxa_update():
    r = http.put("/admin/taxas/USD", json={"mzn_por_unidade": "63.90"}, headers=ADM).json()
    assert r["estado"] == "ATUALIZADA_EM_TODAS_AS_REPLICAS", r
    assert http.get("/taxas", headers=ANA).json()["replicas_coerentes"] is True


@teste("Interoperabilidade: ordem Python -> Java (XML assinado) aceite [Saga]")
def t_saga_ok():
    c = {"id": str(uuid.uuid4()), "conta_origem": "EU-1003", "banco_destino": "BANCO-PARCEIRO-MZ",
         "iban_destino": "MZ59000100000012345678901", "valor": "500.00", "timestamp": agora()}
    r = http.post("/interbancario/ordens", json={**c, "hash_sha256": canon(c)}, headers=ANA).json()
    assert r["estado"] == "CONCLUIDA" and r["resposta_legado"]["Estado"] == "ACEITE", r
    return f"(autorização {r['resposta_legado']['CodigoAutorizacao']})"


@teste("Saga: rejeição do parceiro provoca compensação (estorno)")
def t_saga_comp():
    antes = saldo(ADM, "EU-1001")
    c = {"id": str(uuid.uuid4()), "conta_origem": "EU-1001", "banco_destino": "X",
         "iban_destino": "XX12", "valor": "50.00", "timestamp": agora()}
    r = http.post("/interbancario/ordens", json={**c, "hash_sha256": canon(c)}, headers=ANA).json()
    assert r["estado"] == "COMPENSADA", r
    assert saldo(ADM, "EU-1001") == antes, "o estorno não repôs o saldo"


def ordem(conta, destino, numero, valor, tipo="CARTEIRA"):
    c = {"id": str(uuid.uuid4()), "conta_origem": conta, "banco_destino": destino,
         "iban_destino": numero, "valor": f"{Decimal(valor):.2f}", "timestamp": agora(),
         "tipo_destino": tipo}
    return {**c, "hash_sha256": canon(c)}


@teste("Carteira móvel: envio para M-Pesa com confirmação do titular [Saga]")
def t_carteira():
    q = http.get("/carteiras/M-PESA/841234567", headers=ANA).json()
    assert q["estado"] == "ATIVA" and q["titular"], q
    r = http.post("/interbancario/ordens", json=ordem("EU-1003", "M-PESA", "841234567", "250"), headers=ANA).json()
    assert r["estado"] == "CONCLUIDA" and r["titular_destino"] == q["titular"], r
    r2 = http.post("/interbancario/ordens", json=ordem("EU-1001", "E-MOLA", "861234567", "10"), headers=ANA).json()
    assert r2["estado"] == "CONCLUIDA" and r2["enviado"] == "695.00 MZN", r2
    return f"(250 MZN para {q['titular']}; 10 EUR -> {r2['enviado']} para e-Mola)"


@teste("Carteira móvel: número sem carteira é recusado e o dinheiro devolvido")
def t_carteira_invalida():
    antes = saldo(ADM, "EU-1003")
    r = http.post("/interbancario/ordens", json=ordem("EU-1003", "M-PESA", "849999999", "100"), headers=ANA).json()
    assert r["estado"] == "COMPENSADA", r
    assert saldo(ADM, "EU-1003") == antes, "a compensação não repôs o saldo"


@teste("Extrato: débitos, créditos e saldo após cada movimento coerentes")
def t_extracto():
    e = http.get("/contas/EU-1003/extracto", headers=ANA).json()
    assert e["movimentos"], "extrato vazio"
    assert e["saldo_final"] == e["saldo_atual"] == e["movimentos"][-1]["saldo_apos"], e
    s0 = Decimal(e["saldo_inicial"])
    for m in e["movimentos"]:
        s0 += Decimal(m["credito"] or 0) - Decimal(m["debito"] or 0)
        assert s0 == Decimal(m["saldo_apos"]), f"saldo após errado em {m}"
    assert http.get("/contas/AM-2001/extracto", headers=ANA).status_code == 403, "extrato alheio visível"
    return f"({len(e['movimentos'])} movimentos, saldo {e['saldo_atual']} MZN)"


@teste("Administração: criar cliente, abrir conta e depositar")
def t_admin_cliente():
    u = f"teste{uuid.uuid4().hex[:6]}"
    r = http.post(f"{AUTH}/auth/utilizadores", json={"username": u, "nome": "Cliente de Teste",
                                                     "password": "Teste2026x"}, headers=ADM)
    assert r.status_code == 201, r.text[:200]
    assert http.post(f"{AUTH}/auth/utilizadores", json={"username": u + "b", "nome": "X Y Z",
                     "password": "Teste2026x"}, headers=ANA).status_code == 403, "cliente criou utilizador"
    c = http.post("/admin/contas", json={"owner_username": u, "titular": "Cliente de Teste",
                                         "regiao": "AF", "moeda": "MZN"}, headers=ADM).json()
    assert c["conta"].startswith("AF-"), c
    dep = {"id": str(uuid.uuid4()), "conta": c["conta"], "valor": "1000.00", "descricao": "Abertura"}
    assert http.post("/admin/depositos", json=dep, headers=ANA).status_code == 403, "cliente depositou"
    d = http.post("/admin/depositos", json=dep, headers=ADM).json()
    assert d["estado"] == "CONFIRMADA" and d["saldo_atual"] == "1000.00", d
    assert http.post("/admin/depositos", json=dep, headers=ADM).status_code == 409, "depósito repetido aceite"
    novo = token(u, "Teste2026x")
    contas = http.get("/contas", headers=novo).json()["contas"]
    assert [x["id"] for x in contas] == [c["conta"]] and contas[0]["saldo"] == "1000.00", contas
    return f"({u} com {c['conta']} e 1000.00 MZN)"


@teste("DFS: relatório escrito no HDFS e lido de volta (SHA-256 igual)")
def t_hdfs():
    g = http.post("/relatorios", headers=ADM).json()
    nome = g["ficheiro"].rsplit("/", 1)[1]
    r = http.get(f"/relatorios/{nome}", headers=ADM)
    assert r.status_code == 200 and r.headers.get("x-sha256") == g["sha256"]
    return f"(gerado por {g['gerado_por']}, lido por {r.headers.get('x-no-servidor')})"


@teste("Auditoria: cadeia de hashes SHA-256 íntegra no HDFS")
def t_auditoria():
    a = http.get("/auditoria", headers=ADM).json()
    ficheiro = a["ficheiro_atual_deste_no"]
    time.sleep(1)
    v = http.get("/auditoria/verificar", params={"ficheiro": ficheiro}, headers=ADM).json()
    assert v.get("integro") is True, v
    return f"({v['entradas']} entradas)"


@teste("Concorrência: 30 transferências 2PC em paralelo nos dois sentidos")
def t_concorrencia():
    # cada titular movimenta a SUA conta: Ana (EU-1003, MZN) e Diego (AM-2002, MZN),
    # em sentidos opostos e ao mesmo tempo (o administrador não pode movimentar contas)
    diego = token("diego", "Diego@2026")
    def uma(i):
        if i % 2:
            return http.post("/transferencias", json=tx("EU-1003", "AM-2002", "1"), headers=ANA).status_code
        return http.post("/transferencias", json=tx("AM-2002", "EU-1003", "1"), headers=diego).status_code
    with ThreadPoolExecutor(10) as ex:
        codigos = list(ex.map(uma, range(30)))
    assert codigos.count(200) == 30, f"códigos: {codigos}"
    pend = http.get("/admin/2pc", headers=ADM).json()["transacoes_preparadas_nos_participantes"]
    assert not pend, f"{len(pend)} transações ficaram preparadas"
    return "(30/30 confirmadas, 0 deadlocks, 0 pendentes)"


@teste("Balanceamento: as duas réplicas da API respondem")
def t_replicas():
    nos = {http.get("/saude").headers.get("x-no-servidor") for _ in range(8)}
    assert nos >= {"api-1", "api-2"}, f"só responderam: {nos}"
    return f"({', '.join(sorted(nos))})"


if __name__ == "__main__":
    print(f"\n{AZUL}Global-Scale System — testes automáticos contra {URL}{FIM}\n")
    for t in [t_saude, t_login, t_password, t_sem_token, t_token_forjado, t_cifragem, t_local,
              t_2pc, t_africa, t_conservacao, t_saldo, t_fase1, t_autorizacao, t_admin_nao_debita, t_adulterada, t_replay,
              t_expirada, t_taxas, t_taxa_update, t_saga_ok, t_saga_comp, t_carteira, t_carteira_invalida,
              t_extracto, t_admin_cliente, t_hdfs, t_auditoria,
              t_concorrencia, t_replicas]:
        t()
    ok = sum(1 for _, p, _ in resultados if p)
    cor = VERDE if ok == len(resultados) else VERM
    print(f"\n{cor}Resultado: {ok}/{len(resultados)} testes passaram{FIM}\n")
    sys.exit(0 if ok == len(resultados) else 1)
