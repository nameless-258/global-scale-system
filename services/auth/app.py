"""Serviço de Autenticação (nó emissor de tokens).

* Autentica o utilizador (palavra-passe com PBKDF2-HMAC-SHA256 + salt).
* Emite um JWT assinado com RS256 (chave PRIVADA, que só existe neste nó).
* Publica a chave PÚBLICA em /.well-known/jwks.json (padrão OAuth2/OIDC) para que
  os restantes nós validem os tokens de forma independente e descentralizada.
* Proteção contra força bruta: bloqueio temporário após 5 tentativas falhadas.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PRIVATE_KEY = Path(os.getenv("JWT_PRIVATE_KEY", "/certs/privado/jwt-private.pem")).read_text()
PUBLIC_KEY = Path(os.getenv("JWT_PUBLIC_KEY", "/certs/publico/jwt-public.pem")).read_text()
ISSUER = os.getenv("JWT_ISSUER", "global-scale-auth")
AUDIENCE = os.getenv("JWT_AUDIENCE", "global-scale-api")
TTL = int(os.getenv("JWT_TTL_SEGUNDOS", "900"))
KID = hashlib.sha256(PUBLIC_KEY.encode()).hexdigest()[:16]
UTILIZADORES = json.loads((Path(__file__).parent / "utilizadores.json").read_text())

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


@app.get("/auth/saude")
def saude():
    return {"servico": "auth", "estado": "OK", "kid": KID}
