"""
Serviço de CONTROLO DE FALHAS (só para a demonstração laboratorial).

Permite à interface web "derrubar" e religar nós reais (contentores Docker)
para simular falhas de servidores, tal como `docker compose stop <nó>`.

Segurança — este serviço fala com o Docker Engine (/var/run/docker.sock), o que
equivale a ter controlo do anfitrião. Por isso:
  * só aceita pedidos com um JWT RS256 válido de um ADMINISTRADOR
    (validado localmente com a chave pública, como nas réplicas da API);
  * só atua sobre uma LISTA FECHADA de serviços deste projeto Docker Compose
    e só com 4 ações (parar, ligar, reiniciar, matar);
  * não permite criar, apagar ou executar comandos em contentores;
  * corre em HTTPS e só é alcançável através do gateway;
  * cada ação fica registada no log do serviço.
Num sistema real este serviço não existiria em produção.
"""
import json
import logging
import os
import re
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query

PROJETO = os.getenv("COMPOSE_PROJECT", "global-scale-system")
SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
CHAVE_PUBLICA = Path("/certs/publico/jwt-public.pem").read_text()
ISSUER = os.getenv("JWT_ISSUER", "global-scale-auth")
AUDIENCE = os.getenv("JWT_AUDIENCE", "global-scale-api")

# Nós que podem ser derrubados (o gateway e o próprio controlo ficam de fora,
# senão a interface deixaria de funcionar).
SERVICOS = ["db-europa", "db-europa-r", "db-america", "db-america-r", "db-africa", "db-africa-r",
            "db-central", "db-central-r",
            "api-1", "api-2",
            "auth", "legacy", "namenode", "datanode-1", "datanode-2", "sentinela"]
ACOES = {"parar": "stop", "ligar": "start", "reiniciar": "restart", "matar": "kill"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [controlo] %(message)s")
log = logging.getLogger("controlo")
docker = httpx.Client(transport=httpx.HTTPTransport(uds=SOCKET), base_url="http://docker",
                      timeout=40)
app = FastAPI(title="Global-Scale System — Controlo de falhas (demonstração)",
              docs_url=None, redoc_url=None, openapi_url=None)


def admin(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Token JWT em falta")
    try:
        c = jwt.decode(authorization.split(" ", 1)[1].strip(), CHAVE_PUBLICA,
                       algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER,
                       options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]})
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Token inválido: {e}")
    if c.get("papel") != "admin":
        raise HTTPException(403, "O controlo de falhas é reservado a administradores")
    return c["sub"]


def _contentores() -> dict[str, dict]:
    filtros = json.dumps({"label": [f"com.docker.compose.project={PROJETO}"]})
    r = docker.get("/containers/json", params={"all": "true", "filters": filtros})
    r.raise_for_status()
    out = {}
    for c in r.json():
        svc = c["Labels"].get("com.docker.compose.service")
        if svc in SERVICOS:
            out[svc] = {"id": c["Id"], "estado": c["State"], "detalhe": c["Status"]}
    return out


def _contentor(servico: str) -> str:
    if servico not in SERVICOS:
        raise HTTPException(400, f"Serviço não permitido: {servico}")
    c = _contentores().get(servico)
    if not c:
        raise HTTPException(404, f"Contentor de {servico} não encontrado")
    return c["id"]


@app.get("/controlo/nos")
def listar(u: str = Depends(admin)):
    cs = _contentores()
    return {"nos": [{"servico": s, **{k: v for k, v in cs.get(s, {"estado": "ausente",
                                                                    "detalhe": ""}).items()
                                       if k != "id"}} for s in SERVICOS]}


@app.post("/controlo/nos/{servico}/{acao}")
def atuar(servico: str, acao: str, u: str = Depends(admin)):
    if acao not in ACOES:
        raise HTTPException(400, f"Ação inválida: {acao}")
    cid = _contentor(servico)
    params = {"t": "5"} if acao in ("parar", "reiniciar") else {}
    r = docker.post(f"/containers/{cid}/{ACOES[acao]}", params=params)
    if r.status_code not in (204, 304):
        raise HTTPException(502, f"O Docker recusou: {r.text[:200]}")
    log.info("%s: %s %s", u, acao.upper(), servico)
    return {"servico": servico, "acao": acao, "por": u, "resultado": "ok"}


DADOS_COORDENADOR = Path(os.getenv("DATA_DIR", "/data"))


@app.post("/controlo/reativar/{servico}")
def reativar(servico: str, u: str = Depends(admin)):
    """Volta a ligar uma réplica da API desativada pela falha 'crash_permanente_coordenador'
    (apaga a marca <nó>.desativado do volume partilhado e reinicia o contentor)."""
    if servico not in ("api-1", "api-2"):
        raise HTTPException(400, "Só api-1 ou api-2")
    (DADOS_COORDENADOR / f"{servico}.desativado").unlink(missing_ok=True)
    cid = _contentor(servico)
    r = docker.post(f"/containers/{cid}/restart", params={"t": "3"})
    if r.status_code not in (204, 304):
        raise HTTPException(502, f"O Docker recusou: {r.text[:200]}")
    log.info("%s: REATIVAR %s", u, servico)
    return {"servico": servico, "acao": "reativar", "por": u, "resultado": "ok"}


def _demux(dados: bytes) -> str:
    """Os logs do Docker (sem TTY) vêm em blocos com um cabeçalho de 8 bytes."""
    out, i = [], 0
    while i + 8 <= len(dados):
        n = int.from_bytes(dados[i + 4:i + 8], "big")
        out.append(dados[i + 8:i + 8 + n])
        i += 8 + n
    texto = b"".join(out) if out else dados
    return texto.decode("utf-8", "replace")


ANSI = re.compile(r"\x1b\[[0-9;]*m")


@app.get("/controlo/logs/{servico}")
def logs(servico: str, linhas: int = Query(40, ge=1, le=300), u: str = Depends(admin)):
    cid = _contentor(servico)
    r = docker.get(f"/containers/{cid}/logs",
                   params={"stdout": "true", "stderr": "true", "tail": str(linhas)})
    r.raise_for_status()
    return {"servico": servico, "linhas": ANSI.sub("", _demux(r.content)).splitlines()}
