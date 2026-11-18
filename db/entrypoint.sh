#!/bin/bash
# Entrypoint dos nós PostgreSQL: copia o certificado TLS do nó para um local
# com as permissões exigidas pelo PostgreSQL (chave privada 0600, dono postgres)
# e arranca o servidor com TLS e Two-Phase Commit ativos.
set -e
NODE="${DB_NODE_NAME:?defina DB_NODE_NAME}"
mkdir -p /var/lib/postgresql/tls
cp "/certs/${NODE}.crt" /var/lib/postgresql/tls/server.crt
cp "/certs/${NODE}.key" /var/lib/postgresql/tls/server.key
cp /certs/ca.crt        /var/lib/postgresql/tls/ca.crt
chown -R postgres:postgres /var/lib/postgresql/tls
chmod 600 /var/lib/postgresql/tls/server.key

exec docker-entrypoint.sh postgres \
  -c ssl=on \
  -c ssl_cert_file=/var/lib/postgresql/tls/server.crt \
  -c ssl_key_file=/var/lib/postgresql/tls/server.key \
  -c ssl_ca_file=/var/lib/postgresql/tls/ca.crt \
  -c ssl_min_protocol_version=TLSv1.2 \
  -c max_prepared_transactions=50 \
  -c log_statement=mod \
  -c "log_line_prefix=[%m] [${NODE}] %u@%d "
