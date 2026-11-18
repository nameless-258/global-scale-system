#!/usr/bin/env bash
# =============================================================================
# Global-Scale System — geração da PKI (Infraestrutura de Chave Pública)
#
# Gera:
#   - uma Autoridade de Certificação (CA) própria;
#   - um certificado TLS por serviço (gateway, auth, api-1, api-2, legacy,
#     db-europa, db-america), com Subject Alternative Names (SAN);
#   - o par de chaves RSA usado para assinar os JWT (RS256);
#   - o par de chaves RSA usado pelo núcleo para assinar digitalmente as
#     mensagens XML enviadas ao sistema legado (SHA256withRSA);
#   - um keystore PKCS#12 para o servidor HTTPS em Java.
#
# Uso:  bash scripts/gen-certs.sh [pasta_destino]     (por omissão ./certs)
# Idempotente: se a CA já existir, não faz nada (use FORCE=1 para regenerar).
# =============================================================================
set -euo pipefail

OUT="${1:-./certs}"
DAYS=825
P12_PASS="${P12_PASS:-globalscale}"

mkdir -p "$OUT"
cd "$OUT"

if [[ -f ca.crt && "${FORCE:-0}" != "1" ]]; then
  echo "[certs] PKI já existe em $(pwd) — nada a fazer (FORCE=1 para regenerar)."
  exit 0
fi

echo "[certs] A gerar Autoridade de Certificação (CA)..."
openssl genrsa -out ca.key 4096 2>/dev/null
cat > ca.cnf <<'CNF'
[req]
distinguished_name=dn
[dn]
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
CNF
openssl req -x509 -new -key ca.key -sha256 -days "$DAYS" -out ca.crt \
  -subj "/C=MZ/O=Global Scale Corporation/CN=Global Scale Root CA" \
  -config ca.cnf -extensions v3_ca
rm -f ca.cnf

emitir() {  # emitir <nome> <SANs separados por vírgula>
  local nome="$1" sans="$2"
  openssl genrsa -out "$nome.key" 2048 2>/dev/null
  openssl req -new -key "$nome.key" -out "$nome.csr" \
    -subj "/C=MZ/O=Global Scale Corporation/CN=$nome"
  cat > "$nome.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=$sans
EOF
  openssl x509 -req -in "$nome.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "$nome.crt" -days "$DAYS" -sha256 -extfile "$nome.ext" 2>/dev/null
  rm -f "$nome.csr" "$nome.ext"
  echo "[certs]   ✓ $nome  ($sans)"
}

echo "[certs] A emitir certificados de servidor..."
emitir gateway    "DNS:gateway,DNS:localhost,IP:127.0.0.1,IP:172.28.0.10"
emitir auth       "DNS:auth,DNS:localhost,IP:127.0.0.1,IP:172.28.0.11"
emitir api-1      "DNS:api-1,DNS:api,DNS:localhost,IP:127.0.0.1,IP:172.28.0.21"
emitir api-2      "DNS:api-2,DNS:api,DNS:localhost,IP:127.0.0.1,IP:172.28.0.22"
emitir legacy     "DNS:legacy,DNS:localhost,IP:127.0.0.1,IP:172.28.0.31"
emitir db-europa  "DNS:db-europa,DNS:localhost,IP:127.0.0.1,IP:172.28.0.41"
emitir db-america "DNS:db-america,DNS:localhost,IP:127.0.0.1,IP:172.28.0.42"

echo "[certs] A gerar chaves JWT (RS256)..."
openssl genrsa -out jwt-private.pem 2048 2>/dev/null
openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem 2>/dev/null

echo "[certs] A gerar chaves de assinatura de mensagens (núcleo -> legado)..."
openssl genrsa -out core-signing-private.pem 2048 2>/dev/null
openssl rsa -in core-signing-private.pem -pubout -out core-signing-public.pem 2>/dev/null

echo "[certs] A gerar keystore PKCS#12 para o servidor Java..."
openssl pkcs12 -export -in legacy.crt -inkey legacy.key -certfile ca.crt \
  -name legacy -out legacy.p12 -passout "pass:$P12_PASS"

# Os contentores correm com utilizadores diferentes; as chaves privadas são
# copiadas e protegidas (chmod 600) pelo entrypoint de cada serviço.
chmod 644 ./*.crt ./*.pem ./*.p12
chmod 644 ./*.key
echo "[certs] PKI pronta em $(pwd)"
