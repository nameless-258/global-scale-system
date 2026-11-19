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
    def uma(i):
        o, d = ("EU-1001", "AM-2001") if i % 2 else ("AM-2001", "EU-1001")
        return http.post("/transferencias", json=tx(o, d, "1"), headers=ADM).status_code
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
              t_2pc, t_conservacao, t_saldo, t_fase1, t_autorizacao, t_adulterada, t_replay,
              t_expirada, t_taxas, t_taxa_update, t_saga_ok, t_saga_comp, t_hdfs, t_auditoria,
              t_concorrencia, t_replicas]:
        t()
    ok = sum(1 for _, p, _ in resultados if p)
    cor = VERDE if ok == len(resultados) else VERM
    print(f"\n{cor}Resultado: {ok}/{len(resultados)} testes passaram{FIM}\n")
    sys.exit(0 if ok == len(resultados) else 1)
