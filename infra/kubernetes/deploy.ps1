<#
.SYNOPSIS
    Build, push, and deploy the backend and web images to AKS.

.DESCRIPTION
    Resolves infrastructure values from the Bicep deployment outputs (or accepts them
    as parameters), builds both container images for linux/amd64, pushes to the private
    ACR, substitutes template variables in crv.yaml, and applies the manifest to
    the crv namespace.

.PARAMETER ResourceGroup
    Azure resource group containing the deployment.  Defaults to
    "rg-CRV-southcentralus".

.PARAMETER DeploymentName
    Bicep deployment name to query for outputs.  Defaults to the most recent successful
    deployment in the resource group.

.PARAMETER ImageTag
    Container image tag.  Defaults to the short git SHA of the current HEAD.

.PARAMETER AcrLoginServer
    Override the ACR login server (e.g. "cracrcrv12345678.azurecr.io").
    When omitted the value is read from Bicep output AZURE_CONTAINER_REGISTRY_ENDPOINT.

.PARAMETER AksClusterName
    Override the AKS cluster name.
    When omitted the value is read from Bicep output AKS_CLUSTER_NAME.

.PARAMETER WorkloadIdentityClientId
    Override AKS_WORKLOAD_IDENTITY_CLIENT_ID.
    When omitted the value is read from the Bicep output.

.PARAMETER SkipBuild
    Skip docker build/push steps (re-apply an already-pushed tag).

.PARAMETER SkipApply
    Skip kubectl apply (build and push only).

.PARAMETER RequireGate
    Refuse to deploy unless a fetched benchmark result under Docs/evidence/agent-benchmark/ passed
    its promotion gates (run-benchmark.ps1 -Gate) for this exact image tag, e.g. on a canary.

.EXAMPLE
    # Standard deploy from the repo root:
    .\infra\kubernetes\deploy.ps1

    # Re-apply a previously-pushed tag without rebuilding:
    .\infra\kubernetes\deploy.ps1 -SkipBuild -ImageTag abc1234

    # Promote a tag that passed the gates on a canary:
    .\infra\kubernetes\deploy.ps1 -SkipBuild -ImageTag abc1234 -RequireGate
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup     = 'rg-CRV-southcentralus',
    [string]$DeploymentName    = '',
    [string]$ImageTag          = '',
    [string]$AcrLoginServer    = '',
    [string]$AksClusterName    = '',
    [string]$WorkloadIdentityClientId = '',
    [string]$StorageAccount    = '',
    [string]$SearchService     = '',
    [string]$OpenAiService     = '',
    [string]$PostgresHost      = '',
    [string]$PostgresUser      = 'id-crv-aks-workload',
    [string]$AppInsightsConnectionString = '',
    [switch]$SkipBuild,
    [switch]$SkipApply,
    [switch]$RequireGate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 0. Ensure we are running from the repo root.
# ---------------------------------------------------------------------------
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $repoRoot 'Dockerfile.backend'))) {
    Write-Error "Run this script from the repository root, or ensure Dockerfile.backend exists at: $repoRoot"
    exit 1
}
Set-Location $repoRoot
Write-Host "Working directory: $repoRoot" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 1. Resolve image tag from git SHA if not supplied.
# ---------------------------------------------------------------------------
if (-not $ImageTag) {
    $ImageTag = (git rev-parse --short HEAD 2>$null).Trim()
    if (-not $ImageTag) {
        Write-Error "Could not determine git SHA.  Pass -ImageTag explicitly."
        exit 1
    }
}
Write-Host "Image tag: $ImageTag" -ForegroundColor Cyan

if ($RequireGate) {
    # Evidence of a passing gate for this exact tag (APP_VERSION reported by /health).
    $evidenceRoot = Join-Path $repoRoot 'Docs\evidence\agent-benchmark'
    $passing = @(Get-ChildItem $evidenceRoot -Recurse -Filter 'results-*.json' -ErrorAction SilentlyContinue |
        Where-Object {
            $result = Get-Content $_.FullName -Raw | ConvertFrom-Json -AsHashtable -Depth 64
            $result -is [hashtable] -and $result['gate'] -and $result['release'] -and
                $result['gate']['status'] -eq 'pass' -and $result['release']['app_version'] -eq $ImageTag
        })
    if (-not $passing) {
        Write-Error "No passing promotion gate for image tag '$ImageTag'. Run run-benchmark.ps1 -Gate against it (e.g. on a canary) and fetch the results first."
        exit 1
    }
    Write-Host "Promotion gate passed: $($passing[0].FullName)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 2. Resolve infra values from Bicep deployment outputs (if not overridden).
# ---------------------------------------------------------------------------
function Get-BicepOutput([string]$outputName, [string]$override) {
    if ($override) { return $override }
    Write-Host "  Looking up '$outputName' from Azure resources in '$ResourceGroup' ..." -ForegroundColor Gray

    # NOTE: main.bicep is a subscription-scope deployment, so its outputs are not
    # available via `az deployment group show`.  We look up live resource values directly.
    switch ($outputName) {
        'AZURE_CONTAINER_REGISTRY_ENDPOINT' {
            $value = (az acr list -g $ResourceGroup --query '[0].loginServer' -o tsv 2>$null)
        }
        'AKS_CLUSTER_NAME' {
            $value = (az aks list -g $ResourceGroup --query '[0].name' -o tsv 2>$null)
        }
        'AKS_WORKLOAD_IDENTITY_CLIENT_ID' {
            # The AKS workload identity follows the naming convention *aks-workload*
            $value = (az identity list -g $ResourceGroup `
                --query "[?contains(name,'aks-workload')].clientId | [0]" -o tsv 2>$null)
        }
        default {
            Write-Error "Unknown output '$outputName'. Pass the override parameter explicitly."
            exit 1
        }
    }

    if ($value) { $value = "$value".Trim() }
    if (-not $value -or $value -eq 'null' -or $value -eq 'None') {
        Write-Error "Could not resolve '$outputName' from Azure resources. Pass the override parameter explicitly."
        exit 1
    }
    return $value
}

# The API server private IP changes across `az aks stop`/`az aks start`, and the previous
# value can be reassigned to a worker node, so never cache or hardcode it.
function Get-AksApiServerIp([string]$clusterName) {
    $nodeRg = (az aks show -g $ResourceGroup -n $clusterName --query nodeResourceGroup -o tsv 2>$null)
    if ($nodeRg) { $nodeRg = "$nodeRg".Trim() }
    if (-not $nodeRg) {
        Write-Error "Could not resolve the node resource group for AKS cluster '$clusterName'."
        exit 1
    }

    $zone = (az network private-dns zone list -g $nodeRg `
        --query "[?ends_with(name, 'azmk8s.io')].name | [0]" -o tsv 2>$null)
    if ($zone) { $zone = "$zone".Trim() }
    if (-not $zone) {
        Write-Error "Could not find the privatelink azmk8s.io DNS zone in '$nodeRg'."
        exit 1
    }

    $ip = (az network private-dns record-set a list -g $nodeRg -z $zone `
        --query '[0].aRecords[0].ipv4Address' -o tsv 2>$null)
    if ($ip) { $ip = "$ip".Trim() }
    if (-not $ip) {
        Write-Error "Could not read the API server A record from zone '$zone'."
        exit 1
    }

    Write-Host "  AKS API server: $ip (from $zone)" -ForegroundColor Gray
    return $ip
}

function Get-AgentRuntimeValue([string]$settingName, [string]$override) {
    if ($override) { return $override }
    Write-Host "  Looking up '$settingName' from Azure resources in '$ResourceGroup' ..." -ForegroundColor Gray

    switch ($settingName) {
        'AZURE_STORAGE_ACCOUNT' {
            $value = az storage account list -g $ResourceGroup --query '[].name | [0]' -o tsv 2>$null
        }
        'AZURE_SEARCH_SERVICE' {
            $value = az search service list -g $ResourceGroup --query '[].name | [0]' -o tsv 2>$null
        }
        'AZURE_OPENAI_SERVICE' {
            $value = az cognitiveservices account list -g $ResourceGroup `
                --query "[?kind=='AIServices' && starts_with(name, 'cog-')].name | [0]" -o tsv 2>$null
        }
        'AZURE_POSTGRES_HOST' {
            $value = az postgres flexible-server list -g $ResourceGroup `
                --query '[].fullyQualifiedDomainName | [0]' -o tsv 2>$null
        }
        'AKS_WORKLOAD_IDENTITY_NAME' {
            $value = 'id-crv-aks-workload'
        }
        'APPLICATIONINSIGHTS_CONNECTION_STRING' {
            $value = az monitor app-insights component show -g $ResourceGroup `
                --query "[?starts_with(name, 'appi-')].connectionString | [0]" -o tsv 2>$null
        }
        default {
            Write-Error "Unknown runtime setting '$settingName'. Pass an explicit override."
            exit 1
        }
    }

    if ($value) { $value = "$value".Trim() }
    if (-not $value -or $value -eq 'null' -or $value -eq 'None') {
        Write-Error "Could not resolve '$settingName' from Azure resources. Pass the matching override parameter explicitly."
        exit 1
    }
    return $value
}

if (-not $SkipBuild) {
    # Needed for the private-ACR local build and push path.
    $AcrLoginServer = Get-BicepOutput 'AZURE_CONTAINER_REGISTRY_ENDPOINT' $AcrLoginServer
} elseif (-not $AcrLoginServer) {
    $AcrLoginServer = Get-BicepOutput 'AZURE_CONTAINER_REGISTRY_ENDPOINT' ''
}

if (-not $SkipApply) {
    $AksClusterName = Get-BicepOutput 'AKS_CLUSTER_NAME' $AksClusterName
    $WorkloadIdentityClientId = Get-BicepOutput 'AKS_WORKLOAD_IDENTITY_CLIENT_ID' $WorkloadIdentityClientId
    $StorageAccount = Get-AgentRuntimeValue 'AZURE_STORAGE_ACCOUNT' $StorageAccount
    $SearchService = Get-AgentRuntimeValue 'AZURE_SEARCH_SERVICE' $SearchService
    $OpenAiService = Get-AgentRuntimeValue 'AZURE_OPENAI_SERVICE' $OpenAiService
    $PostgresHost = Get-AgentRuntimeValue 'AZURE_POSTGRES_HOST' $PostgresHost
    $PostgresUser = Get-AgentRuntimeValue 'AKS_WORKLOAD_IDENTITY_NAME' $PostgresUser
    $AppInsightsConnectionString = Get-AgentRuntimeValue 'APPLICATIONINSIGHTS_CONNECTION_STRING' $AppInsightsConnectionString
}

# Strip trailing slash from ACR login server if present.
$AcrLoginServer = $AcrLoginServer.TrimEnd('/')

$backendImage = "$AcrLoginServer/crv-backend:$ImageTag"
$webImage     = "$AcrLoginServer/crv-web:$ImageTag"

Write-Host "ACR:         $AcrLoginServer" -ForegroundColor Cyan
Write-Host "Backend img: $backendImage"   -ForegroundColor Cyan
Write-Host "Web img:     $webImage"       -ForegroundColor Cyan
if (-not $SkipApply) {
    Write-Host "AKS cluster: $AksClusterName"  -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 3. Build and push images.
# ---------------------------------------------------------------------------
if (-not $SkipBuild) {
    # This private registry rejects ACR Tasks from public build agents. Build locally
    # over the VPN and push an explicit linux/amd64 manifest for AKS node compatibility.
    Write-Host "`nLogging into ACR $AcrLoginServer ..." -ForegroundColor Yellow
    az acr login --name $AcrLoginServer.Split('.')[0]
    if ($LASTEXITCODE -ne 0) {
        Write-Error "az acr login failed. Connect to the private VPN and verify ACR private DNS before retrying."
        exit 1
    }

    Write-Host "`nBuilding and pushing backend image ..." -ForegroundColor Yellow
    docker buildx build --platform linux/amd64 --target runtime --push `
        --build-arg "APP_VERSION=$ImageTag" -t $backendImage -f Dockerfile.backend .
    if ($LASTEXITCODE -ne 0) { Write-Error "Backend build or push failed."; exit 1 }

    Write-Host "`nBuilding and pushing web image ..." -ForegroundColor Yellow
    docker buildx build --platform linux/amd64 --target runtime --push `
        -t $webImage -f Dockerfile.web .
    if ($LASTEXITCODE -ne 0) { Write-Error "Web build or push failed."; exit 1 }
    Write-Host "Images built and pushed successfully." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. Apply the Kubernetes manifest.
# ---------------------------------------------------------------------------
if (-not $SkipApply) {
    $manifestSrc = Join-Path $PSScriptRoot 'crv.yaml'
    $manifestTmp = Join-Path ([System.IO.Path]::GetTempPath()) "crv-$ImageTag.yaml"

    Write-Host "`nGenerating manifest with substituted variables ..." -ForegroundColor Yellow
    $manifestContent = (Get-Content $manifestSrc -Raw) `
        -replace '\$\{AZURE_CONTAINER_REGISTRY_ENDPOINT\}', $AcrLoginServer `
        -replace '\$\{IMAGE_TAG\}', $ImageTag `
        -replace '\$\{AKS_WORKLOAD_IDENTITY_CLIENT_ID\}', $WorkloadIdentityClientId `
        -replace '\$\{AZURE_STORAGE_ACCOUNT\}', $StorageAccount `
        -replace '\$\{AZURE_SEARCH_SERVICE\}', $SearchService `
        -replace '\$\{AZURE_OPENAI_SERVICE\}', $OpenAiService `
        -replace '\$\{AZURE_POSTGRES_HOST\}', $PostgresHost `
        -replace '\$\{AKS_WORKLOAD_IDENTITY_NAME\}', $PostgresUser `
        -replace '\$\{APPLICATIONINSIGHTS_CONNECTION_STRING\}', $AppInsightsConnectionString

    # An unsubstituted "${VAR}" is applied verbatim and reaches production as a literal
    # (a bad image reference, or config the browser cannot parse), so fail before apply.
    $leftover = [regex]::Matches($manifestContent, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') |
        ForEach-Object { $_.Value } | Sort-Object -Unique
    if ($leftover) {
        Write-Error "Unsubstituted placeholders remain in the rendered manifest: $($leftover -join ', '). Add each one to the substitution list above before deploying."
        exit 1
    }

    Set-Content $manifestTmp -Value $manifestContent -Encoding UTF8

    # Direct-IP access, because the privatelink FQDN does not resolve on every dev machine
    # (see /memories/windows-vpn-gotchas.md). The IP is resolved at run time: it changes across
    # aks stop/start, and a stale one may now belong to a worker node.
    $kubectlArgs = @(
        "--server=https://$(Get-AksApiServerIp $AksClusterName):443",
        '--insecure-skip-tls-verify=true'
    )

    Write-Host "Applying manifest to AKS (dry-run first) ..." -ForegroundColor Yellow
    kubectl @kubectlArgs apply --dry-run=server -f $manifestTmp
    if ($LASTEXITCODE -ne 0) {
        Write-Error "kubectl dry-run failed - manifest not applied. Fix the error above and re-run."
        Remove-Item $manifestTmp -ErrorAction SilentlyContinue
        exit 1
    }

    Write-Host "`nDry-run passed. Applying for real ..." -ForegroundColor Yellow
    kubectl @kubectlArgs apply -f $manifestTmp
    if ($LASTEXITCODE -ne 0) { Write-Error "kubectl apply failed."; Remove-Item $manifestTmp; exit 1 }

    Remove-Item $manifestTmp -ErrorAction SilentlyContinue

    Write-Host "`nWaiting for rollout ..." -ForegroundColor Yellow
    kubectl @kubectlArgs rollout status deployment/backend -n crv --timeout=300s
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Backend rollout failed. Inspect the backend pod logs before retrying."
        exit 1
    }
    kubectl @kubectlArgs rollout status deployment/web     -n crv --timeout=300s
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Web rollout failed. Inspect the web pod logs before retrying."
        exit 1
    }

    Write-Host "`nPod status:" -ForegroundColor Cyan
    kubectl @kubectlArgs get pods -n crv

    $kubectlHint = "kubectl $($kubectlArgs -join ' ')"
    Write-Host "`nDeploy complete." -ForegroundColor Green
    Write-Host "Next step - run the Level 6 smoke test:" -ForegroundColor Cyan
    Write-Host "  $kubectlHint port-forward svc/backend 8000:80 -n crv"
    Write-Host "  curl -s http://localhost:8000/healthz"
    Write-Host "  $kubectlHint port-forward svc/web 8080:80 -n crv"
    Write-Host "  curl -s http://localhost:8080/healthz"
}
