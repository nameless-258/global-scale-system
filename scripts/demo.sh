#!/usr/bin/env bash
# =============================================================================
# Guião da DEMONSTRAÇÃO LABORATORIAL (defesa prática)
# Uso (Git Bash ou terminal do Docker Desktop -> Git Bash):  bash scripts/demo.sh
# Pré-requisito: docker compose up -d --build   (e esperar ~60 s pelo HDFS)
# Depois da demonstração do failover (passo 15), repor tudo com:
#   bash scripts/reset-alta-disponibilidade.sh
# =============================================================================
set -u
C="docker compose run --rm -T cliente"
C1="docker compose run --rm -T -e GSS_URL=https://api-1:8443 cliente"   # fala só com a api-1
B="\033[1;94m"; R="\033[1;91m"; G="\033[1;92m"; N="\033[0m"

passo() { echo; echo -e "${B}════════════════════════════════════════════════════════════${N}";
          echo -e "${B}  $1${N}"; echo -e "${B}════════════════════════════════════════════════════════════${N}";
          read -rp "  [ENTER para executar] " _; }
psql_db() { docker compose exec -T "$1" psql -U postgres -d banco -Atc "$2" 2>/dev/null || echo "($1 em baixo)"; }
preparadas() {
  for db in db-europa db-europa-r db-america db-america-r; do
    docker compose exec -T "$db" psql -U postgres -d banco -Atc \
      "select '$db PREPARADA: '||gid from pg_prepared_xacts" 2>/dev/null
  done
}
totais() { $C totais | grep -E 'EUR"|USD"|MZN"|total_equivalente'; }

passo "1. Infraestrutura: contentores e IPs fixos na rede global-net"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
docker network inspect global-scale-system_global-net \
  --format '{{range .Containers}}{{.Name}} -> {{.IPv4Address}}{{"\n"}}{{end}}'

passo "2. Saúde global: primários, réplicas SÍNCRONAS, HDFS e mainframe Java"
$C saude

passo "3. Autenticação distribuída: login no nó emissor (auth) -> JWT RS256"
$C login admin Admin@2026

passo "4. Cifragem em REPOUSO: a API mostra os nomes, a base de dados só tem bytes cifrados"
$C contas | grep -E '"id"|titular|moeda'
psql_db db-europa "select id, titular, encode(substr(titular_cifrado,1,24),'hex')||'...' as titular_cifrado from contas"

passo "5. Taxas de câmbio: tabela REPLICADA em todos os nós"
$C taxas

passo "6. Transferência DISTRIBUÍDA com conversão: 150 EUR (EU-1001) -> USD (AM-2001) [2PC]"
totais
$C transferir EU-1001 AM-2001 150
totais

passo "7. Segurança: mensagem adulterada, replay e token JWT forjado"
$C transferir EU-1001 AM-2001 10 --adulterar
$C replay EU-1001 EU-1002 5
$C token-falso

passo "8. Falha de rede na fase 1 -> ABORT global (atomicidade)"
$C transferir EU-1001 AM-2001 50 --falha participante_indisponivel_fase1 | grep -E 'HTTP|DECIS|ROLLBACK'

passo "9. CRASH do coordenador na fase 2 (o Docker reinicia-o e ele recupera sozinho)"
totais
$C transferir AM-2002 EU-1003 3000 --falha crash_coordenador_fase2
preparadas
sleep 15; docker compose logs --since 1m api-1 api-2 | grep -i "recupera"; totais

passo "10. Coordenador PERDIDO de vez: a api-2 conclui as transações da api-1"
$C1 transferir AM-2002 EU-1003 2000 --falha crash_permanente_coordenador
echo -e "${R}>>> a api-1 não volta (simula uma máquina perdida)${N}"; preparadas
sleep 20; docker compose logs --since 1m api-2 | grep -i "assumiu"; preparadas; totais
echo -e "${G}>>> reativar a api-1${N}"
docker compose exec -T api-2 rm -f /data/api-1.desativado; docker compose restart api-1 >/dev/null

passo "11. Réplica da API em baixo: o gateway encaminha tudo para a outra"
docker compose stop api-1
for i in 1 2 3; do $C transferir EU-1001 EU-1003 1 | grep HTTP; done
docker compose start api-1

passo "12. Interoperabilidade: Python (JSON) -> Java (XML assinado) [SAGA], em meticais"
$C login ana Ana@2026 >/dev/null
$C ordem EU-1003 MZ59000100000012345678901 20000
docker compose logs --tail 2 legacy
echo -e "${R}>>> legado em baixo -> compensação${N}"
docker compose stop legacy; $C ordem EU-1001 MZ59000100000012345678901 70 | grep -E '"estado"|SAGA'
docker compose start legacy; sleep 3
echo -e "${G}>>> o mainframe reiniciado lembra-se das ordens (registo persistente):${N}"
docker compose logs --tail 3 legacy | grep -i carregado

passo "13. DFS (HDFS): relatório escrito por uma réplica, lido por outra; DataNode em baixo"
$C login admin Admin@2026 >/dev/null
$C relatorio-gerar; $C relatorios | grep -E 'nome|replicacao' | head -4
docker compose stop datanode-1; $C relatorios | grep -E 'HTTP|nome' | head -2; docker compose start datanode-1
echo "Interface do HDFS: http://localhost:9870"

passo "14. Auditoria à prova de adulteração (cadeia de hashes SHA-256 no HDFS)"
$C auditoria | head -20

passo "15. ALTA DISPONIBILIDADE: o PRIMÁRIO db-america morre a meio de uma transação 2PC"
totais
$C transferir EU-1002 AM-2002 100 --falha atraso_fase2 &
sleep 7; echo -e "${R}>>> docker compose stop db-america (primário do fragmento B)${N}"
docker compose stop db-america; wait
sleep 8; docker compose logs --since 1m sentinela
preparadas
sleep 15; docker compose logs --since 1m api-1 api-2 | grep -i "recupera" | tail -3
$C saude | grep -A4 '"db-america'
echo -e "${G}>>> a América continua a funcionar no novo primário (db-america-r):${N}"
$C transferir AM-2002 EU-1003 500 | grep -E 'HTTP|"estado"'
totais

passo "16. FENCING: o antigo primário volta, mas recusa-se a ser primário (sem split-brain)"
docker compose start db-america; sleep 5; docker compose logs --tail 3 db-america

passo "17. Testes automáticos de aceitação"
docker compose run --rm -T --entrypoint python cliente /scripts/testes.py

echo -e "\n${G}Demonstração concluída. Para repor o estado inicial: bash scripts/reset-alta-disponibilidade.sh${N}"
