#!/usr/bin/env bash
# =============================================================================
# Guião da DEMONSTRAÇÃO LABORATORIAL (defesa prática)
# Uso:  bash scripts/demo.sh          (prima ENTER entre passos)
# Pré-requisito: docker compose up -d --build   (e esperar ~60 s pelo HDFS)
# =============================================================================
set -u
C="docker compose run --rm -T cliente"
B="\033[1;94m"; R="\033[1;91m"; G="\033[1;92m"; N="\033[0m"

passo() { echo; echo -e "${B}════════════════════════════════════════════════════════════${N}";
          echo -e "${B}  $1${N}"; echo -e "${B}════════════════════════════════════════════════════════════${N}";
          read -rp "  [ENTER para executar] " _; }
psql_saldos() {
  for db in db-europa db-america; do
    docker compose exec -T "$db" psql -U postgres -d banco -Atc \
      "select '$db: '||string_agg(id||'='||saldo, '  ' order by id) from contas" 2>/dev/null \
      || echo "$db: (em baixo)"
  done
}
preparadas() {
  for db in db-europa db-america; do
    docker compose exec -T "$db" psql -U postgres -d banco -Atc \
      "select '$db PREPARADA: '||gid||' desde '||prepared from pg_prepared_xacts" 2>/dev/null
  done
}

passo "1. Estado da infraestrutura (contentores, IPs, rede)"
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
docker network inspect global-scale-system_global-net \
  --format '{{range .Containers}}{{.Name}} -> {{.IPv4Address}}{{"\n"}}{{end}}'

passo "2. Saúde global (fragmentos, HDFS, legado Java)"
$C saude

passo "3. Autenticação distribuída: login no nó emissor (auth) -> JWT RS256"
$C login ana Ana@2026

passo "4. Consulta global scatter-gather às contas da Ana"
$C contas

passo "5. Transferência DISTRIBUÍDA EU-1001 -> AM-2001 (Two-Phase Commit)"
psql_saldos
$C transferir EU-1001 AM-2001 150
psql_saldos

passo "6. Segurança: mensagem ADULTERADA (SHA-256 não confere)"
$C transferir EU-1001 AM-2001 10 --adulterar

passo "7. Segurança: ataque de REPLAY"
$C replay EU-1001 AM-2002 5

passo "8. Segurança: token JWT FORJADO (papel=admin) e pedido sem token"
$C token-falso; $C sem-token

passo "9. FALHA: participante inacessível na fase 1 -> ABORT global (atomicidade)"
$C transferir EU-1001 AM-2001 50 --falha participante_indisponivel_fase1
psql_saldos

passo "10. FALHA REAL: derrubar o nó db-america"
docker compose stop db-america
$C transferir EU-1001 AM-2001 20
echo -e "${G}A Europa continua a operar (transferência local):${N}"
$C transferir EU-1001 EU-1003 5
$C login admin Admin@2026 >/dev/null; $C contas
docker compose start db-america; sleep 4

passo "11. FALHA na FASE 2: db-america cai DEPOIS da decisão COMMIT"
echo "A transferência espera 20s antes da fase 2; durante a espera derrubamos db-america."
$C transferir EU-1002 AM-2002 100 --falha atraso_fase2 &
sleep 6; echo -e "${R}>>> docker compose kill db-america${N}"; docker compose kill db-america
wait
preparadas; psql_saldos
echo -e "${G}>>> A religar db-america — a recuperação automática do coordenador conclui o COMMIT${N}"
docker compose start db-america; sleep 15
preparadas; psql_saldos
docker compose logs --since 1m api-1 api-2 | grep -i recupera

passo "12. FALHA do COORDENADOR: o processo api-X morre a meio da fase 2"
$C transferir AM-2001 EU-1003 300 --falha crash_coordenador_fase2
preparadas; psql_saldos
echo -e "${G}>>> O Docker reinicia o contentor (restart: unless-stopped) e a recuperação lê o WAL${N}"
sleep 12; docker compose logs --since 1m api-1 api-2 | grep -i recupera; preparadas; psql_saldos

passo "13. Resiliência da camada aplicacional: derrubar api-1"
docker compose stop api-1
$C login ana Ana@2026 >/dev/null
for i in 1 2 3; do $C transferir EU-1001 EU-1003 1 | head -n 1; done
docker compose start api-1

passo "14. Interoperabilidade: ordem interbancária Python(JSON) -> Java(XML assinado) [SAGA]"
$C ordem EU-1001 MZ59000100000012345678901 200
docker compose logs --tail 3 legacy

passo "15. SAGA com compensação: IBAN rejeitado e legado em baixo"
$C ordem EU-1001 XX12 50
docker compose stop legacy; $C ordem EU-1001 MZ59000100000012345678901 70; docker compose start legacy

passo "16. DFS (HDFS): relatório escrito por uma réplica e lido por outra"
$C login admin Admin@2026 >/dev/null
$C relatorio-gerar; $C relatorios
echo "Ver também: http://localhost:9870  (Utilities > Browse the file system)"

passo "17. DFS: derrubar um DataNode — os ficheiros continuam acessíveis (replicação=2)"
docker compose stop datanode-1; sleep 3; $C relatorios; docker compose start datanode-1

passo "18. Auditoria à prova de adulteração (cadeia de hashes SHA-256 no HDFS)"
$C auditoria
echo "Verifique um ficheiro com:  $C auditoria-verificar /auditoria/api-1/<ficheiro>.log"

echo -e "\n${G}Demonstração concluída.${N}"
