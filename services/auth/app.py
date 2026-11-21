"""Serviço de Autenticação (nó emissor de tokens).

* Autentica o utilizador (palavra-passe com PBKDF2-HMAC-SHA256 + salt).
* Emite um JWT assinado com RS256 (chave PRIVADA, que só existe neste nó).
* Publica a chave PÚBLICA em /.well-known/jwks.json (padrão OAuth2/OIDC) para que
  os restantes nós validem os tokens de forma independente e descentralizada.
* Proteção contra força bruta: bloqueio temporário após 5 tentativas falhadas.
* Gestão de utilizadores pelo administrador: criar clientes novos. Os utilizadores
  criados ficam num ficheiro num volume persistente (/data), escrito de forma atómica
  (ficheiro temporário + fsync + rename); os utilizadores iniciais vêm da imagem.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

PRIVATE_KEY = Path(os.getenv("JWT_PRIVATE_KEY", "/certs/privado/jwt-private.pem")).read_text()
PUBLIC_KEY = Path(os.getenv("JWT_PUBLIC_KEY", "/certs/publico/jwt-public.pem")).read_text()
ISSUER = os.getenv("JWT_ISSUER", "global-scale-auth")
AUDIENCE = os.getenv("JWT_AUDIENCE", "global-scale-api")
TTL = int(os.getenv("JWT_TTL_SEGUNDOS", "900"))
KID = hashlib.sha256(PUBLIC_KEY.encode()).hexdigest()[:16]
UTILIZADORES = json.loads((Path(__file__).parent / "utilizadores.json").read_text())
FICHEIRO_EXTRA = Path(os.getenv("UTILIZADORES_EXTRA", "/data/utilizadores-criados.json"))
if FICHEIRO_EXTRA.exists():                       # clientes criados pelo administrador
    UTILIZADORES.update(json.loads(FICHEIRO_EXTRA.read_text()))
_trinco = threading.Lock()
ITERACOES = 310_000

MAX_TENTATIVAS, BLOQUEIO_S = 5, 60
_falhas: dict[str, list[float]] = {}

app = FastAPI(title="Global-Scale — Serviço de Autenticação", version="1.0.0")


class Credenciais(BaseModel):
    username: str
    password: str


def _verificar_password(password: str, registo: dict) -> bool:
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(registo["salt"]),
                               registo["iteracoes"])
    return hmac.compare_digest(calc.hex(), registo["hash"])


@app.post("/auth/login")
def login(c: Credenciais):
    agora = time.time()
    recentes = [t for t in _falhas.get(c.username, []) if agora - t < BLOQUEIO_S]
    if len(recentes) >= MAX_TENTATIVAS:
        raise HTTPException(429, "Demasiadas tentativas falhadas. Tente daqui a 60 segundos.")
    reg = UTILIZADORES.get(c.username)
    if not reg or not _verificar_password(c.password, reg):
        _falhas[c.username] = recentes + [agora]
        raise HTTPException(401, "Credenciais inválidas")
    _falhas.pop(c.username, None)
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": c.username, "papel": reg["papel"],
              "nome": reg["nome"], "iat": int(agora), "nbf": int(agora),
              "exp": int(agora) + TTL, "jti": str(uuid.uuid4())}
    token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    return {"access_token": token, "token_type": "Bearer", "expires_in": TTL,
            "emitido_por": "auth", "algoritmo": "RS256", "papel": reg["papel"]}


def _b64url_uint(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


@app.get("/.well-known/jwks.json")
def jwks():
    pub = serialization.load_pem_public_key(PUBLIC_KEY.encode()).public_numbers()
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                      "n": _b64url_uint(pub.n), "e": _b64url_uint(pub.e)}]}


# ------------------------------------------------------- gestão de utilizadores
def _admin(authorization: str | None) -> str:
    """Valida o JWT (com a chave pública, como as outras réplicas) e exige papel admin."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Token JWT em falta")
    try:
        c = jwt.decode(authorization.split(" ", 1)[1].strip(), PUBLIC_KEY, algorithms=["RS256"],
                       audience=AUDIENCE, issuer=ISSUER,
                       options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]})
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Token inválido: {e}")
    if c.get("papel") != "admin":
        raise HTTPException(403, "Só o administrador gere utilizadores")
    return c["sub"]


class NovoUtilizador(BaseModel):
    username: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,30}$")
    nome: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=128)


@app.get("/auth/utilizadores")
def listar_utilizadores(authorization: str | None = Header(default=None)):
    _admin(authorization)
    return {"utilizadores": [{"username": u, "nome": r["nome"], "papel": r["papel"]}
                             for u, r in UTILIZADORES.items()]}


@app.post("/auth/utilizadores", status_code=201)
def criar_utilizador(n: NovoUtilizador, authorization: str | None = Header(default=None)):
    por = _admin(authorization)
    if not (re.search(r"[A-Za-z]", n.password) and re.search(r"[0-9]", n.password)):
        raise HTTPException(400, "A palavra-passe precisa de letras e números (mínimo 8 caracteres)")
    with _trinco:
        if n.username in UTILIZADORES:
            raise HTTPException(409, f"O utilizador '{n.username}' já existe")
        salt = secrets.token_bytes(16)
        registo = {"nome": n.nome.strip(), "papel": "cliente", "salt": salt.hex(), "iteracoes": ITERACOES,
                   "hash": hashlib.pbkdf2_hmac("sha256", n.password.encode(), salt, ITERACOES).hex(),
                   "criado_por": por, "criado_em": int(time.time())}
        extra = json.loads(FICHEIRO_EXTRA.read_text()) if FICHEIRO_EXTRA.exists() else {}
        extra[n.username] = registo
        FICHEIRO_EXTRA.parent.mkdir(parents=True, exist_ok=True)
        tmp = FICHEIRO_EXTRA.with_suffix(".tmp")
        with open(tmp, "w") as f:                    # escrita atómica e durável
            json.dump(extra, f, ensure_ascii=False, indent=1)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, FICHEIRO_EXTRA)
        UTILIZADORES[n.username] = registo
    return {"username": n.username, "nome": registo["nome"], "papel": "cliente", "criado_por": por}


@app.get("/auth/saude")
def saude():
    return {"servico": "auth", "estado": "OK", "kid": KID}
