-- =============================================================================
-- NÓ CENTRAL — caminho alternativo quando uma região inteira fica em baixo
--
-- Não é um fragmento: não é "dono" de nenhuma conta. Guarda:
--   * espelho     : cópia do saldo de TODAS as contas das três regiões, atualizada
--                   depois de cada transação confirmada (e verificada pelo
--                   sincronizador a cada 2 s, por número de versão);
--   * pendentes   : operações autorizadas pela central EM NOME de uma região que
--                   está em baixo (os dois nós). São aplicadas nessa região, pela
--                   ordem em que foram feitas, assim que ela voltar (reconciliação);
--   * taxas_cambio: cópia da tabela replicada, para a central converter moedas
--                   sozinha quando as duas pontas estão em baixo.
-- Tal como as regiões, a central tem uma réplica síncrona (db-central-r) e failover.
-- =============================================================================
CREATE TABLE taxas_cambio (
    moeda           CHAR(3)       PRIMARY KEY,
    mzn_por_unidade NUMERIC(18,6) NOT NULL CHECK (mzn_por_unidade > 0),
    atualizado_em   TIMESTAMPTZ   NOT NULL DEFAULT now()
);
INSERT INTO taxas_cambio (moeda, mzn_por_unidade) VALUES
  ('MZN', 1.000000), ('EUR', 69.500000), ('USD', 63.900000), ('ZAR', 3.550000);

CREATE TABLE espelho (
    conta           TEXT          PRIMARY KEY,
    regiao          CHAR(2)       NOT NULL,
    moeda           CHAR(3)       NOT NULL REFERENCES taxas_cambio(moeda),
    owner_username  TEXT          NOT NULL,
    titular_cifrado BYTEA,                       -- AES-256-GCM, o mesmo valor da região
    saldo           NUMERIC(18,2) NOT NULL CHECK (saldo >= 0),
    versao          INTEGER       NOT NULL,      -- versão da linha na região (ordena as atualizações)
    atualizado_em   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE pendentes (
    id                  BIGSERIAL     PRIMARY KEY,   -- ordem de aplicação na região
    tx_id               UUID          NOT NULL,
    conta               TEXT          NOT NULL REFERENCES espelho(conta),
    regiao              CHAR(2)       NOT NULL,
    tipo                TEXT          NOT NULL CHECK (tipo IN
                          ('DEBITO','CREDITO','DEBITO_EXTERNO','COMPENSACAO','DEPOSITO')),
    valor               NUMERIC(18,2) NOT NULL CHECK (valor > 0),
    moeda               CHAR(3)       NOT NULL,
    valor_contraparte   NUMERIC(18,2),
    moeda_contraparte   CHAR(3),
    taxa_aplicada       NUMERIC(18,8),
    contraparte_cifrada BYTEA         NOT NULL,
    hash_sha256         CHAR(64)      NOT NULL,
    no_coordenador      TEXT          NOT NULL,
    criado_em           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    estado              TEXT          NOT NULL DEFAULT 'PENDENTE'
                          CHECK (estado IN ('PENDENTE', 'APLICADA')),
    aplicada_em         TIMESTAMPTZ,
    aplicada_por        TEXT,
    UNIQUE (tx_id, conta, tipo)                       -- idempotência / anti-replay
);
CREATE INDEX idx_pendentes_regiao ON pendentes (regiao, estado, id);

CREATE ROLE app LOGIN;
GRANT SELECT, INSERT, UPDATE ON espelho, pendentes TO app;
GRANT SELECT, UPDATE ON taxas_cambio TO app;
GRANT USAGE, SELECT ON SEQUENCE pendentes_id_seq TO app;
