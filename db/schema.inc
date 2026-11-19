-- Esquema comum aos dois fragmentos (cada nó tem a MESMA estrutura,
-- mas apenas as linhas que satisfazem o seu predicado de fragmentação).
--
-- Alocação HÍBRIDA dos dados:
--   * contas / movimentos -> FRAGMENTADAS (cada linha vive num só fragmento)
--   * taxas_cambio        -> REPLICADA TOTALMENTE (cópia igual em todos os nós,
--                            atualizada de forma síncrona com Two-Phase Commit)
-- Cada fragmento tem ainda uma RÉPLICA SÍNCRONA (alta disponibilidade).
--
-- Cifragem em REPOUSO: os dados pessoais (titular, contraparte/IBAN) são
-- cifrados pela aplicação com AES-256-GCM ANTES de chegarem à base de dados.
-- A chave nunca é enviada ao PostgreSQL: quem tiver acesso aos ficheiros da
-- BD, a um backup ou a uma réplica só vê bytes cifrados.

-- Tabela replicada: quantos meticais vale 1 unidade de cada moeda.
-- O metical (MZN) é a moeda de referência da plataforma.
CREATE TABLE taxas_cambio (
    moeda           CHAR(3)       PRIMARY KEY,
    mzn_por_unidade NUMERIC(18,6) NOT NULL CHECK (mzn_por_unidade > 0),
    atualizado_em   TIMESTAMPTZ   NOT NULL DEFAULT now()
);
INSERT INTO taxas_cambio (moeda, mzn_por_unidade) VALUES
  ('MZN',  1.000000),
  ('EUR', 69.500000),
  ('USD', 63.900000);

CREATE TABLE contas (
    id              TEXT PRIMARY KEY,
    titular         TEXT,                 -- só usado na carga inicial; a aplicação
                                          -- cifra-o no 1.º arranque e apaga-o (NULL)
    titular_cifrado BYTEA,                -- AES-256-GCM (nonce || texto cifrado || tag)
    owner_username  TEXT          NOT NULL,
    regiao          CHAR(2)       NOT NULL,
    moeda           CHAR(3)       NOT NULL REFERENCES taxas_cambio(moeda),
    saldo           NUMERIC(18,2) NOT NULL CHECK (saldo >= 0),   -- Consistência (o C de ACID)
    versao          INTEGER       NOT NULL DEFAULT 0,
    atualizado_em   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CHECK (titular IS NOT NULL OR titular_cifrado IS NOT NULL)
);

-- Diário de movimentos (journal). Cada transação distribuída deixa uma linha
-- em CADA fragmento envolvido. A restrição UNIQUE torna as operações
-- idempotentes e impede ataques de repetição (replay).
-- Numa transferência entre moedas, cada lado guarda a taxa usada e o valor
-- equivalente na outra moeda (rasto de auditoria da conversão).
CREATE TABLE movimentos (
    id                  BIGSERIAL PRIMARY KEY,
    tx_id               UUID          NOT NULL,
    conta_id            TEXT          NOT NULL REFERENCES contas(id),
    tipo                TEXT          NOT NULL CHECK (tipo IN
                          ('DEBITO','CREDITO','DEBITO_EXTERNO','COMPENSACAO')),
    valor               NUMERIC(18,2) NOT NULL CHECK (valor > 0),   -- na moeda da conta
    moeda               CHAR(3)       NOT NULL,
    valor_contraparte   NUMERIC(18,2),                              -- na moeda da outra conta
    moeda_contraparte   CHAR(3),
    taxa_aplicada       NUMERIC(18,8),                              -- 1 moeda origem = X moeda destino
    contraparte_cifrada BYTEA         NOT NULL,                     -- conta/IBAN cifrado (AES-256-GCM)
    hash_sha256         CHAR(64)      NOT NULL,
    protocolo           TEXT          NOT NULL,      -- LOCAL | 2PC | SAGA
    no_coordenador      TEXT          NOT NULL,
    criado_em           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (tx_id, conta_id, tipo)
);

CREATE INDEX idx_mov_tx ON movimentos (tx_id);

-- Utilizador aplicacional com privilégios mínimos (princípio do menor privilégio).
-- A palavra-passe é definida a partir de um segredo em 20-segredos.sh.
CREATE ROLE app LOGIN;
GRANT SELECT, UPDATE ON contas TO app;
GRANT SELECT, INSERT ON movimentos TO app;
GRANT SELECT, UPDATE ON taxas_cambio TO app;
GRANT USAGE, SELECT ON SEQUENCE movimentos_id_seq TO app;
