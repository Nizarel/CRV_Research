<#
.SYNOPSIS
    Safely start AKS and Application Gateway together, and prevent the two incidents
    seen on 2026-08-31/09-01: stale Application Gateway backend pools (502) and an
    AKS start landing in a 'Failed' provisioning state.

.DESCRIPTION
    Manually starting AKS and Application Gateway as two independent az commands has
    twice caused an incident:
      1. AKS pod overlay IPs change on every stop/start. The AGIC controller does not
         always pick this up on its own, so Application Gateway keeps routing to dead
         pod IPs and returns 502, even though every pod is Running. The fix is a
         mandatory `kubectl rollout restart deployment/ingress-azure` AFTER both AKS
         and Application Gateway report ready - a step that is easy to forget when
         done by hand.
      2. AKS can land in provisioningState 'Failed' with an Azure-side
         'OverlaymgrReconcileError' after a stop/start cycle. Once Failed, further
         start/stop calls are rejected until 'az aks update' reconciles the cluster.
    This script performs both resources' startup, the AGIC reconcile, and a
    backend-health check as one sequence, so the reconcile step can no longer be
    skipped.

.PARAMETER SkipAgicRestart
    Skip the ingress-azure rollout restart (only if you are certain backend pools are
    already current, e.g. AKS was never actually stopped).

.EXAMPLE
    .\infra\kubernetes\start-environment.ps1
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup  = 'rg-CRV-southcentralus',
    [string]$AksClusterName = 'aks-crv-southcentralus',
    [string]$AppGatewayName = 'agw-crv-southcentralus',
    [switch]$SkipAgicRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AksState {
    az aks show -g $ResourceGroup -n $AksClusterName --query "{power:powerState.code,provisioning:provisioningState}" -o json | ConvertFrom-Json
}

function Get-AgwState {
    az network application-gateway show -g $ResourceGroup -n $AppGatewayName --query "{operationalState:operationalState,provisioningState:provisioningState}" -o json | ConvertFrom-Json
}

function Get-LatestFailure {
    # Surfaces the correlation ID needed for an Azure support case without digging manually.
    az monitor activity-log list -g $ResourceGroup --offset 30m `
        --query "[?contains(resourceId,'$AksClusterName') && status.value=='Failed'] | [0]" -o json | ConvertFrom-Json
}

# ---------------------------------------------------------------------------
# 1. Start AKS and Application Gateway in parallel - they are independent resources.
# ---------------------------------------------------------------------------
Write-Host 'Starting AKS ...' -ForegroundColor Yellow
az aks start -g $ResourceGroup -n $AksClusterName --no-wait

Write-Host 'Starting Application Gateway ...' -ForegroundColor Yellow
az network application-gateway start -g $ResourceGroup -n $AppGatewayName --no-wait

# ---------------------------------------------------------------------------
# 2. Wait for AKS to reach a terminal provisioning state (not just powerState=Running,
#    which flips before the control plane is actually usable).
# ---------------------------------------------------------------------------
Write-Host "`nWaiting for AKS provisioning to complete (this can take 10+ minutes) ..." -ForegroundColor Yellow
$deadline = (Get-Date).AddMinutes(20)
do {
    Start-Sleep -Seconds 20
    $aks = Get-AksState
    Write-Host "  powerState=$($aks.power) provisioningState=$($aks.provisioning)" -ForegroundColor Gray
} while ($aks.provisioning -eq 'Starting' -or $aks.provisioning -eq 'Updating' -and (Get-Date) -lt $deadline)

if ($aks.provisioning -eq 'Failed') {
    Write-Host "`nAKS start failed (provisioningState=Failed). Attempting one reconcile via 'az aks update' ..." -ForegroundColor Yellow
    az aks update -g $ResourceGroup -n $AksClusterName -y
    $aks = Get-AksState
    if ($aks.provisioning -ne 'Succeeded') {
        $failure = Get-LatestFailure
        Write-Error @"
AKS did not recover after reconciliation (provisioningState=$($aks.provisioning)).
Do NOT loop start/stop/update again - each attempt so far has reproduced the same
Azure-side error. Open an Azure support case with this evidence:
  Correlation ID: $($failure.correlationId)
  Operation:      $($failure.operationName.localizedValue)
  Message:        $($failure.properties.statusMessage)
"@
        exit 1
    }
    Write-Host 'Reconciliation succeeded.' -ForegroundColor Green
} elseif ($aks.provisioning -ne 'Succeeded') {
    Write-Error "AKS did not reach 'Succeeded' within the wait window (currently '$($aks.provisioning)'). Re-run this script once it settles; do not stack another start."
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Wait for Application Gateway.
# ---------------------------------------------------------------------------
Write-Host "`nWaiting for Application Gateway ..." -ForegroundColor Yellow
az network application-gateway wait -g $ResourceGroup -n $AppGatewayName --custom "operationalState=='Running'" --interval 15 --timeout 600
$agw = Get-AgwState
if ($agw.operationalState -ne 'Running') {
    Write-Error "Application Gateway did not reach 'Running' (currently '$($agw.operationalState)')."
    exit 1
}
Write-Host "Application Gateway is Running." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4. Mandatory AGIC reconcile - this is the step that was skipped manually and
#    caused the 502 on 2026-08-31/09-01. Both resources must be ready first.
# ---------------------------------------------------------------------------
if (-not $SkipAgicRestart) {
    Write-Host "`nRestarting ingress-azure so Application Gateway backend pools match the new pod IPs ..." -ForegroundColor Yellow
    $nodeRg = (az aks show -g $ResourceGroup -n $AksClusterName --query nodeResourceGroup -o tsv).Trim()
    $zone = (az network private-dns zone list -g $nodeRg --query "[?ends_with(name,'azmk8s.io')].name | [0]" -o tsv).Trim()
    $apiServer = (az network private-dns record-set a list -g $nodeRg -z $zone --query '[0].aRecords[0].ipv4Address' -o tsv).Trim()
    $k = @("--server=https://${apiServer}:443", '--insecure-skip-tls-verify=true')

    kubectl @k get namespace kube-system -o name 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Local TLS-inspection interference has broken direct kubectl before; the control-plane
        # channel is unaffected by it.
        Write-Host "Direct kubectl unreachable; falling back to 'az aks command invoke'." -ForegroundColor Gray
        az aks command invoke -g $ResourceGroup -n $AksClusterName `
            --command "kubectl rollout restart deployment/ingress-azure -n kube-system && kubectl rollout status deployment/ingress-azure -n kube-system --timeout=240s"
    } else {
        kubectl @k rollout restart deployment/ingress-azure -n kube-system
        kubectl @k rollout status deployment/ingress-azure -n kube-system --timeout=240s
    }
}

# ---------------------------------------------------------------------------
# 5. Verify backend health before declaring success.
# ---------------------------------------------------------------------------
Write-Host "`nChecking Application Gateway backend health ..." -ForegroundColor Yellow
$health = @(az network application-gateway show-backend-health -g $ResourceGroup -n $AppGatewayName `
    --query "backendAddressPools[].backendHttpSettingsCollection[].servers[].health" -o tsv)
$unhealthy = @($health | Where-Object { $_ -ne 'Healthy' })

Write-Host "`nFinal state:" -ForegroundColor Cyan
Write-Host "  AKS:              powerState=$($aks.power) provisioningState=$($aks.provisioning)"
Write-Host "  Application GW:   operationalState=$($agw.operationalState) provisioningState=$($agw.provisioningState)"
Write-Host "  Backends healthy: $(@($health | Where-Object { $_ -eq 'Healthy' }).Count) / $($health.Count)"

if ($unhealthy.Count) {
    Write-Error "$($unhealthy.Count) backend(s) are not Healthy. Re-run with default settings (do not pass -SkipAgicRestart) or investigate the affected pods."
    exit 1
}

Write-Host "`nAKS and Application Gateway are up and all backends are healthy." -ForegroundColor Green
