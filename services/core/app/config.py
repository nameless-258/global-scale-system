"""Configuração do nó da aplicação (lida de variáveis de ambiente).

Cada réplica da API (api-1, api-2) corre exatamente o mesmo código; apenas o
NODE_ID muda. Isto é a base da replicação ativa da camada aplicacional.
"""
import os
from pathlib import Path

NODE_ID = os.getenv("NODE_ID", "api-1")

# --- Catálogo global de fragmentos (shard directory) -------------------------
# Mapeia o prefixo da conta para o nó de base de dados que a guarda.
# A aplicação usa este catálogo para garantir TRANSPARÊNCIA DE LOCALIZAÇÃO:
# o cliente só indica "EU-1001", nunca o servidor onde a conta vive.
_PG_COMMON = (
    "dbname={db} user={user} password={pwd} sslmode=verify-full "
    "sslrootcert={ca} connect_timeout=3"
)
CA_CERT = os.getenv("CA_CERT", "/certs/ca.crt")


def _dsn(host_env: str, port_env: str, default_host: str) -> str:
    host = os.getenv(host_env, default_host)
    port = os.getenv(port_env, "5432")
    base = _PG_COMMON.format(
        db=os.getenv("DB_NAME", "banco"),
        user=os.getenv("DB_USER", "app"),
        pwd=os.getenv("DB_PASSWORD", "app_s3cret"),
        ca=CA_CERT,
    )
    return f"host={host} port={port} {base}"


SHARDS = {
    "EU": _dsn("DB_EUROPA_HOST", "DB_EUROPA_PORT", "db-europa"),
    "AM": _dsn("DB_AMERICA_HOST", "DB_AMERICA_PORT", "db-america"),
}
SHARD_NOMES = {"EU": "db-europa (Nó A — Europa)", "AM": "db-america (Nó B — América)"}

# --- Segurança -----------------------------------------------------------------
JWT_PUBLIC_KEY = Path(os.getenv("JWT_PUBLIC_KEY", "/certs/jwt-public.pem")).read_text()
JWT_ISSUER = os.getenv("JWT_ISSUER", "global-scale-auth")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "global-scale-api")
SIGNING_PRIVATE_KEY = os.getenv("SIGNING_PRIVATE_KEY", "/certs/core-signing-private.pem")
TX_MAX_DESVIO_SEGUNDOS = int(os.getenv("TX_MAX_DESVIO_SEGUNDOS", "300"))

# --- Interoperabilidade (banco parceiro / mainframe legado) ------------------
LEGACY_URL = os.getenv("LEGACY_URL", "https://legacy:8443")

# --- DFS ----------------------------------------------------------------------
DFS_BACKEND = os.getenv("DFS_BACKEND", "webhdfs")          # webhdfs | local
DFS_URL = os.getenv("DFS_URL", "http://namenode:9870")
DFS_USER = os.getenv("DFS_USER", "root")
DFS_DATANODES = [d.strip() for d in os.getenv("DFS_DATANODES", "datanode-1:9864,datanode-2:9864").split(",") if d.strip()]
DFS_LOCAL_PATH = os.getenv("DFS_LOCAL_PATH", "/mnt/dfs")

# --- Registo durável do coordenador 2PC ---------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
RECUPERACAO_INTERVALO_S = int(os.getenv("RECUPERACAO_INTERVALO_S", "10"))
# Transações preparadas sem decisão há mais de X segundos são abortadas
# (regra "presumed abort" do 2PC).
PRESUMED_ABORT_APOS_S = int(os.getenv("PRESUMED_ABORT_APOS_S", "60"))

# Permite injetar falhas controladas para a demonstração laboratorial.
PERMITIR_INJECAO_FALHAS = os.getenv("PERMITIR_INJECAO_FALHAS", "true").lower() == "true"
