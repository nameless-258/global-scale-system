"""Sentinela de alta disponibilidade dos fragmentos PostgreSQL.

Cada fragmento tem DOIS nós (ex.: db-america e db-america-r). Os papéis não são
fixos: a cada segundo a sentinela vê qual dos dois é o PRIMÁRIO (aceita escritas)
e qual é a RÉPLICA (está em recuperação, a receber o WAL).

1. GESTÃO DA REPLICAÇÃO SÍNCRONA
   * réplica em streaming -> o primário exige a confirmação dela em cada COMMIT
     (synchronous_standby_names): nenhuma transação confirmada se perde (RPO = 0);
   * réplica em baixo -> o primário passa a assíncrono, para não bloquear as escritas
     (o mesmo compromisso que o Patroni faz em "synchronous mode").

2. FAILOVER AUTOMÁTICO
   Se o primário falhar 3 verificações seguidas (~3-5 s) E a réplica estava SÍNCRONA:
     a) FENCING: grava /fence/<primário>.vedado — o nó que falhou não pode voltar
        como primário (evita split-brain);
     b) PROMOÇÃO: pg_promote() na réplica, que passa a aceitar escritas;
     c) a aplicação liga-se sozinha ao novo primário (target_session_attrs=read-write).

3. REINTEGRAÇÃO
   Quando o nó vedado volta a arrancar, o seu entrypoint vê a marca de fencing e,
   em vez de arrancar como primário, faz uma cópia base nova a partir do novo primário
   e volta como RÉPLICA (como o Patroni faz com pg_rewind). A sentinela deteta a nova
   réplica e reativa a replicação síncrona: o fragmento volta a ter redundância e pode
   sofrer um novo failover, agora no sentido contrário.

Limitação conhecida: existe uma só sentinela. Em produção usar-se-iam várias
sentinelas com quórum (Patroni + etcd) para evitar que uma partição de rede faça
a sentinela promover a réplica por engano.
"""
import os
import socket
import time
from datetime import datetime
from pathlib import Path

import psycopg

PUBLICO = Path(os.getenv("CERTS_PUBLICO", "/certs/publico"))
PRIVADO = Path(os.getenv("CERTS_PRIVADO", "/certs/privado"))
FENCE = Path(os.getenv("FENCE_DIR", "/fence"))
INTERVALO = float(os.getenv("SENTINELA_INTERVALO_S", "1"))
FALHAS_PARA_PROMOVER = int(os.getenv("SENTINELA_FALHAS", "3"))
TIMEOUT_LIGACAO = int(os.getenv("SENTINELA_TIMEOUT_S", "1"))
# fragmento:no_a:no_b[:application_name][:porta_a:porta_b]
FRAGMENTOS = os.getenv("FRAGMENTOS", "europa:db-europa:db-europa-r,"
                                     "america:db-america:db-america-r,"
                                     "africa:db-africa:db-africa-r,"
                                     "central:db-central:db-central-r")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sentinela] {msg}", flush=True)


def app_name(no: str) -> str:
    """Nome com que um nó se apresenta ao primário quando é réplica (db-america -> db_america)."""
    return no.replace("-", "_")


class Fragmento:
    def __init__(self, spec: str):
        p = [x for x in spec.split(":") if x]
        self.nome, a, b = p[:3]
        portas = [int(x) for x in p[3:] if x.isdigit()]
        self.nos = [a, b]
        self.porta = {a: portas[0] if len(portas) > 1 else 5432, b: portas[1] if len(portas) > 1 else 5432}
        self.primario = None            # último primário visto
        self.falhas = 0
        self.replica_sincrona = False   # a réplica estava síncrona na última verificação boa

    # IPs fixos da rede global-net (iguais aos do docker-compose.yml); para outros
    # ambientes, IP_<NÓ> no ambiente ou resolução DNS guardada em cache.
    _ips: dict = {"db-europa": "172.28.0.41", "db-america": "172.28.0.42", "db-europa-r": "172.28.0.43",
                  "db-america-r": "172.28.0.44", "db-africa": "172.28.0.47", "db-africa-r": "172.28.0.48",
                  "db-central": "172.28.0.49", "db-central-r": "172.28.0.50"}

    def _ip(self, no):
        ip = os.getenv(f"IP_{no.upper().replace('-', '_')}") or Fragmento._ips.get(no)
        if not ip:
            try:
                ip = Fragmento._ips[no] = socket.gethostbyname(no)
            except OSError:
                ip = None
        return ip

    def outro(self, no):
        return self.nos[1] if no == self.nos[0] else self.nos[0]

    def _ligar(self, no):
        # hostaddr (IP em cache) evita esperar pelo DNS quando o nó está parado;
        # o certificado continua a ser verificado pelo nome (host).
        return psycopg.connect(
            host=no, hostaddr=self._ip(no), port=self.porta[no], dbname="postgres", user="postgres",
            password=(PRIVADO / "postgres_password").read_text().strip(),
            sslmode="verify-full", sslrootcert=str(PUBLICO / "ca.crt"),
            connect_timeout=TIMEOUT_LIGACAO, autocommit=True)

    def _papel(self, no):
        """'primario', 'replica' ou None (inacessível)."""
        # Teste rápido da porta (0,5 s): o libpq impõe um mínimo de 2 s ao connect_timeout,
        # o que atrasaria a deteção de um nó morto.
        try:
            socket.create_connection((self._ip(no) or no, self.porta[no]), timeout=0.5).close()
        except OSError:
            return None
        try:
            with self._ligar(no) as c:
                return "replica" if c.execute("SELECT pg_is_in_recovery()").fetchone()[0] else "primario"
        except psycopg.OperationalError:
            return None

    def verificar(self):
        papeis = {n: self._papel(n) for n in self.nos}
        primarios = [n for n, p in papeis.items() if p == "primario"]
        if len(primarios) == 2:
            # Nunca deveria acontecer (o fencing impede-o): manter o primário conhecido
            # e vedar o outro, para não haver dois nós a aceitar escritas.
            manter = self.primario if self.primario in primarios else primarios[0]
            self._vedar(self.outro(manter), "dois primários detetados (split-brain)")
            return
        if primarios:
            p = primarios[0]
            if p != self.primario:
                log(f"{self.nome}: primário atual é {p}" + (f" (réplica: {self.outro(p)})" if papeis[self.outro(p)] else ""))
                self.primario = p
            self.falhas = 0
            with self._ligar(p) as c:
                self._gerir_sincronismo(c, self.outro(p))
            return
        if self.primario is None:
            return                                         # ainda a arrancar
        self.falhas += 1
        log(f"{self.nome}: primário {self.primario} não responde ({self.falhas}/{FALHAS_PARA_PROMOVER})")
        if self.falhas >= FALHAS_PARA_PROMOVER:
            self._failover(papeis)

    def _gerir_sincronismo(self, c, replica):
        app = app_name(replica)
        linhas = c.execute("SELECT state, sync_state FROM pg_stat_replication "
                           "WHERE application_name = %s", (app,)).fetchall()
        em_streaming = any(l[0] == "streaming" for l in linhas)
        atual = c.execute("SHOW synchronous_standby_names").fetchone()[0]
        if em_streaming and atual != app:
            c.execute(f"ALTER SYSTEM SET synchronous_standby_names = '{app}'")
            c.execute("SELECT pg_reload_conf()")
            log(f"{self.nome}: réplica {replica} em streaming -> replicação SÍNCRONA ativada")
        elif not em_streaming and atual:
            c.execute("ALTER SYSTEM SET synchronous_standby_names = ''")
            c.execute("SELECT pg_reload_conf()")
            log(f"{self.nome}: réplica {replica} em baixo -> primário passa a ASSÍNCRONO "
                f"(não bloqueia as escritas)")
        self.replica_sincrona = any(l[1] == "sync" for l in linhas)

    def _vedar(self, no, motivo):
        if (FENCE / f"{no}.vedado").exists():
            return
        FENCE.mkdir(parents=True, exist_ok=True)
        (FENCE / f"{no}.vedado").write_text(f"vedado em {datetime.now().isoformat()}: {motivo}\n")
        log(f"{self.nome}: FENCING — {no} vedado (não poderá voltar como primário; volta como réplica)")

    def _failover(self, papeis):
        antigo, novo = self.primario, self.outro(self.primario)
        if papeis[novo] != "replica":
            log(f"{self.nome}: réplica {novo} também inacessível — fragmento em baixo")
            return
        if not self.replica_sincrona:
            log(f"{self.nome}: NÃO promove {novo}: não estava síncrona (risco de perder "
                f"transações confirmadas). Intervenção manual necessária.")
            return
        try:
            with self._ligar(novo) as c:
                self._vedar(antigo, f"substituído por {novo}")
                c.execute("SELECT pg_promote(true, 30)")
                log(f"{self.nome}: FAILOVER — {novo} PROMOVIDA a primário")
                # o novo primário ainda não tem réplica: não pode esperar confirmação síncrona
                c.execute("ALTER SYSTEM SET synchronous_standby_names = ''")
                c.execute("SELECT pg_reload_conf()")
            self.primario, self.falhas, self.replica_sincrona = novo, 0, False
        except psycopg.OperationalError as e:
            log(f"{self.nome}: falhou a promoção de {novo}: {e}")


def main():
    fragmentos = [Fragmento(s) for s in FRAGMENTOS.split(",") if s.strip()]
    log("a vigiar: " + ", ".join(f"{f.nome} ({f.nos[0]} e {f.nos[1]})" for f in fragmentos))
    while True:
        for f in fragmentos:
            try:
                f.verificar()
            except Exception as e:                      # nunca deixar a sentinela morrer
                log(f"{f.nome}: erro inesperado: {e}")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
