"""Sentinela de alta disponibilidade dos fragmentos PostgreSQL.

Para cada fragmento (primário + réplica), a cada 2 segundos:

1. GESTÃO DA REPLICAÇÃO SÍNCRONA
   * réplica ligada e em streaming -> o primário passa a exigir confirmação da
     réplica em cada COMMIT (synchronous_standby_names = <réplica>): nenhuma
     transação confirmada pode ser perdida numa falha do primário (RPO = 0);
   * réplica em baixo -> o primário passa a modo assíncrono, para não bloquear
     as escritas (o mesmo compromisso que o Patroni faz em "synchronous mode").

2. FAILOVER AUTOMÁTICO
   Se o primário falhar 3 verificações seguidas E a réplica estava SÍNCRONA na
   última verificação boa (ou seja, tem todos os dados confirmados):
     a) FENCING: grava /fence/<primário>.vedado — o antigo primário recusa-se a
        arrancar como primário quando voltar (evita split-brain);
     b) PROMOÇÃO: SELECT pg_promote() na réplica, que passa a aceitar escritas;
     c) a aplicação liga-se sozinha ao novo primário (o libpq procura o nó com
        target_session_attrs=read-write).
   As transações PREPARADAS (2PC) também são replicadas, por isso a recuperação
   do coordenador conclui-as no novo primário.

Limitação conhecida: existe uma só sentinela. Em produção usar-se-iam várias
sentinelas com quórum (Patroni + etcd, ou Redis Sentinel) para evitar que uma
partição de rede faça a sentinela promover a réplica por engano.
"""
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg

PUBLICO = Path(os.getenv("CERTS_PUBLICO", "/certs/publico"))
PRIVADO = Path(os.getenv("CERTS_PRIVADO", "/certs/privado"))
FENCE = Path(os.getenv("FENCE_DIR", "/fence"))
INTERVALO = float(os.getenv("SENTINELA_INTERVALO_S", "2"))
FALHAS_PARA_PROMOVER = int(os.getenv("SENTINELA_FALHAS", "3"))
# fragmento:primario:replica:application_name[:porta_primario:porta_replica]
FRAGMENTOS = os.getenv("FRAGMENTOS", "europa:db-europa:db-europa-r:db_europa_r,"
                                     "america:db-america:db-america-r:db_america_r")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sentinela] {msg}", flush=True)


class Fragmento:
    def __init__(self, spec: str):
        p = spec.split(":")
        self.nome, self.primario, self.replica, self.app_name = p[:4]
        self.porta_p = int(p[4]) if len(p) > 4 else 5432
        self.porta_r = int(p[5]) if len(p) > 5 else 5432
        self.falhas = 0
        self.primario_visto = False
        self.replica_sincrona = False
        self.vedado = (FENCE / f"{self.primario}.vedado").exists()
        if self.vedado:
            log(f"{self.nome}: {self.primario} já está vedado; o primário atual é {self.replica}")

    def _ligar(self, host, porta):
        return psycopg.connect(
            host=host, port=porta, dbname="postgres", user="postgres",
            password=(PRIVADO / "postgres_password").read_text().strip(),
            sslmode="verify-full", sslrootcert=str(PUBLICO / "ca.crt"),
            connect_timeout=2, autocommit=True)

    def verificar(self):
        if self.vedado:
            return
        try:
            with self._ligar(self.primario, self.porta_p) as c:
                if c.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                    return                                    # ainda não é primário
                self.primario_visto, self.falhas = True, 0
                self._gerir_sincronismo(c)
        except psycopg.OperationalError:
            if not self.primario_visto:
                return                                        # ainda a arrancar
            self.falhas += 1
            log(f"{self.nome}: primário {self.primario} não responde ({self.falhas}/"
                f"{FALHAS_PARA_PROMOVER})")
            if self.falhas >= FALHAS_PARA_PROMOVER:
                self._failover()

    def _gerir_sincronismo(self, c):
        linhas = c.execute("SELECT state, sync_state FROM pg_stat_replication "
                           "WHERE application_name = %s", (self.app_name,)).fetchall()
        em_streaming = any(l[0] == "streaming" for l in linhas)
        atual = c.execute("SHOW synchronous_standby_names").fetchone()[0]
        if em_streaming and atual != self.app_name:
            c.execute(f"ALTER SYSTEM SET synchronous_standby_names = '{self.app_name}'")
            c.execute("SELECT pg_reload_conf()")
            log(f"{self.nome}: réplica {self.replica} em streaming -> replicação SÍNCRONA ativada")
        elif not em_streaming and atual:
            c.execute("ALTER SYSTEM SET synchronous_standby_names = ''")
            c.execute("SELECT pg_reload_conf()")
            log(f"{self.nome}: réplica {self.replica} em baixo -> primário passa a ASSÍNCRONO "
                f"(não bloqueia as escritas)")
        self.replica_sincrona = any(l[1] == "sync" for l in linhas)

    def _failover(self):
        if not self.replica_sincrona:
            log(f"{self.nome}: NÃO promove {self.replica}: não estava síncrona (risco de perder "
                f"transações confirmadas). Intervenção manual necessária.")
            return
        try:
            with self._ligar(self.replica, self.porta_r) as c:
                if not c.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                    log(f"{self.nome}: {self.replica} já é primário")
                else:
                    FENCE.mkdir(parents=True, exist_ok=True)
                    (FENCE / f"{self.primario}.vedado").write_text(
                        f"vedado em {datetime.now().isoformat()}: substituído por {self.replica}\n")
                    log(f"{self.nome}: FENCING — {self.primario} vedado (não poderá voltar como primário)")
                    c.execute("SELECT pg_promote(true, 30)")
                    log(f"{self.nome}: FAILOVER — {self.replica} PROMOVIDA a primário")
                # o novo primário não tem réplica: não pode esperar confirmação síncrona
                c.execute("ALTER SYSTEM SET synchronous_standby_names = ''")
                c.execute("SELECT pg_reload_conf()")
                self.vedado = True
        except psycopg.OperationalError as e:
            log(f"{self.nome}: réplica {self.replica} também inacessível — fragmento em baixo ({e})")


def main():
    fragmentos = [Fragmento(s) for s in FRAGMENTOS.split(",") if s.strip()]
    log("a vigiar: " + ", ".join(f"{f.nome} ({f.primario} -> {f.replica})" for f in fragmentos))
    while True:
        for f in fragmentos:
            try:
                f.verificar()
            except Exception as e:                      # nunca deixar a sentinela morrer
                log(f"{f.nome}: erro inesperado: {e}")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
