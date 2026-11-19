#!/usr/bin/env bash
# =============================================================================
# Repõe o laboratório no estado inicial depois de uma demonstração de failover.
#
# Depois de um failover, a réplica promovida passa a ser o primário do fragmento
# e o antigo primário fica "vedado" (fencing). Em produção, o antigo primário
# seria reintegrado como nova réplica (pg_rewind ou nova cópia base). No
# laboratório recriamos todos os dados a partir dos scripts iniciais.
# =============================================================================
set -e
cd "$(dirname "$0")/.."
echo ">>> A parar tudo e a apagar volumes (dados, registos, marcas de fencing)..."
docker compose down -v
echo ">>> A arrancar de novo..."
docker compose up -d
echo ">>> Pronto. Aguarde ~60 s pelo HDFS e verifique com: docker compose run --rm cliente saude"
