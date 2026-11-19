#!/bin/bash
# Executado UMA vez na inicialização do cluster PostgreSQL (só no primário;
# a réplica herda-o através do pg_basebackup).
# Obriga TODAS as ligações TCP a usar TLS (hostssl) com autenticação SCRAM,
# incluindo a ligação de replicação. Ligações TCP sem TLS são rejeitadas.
set -e
cat > "$PGDATA/pg_hba.conf" <<'EOF'
# TYPE    DATABASE     USER        ADDRESS      METHOD
local     all          all                      trust
hostssl   replication  replicador  0.0.0.0/0    scram-sha-256
hostssl   all          all         0.0.0.0/0    scram-sha-256
hostssl   all          all         ::/0         scram-sha-256
host      all          all         0.0.0.0/0    reject
host      all          all         ::/0         reject
EOF
echo "[pg_hba] Apenas ligações TLS (hostssl) são aceites."
