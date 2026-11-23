"""Prova de conectividade entre os nós da rede global-net.

Corre DENTRO de um contentor (por exemplo api-1) e, para cada nó:
  1. resolve o nome pelo DNS interno do Docker (127.0.0.11);
  2. abre uma ligação TCP à porta do serviço e mede o tempo.

Uso:  Get-Content scripts/conectividade.py | docker compose exec -T api-1 python -
      (bash: docker compose exec -T api-1 python - < scripts/conectividade.py)
"""
import socket
import time

NOS = [("gateway", 443, "Nginx (HTTPS)"), ("auth", 8443, "emissor JWT"), ("api-1", 8443, "API (réplica 1)"),
       ("api-2", 8443, "API (réplica 2)"), ("legacy", 8443, "mainframe Java"), ("controlo", 8443, "controlo"),
       ("db-europa", 5432, "PostgreSQL"), ("db-europa-r", 5432, "PostgreSQL"), ("db-america", 5432, "PostgreSQL"),
       ("db-america-r", 5432, "PostgreSQL"), ("db-africa", 5432, "PostgreSQL"), ("db-africa-r", 5432, "PostgreSQL"),
       ("db-central", 5432, "PostgreSQL"), ("db-central-r", 5432, "PostgreSQL"), ("namenode", 9870, "HDFS NameNode"),
       ("datanode-1", 9864, "HDFS DataNode"), ("datanode-2", 9864, "HDFS DataNode")]

eu = socket.gethostname()
dns = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
print(f"Origem: {eu} ({socket.gethostbyname(eu)})   servidor DNS: {', '.join(dns)}\n")
print(f"{'nó':<14}{'IP (DNS)':<15}{'porta':>6}  {'resultado':<22}serviço")
ok = 0
for nome, porta, servico in NOS:
    try:
        ip = socket.gethostbyname(nome)
    except OSError:
        print(f"{nome:<14}{'—':<15}{porta:>6}  {'nome não resolve':<22}{servico}")
        continue
    t = time.perf_counter()
    try:
        socket.create_connection((ip, porta), timeout=2).close()
        res = f"ligação OK ({(time.perf_counter() - t) * 1000:.1f} ms)"
        ok += 1
    except OSError as e:
        res = f"FALHOU ({e.__class__.__name__})"
    print(f"{nome:<14}{ip:<15}{porta:>6}  {res:<22}{servico}")
print(f"\n{ok}/{len(NOS)} nós alcançáveis a partir de {eu}.")
