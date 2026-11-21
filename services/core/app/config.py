"""Configuração do nó da aplicação (lida de variáveis de ambiente e de segredos).

Cada réplica da API (api-1, api-2) corre exatamente o mesmo código; apenas o
NODE_ID muda. Isto é a base da replicação ativa da camada aplicacional.

Segredos (palavra-passe da BD, chave de cifragem de dados, chave de assinatura)
são lidos de ficheiros montados SÓ neste contentor (/certs/privado), nunca de
variáveis no docker-compose.yml nem do código-fonte.
"""
import os
from pathlib import Path

NODE_ID = os.getenv("NODE_ID", "api-1")
# Réplicas da API que partilham o volume do registo do coordenador (ver recuperação)
PEERS = [p.strip() for p in os.getenv("PEERS", "api-1,api-2").split(",") if p.strip()]
PEER_PORTA = int(os.getenv("PEER_PORTA", "8443"))
# Endereço de cada réplica (por omissão <nome>:8443); ex: "api-1=localhost:9101,api-2=localhost:9102"
PEER_ENDERECOS = dict(e.split("=", 1) for e in os.getenv("PEER_ENDERECOS", "").split(",") if "=" in e)

PUBLICO = Path(os.getenv("CERTS_PUBLICO", "/certs/publico"))
PRIVADO = Path(os.getenv("CERTS_PRIVADO", "/certs/privado"))
CA_CERT = str(PUBLICO / "ca.crt")


def _segredo(nome: str, env: str | None = None) -> str:
    if env and os.getenv(env):
        return os.environ[env]
    return (PRIVADO / nome).read_text().strip()


# --- Catálogo global de fragmentos (shard directory) -------------------------
# Mapeia o prefixo da conta para o fragmento que a guarda. Cada fragmento tem
# um PRIMÁRIO e uma RÉPLICA; a cadeia de ligação lista os dois e o libpq escolhe
# o que aceita escritas (target_session_attrs=read-write). Depois de uma
# promoção feita pela sentinela, a aplicação passa automaticamente para a réplica.
# IP fixo de cada nó na rede global-net. Liga-se pelo IP (hostaddr) e verifica-se o
# certificado pelo NOME (host): quando um nó está parado, o seu nome deixa de existir
# no DNS do Docker e cada resolução demoraria segundos; pelo IP, a falha é imediata.
DB_IPS = {"db-europa": "172.28.0.41", "db-america": "172.28.0.42", "db-europa-r": "172.28.0.43",
          "db-america-r": "172.28.0.44", "db-africa": "172.28.0.47", "db-africa-r": "172.28.0.48",
          **dict(e.split("=", 1) for e in os.getenv("DB_IPS", "").split(",") if "=" in e)}


def _dsn(prefixo: str, primario: str, replica: str) -> str:
    hosts = os.getenv(f"DB_{prefixo}_HOSTS", f"{primario},{replica}")
    portas = os.getenv(f"DB_{prefixo}_PORTS", "5432,5432")
    ips = [DB_IPS.get(h) for h in hosts.split(",")]
    hostaddr = f"hostaddr={','.join(ips)} " if all(ips) else ""
    return (f"host={hosts} {hostaddr}port={portas} dbname={os.getenv('DB_NAME', 'banco')} "
            f"user={os.getenv('DB_USER', 'app')} password={_segredo('db_app_password', 'DB_PASSWORD')} "
            f"sslmode=verify-full sslrootcert={CA_CERT} connect_timeout=3 "
            f"target_session_attrs=read-write")


def dsn_no(no: str) -> str:
    """Ligação a UM nó concreto (primário ou réplica), para inspeção pelo administrador."""
    return (f"host={no} hostaddr={DB_IPS[no]} port=5432 dbname={os.getenv('DB_NAME', 'banco')} "
            f"user={os.getenv('DB_USER', 'app')} password={_segredo('db_app_password', 'DB_PASSWORD')} "
            f"sslmode=verify-full sslrootcert={CA_CERT} connect_timeout=2 target_session_attrs=any")


NOS_BD = {"db-europa": "EU", "db-europa-r": "EU", "db-america": "AM", "db-america-r": "AM",
          "db-africa": "AF", "db-africa-r": "AF"}

SHARDS = {
    "EU": _dsn("EUROPA", "db-europa", "db-europa-r"),
    "AM": _dsn("AMERICA", "db-america", "db-america-r"),
    "AF": _dsn("AFRICA", "db-africa", "db-africa-r"),
}
SHARD_NOMES = {"EU": "db-europa (Nó A — Europa)", "AM": "db-america (Nó B — América)",
               "AF": "db-africa (Nó C — África)"}

# --- Segurança -----------------------------------------------------------------
JWT_PUBLIC_KEY = (PUBLICO / "jwt-public.pem").read_text()
JWT_ISSUER = os.getenv("JWT_ISSUER", "global-scale-auth")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "global-scale-api")
SIGNING_PRIVATE_KEY = str(PRIVADO / "core-signing-private.pem")
DADOS_KEY = PRIVADO / "dados.key"          # chave AES-256 de cifragem em repouso
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
