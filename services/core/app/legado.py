"""Interoperabilidade: cliente do 'mainframe' do banco parceiro (serviço Java).

O núcleo (Python, JSON) e o legado (Java, XML) usam tecnologias e formatos de
dados diferentes. Esta camada é um ADAPTADOR (padrão Adapter / Anti-Corruption
Layer): converte o modelo interno (dict/JSON) para o contrato XML do parceiro e
vice-versa.

Segurança da mensagem:
  * canal cifrado HTTPS (TLS) com verificação do certificado do legado pela nossa CA;
  * cabeçalho X-Digest-SHA256  -> integridade do corpo;
  * cabeçalho X-Assinatura     -> assinatura digital SHA256withRSA (autenticidade e
                                  não-repúdio: só o núcleo tem a chave privada).
"""
import xml.etree.ElementTree as ET

import httpx
from defusedxml.ElementTree import fromstring as xml_seguro   # bloqueia XXE / "billion laughs"

from . import config
from .security import assinar, sha256_hex

_http = httpx.Client(verify=config.CA_CERT, timeout=5)


def para_xml(campos: dict) -> bytes:
    raiz = ET.Element("OrdemPagamento", {"versao": "1.0", "xmlns": "urn:globalscale:interbancario"})
    for k, v in campos.items():
        ET.SubElement(raiz, k).text = str(v)
    return ET.tostring(raiz, encoding="utf-8", xml_declaration=True)


def de_xml(corpo: bytes) -> dict:
    raiz = xml_seguro(corpo)
    return {el.tag.split("}")[-1]: (el.text or "") for el in raiz}


def _verificar_digest(r: httpx.Response):
    recebido = r.headers.get("x-digest-sha256", "")
    if recebido and recebido != sha256_hex(r.content):
        raise ValueError("Resposta do legado adulterada (SHA-256 não confere)")


def enviar_ordem(campos: dict) -> dict:
    corpo = para_xml(campos)
    r = _http.post(f"{config.LEGACY_URL}/mainframe/ordens", content=corpo, headers={
        "Content-Type": "application/xml; charset=utf-8",
        "X-Digest-SHA256": sha256_hex(corpo),
        "X-Assinatura": assinar(corpo),
        "X-Origem": config.NODE_ID,
    })
    _verificar_digest(r)
    if r.status_code >= 500:
        raise ConnectionError(f"Legado devolveu HTTP {r.status_code}")
    dados = de_xml(r.content)
    dados["_xml_enviado"] = corpo.decode()
    dados["_xml_recebido"] = r.content.decode()
    dados["_http_status"] = r.status_code
    return dados


def consultar_ordem(referencia: str) -> dict | None:
    r = _http.get(f"{config.LEGACY_URL}/mainframe/ordens/{referencia}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    _verificar_digest(r)
    return de_xml(r.content)


def saude() -> dict:
    r = _http.get(f"{config.LEGACY_URL}/mainframe/saude", timeout=2)
    return de_xml(r.content)
