-- =============================================================================
-- Fragmento ÁFRICA (Nó C) — fragmentação horizontal por região
-- Predicado de fragmentação:  regiao = 'AF'   (contas com prefixo "AF-")
-- Cada conta tem a sua moeda; as conversões usam a tabela replicada taxas_cambio.
-- =============================================================================
\i /docker-entrypoint-initdb.d/schema.inc

INSERT INTO contas (id, titular, owner_username, regiao, moeda, saldo) VALUES
  ('AF-3001', 'Fátima Langa', 'fatima', 'AF', 'MZN', 150000.00),
  ('AF-3002', 'Thabo Nkosi',  'thabo',  'AF', 'ZAR',  25000.00);
