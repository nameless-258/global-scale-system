#!/usr/bin/env bash
# =============================================================================
# Global-Scale System — PKI e SEGREDOS, distribuídos por serviço
#
# Princípio do menor privilégio: cada contentor monta APENAS
#   certs/publico/      -> material público (CA, chaves públicas)      [todos]
#   certs/<serviço>/    -> as SUAS chaves privadas e palavras-passe    [só ele]
# Assim, por exemplo, uma réplica da API comprometida não consegue ler a chave
# privada de assinatura dos JWT (que só existe em certs/auth/).
#
#   certs/ca/           -> chave privada da CA (NUNCA montada em contentores)
#
# Uso:  bash scripts/gen-certs.sh [pasta_destino]     (por omissão ./certs)
# Idempotente: se a PKI já existir, não faz nada (FORCE=1 para regenerar).
# =============================================================================
set -euo pipefail

OUT="${1:-./certs}"
DAYS=825
mkdir -p "$OUT"
cd "$OUT"

emitir() {  # emitir <serviço> <SANs>  -> <serviço>/tls.crt + tls.key
  local nome="$1" sans="$2"
  openssl genrsa -out "$nome/tls.key" 2048 2>/dev/null
  openssl req -new -key "$nome/tls.key" -out "tmp/$nome.csr" \
    -subj "/C=MZ/O=Global Scale Corporation/CN=$nome"
  cat > "tmp/$nome.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=$sans
EOF
  openssl x509 -req -in "tmp/$nome.csr" -CA publico/ca.crt -CAkey ca/ca.key \
    -CAcreateserial -CAserial tmp/ca.srl -out "$nome/tls.crt" -days "$DAYS" -sha256 \
    -extfile "tmp/$nome.ext" 2>/dev/null
  echo "[certs]   ✓ $nome  ($sans)"
}

SAN_CONTROLO="DNS:controlo,DNS:localhost,IP:127.0.0.1,IP:172.28.0.46"
SAN_AFRICA="DNS:db-africa,DNS:localhost,IP:127.0.0.1,IP:172.28.0.47"
SAN_AFRICA_R="DNS:db-africa-r,DNS:localhost,IP:127.0.0.1,IP:172.28.0.48"
SAN_CENTRAL="DNS:db-central,DNS:localhost,IP:127.0.0.1,IP:172.28.0.49"
SAN_CENTRAL_R="DNS:db-central-r,DNS:localhost,IP:127.0.0.1,IP:172.28.0.50"

# Instalação já existente: só acrescenta os serviços que foram adicionados depois
# (controlo, fragmento África, nó central), sem tocar na CA nem nos segredos já em uso.
if [[ -f publico/ca.crt && -f ca/ca.key && "${FORCE:-0}" != "1" ]]; then
  mkdir -p tmp
  [[ -f controlo/tls.crt ]]    || { mkdir -p controlo;    echo "[certs] + controlo";    emitir controlo    "$SAN_CONTROLO"; }
  [[ -f db-africa/tls.crt ]]   || { mkdir -p db-africa;   echo "[certs] + db-africa";   emitir db-africa   "$SAN_AFRICA"; }
  [[ -f db-africa-r/tls.crt ]] || { mkdir -p db-africa-r; echo "[certs] + db-africa-r"; emitir db-africa-r "$SAN_AFRICA_R"; }
  [[ -f db-central/tls.crt ]]   || { mkdir -p db-central;   echo "[certs] + db-central";   emitir db-central   "$SAN_CENTRAL"; }
  [[ -f db-central-r/tls.crt ]] || { mkdir -p db-central-r; echo "[certs] + db-central-r"; emitir db-central-r "$SAN_CENTRAL_R"; }
  for s in db-africa db-africa-r db-central db-central-r; do       # as BD partilham as mesmas palavras-passe
    for f in postgres_password db_app_password replicador_password; do
      [[ -f "$s/$f" ]] || cp "db-europa/$f" "$s/$f"
    done
  done
  rm -rf tmp
  chmod -R a+rX publico controlo db-africa db-africa-r db-central db-central-r 2>/dev/null || true
fi

if [[ -f publico/ca.crt && "${FORCE:-0}" != "1" ]]; then
  echo "[certs] PKI já existe em $(pwd) — nada a fazer (FORCE=1 para regenerar)."
  exit 0
fi

SERVICOS="gateway auth api-1 api-2 legacy db-europa db-europa-r db-america db-america-r db-africa db-africa-r db-central db-central-r sentinela controlo"
mkdir -p ca publico tmp
for s in $SERVICOS; do mkdir -p "$s"; done

# ------------------------------------------------------------------ CA
echo "[certs] A gerar Autoridade de Certificação (CA)..."
openssl genrsa -out ca/ca.key 4096 2>/dev/null
cat > tmp/ca.cnf <<'CNF'
[req]
distinguished_name=dn
[dn]
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
CNF
openssl req -x509 -new -key ca/ca.key -sha256 -days "$DAYS" -out publico/ca.crt \
  -subj "/C=MZ/O=Global Scale Corporation/CN=Global Scale Root CA" \
  -config tmp/ca.cnf -extensions v3_ca

echo "[certs] A emitir certificados TLS de servidor..."
emitir gateway      "DNS:gateway,DNS:localhost,IP:127.0.0.1,IP:172.28.0.10"
emitir auth         "DNS:auth,DNS:localhost,IP:127.0.0.1,IP:172.28.0.11"
emitir api-1        "DNS:api-1,DNS:api,DNS:localhost,IP:127.0.0.1,IP:172.28.0.21"
emitir api-2        "DNS:api-2,DNS:api,DNS:localhost,IP:127.0.0.1,IP:172.28.0.22"
emitir legacy       "DNS:legacy,DNS:localhost,IP:127.0.0.1,IP:172.28.0.31"
emitir db-europa    "DNS:db-europa,DNS:localhost,IP:127.0.0.1,IP:172.28.0.41"
emitir db-america   "DNS:db-america,DNS:localhost,IP:127.0.0.1,IP:172.28.0.42"
emitir db-europa-r  "DNS:db-europa-r,DNS:localhost,IP:127.0.0.1,IP:172.28.0.43"
emitir db-america-r "DNS:db-america-r,DNS:localhost,IP:127.0.0.1,IP:172.28.0.44"
emitir db-africa    "$SAN_AFRICA"
emitir db-africa-r  "$SAN_AFRICA_R"
emitir db-central   "$SAN_CENTRAL"
emitir db-central-r "$SAN_CENTRAL_R"
emitir controlo      "$SAN_CONTROLO"

echo "[certs] A gerar pares de chaves de assinatura..."
openssl genrsa -out auth/jwt-private.pem 2048 2>/dev/null                  # só o emissor
openssl rsa -in auth/jwt-private.pem -pubout -out publico/jwt-public.pem 2>/dev/null
openssl genrsa -out tmp/core-signing-private.pem 2048 2>/dev/null           # só o núcleo
openssl rsa -in tmp/core-signing-private.pem -pubout -out publico/core-signing-public.pem 2>/dev/null
cp tmp/core-signing-private.pem api-1/; cp tmp/core-signing-private.pem api-2/

echo "[certs] A gerar segredos aleatórios..."
segredo() { openssl rand -hex 18; }
PG_ADMIN=$(segredo); PG_APP=$(segredo); PG_REPL=$(segredo); P12=$(segredo)
DADOS=$(openssl rand -base64 32)          # chave AES-256 de cifragem em repouso
for s in db-europa db-europa-r db-america db-america-r db-africa db-africa-r db-central db-central-r; do
  printf '%s' "$PG_ADMIN" > "$s/postgres_password"
  printf '%s' "$PG_APP"   > "$s/db_app_password"
  printf '%s' "$PG_REPL"  > "$s/replicador_password"
done
for s in api-1 api-2; do
  printf '%s' "$PG_APP" > "$s/db_app_password"
  printf '%s' "$DADOS"  > "$s/dados.key"
done
printf '%s' "$PG_ADMIN" > sentinela/postgres_password
printf '%s' "$P12"      > legacy/keystore_pass

echo "[certs] A gerar keystore PKCS#12 para o servidor Java..."
openssl pkcs12 -export -in legacy/tls.crt -inkey legacy/tls.key -certfile publico/ca.crt \
  -name legacy -out legacy/legacy.p12 -passout "pass:$P12"

rm -rf tmp
# Os contentores correm com utilizadores diferentes; cada entrypoint copia e
# protege (chmod 600) o que precisa. Em Windows estas permissões são ignoradas.
chmod -R a+rX publico */ 2>/dev/null || true
echo "[certs] PKI e segredos prontos em $(pwd)"
