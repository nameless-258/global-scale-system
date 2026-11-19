"""Cifragem de dados em REPOUSO ao nível da aplicação (AES-256-GCM).

Os dados pessoais (nome do titular, conta/IBAN da contraparte) são cifrados
AQUI, antes de serem enviados para a base de dados. A chave existe apenas nos
contentores da API (certs/api-*/dados.key); o PostgreSQL, os seus ficheiros, os
backups e as réplicas só guardam bytes cifrados.

AES-GCM é cifragem AUTENTICADA: além de confidencialidade dá integridade. O
"contexto" (dados associados, AAD) amarra o texto cifrado à linha a que pertence:
copiar o titular cifrado de uma conta para outra faz a decifragem falhar.

Formato guardado: versão (1 byte) || nonce (12 bytes) || texto cifrado + tag (16 bytes)
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

VERSAO = b"\x01"
_aes: AESGCM | None = None


def _cifra() -> AESGCM:
    global _aes
    if _aes is None:
        chave = base64.b64decode(config.DADOS_KEY.read_text().strip())
        if len(chave) != 32:
            raise ValueError("A chave de dados tem de ter 256 bits")
        _aes = AESGCM(chave)
    return _aes


def cifrar(texto: str, contexto: str) -> bytes:
    nonce = os.urandom(12)                       # nunca reutilizar um nonce com a mesma chave
    return VERSAO + nonce + _cifra().encrypt(nonce, texto.encode("utf-8"), contexto.encode())


def decifrar(blob, contexto: str) -> str:
    if blob is None:
        return None
    blob = bytes(blob)
    if blob[:1] != VERSAO:
        raise ValueError("Formato de dados cifrados desconhecido")
    try:
        return _cifra().decrypt(blob[1:13], blob[13:], contexto.encode()).decode("utf-8")
    except InvalidTag:
        return "<<DADO ADULTERADO OU CHAVE ERRADA>>"


def ctx_titular(conta_id: str) -> str:
    return f"contas:{conta_id}:titular"


def ctx_contraparte(tx_id, conta_id: str, tipo: str) -> str:
    return f"movimentos:{tx_id}:{conta_id}:{tipo}:contraparte"
