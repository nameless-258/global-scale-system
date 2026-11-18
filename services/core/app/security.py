"""Técnicas de segurança do núcleo.

1. Autenticação distribuída: validação LOCAL de JWT (RS256). O nó da API só
   precisa da chave PÚBLICA do emissor; não contacta o serviço de autenticação
   em cada pedido (validação descentralizada, sem ponto único de falha).
2. Integridade: SHA-256 sobre a forma canónica da mensagem de transação.
3. Assinatura digital (SHA256withRSA) das mensagens enviadas ao sistema legado.
"""
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import Header, HTTPException

from . import config


# ----------------------------------------------------------------------------- JWT
@dataclass
class Utilizador:
    username: str
    papel: str          # "cliente" | "admin"
    token_id: str

    @property
    def is_admin(self) -> bool:
        return self.papel == "admin"


def utilizador_atual(authorization: str | None = Header(default=None)) -> Utilizador:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Token JWT em falta (cabeçalho Authorization: Bearer ...)",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(
            token,
            config.JWT_PUBLIC_KEY,
            algorithms=["RS256"],          # lista fechada: impede o ataque "alg=none"/HS256
            audience=config.JWT_AUDIENCE,
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expirado", headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Token inválido: {e}", headers={"WWW-Authenticate": "Bearer"})
    return Utilizador(username=claims["sub"], papel=claims.get("papel", "cliente"),
                      token_id=claims["jti"])


def exigir_admin(u: Utilizador) -> None:
    if not u.is_admin:
        raise HTTPException(403, "Operação reservada a administradores")


# ----------------------------------------------------------------------- SHA-256
def forma_canonica(dados: dict) -> bytes:
    """Serialização determinística: chaves ordenadas, sem espaços, UTF-8.
    Cliente e servidor têm de produzir exatamente os mesmos bytes."""
    return json.dumps(dados, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def verificar_integridade(campos: dict, hash_recebido: str) -> str:
    calculado = sha256_hex(forma_canonica(campos))
    # compare_digest: comparação em tempo constante (evita timing attacks)
    if not hmac.compare_digest(calculado, (hash_recebido or "").lower()):
        raise HTTPException(
            400,
            {"erro": "INTEGRIDADE_VIOLADA",
             "detalhe": "O hash SHA-256 não corresponde ao conteúdo: a mensagem foi adulterada.",
             "hash_recebido": hash_recebido, "hash_calculado": calculado},
        )
    return calculado


def verificar_frescura(timestamp_iso: str) -> None:
    """Proteção anti-replay temporal: rejeita mensagens fora da janela permitida."""
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "timestamp inválido (use ISO-8601, ex: 2026-11-18T10:00:00Z)")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    desvio = abs((datetime.now(timezone.utc) - ts).total_seconds())
    if desvio > config.TX_MAX_DESVIO_SEGUNDOS:
        raise HTTPException(400, {"erro": "MENSAGEM_EXPIRADA",
                                  "detalhe": f"Desvio de {int(desvio)}s excede o máximo de "
                                             f"{config.TX_MAX_DESVIO_SEGUNDOS}s (possível replay)."})


# ------------------------------------------------------------ Assinatura digital
_chave_privada = None


def assinar(dados: bytes) -> str:
    """Assinatura SHA256withRSA (PKCS#1 v1.5) em Base64 — compatível com Java."""
    global _chave_privada
    if _chave_privada is None:
        with open(config.SIGNING_PRIVATE_KEY, "rb") as f:
            _chave_privada = serialization.load_pem_private_key(f.read(), password=None)
    assinatura = _chave_privada.sign(dados, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(assinatura).decode()
