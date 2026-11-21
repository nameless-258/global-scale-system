#!/bin/bash
# =============================================================================
# Entrypoint dos nós PostgreSQL (primário ou réplica de um fragmento).
#
#  DB_NODE_NAME  nome do nó (db-europa, db-europa-r, ...)
#  DB_ROLE       primario | replica
#  DB_PRIMARIO   (réplica) nó primário de onde copiar os dados
#  DB_APP_NAME   (réplica) nome com que a réplica se apresenta ao primário
#  DB_PARCEIRO   o outro nó do mesmo fragmento (por omissão = DB_PRIMARIO)
#
# FENCING + REINTEGRAÇÃO: se a sentinela promoveu o parceiro, este nó fica
# "vedado" (/fence/<nó>.vedado) e NUNCA volta a arrancar como primário (evita o
# split-brain). Em vez disso, apaga os seus dados, faz uma cópia base nova a
# partir do novo primário e volta como RÉPLICA. Não se perde nada: a replicação
# era síncrona, por isso o novo primário já tem todas as transações confirmadas.
# =============================================================================
set -e
NODE="${DB_NODE_NAME:?defina DB_NODE_NAME}"
ROLE="${DB_ROLE:-primario}"

# Certificado TLS do nó, com as permissões exigidas pelo PostgreSQL
TLS=/var/lib/postgresql/tls
mkdir -p "$TLS"
cp /certs/privado/tls.crt "$TLS/server.crt"
cp /certs/privado/tls.key "$TLS/server.key"
cp /certs/publico/ca.crt  "$TLS/ca.crt"
chown -R postgres:postgres "$TLS"
chmod 600 "$TLS/server.key"

# Palavra-passe de replicação num .pgpass (nunca fica nos ficheiros de configuração)
printf '*:*:*:replicador:%s\n' "$(cat /certs/privado/replicador_password)" > /var/lib/postgresql/.pgpass
chown postgres:postgres /var/lib/postgresql/.pgpass
chmod 600 /var/lib/postgresql/.pgpass

PARCEIRO="${DB_PARCEIRO:-$DB_PRIMARIO}"
APP="${DB_APP_NAME:-${NODE//-/_}}"

if [ -f "/fence/${NODE}.vedado" ]; then
  echo "[fencing] ${NODE} foi substituído por ${PARCEIRO}: não volta a arrancar como primário (evita split-brain)."
  echo "[reintegração] ${NODE} volta como RÉPLICA de ${PARCEIRO} (cópia base nova; a replicação era síncrona, nada se perde)."
  until pg_isready -h "$PARCEIRO" -p 5432 -q; do sleep 2; done
  rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  mkdir -p "$PGDATA"; chown postgres:postgres "$PGDATA"; chmod 700 "$PGDATA"
  su-exec postgres pg_basebackup -D "$PGDATA" -X stream -R --checkpoint=fast \
    -d "host=${PARCEIRO} port=5432 user=replicador sslmode=verify-full sslrootcert=${TLS}/ca.crt application_name=${APP}"
  rm -f "/fence/${NODE}.vedado"
  echo "[reintegração] ${NODE} é agora réplica de ${PARCEIRO} (streaming replication)."
fi

if [ "$ROLE" = "replica" ] && [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[réplica] À espera do primário ${DB_PRIMARIO}..."
  until pg_isready -h "$DB_PRIMARIO" -p 5432 -q; do sleep 2; done
  sleep 3
  mkdir -p "$PGDATA"
  chown postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
  echo "[réplica] Cópia base (pg_basebackup) a partir de ${DB_PRIMARIO}, por TLS..."
  su-exec postgres pg_basebackup -D "$PGDATA" -X stream -R --checkpoint=fast \
    -d "host=${DB_PRIMARIO} port=5432 user=replicador sslmode=verify-full sslrootcert=${TLS}/ca.crt application_name=${APP}"
  echo "[réplica] Pronta: arranca em modo standby (streaming replication)."
fi

exec docker-entrypoint.sh postgres \
  -c ssl=on \
  -c ssl_cert_file=$TLS/server.crt \
  -c ssl_key_file=$TLS/server.key \
  -c ssl_ca_file=$TLS/ca.crt \
  -c ssl_min_protocol_version=TLSv1.2 \
  -c max_prepared_transactions=50 \
  -c wal_level=replica \
  -c max_wal_senders=10 \
  -c wal_keep_size=256MB \
  -c hot_standby=on \
  -c log_statement=ddl \
  -c "log_line_prefix=[%m] [${NODE}] %u@%d "
