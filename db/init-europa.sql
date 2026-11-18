-- =============================================================================
-- Fragmento EUROPA (Nó A) — fragmentação horizontal por região
-- Predicado de fragmentação:  regiao = 'EU'   (contas com prefixo "EU-")
-- Cada conta tem a sua moeda; as conversões usam a tabela replicada taxas_cambio.
-- =============================================================================
\i /docker-entrypoint-initdb.d/schema.inc

INSERT INTO contas (id, titular, owner_username, regiao, moeda, saldo) VALUES
  ('EU-1001', 'Ana Machava',   'ana',   'EU', 'EUR',   5000.00),
  ('EU-1002', 'Carla Sitoe',   'carla', 'EU', 'EUR',   3000.00),
  ('EU-1003', 'Ana Machava',   'ana',   'EU', 'MZN', 250000.00);
