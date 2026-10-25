#!/usr/bin/env pwsh
#Requires -Version 7.0
<#
.SYNOPSIS
Creates an isolated Manadry agent backend without changing the current web deployment.
.DESCRIPTION
Freezes the current backend image and retrieval ConfigMap, overlays the tested model
client settings, and supplies the approved Key Vault credential through a new Secret.
Without Apply, only validates the source configuration and prints a safe snapshot.
.PARAMETER RunId
Unique DNS-safe evaluation run identifier.
.PARAMETER Apply
Create the isolated resources. No ingress or production selector is used.
.EXAMPLE
./Start-IsolatedBusinessEval.ps1 -RunId manadry-20261024-v1 -Apply
.NOTES
The secret value is never written to a local file or printed. Delete only the returned
resource names after the evaluation; the source deployment and ConfigMap are unchanged.
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[a-z][a-z0-9-]{1,42}$')]
    [string] $RunId = 'manadry-20261024-v1',
    [string] $Kubeconfig = (Join-Path $env:TEMP 'crv-eval-20261024.kubeconfig'),
    [string] $Namespace = 'crv',
    [string] $SourceDeployment = 'crv-research-web',
    [string] $Vault = 'kv-awmc6icu2kueq',
    [string] $SecretName = 'manadry-di-key',
    [switch] $Apply
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$Name = "crv-eval-$RunId"
$NativeName = "$Name-native"
$CodeName = "$Name-code"
$CredentialName = "$Name-model"
$Output = Join-Path $Repo "Docs/evaluation/copilot-extension/$RunId"
$KubeArgs = @('--kubeconfig', $Kubeconfig, '-n', $Namespace)

function Get-KubeObject([string] $Kind, [string] $ObjectName) {
    $Text = & kubectl @KubeArgs get $Kind $ObjectName -o json --request-timeout=30s
    if ($LASTEXITCODE -ne 0) { throw "Cannot read $Kind $ObjectName" }
    return $Text | ConvertFrom-Json -AsHashtable
}

function Submit-KubeObject([hashtable] $Document) {
    $Document | ConvertTo-Json -Depth 80 -Compress |
        & kubectl @KubeArgs apply --server-side --field-manager=crv-business-eval -f -
    if ($LASTEXITCODE -ne 0) { throw 'Isolated evaluation resource apply failed' }
}

function Set-ContainerSetting([hashtable] $Container, [string] $Setting, [hashtable] $Value) {
    $Container.env = @($Container.env | Where-Object { $_.name -ne $Setting }) +
        @(@{ name = $Setting } + $Value)
}

if ($MyInvocation.InvocationName -ne '.') {
    $Source = Get-KubeObject deployment $SourceDeployment
    $Pod = $Source.spec.template.spec
    $Backend = @($Pod.containers | Where-Object name -eq 'backend')[0]
    if (-not $Backend -or $Backend.image -notmatch '@sha256:[a-f0-9]{64}$') {
        throw 'The source backend must exist and use a pinned image digest'
    }
    $References = @($Backend.envFrom | Where-Object { $_.ContainsKey('configMapRef') })
    if ($References.Count -ne 1 -or $Backend.envFrom.Count -ne 1) {
        throw 'Expected exactly one native ConfigMap; review source envFrom before applying'
    }
    $Native = Get-KubeObject configmap $References[0].configMapRef.name
    if (@($Native.data.Keys | Where-Object { $_ -match '(_API_KEY|_PASSWORD|_TOKEN)$' }).Count) {
        throw 'Source ConfigMap contains secret-shaped settings; do not copy into a ConfigMap'
    }
    $SettingsPath = Join-Path $Repo 'src/backend/crv_agent/runtime/settings.py'
    $InstalledText = & kubectl @KubeArgs exec "deployment/$SourceDeployment" -c backend -- python -P -c 'import json,importlib.util; from pathlib import Path; root=Path(importlib.util.find_spec("crv_agent").origin).parent; print(json.dumps({"package_root":str(root),"infrastructure":(root/"runtime"/"infrastructure.py").read_text()}))'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the installed serving package' }
    $Installed = $InstalledText | ConvertFrom-Json -AsHashtable
    $EndpointAnchor = 'endpoint = settings.openai_endpoint.rstrip("/")'
    $KeyAnchor = 'model=deployment, base_url=endpoint + "/", api_key=token_provider,'
    if (($Installed.infrastructure.Split($EndpointAnchor).Count - 1) -ne 1 -or
        ($Installed.infrastructure.Split($KeyAnchor).Count - 1) -ne 1) {
        throw 'Deployed chat factory changed; review the two chat-only overlay anchors'
    }
    $InfrastructureCode = $Installed.infrastructure.Replace(
        $EndpointAnchor, 'endpoint = (settings.model_endpoint or settings.openai_endpoint).rstrip("/")'
    ).Replace(
        $KeyAnchor,
        'model=deployment, base_url=endpoint + "/", api_key=(settings.model_api_key.get_secret_value() if settings.model_api_key else token_provider),'
    )
    $Code = @{
        'settings.py' = [IO.File]::ReadAllText($SettingsPath)
        'infrastructure.py' = $InfrastructureCode
    }
    $Snapshot = [ordered]@{
        run_id = $RunId
        captured_at = [DateTime]::UtcNow.ToString('o')
        source_deployment = $SourceDeployment
        isolated_deployment = $Name
        namespace = $Namespace
        agent_image = $Backend.image
        agent_model_endpoint = 'https://manadry.cognitiveservices.azure.com/'
        retrieval_model_endpoint = $Native.data.CRV_OPENAI_ENDPOINT
        secret_reference = "https://$Vault.vault.azure.net/secrets/$SecretName"
        settings_sha256 = (Get-FileHash $SettingsPath -Algorithm SHA256).Hash.ToLowerInvariant()
        infrastructure_sha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData(
            [Text.Encoding]::UTF8.GetBytes($InfrastructureCode)
        )).ToLowerInvariant()
        package_root = $Installed.package_root
        judge = @{ provider = 'github-copilot-sdk'; model = 'claude-haiku-5.5'; sdk_version = '1.0.26' }
        resources = @{ deployment = $Name; native_config = $NativeName; code_config = $CodeName; credential = $CredentialName }
        production_deployment_changed = $false
    }
    if (-not $Apply) {
        $Snapshot | ConvertTo-Json -Depth 10
        return
    }
    if (Test-Path (Join-Path $Output 'deployment-snapshot.json')) {
        throw 'This run already has a snapshot; use a new run ID'
    }
    $Existing = & kubectl @KubeArgs get deployment $Name --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0 -or $Existing) { throw 'Cannot reuse an existing isolated deployment' }
    $Labels = @{ app = $Name; 'app.kubernetes.io/name' = $Name; 'app.kubernetes.io/part-of' = 'crv-evaluation' }
    $PodLabels = @{} + $Labels + @{ 'azure.workload.identity/use' = 'true' }
    $Backend.envFrom = @(@{ configMapRef = @{ name = $NativeName } })
    Set-ContainerSetting $Backend 'CRV_MODEL_ENDPOINT' @{ value = 'https://manadry.cognitiveservices.azure.com/' }
    Set-ContainerSetting $Backend 'CRV_MODEL_API_KEY' @{
        valueFrom = @{ secretKeyRef = @{ name = $CredentialName; key = 'api-key' } }
    }
    Set-ContainerSetting $Backend 'CRV_LOCAL_OWNER' @{ value = $RunId }
    Set-ContainerSetting $Backend 'CRV_EVAL_DIAGNOSTICS' @{ value = 'false' }
    $Backend.volumeMounts = @($Backend.volumeMounts) + @(
        @{ name = 'eval-code'; mountPath = "$($Installed.package_root)/runtime/settings.py"; subPath = 'settings.py'; readOnly = $true },
        @{ name = 'eval-code'; mountPath = "$($Installed.package_root)/runtime/infrastructure.py"; subPath = 'infrastructure.py'; readOnly = $true }
    )
    $Pod.containers = @($Backend)
    $UsedVolumes = @($Backend.volumeMounts | ForEach-Object name)
    $Pod.volumes = @($Pod.volumes | Where-Object { $_.name -in $UsedVolumes }) +
        @(@{ name = 'eval-code'; configMap = @{ name = $CodeName } })
    $Deployment = @{
        apiVersion = 'apps/v1'; kind = 'Deployment'
        metadata = @{ name = $Name; namespace = $Namespace; labels = $Labels }
        spec = @{
            replicas = 1; strategy = @{ type = 'Recreate' }
            selector = @{ matchLabels = @{ app = $Name } }
            template = @{ metadata = @{ labels = $PodLabels }; spec = $Pod }
        }
    }
    $Credential = $null
    try {
        $Credential = & az keyvault secret show --vault-name $Vault --name $SecretName -o json --only-show-errors |
            ConvertFrom-Json -AsHashtable
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Credential.value)) {
            throw 'Cannot obtain the approved external-model credential'
        }
        Submit-KubeObject @{
            apiVersion = 'v1'; kind = 'Secret'; type = 'Opaque'
            metadata = @{ name = $CredentialName; namespace = $Namespace; labels = $Labels }
            immutable = $true
            data = @{ 'api-key' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Credential.value)) }
        }
        $Snapshot.secret_version = ($Credential.id -split '/')[-1]
    }
    finally {
        if ($Credential) { $Credential.Clear() }
        $Credential = $null
    }
    Submit-KubeObject @{
        apiVersion = 'v1'; kind = 'ConfigMap'; immutable = $true
        metadata = @{ name = $NativeName; namespace = $Namespace; labels = $Labels }
        data = $Native.data
    }
    Submit-KubeObject @{
        apiVersion = 'v1'; kind = 'ConfigMap'; immutable = $true
        metadata = @{ name = $CodeName; namespace = $Namespace; labels = $Labels }
        data = $Code
    }
    Submit-KubeObject $Deployment
    New-Item -ItemType Directory -Path $Output -Force | Out-Null
    $Snapshot | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $Output 'deployment-snapshot.json') -Encoding utf8
    & kubectl @KubeArgs rollout status "deployment/$Name" --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw 'Isolated backend did not become ready; inspect its status' }
    Write-Output "Isolated evaluation backend ready: $Name"
    Write-Output "Snapshot: $Output/deployment-snapshot.json"
}