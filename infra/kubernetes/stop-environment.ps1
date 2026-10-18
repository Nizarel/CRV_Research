<#
.SYNOPSIS
    Safely stop AKS and Application Gateway together.

.DESCRIPTION
    Stopping AKS while it is in a 'Failed' provisioning state is rejected by Azure
    ("Run 'az aks update' to reconcile ... Retry after 'Succeeded' provisioning state").
    This happened on 2026-09-01 and blocked a routine end-of-day shutdown. This script
    checks provisioning state first and reconciles automatically before stopping, instead
    of failing partway through.

.EXAMPLE
    .\infra\kubernetes\stop-environment.ps1
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup  = 'rg-CRV-southcentralus',
    [string]$AksClusterName = 'aks-crv-southcentralus',
    [string]$AppGatewayName = 'agw-crv-southcentralus'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AksState {
    $json = az aks show -g $ResourceGroup -n $AksClusterName --query "{power:powerState.code,provisioning:provisioningState}" -o json | ConvertFrom-Json
    return $json
}

Write-Host 'Checking AKS provisioning state ...' -ForegroundColor Cyan
$aks = Get-AksState
Write-Host "  powerState=$($aks.power) provisioningState=$($aks.provisioning)" -ForegroundColor Gray

if ($aks.provisioning -eq 'Failed') {
    # Azure rejects 'stop' outright while Failed - reconcile first or the stop call errors out.
    Write-Host "AKS is in 'Failed' state; stop would be rejected. Reconciling first ..." -ForegroundColor Yellow
    az aks update -g $ResourceGroup -n $AksClusterName -y
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Reconciliation failed. Do not retry blindly - check 'az monitor activity-log list -g $ResourceGroup --offset 30m' for the correlation ID and consider an Azure support case if 'OverlaymgrReconcileError' repeats."
        exit 1
    }
    $aks = Get-AksState
    if ($aks.provisioning -ne 'Succeeded') {
        Write-Error "AKS provisioningState is '$($aks.provisioning)' after reconciliation, not 'Succeeded'. Stopping now would likely fail or leave the cluster in an inconsistent state - stop manually once it settles."
        exit 1
    }
}

Write-Host "`nStopping Application Gateway ..." -ForegroundColor Yellow
az network application-gateway stop -g $ResourceGroup -n $AppGatewayName --no-wait

Write-Host "Stopping AKS ..." -ForegroundColor Yellow
az aks stop -g $ResourceGroup -n $AksClusterName --no-wait

Write-Host "`nWaiting for both to report Stopped ..." -ForegroundColor Yellow
az aks wait -g $ResourceGroup -n $AksClusterName --custom "powerState.code=='Stopped'" --interval 20 --timeout 900
az network application-gateway wait -g $ResourceGroup -n $AppGatewayName --custom "operationalState=='Stopped'" --interval 20 --timeout 900

$aks = Get-AksState
$agw = az network application-gateway show -g $ResourceGroup -n $AppGatewayName --query "{operationalState:operationalState,provisioningState:provisioningState}" -o json | ConvertFrom-Json

Write-Host "`nFinal state:" -ForegroundColor Cyan
Write-Host "  AKS:              powerState=$($aks.power) provisioningState=$($aks.provisioning)"
Write-Host "  Application GW:   operationalState=$($agw.operationalState) provisioningState=$($agw.provisioningState)"

if ($aks.power -ne 'Stopped' -or $agw.operationalState -ne 'Stopped') {
    Write-Error 'One or both resources did not reach Stopped. Check the states above before assuming they are off.'
    exit 1
}

Write-Host "`nBoth resources stopped." -ForegroundColor Green
