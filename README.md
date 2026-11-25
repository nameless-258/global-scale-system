# Global-Scale System

**Uma plataforma distribuída e resiliente para transações financeiras internacionais**

> **Demonstração online:** https://nameless-258.github.io/global-scale-system/
> É a mesma interface, com um simulador que corre no próprio browser (sem servidores). Serve para ver o sistema e experimentar os cenários de falha sem instalar nada; o sistema real corre com Docker, como descrito abaixo.

Projeto prático do Exame Laboratorial da disciplina **Sistemas Distribuídos e Paralelos**
(Licenciatura em Engenharia e Ciência dos Computadores — ISUTC).



## Componentes

| Serviço | Tecnologia | IP / Porta | Função |
|---|---|---|---|
| `gateway` | Nginx 1.27 | 172.28.0.10 · host **:8443** | Ponto de entrada HTTPS, balanceamento de carga entre as réplicas e **interface gráfica** em `/ui/` |
| `auth` | Python / FastAPI | 172.28.0.11 :8443 | Emissor de tokens **JWT RS256**, com JWKS |
| `api-1`, `api-2` | Python / FastAPI | 172.28.0.21/22 :8443 | Núcleo REST/JSON e coordenadores de **2PC** e **Saga** |
| `db-europa` | PostgreSQL 16 | 172.28.0.41 :5432 | Fragmento A, com as contas `EU-*` |
| `db-america` | PostgreSQL 16 | 172.28.0.42 :5432 | Fragmento B, com as contas `AM-*` |
| `db-africa` | PostgreSQL 16 | 172.28.0.47 :5432 | Fragmento C, com as contas `AF-*` |
| `db-europa-r`, `db-america-r`, `db-africa-r` | PostgreSQL 16 | 172.28.0.43/44/48 :5432 | **Réplicas síncronas** (streaming sobre TLS) de cada fragmento |
| `db-central`, `db-central-r` | PostgreSQL 16 | 172.28.0.49/50 :5432 | **Nó Central**: espelho dos saldos de todas as contas e operações pendentes; caminho alternativo quando uma região perde os dois nós |
| `sentinela` | Python | 172.28.0.45 | Vigia os primários, gere o modo síncrono, faz **failover automático** com *fencing* e reintegra o antigo primário como réplica |
| `controlo` | Python / FastAPI | 172.28.0.46 | Só para o administrador: parar, ligar e matar nós a partir da interface, e ler os seus registos |
| `namenode` | Hadoop HDFS 3.3.6 | 172.28.0.51 · host **:9870** | Metadados do DFS |
| `datanode-1/2` | Hadoop HDFS 3.3.6 | 172.28.0.52/53 | Blocos do DFS, com replicação = 2 |
| `legacy` | **Java 21** | 172.28.0.31 :8443 | "Mainframe" do banco parceiro e das carteiras móveis (M-Pesa, e-Mola, mKesh): XML com assinatura digital |

## Requisitos

- Docker Desktop (Windows ou macOS) ou Docker Engine com Compose v2 (Linux)
- Cerca de 4 GB de RAM livres para o Docker
- Portas livres no anfitrião: `8443`, `8080` e `9870`

## Arranque rápido

```bash
git clone <url-do-repositorio> global-scale-system
cd global-scale-system
docker compose up -d --build        # 1.ª vez: 3–6 min (descarrega imagens)
docker compose ps                   # todos "running" / "healthy"
```

O serviço `certs` gera automaticamente, numa instalação nova, todos os segredos em `./certs`: a CA, os certificados de cada serviço, as chaves JWT e de assinatura, a chave AES de cifragem dos dados e **palavras-passe aleatórias** para a base de dados. Cada serviço só monta a sua pasta (`certs/<serviço>` → `/certs/privado`) e a pasta comum `certs/publico`. Esta pasta **não é versionada**.

> Espere cerca de **60 segundos** depois do arranque para o HDFS sair do *safe mode*. Até lá, os registos de auditoria ficam guardados em memória e são enviados automaticamente quando o HDFS estiver pronto.

Interfaces disponíveis:

- **Interface gráfica: https://localhost:8443/ui/** (ver a secção seguinte).
- **API (Swagger):** https://localhost:8443/docs. O certificado é da CA própria; pode importar `certs/publico/ca.crt` no browser ou aceitar o aviso.
- **HDFS:** http://localhost:9870, em *Datanodes* e em *Utilities → Browse the file system*.

## Interface gráfica

Abra **https://localhost:8443/ui/** (para o browser confiar no certificado, importe `certs/publico/ca.crt` como autoridade de certificação de raiz). No topo está o **mapa da rede ao vivo**: cada nó muda de cor quando fica degradado, em baixo ou vedado, e o administrador pode clicar num nó para o parar, ligar, matar ou ver os seus registos. A autenticação é feita com utilizador e palavra-passe, e a pílula no canto mostra quem está a usar a consola.

**Cliente**

- **Início:** cartões das contas, atalhos e últimos movimentos.
- **Transferir:** para outro cliente do banco (pelo nome, não pelo número da conta), para uma **carteira móvel** (M-Pesa 84/85, e-Mola 86/87, mKesh 82/83, com confirmação do nome do titular antes de enviar) ou para **outro banco** (IBAN). Mostra a conversão de moeda, pede confirmação e emite um comprovativo.
- **Extrato:** débitos, créditos e saldo após cada movimento, com filtro por datas, exportação CSV e impressão.
- **Ataques:** demonstra adulteração (SHA-256), replay e token forjado, todos rejeitados.

**Administrador** (não pode movimentar contas de clientes; o servidor recusa)

- **Clientes e contas:** lista com **filtro por região** (Europa, América, África), criação de clientes e abertura de contas em qualquer região e moeda.
- **Depositar:** credita dinheiro numa conta de cliente.
- **Bases de dados:** lê cada um dos oito nós diretamente (incluindo o Nó Central, com o espelho e as operações pendentes). Mostra o papel (primário ou réplica), o número de contas e movimentos, a posição do WAL e se a réplica está igual ao primário; ao clicar num nó vê as tabelas tal como estão guardadas, com o nome do titular **cifrado** e, ao lado, decifrado pela API.
- **Cenários de falha:** botões que executam os cenários da tabela mais abaixo e explicam o resultado.
- **Coordenador 2PC**, **Ficheiros no HDFS** e **Câmbio**.

## Utilizadores de demonstração

| Utilizador | Password | Papel | Contas |
|---|---|---|---|
| ana | `Ana@2026` | cliente | EU-1001 (EUR), EU-1003 (MZN) |
| carla | `Carla@2026` | cliente | EU-1002 (EUR) |
| bruno | `Bruno@2026` | cliente | AM-2001 (USD) |
| diego | `Diego@2026` | cliente | AM-2002 (MZN) |
| fatima | `Fatima@2026` | cliente | AF-3001 (MZN) |
| thabo | `Thabo@2026` | cliente | AF-3002 (ZAR) |
| admin | `Admin@2026` | admin | gere clientes e depósitos; não movimenta contas |

Os clientes criados pelo administrador ficam guardados no volume `auth-data`.

Carteiras móveis de demonstração no mainframe: 841234567 (Joana Mabunda) e 851112233 (Carlos Tembe) na M-Pesa, 861234567 (Luísa Cumbe) e 871112233 (Rui Macuácua) na e-Mola, 821234567 (Sónia Bila) e 831112233 (Pedro Nhaca) na mKesh. Um número sem carteira é recusado e o dinheiro é devolvido (compensação da Saga).

### Alta disponibilidade e segurança dos dados

- **Réplica síncrona por fragmento:** cada `COMMIT` (e cada `PREPARE TRANSACTION` do 2PC) só é confirmado depois de gravado também na réplica, por isso um failover não perde transações confirmadas (RPO = 0).
- **Failover automático:** a `sentinela` verifica os primários a cada 1 s. À 3.ª falha seguida veda o antigo primário (marca no volume `fencing`) e promove a réplica, em cerca de 5 s. A API liga-se por IP fixo com `target_session_attrs=read-write` aos dois nós e espera até 25 s por um failover em curso, por isso uma transferência feita durante a queda é concluída no novo primário (por exemplo, com o `db-america` em baixo as transferências continuam pelo `db-america-r`).
- **Nó Central (região inteira em baixo):** se os **dois** nós de uma região caírem, as transferências para e a partir dessa região continuam. O `db-central` guarda uma cópia exata dos saldos de todas as contas (publicada depois de cada transação e verificada a cada 2 s por número de versão). Com a região em baixo, a central confirma o titular e o saldo, atualiza o espelho e guarda a operação como pendente; uma transferência com a outra região é um 2PC entre a central e essa região. Quando a região volta, as operações pendentes são aplicadas nela pela ordem em que foram feitas (idempotente, `UNIQUE (tx_id, conta_id, tipo)`), e só depois a região volta a aceitar operações diretas. Se a região **e** a central estiverem em baixo, a transferência é abortada sem movimentar dinheiro. Ver `services/core/app/central.py`.
- **Reintegração:** quando o antigo primário volta, não arranca como primário (sem *split-brain*): copia o estado do novo primário com `pg_basebackup` e passa a ser a sua réplica síncrona. Os papéis são dinâmicos.
- **Recuperação por outra réplica:** o WAL dos coordenadores está num volume partilhado. Se a `api-1` morrer de vez a meio do 2PC, a `api-2` lê a decisão e conclui as transações preparadas.
- **Cifragem em repouso:** os nomes dos titulares e das contrapartes são cifrados na aplicação com **AES-256-GCM** (com o contexto da linha como dados associados). A base de dados e as réplicas só guardam bytes cifrados.
- **Mainframe persistente:** as ordens aceites pelo legado Java ficam num registo em disco e são recarregadas no arranque.

### Moedas e câmbio

Cada conta tem a sua moeda (EUR, USD, MZN ou ZAR). O valor de uma transferência é indicado na moeda da conta de **origem**; o servidor converte-o para a moeda do destino. As taxas ficam na tabela `taxas_cambio`, **replicada** em todos os nós, e o metical é a moeda de referência (1 EUR = 69,50 MZN; 1 USD = 63,90 MZN; 1 ZAR = 3,55 MZN). Alterar uma taxa (`gss taxa-atualizar EUR 70.25`, só para o admin) atualiza todas as réplicas com Two-Phase Commit.

## Utilização com o cliente incluído

O cliente corre num contentor, por isso não precisa de Python instalado no anfitrião:

```bash
alias gss="docker compose run --rm cliente"      # PowerShell: function gss { docker compose run --rm cliente @args }

gss login ana Ana@2026
gss contas                                        # consulta scatter-gather aos 3 fragmentos
gss transferir EU-1001 AM-2001 150                # 2PC com conversão: 150 EUR -> 163,15 USD
gss transferir EU-1001 EU-1003 10                 # transação LOCAL: 10 EUR -> 695 MZN
gss taxas                                         # taxas de câmbio em todas as réplicas
gss transferir EU-1001 AM-2001 10 --adulterar     # integridade: SHA-256 rejeita
gss replay EU-1001 AM-2002 5                      # anti-replay
gss token-falso                                   # JWT forjado -> 401
gss ordem EU-1003 MZ59000100000012345678901 20000 # interoperabilidade Python -> Java (Saga), em MZN
gss login admin Admin@2026
gss totais                                        # saldos por moeda e total em MZN
gss taxa-atualizar EUR 70.25                      # replicação síncrona da taxa (2PC)
gss relatorio-gerar && gss relatorios             # ficheiros no HDFS
gss auditoria                                     # registos de auditoria encadeados
gss admin-2pc                                     # estado do coordenador e transações preparadas
```

Exemplo com `curl`:

```bash
TOKEN=$(curl -s --cacert certs/publico/ca.crt https://localhost:8443/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"ana","password":"Ana@2026"}' | jq -r .access_token)
curl -s --cacert certs/publico/ca.crt https://localhost:8443/contas -H "Authorization: Bearer $TOKEN" | jq
```

## Simulação de falhas (demonstração laboratorial)

O guião completo, passo a passo, está em `bash scripts/demo.sh`. Os cenários principais são estes:

| Cenário | Comando | Resultado esperado |
|---|---|---|
| Primário da BD em baixo | `docker compose stop db-america`, depois `gss transferir EU-1001 AM-2001 20` | A sentinela promove `db-america-r` em ~5 s e a transferência é confirmada nele |
| Falha de rede na fase 1 | `gss transferir EU-1001 AM-2001 50 --falha participante_indisponivel_fase1` | ABORT global e ROLLBACK do participante já preparado |
| Nó cai na fase 2 | `gss transferir EU-1002 AM-2002 100 --falha atraso_fase2` e, durante a espera, `docker compose kill db-america` | `CONFIRMADA_PENDENTE`. Depois de `docker compose start db-america`, a recuperação faz `COMMIT PREPARED` |
| Crash do coordenador | `gss transferir AM-2001 EU-1003 300 --falha crash_coordenador_fase2` | O contentor morre e o Docker reinicia-o. A recuperação lê o WAL e conclui o commit |
| Réplica da API em baixo | `docker compose stop api-1` | O gateway encaminha tudo para a `api-2` |
| Nó em baixo ao atualizar uma taxa | `docker compose stop db-america`, depois `gss taxa-atualizar EUR 99` | ABORT; **nenhuma** réplica é alterada |
| Legado em baixo | `docker compose stop legacy`, depois `gss ordem ...` | A Saga executa a **compensação** (estorno) |
| DataNode em baixo | `docker compose stop datanode-1` | Os ficheiros continuam legíveis, graças à réplica no `datanode-2` |
| Primário morre na fase 2 | `gss transferir EU-1002 AM-2002 100 --falha atraso_fase2` e, durante a espera, `docker compose stop db-america` | A sentinela promove `db-america-r` e a recuperação faz `COMMIT PREPARED` no novo primário |
| Antigo primário volta | `docker compose start db-america` | Fica **vedado** (não arranca como primário), copia o novo primário e volta como **réplica síncrona**: sem *split-brain* |
| Transferir durante a queda | Na interface: "A América perde o servidor principal" | A transferência espera pelo failover e é confirmada no `db-america-r` |
| **Os dois nós da América em baixo** | Na interface: "A América fica sem os dois servidores" | As transferências nos dois sentidos seguem pelo **Nó Central**; quando a América volta, recebe as operações por ordem e o total mantém-se |
| América **e** Nó Central em baixo | Na interface: "A América e o Nó Central em baixo" | Sem caminho alternativo: transferência abortada (HTTP 503), a Europa continua, consulta com resultado parcial |
| Coordenador morre de vez | `gss transferir ... --falha crash_permanente_coordenador` (enviado à `api-1`) | A `api-2` assume e conclui a transação a partir do WAL da `api-1` |

Depois dos cenários de failover, reponha a topologia inicial com `bash scripts/reset-alta-disponibilidade.sh` (no PowerShell: `.\scripts\reset-alta-disponibilidade.ps1`). Todos estes cenários também existem como botões na interface, em **Cenários de falha**.

## Testes automáticos

```bash
docker compose run --rm -T --entrypoint python cliente /scripts/testes.py
```

São 30 testes: autenticação, cifragem, 2PC local e distribuído, as três regiões, conservação do valor, saldo insuficiente, falha na fase 1, autorização (incluindo o administrador impedido de debitar contas de clientes), integridade, replay, mensagens expiradas, taxas replicadas, Saga (sucesso e compensação), carteiras móveis (envio e número inválido), extrato, criação de clientes, contas e depósitos, HDFS, auditoria, **Nó Central** (os dois nós da América desligados a sério, transferências nos dois sentidos e reconciliação), concorrência sem deadlocks e estado das réplicas.

Prova de conectividade entre os nós (DNS interno e ligação TCP a cada serviço):

```bash
docker compose exec -T api-1 python - < scripts/conectividade.py                 # bash
Get-Content scripts/conectividade.py | docker compose exec -T api-1 python -     # PowerShell
```

Para consultar os saldos e as transações preparadas diretamente nos nós:

```bash
docker compose exec db-europa  psql -U postgres -d banco -c "select id, saldo from contas"
docker compose exec db-america psql -U postgres -d banco -c "select gid, prepared from pg_prepared_xacts"
```

## Estrutura do repositório

```
├── docker-compose.yml          Orquestração de todos os nós e da rede
├── nginx/nginx.conf            Gateway HTTPS, balanceamento e interface /ui/
├── ui/                         Interface gráfica (HTML, CSS e JavaScript, sem dependências externas)
├── demo/                       Demonstração no GitHub Pages: a interface + simulador.js (sem servidores)
├── config/hadoop.env           Configuração do HDFS
├── db/                         Esquema das regiões e do Nó Central, TLS e pg_hba do PostgreSQL
├── services/
│   ├── auth/                   Serviço de autenticação (JWT RS256) e gestão de utilizadores
│   ├── core/app/
│   │   ├── main.py             Endpoints REST
│   │   ├── transacoes.py       Coordenador 2PC, Saga e recuperação
│   │   ├── shards.py           Encaminhamento para fragmentos e scatter-gather
│   │   ├── security.py         JWT, SHA-256 e assinatura digital
│   │   ├── cripto.py           Cifragem em repouso AES-256-GCM
│   │   ├── sentinela.py        Vigilância, failover, fencing e reintegração das réplicas
│   │   ├── controlo.py         Controlo dos nós pela interface (só admin)
│   │   ├── central.py          Nó Central: espelho, operações em substituição e reconciliação
│   │   ├── dfs.py              Cliente WebHDFS e auditoria encadeada
│   │   └── legado.py           Adaptador JSON <-> XML para o mainframe
│   └── legacy-java/            Mainframe do banco parceiro (Java 21)
├── scripts/
│   ├── gen-certs.sh            PKI e segredos por serviço
│   ├── cliente.py              Cliente de linha de comandos
│   ├── testes.py               30 testes automáticos
│   ├── verificar_bd.py         Lê cada nó da base de dados pela API
│   ├── conectividade.py        Prova de conectividade entre os nós (DNS + TCP)
│   ├── gerar-demo.py           Gera a demonstração para o GitHub Pages (pasta demo/)
│   ├── reset-alta-disponibilidade.sh  Repõe primários e réplicas (.ps1 para PowerShell)
│   └── demo.sh                 Guião da demonstração (17 passos)
└── docs/arquitetura.(png|svg|dot)
```

## Resolução de problemas

- **`bad interpreter` ou `\r` nos scripts (Windows):** o `.gitattributes` força fins de linha LF. Se copiou os ficheiros sem usar o git, converta-os com `dos2unix db/*.sh scripts/*.sh`.
- **HDFS "Safe mode" ou auditoria pendente:** espere 1 minuto. `gss saude` mostra `dfs_auditoria_pendente`.
- **Mac com Apple Silicon:** a imagem `apache/hadoop:3.3.6` é amd64 e corre por emulação. É mais lenta, mas funciona.
- **Recomeçar do zero:** `docker compose down -v && rm -rf certs && docker compose up -d --build`.
