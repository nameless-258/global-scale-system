# =============================================================================
# Repõe o laboratório no estado inicial depois de uma demonstração de failover
# (versão PowerShell para Windows; a versão bash é reset-alta-disponibilidade.sh).
#
# Depois de um failover, a réplica promovida passa a ser o primário e o antigo
# primário fica "vedado" (fencing). Aqui recriamos todos os dados a partir dos
# scripts iniciais. ATENÇÃO: apaga saldos, movimentos e registos de demonstração.
#
# Uso (na pasta do projeto):   powershell -ExecutionPolicy Bypass -File scripts\reset-alta-disponibilidade.ps1
# =============================================================================
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Write-Host ">>> A parar tudo e a apagar volumes (dados, registos, marcas de fencing)..."
docker compose down -v
Write-Host ">>> A arrancar de novo..."
docker compose up -d
Write-Host ">>> Pronto. Aguarde ~60 s e recarregue a consola (https://localhost:8443/ui/)."
