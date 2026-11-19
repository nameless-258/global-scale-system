#!/bin/bash
# Executado UMA vez na inicialização do primário, depois do esquema.
# As palavras-passe vêm de ficheiros de segredos montados só neste contentor
# (certs/<nó>/), nunca do código-fonte nem do docker-compose.yml.
set -e
APP_PWD="$(cat /certs/privado/db_app_password)"
REPL_PWD="$(cat /certs/privado/replicador_password)"
psql -v ON_ERROR_STOP=1 --username postgres --dbname banco <<SQL
ALTER ROLE app PASSWORD '${APP_PWD}';
CREATE ROLE replicador WITH REPLICATION LOGIN PASSWORD '${REPL_PWD}';
GRANT pg_read_all_stats TO app;   -- ver o estado da replicação em /saude
SQL
echo "[segredos] Palavras-passe dos papéis 'app' e 'replicador' definidas a partir dos segredos."
