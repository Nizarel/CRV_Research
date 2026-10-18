<#
.SYNOPSIS
    Render and apply the exploration stack manifests to AKS.

.DESCRIPTION
    The exploration manifests carry ${VAR} placeholders that a bare `kubectl apply -f`
    writes to the cluster verbatim, which is how a literal
    "${APPLICATIONINSIGHTS_CONNECTION_STRING}" once reached the browser and blanked the SPA.
    This script resolves every placeholder from live Azure resources, refuses to apply a
    manifest that still contains one, and restarts deployments whose config changed.

    Image tags are per component and are NOT derived from the git SHA: exploration images
    are published with descriptive date tags (e.g. obs-browser-telemetry-relay-20260829-191500).
    When a tag is omitted the value currently running in the cluster is reused.

.PARAMETER AgentImageTag
    Tag for the exploration-agent image. Defaults to the running deployment's tag.

.PARAMETER WebImageTag
    Tag for the exploration-web image. Defaults to the running deployment's tag.

.PARAMETER SkipRestart
    Skip the post-apply rollout restart. ConfigMaps are mounted with subPath, which never
    hot-reloads, so config-only changes need a restart to take effect.

.EXAMPLE
    # Re-apply the current state (repairs placeholder damage without changing versions):
    .\infra\kubernetes\deploy-exploration.ps1

    # Roll out a new web build:
    .\infra\kubernetes\deploy-exploration.ps1 -WebImageTag obs-fix-20260901-101500
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ResourceGroup  = 'rg-CRV-southcentralus',
    [string]$AksClusterName = 'aks-crv-southcentralus',
    [string]$Namespace      = 'crv-exploration',
    [string]$AgentImageTag  = '',
    [string]$WebImageTag    = '',
    [switch]$SkipRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repoRoot

function Get-RequiredValue([string]$label, [scriptblock]$query) {
    $value = & $query 2>$null
    if ($value) { $value = "$value".Trim() }
    if (-not $value -or $value -eq 'null' -or $value -eq 'None') {
        Write-Error "Could not resolve '$label' from Azure. Fix access or pass an explicit override."
        exit 1
    }
    return $value
}

# The API server private IP moves across aks stop/start and the old one can end up on a
# worker node, so it is resolved fresh rather than hardcoded.
function Get-AksApiServerIp {
    $nodeRg = Get-RequiredValue 'node resource group' {
        az aks show -g $ResourceGroup -n $AksClusterName --query nodeResourceGroup -o tsv }
    $zone = Get-RequiredValue 'privatelink azmk8s.io zone' {
        az network private-dns zone list -g $nodeRg --query "[?ends_with(name, 'azmk8s.io')].name | [0]" -o tsv }
    return Get-RequiredValue 'API server A record' {
        az network private-dns record-set a list -g $nodeRg -z $zone --query '[0].aRecords[0].ipv4Address' -o tsv }
}

function Get-IdentityClientId([string]$identityName) {
    return Get-RequiredValue "client id for $identityName" {
        az identity show -g $ResourceGroup -n $identityName --query clientId -o tsv }
}

Write-Host 'Resolving Azure values ...' -ForegroundColor Yellow

$acr = (Get-RequiredValue 'ACR login server' {
    az acr list -g $ResourceGroup --query '[0].loginServer' -o tsv }).TrimEnd('/')

$appInsights = Get-RequiredValue 'App Insights connection string' {
    az monitor app-insights component show -g $ResourceGroup --query '[0].connectionString' -o tsv }

$substitutions = @{
    AZURE_CONTAINER_REGISTRY_ENDPOINT   = $acr
    APPLICATIONINSIGHTS_CONNECTION_STRING = $appInsights
    EXPLORATION_IDENTITY_CLIENT_ID      = Get-IdentityClientId 'id-crv-exploration'
    EXPL_AGENT_IDENTITY_CLIENT_ID       = Get-IdentityClientId 'id-crv-expl-agent'
    EXPL_AGENT_IDENTITY_NAME            = 'id-crv-expl-agent'
    AZURE_STORAGE_ACCOUNT               = Get-RequiredValue 'storage account' {
        az storage account list -g $ResourceGroup --query '[].name | [0]' -o tsv }
    AZURE_SEARCH_SERVICE                = Get-RequiredValue 'search service' {
        az search service list -g $ResourceGroup --query '[].name | [0]' -o tsv }
    AZURE_OPENAI_SERVICE                = Get-RequiredValue 'AI services account' {
        az cognitiveservices account list -g $ResourceGroup --query "[?kind=='AIServices' && starts_with(name, 'cog-')].name | [0]" -o tsv }
    AZURE_POSTGRES_HOST                 = Get-RequiredValue 'postgres host' {
        az postgres flexible-server list -g $ResourceGroup --query '[].fullyQualifiedDomainName | [0]' -o tsv }
}

$apiServer = Get-AksApiServerIp
$kubectlArgs = @("--server=https://${apiServer}:443", '--insecure-skip-tls-verify=true')
Write-Host "AKS API server: $apiServer" -ForegroundColor Cyan

kubectl @kubectlArgs get namespace $Namespace -o name 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error @"
Cannot reach the AKS API server at $apiServer.
  - cluster stopped?  az aks show -g $ResourceGroup -n $AksClusterName --query powerState.code
  - not on the VPN, or local TLS inspection (e.g. Global Secure Access) is resetting the
    handshake. Symptom: 'wsarecv: An existing connection was forcibly closed'.
This script needs direct kubectl. As a fallback, apply the rendered manifests through the
control plane instead: az aks command invoke -g $ResourceGroup -n $AksClusterName --command
"kubectl apply -f <file>" --file <file>
"@
    exit 1
}

function Get-RunningImageTag([string]$deployment, [string]$override) {
    if ($override) { return $override }
    $image = Get-RequiredValue "running image for $deployment" {
        kubectl @kubectlArgs get deployment $deployment -n $Namespace `
            -o jsonpath='{.spec.template.spec.containers[0].image}' }
    if ($image -notmatch ':([^:]+)$') {
        Write-Error "Deployment '$deployment' runs an untagged or digest-pinned image ($image). Pass the tag explicitly."
        exit 1
    }
    return $Matches[1]
}

$AgentImageTag = Get-RunningImageTag 'exploration-agent' $AgentImageTag
$WebImageTag   = Get-RunningImageTag 'exploration-web'   $WebImageTag
Write-Host "Agent tag: $AgentImageTag" -ForegroundColor Cyan
Write-Host "Web tag:   $WebImageTag"   -ForegroundColor Cyan

# IMAGE_TAG differs per manifest, so each file is rendered with its own value.
$manifests = @(
    @{ Path = 'infra/kubernetes/exploration-namespace.yaml'; Tag = ''             }
    @{ Path = 'infra/kubernetes/exploration-agent.yaml';     Tag = $AgentImageTag }
    @{ Path = 'infra/kubernetes/exploration-web.yaml';       Tag = $WebImageTag   }
)

$rendered = @()
foreach ($manifest in $manifests) {
    $content = Get-Content (Join-Path $repoRoot $manifest.Path) -Raw
    foreach ($key in $substitutions.Keys) {
        $content = $content -replace ('\$\{' + $key + '\}'), $substitutions[$key]
    }
    if ($manifest.Tag) {
        $content = $content -replace '\$\{IMAGE_TAG\}', $manifest.Tag
    }

    # A surviving ${VAR} would be applied verbatim and become a literal in production.
    $leftover = [regex]::Matches($content, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') |
        ForEach-Object { $_.Value } | Sort-Object -Unique
    if ($leftover) {
        Write-Error "$($manifest.Path): unsubstituted placeholders remain: $($leftover -join ', ')."
        exit 1
    }

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("expl-" + (Split-Path $manifest.Path -Leaf))
    # Rendering is not the operation -WhatIf gates; only the cluster apply below is.
    Set-Content $tmp -Value $content -Encoding UTF8 -WhatIf:$false -Confirm:$false
    $rendered += $tmp
}

Write-Host "`nValidating against the API server (dry-run) ..." -ForegroundColor Yellow
foreach ($file in $rendered) {
    kubectl @kubectlArgs apply --dry-run=server -f $file
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Dry-run failed for $file - nothing was applied."
        $rendered | Remove-Item -ErrorAction SilentlyContinue -WhatIf:$false
        exit 1
    }
}

if (-not $PSCmdlet.ShouldProcess($Namespace, 'apply exploration manifests')) {
    $rendered | Remove-Item -ErrorAction SilentlyContinue -WhatIf:$false
    return
}

Write-Host "`nApplying ..." -ForegroundColor Yellow
foreach ($file in $rendered) {
    kubectl @kubectlArgs apply -f $file
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Apply failed for $file."
        $rendered | Remove-Item -ErrorAction SilentlyContinue -WhatIf:$false
        exit 1
    }
}
$rendered | Remove-Item -ErrorAction SilentlyContinue -WhatIf:$false

if (-not $SkipRestart) {
    # config.js is mounted with subPath, which kubelet never refreshes in a running pod.
    Write-Host "`nRestarting deployments to pick up config changes ..." -ForegroundColor Yellow
    foreach ($deployment in @('exploration-agent', 'exploration-web')) {
        kubectl @kubectlArgs rollout restart "deployment/$deployment" -n $Namespace
        kubectl @kubectlArgs rollout status  "deployment/$deployment" -n $Namespace --timeout=300s
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Rollout failed for $deployment. Inspect the pod logs."
            exit 1
        }
    }
}

Write-Host "`nPod status:" -ForegroundColor Cyan
kubectl @kubectlArgs get pods -n $Namespace -o wide

Write-Host "`nDone. Pod IPs changed, so reconcile Application Gateway if it was stopped:" -ForegroundColor Green
Write-Host "  kubectl $($kubectlArgs -join ' ') rollout restart deployment/ingress-azure -n kube-system"
Write-Host "  az network application-gateway show-backend-health -g $ResourceGroup -n agw-crv-southcentralus --query `"backendAddressPools[].backendHttpSettingsCollection[].servers[].{address:address,health:health}`" -o table"
