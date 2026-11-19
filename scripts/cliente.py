#!/usr/bin/env python3
"""Cliente de linha de comandos do Global-Scale System (para testes e demonstração).

Calcula o hash SHA-256 de cada transação, gere o token JWT e mostra que réplica
respondeu (cabeçalho X-No-Servidor).

Variáveis de ambiente:
  GSS_URL   URL do gateway   (por omissão https://localhost:8443)
  GSS_AUTH  URL da autenticação (por omissão = GSS_URL)
  GSS_CA    certificado da CA (por omissão ./certs/ca.crt)

Exemplos:
  python scripts/cliente.py login ana Ana@2026
  python scripts/cliente.py contas
  python scripts/cliente.py transferir EU-1001 AM-2001 150.00      (150 EUR -> convertido para USD)
  python scripts/cliente.py transferir EU-1001 AM-2001 50 --falha participante_indisponivel_fase1
  python scripts/cliente.py transferir EU-1001 AM-2001 50 --adulterar
  python scripts/cliente.py replay EU-1001 AM-2001 10
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

URL = os.getenv("GSS_URL", "https://localhost:8443").rstrip("/")
AUTH = os.getenv("GSS_AUTH", URL).rstrip("/")
CA = os.getenv("GSS_CA", str(Path(__file__).resolve().parent.parent / "certs" / "publico" / "ca.crt"))
TOKEN_FILE = Path(os.getenv("GSS_TOKEN_FILE", Path.home() / ".gss_token"))

AZUL, VERDE, VERM, AMAR, FIM = "\033[94m", "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def http() -> httpx.Client:
    return httpx.Client(verify=CA, timeout=60)


def token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit("Faça login primeiro: cliente.py login <utilizador> <password>")
    return TOKEN_FILE.read_text().strip()


def cab() -> dict:
    return {"Authorization": f"Bearer {token()}"}


def mostrar(r: httpx.Response):
    cor = VERDE if r.status_code < 300 else (AMAR if r.status_code < 500 else VERM)
    print(f"{cor}HTTP {r.status_code}{FIM}  respondido por: {AZUL}"
          f"{r.headers.get('x-no-servidor', '?')}{FIM}")
    try:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    except Exception:
        print(r.text)


def sha256_canonico(campos: dict) -> str:
    return hashlib.sha256(json.dumps(campos, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def corpo_transferencia(origem, destino, valor, tx_id=None):
    campos = {"id": tx_id or str(uuid.uuid4()), "origem": origem, "destino": destino,
              "valor": f"{float(valor):.2f}", "timestamp": agora()}
    return {**campos, "hash_sha256": sha256_canonico(campos)}


# ------------------------------------------------------------------ comandos
def c_login(a):
    with http() as h:
        r = h.post(f"{AUTH}/auth/login", json={"username": a.utilizador, "password": a.password})
    if r.status_code == 200:
        TOKEN_FILE.write_text(r.json()["access_token"])
        t = r.json()["access_token"]
        cabecalho, carga, _ = t.split(".")
        dec = lambda s: json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
        print(f"{VERDE}Login OK{FIM} — token guardado em {TOKEN_FILE}")
        print("Cabeçalho JWT:", json.dumps(dec(cabecalho)))
        print("Claims JWT   :", json.dumps(dec(carga), ensure_ascii=False))
    else:
        mostrar(r)


def c_get(caminho):
    def f(a):
        with http() as h:
            mostrar(h.get(f"{URL}{caminho.format(**vars(a))}", headers=cab()))
    return f


def c_post(caminho):
    def f(a):
        with http() as h:
            mostrar(h.post(f"{URL}{caminho}", headers=cab()))
    return f


def c_transferir(a):
    corpo = corpo_transferencia(a.origem, a.destino, a.valor)
    print(f"{AZUL}Hash SHA-256 calculado pelo cliente:{FIM} {corpo['hash_sha256']}")
    if a.adulterar:
        corpo["valor"] = f"{float(a.valor) * 100:.2f}"
        print(f"{VERM}⚡ Ataque 'man-in-the-middle' simulado: valor alterado para "
              f"{corpo['valor']} DEPOIS de calculado o hash{FIM}")
    params = {"falha": a.falha} if a.falha else {}
    with http() as h:
        mostrar(h.post(f"{URL}/transferencias", json=corpo, params=params, headers=cab()))
    print(f"tx_id = {corpo['id']}")


def c_replay(a):
    corpo = corpo_transferencia(a.origem, a.destino, a.valor)
    with http() as h:
        print(f"{AZUL}1.º envio (legítimo):{FIM}")
        mostrar(h.post(f"{URL}/transferencias", json=corpo, headers=cab()))
        print(f"\n{VERM}2.º envio — a MESMA mensagem capturada e reenviada (ataque de replay):{FIM}")
        mostrar(h.post(f"{URL}/transferencias", json=corpo, headers=cab()))


def c_token_falso(a):
    t = token()
    cabecalho, carga, assinatura = t.split(".")
    dados = json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))
    dados["papel"] = "admin"
    nova = base64.urlsafe_b64encode(json.dumps(dados).encode()).rstrip(b"=").decode()
    print(f"{VERM}⚡ Token adulterado: papel alterado para 'admin' sem a chave privada{FIM}")
    with http() as h:
        mostrar(h.get(f"{URL}/admin/2pc",
                      headers={"Authorization": f"Bearer {cabecalho}.{nova}.{assinatura}"}))


def c_sem_token(a):
    with http() as h:
        mostrar(h.get(f"{URL}/contas"))


def c_ordem(a):
    campos = {"id": str(uuid.uuid4()), "conta_origem": a.conta, "banco_destino": a.banco,
              "iban_destino": a.iban, "valor": f"{float(a.valor):.2f}",
              "timestamp": agora()}
    corpo = {**campos, "hash_sha256": sha256_canonico(campos)}
    params = {"falha": a.falha} if a.falha else {}
    with http() as h:
        mostrar(h.post(f"{URL}/interbancario/ordens", json=corpo, params=params, headers=cab()))


def c_taxa_atualizar(a):
    with http() as h:
        mostrar(h.put(f"{URL}/admin/taxas/{a.moeda}", json={"mzn_por_unidade": a.valor},
                      headers=cab()))


def c_saude(a):
    with http() as h:
        mostrar(h.get(f"{URL}/saude"))


def c_ler_relatorio(a):
    with http() as h:
        r = h.get(f"{URL}/relatorios/{a.nome}", headers=cab())
        print(f"HTTP {r.status_code} — lido por {r.headers.get('x-no-servidor')} — "
              f"SHA-256 {r.headers.get('x-sha256')}\n{r.text}")


def c_carga(a):
    """Teste de concorrência: N transferências cruzadas em paralelo (threads)."""
    from concurrent.futures import ThreadPoolExecutor
    pares = [("EU-1001", "AM-2001"), ("AM-2001", "EU-1001")] if a.admin_mode else \
        [("EU-1001", "AM-2001")]
    def uma(i):
        o, d = pares[i % len(pares)]
        with http() as h:
            r = h.post(f"{URL}/transferencias", json=corpo_transferencia(o, d, a.valor),
                       headers=cab())
            return r.status_code, r.headers.get("x-no-servidor"), r.json().get("estado") or \
                r.json().get("erro")
    with ThreadPoolExecutor(max_workers=a.paralelo) as ex:
        res = list(ex.map(uma, range(a.n)))
    from collections import Counter
    print("Resultados (status, réplica, estado):")
    for k, v in Counter(res).most_common():
        print(f"  {v:3d} x {k}")


def main():
    p = argparse.ArgumentParser(description="Cliente do Global-Scale System")
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("login"); x.add_argument("utilizador"); x.add_argument("password"); x.set_defaults(f=c_login)
    s.add_parser("saude").set_defaults(f=c_saude)
    s.add_parser("contas").set_defaults(f=c_get("/contas"))
    x = s.add_parser("conta"); x.add_argument("conta_id"); x.set_defaults(f=c_get("/contas/{conta_id}"))
    x = s.add_parser("transferir"); x.add_argument("origem"); x.add_argument("destino"); x.add_argument("valor")
    x.add_argument("--falha", choices=["participante_indisponivel_fase1", "atraso_fase2",
                                       "crash_coordenador_fase2", "crash_permanente_coordenador"])
    x.add_argument("--adulterar", action="store_true", help="altera o valor depois do hash")
    x.set_defaults(f=c_transferir)
    x = s.add_parser("estado"); x.add_argument("tx_id"); x.set_defaults(f=c_get("/transferencias/{tx_id}"))
    x = s.add_parser("replay"); x.add_argument("origem"); x.add_argument("destino"); x.add_argument("valor"); x.set_defaults(f=c_replay)
    x = s.add_parser("ordem", help="ordem interbancária para o legado (Saga)")
    x.add_argument("conta"); x.add_argument("iban"); x.add_argument("valor")
    x.add_argument("--banco", default="BANCO-PARCEIRO-MZ")
    x.add_argument("--falha", choices=["legado_indisponivel"]); x.set_defaults(f=c_ordem)
    s.add_parser("token-falso").set_defaults(f=c_token_falso)
    s.add_parser("sem-token").set_defaults(f=c_sem_token)
    s.add_parser("relatorio-gerar").set_defaults(f=c_post("/relatorios"))
    s.add_parser("relatorios").set_defaults(f=c_get("/relatorios"))
    x = s.add_parser("relatorio-ler"); x.add_argument("nome"); x.set_defaults(f=c_ler_relatorio)
    s.add_parser("auditoria").set_defaults(f=c_get("/auditoria"))
    x = s.add_parser("auditoria-verificar"); x.add_argument("ficheiro")
    x.set_defaults(f=c_get("/auditoria/verificar?ficheiro={ficheiro}"))
    s.add_parser("admin-2pc").set_defaults(f=c_get("/admin/2pc"))
    s.add_parser("taxas", help="taxas de câmbio em todas as réplicas").set_defaults(f=c_get("/taxas"))
    x = s.add_parser("taxa-atualizar", help="atualiza uma taxa em todas as réplicas (2PC)")
    x.add_argument("moeda"); x.add_argument("valor", help="meticais por 1 unidade da moeda")
    x.set_defaults(f=c_taxa_atualizar)
    s.add_parser("totais", help="saldos por moeda e total em MZN (admin)").set_defaults(f=c_get("/admin/totais"))
    s.add_parser("recuperar").set_defaults(f=c_post("/admin/recuperar"))
    x = s.add_parser("carga", help="teste de concorrência")
    x.add_argument("--n", type=int, default=20); x.add_argument("--paralelo", type=int, default=10)
    x.add_argument("--valor", default="1.00"); x.add_argument("--admin-mode", action="store_true",
                                                             help="transferências nos dois sentidos (requer admin)")
    x.set_defaults(f=c_carga)
    a = p.parse_args()
    try:
        a.f(a)
    except httpx.RemoteProtocolError:
        print(f"{VERM}O servidor fechou a ligação sem responder (o nó morreu a meio do pedido).{FIM}")
    except httpx.ConnectError as e:
        print(f"{VERM}Não foi possível ligar a {URL}: {e}{FIM}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
