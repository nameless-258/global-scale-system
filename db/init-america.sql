-- =============================================================================
-- Fragmento AMÉRICA (Nó B) — fragmentação horizontal por região
-- Predicado de fragmentação:  regiao = 'AM'   (contas com prefixo "AM-")
-- Cada conta tem a sua moeda; as conversões usam a tabela replicada taxas_cambio.
-- =============================================================================
\i /docker-entrypoint-initdb.d/schema.inc

INSERT INTO contas (id, titular, owner_username, regiao, moeda, saldo) VALUES
  ('AM-2001', 'Bruno Cossa',    'bruno', 'AM', 'USD',   4000.00),
  ('AM-2002', 'Diego Mondlane', 'diego', 'AM', 'MZN', 180000.00);
