<#
.SYNOPSIS
Render one CRV pilot operations job (crv-pilot-jobs.yaml) and optionally apply it through
`az aks command invoke` (the AKS API server is private).

.DESCRIPTION
Shared objects (ops service account, converter service account, work disk, isolation policy and
pilot list) are always rendered; exactly one job is selected with -Job, so stages run in order:
verify-raw, dbsetup, index (setup, dry runs by default) then stage -> convert -> intake ->
(review) -> publish.
Every ${VAR} is substituted and the script refuses to continue if any placeholder survives.
Images are referenced only by digest.

.EXAMPLE
./render-pilot.ps1 -Job stage -OpsClientId <guid> -PipelineDigest sha256:... -ConverterDigest sha256:... `
  -PilotList ./pilot.txt -RenderPath ./stage.yaml
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('stage', 'convert', 'intake', 'publish', 'dbsetup', 'verify-raw', 'index')] [string] $Job,
    [Parameter(Mandatory)] [guid] $OpsClientId,
    [Parameter(Mandatory)] [ValidatePattern('^sha256:[a-f0-9]{64}$')] [string] $PipelineDigest,
    [ValidatePattern('^$|^sha256:[a-f0-9]{64}$')] [string] $ConverterDigest = '',
    [string] $PilotList = '',
    [ValidatePattern('^[a-z0-9]{1,12}$')] [string] $RunId = (Get-Date -Format 'MMddHHmm'),
    [string] $OpsIdentityName = 'id-crv-pipeline-ops',
    [guid] $AdminClientId = 'f7c37964-eec7-48b2-b741-ab2dc9ba38ee',
    [string] $AdminIdentityName = 'id-crv-aks-workload',
    [hashtable] $Principals = [ordered]@{
        coordinator = 'id-crv-pipeline-coordinator=32bc9898-4b66-4419-b9c8-174930596c33'
        worker      = 'id-crv-pipeline-worker=7c1962c1-4f63-451f-9ace-bb5fbecd1eda'
        ops         = 'id-crv-pipeline-ops=22570fca-b1a6-48b2-afe4-949a09b3fca1'
    },
    [string] $PgHost = 'psql-crv-awmc6icu2kueq-ncus.postgres.database.azure.com',
    [string] $SearchEndpoint = 'https://gptkb-awmc6icu2kueq.search.windows.net',
    [string] $AcrLoginServer = 'acrcrvscus.azurecr.io',
    [string] $StorageAccountUrl = 'https://stawmc6icu2kueq.blob.core.windows.net',
    [string] $OpenAiEndpoint = 'https://cog-awmc6icu2kueq.cognitiveservices.azure.com/',
    [string] $EmbeddingDeployment = 'text-embedding-3-large',
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,62}$')] [string] $SourcePrefix = 'crv-pilot',
    [ValidateRange(8, 512)] [int] $WorkGib = 64,
    [switch] $DbApply,
    [switch] $IndexApply,
    [string] $IndexSchema = (Join-Path $PSScriptRoot '../../search/crv-evidence-index.json'),
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [string] $RenderPath = '',
    [switch] $Invoke
)

$ErrorActionPreference = 'Stop'
if ($Job -in 'convert', 'intake' -and -not $ConverterDigest) { throw "-Job $Job needs -ConverterDigest." }
if ($Job -eq 'stage' -and -not $PilotList) { throw '-Job stage needs -PilotList.' }

$entries = @()
if ($PilotList) {
    $entries = @([IO.File]::ReadAllLines((Resolve-Path -LiteralPath $PilotList).Path, [Text.UTF8Encoding]::new($false)) |
        Where-Object { $_.Trim() -and -not $_.StartsWith('#') })
    foreach ($entry in $entries) {
        if ($entry -match '(^|/)\.\.(/|$)|^/|[\x00-\x1f]') { throw "Unsafe pilot entry: $entry" }
    }
}
$listBlock = if ($entries.Count) { ($entries | ForEach-Object { "    $_" }) -join "`n" } else { '    # empty' }

$values = [ordered]@{
    OPS_CLIENT_ID         = $OpsClientId.ToString()
    OPS_IDENTITY_NAME     = $OpsIdentityName
    ADMIN_CLIENT_ID       = $AdminClientId.ToString()
    ADMIN_IDENTITY_NAME   = $AdminIdentityName
    PRINCIPAL_COORDINATOR = $Principals['coordinator']
    PRINCIPAL_WORKER      = $Principals['worker']
    PRINCIPAL_OPS         = $Principals['ops']
    PG_HOST               = $PgHost
    SEARCH_ENDPOINT       = $SearchEndpoint
    ACR_LOGIN_SERVER      = $AcrLoginServer
    PIPELINE_DIGEST       = $PipelineDigest
    CONVERTER_DIGEST      = if ($ConverterDigest) { $ConverterDigest } else { 'sha256:' + ('0' * 64) }
    STORAGE_ACCOUNT_URL   = $StorageAccountUrl
    OPENAI_ENDPOINT       = $OpenAiEndpoint
    EMBEDDING_DEPLOYMENT  = $EmbeddingDeployment
    SOURCE_PREFIX         = $SourcePrefix
    WORK_GIB              = $WorkGib.ToString()
    RUN_ID                = $RunId
    DB_ARGS               = if ($DbApply) { ', --apply' } else { '' }
    INDEX_ARGS            = if ($IndexApply) { ', --apply' } else { '' }
}
foreach ($name in 'PRINCIPAL_COORDINATOR', 'PRINCIPAL_WORKER', 'PRINCIPAL_OPS') {
    if ($values[$name] -notmatch '^[a-z][a-z0-9-]{2,62}=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        throw "$name must be LOGIN=ENTRA_OBJECT_ID"
    }
}
$text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'crv-pilot-jobs.yaml') -Raw
foreach ($key in $values.Keys) {
    if ($values[$key] -match '["\r\n]') { throw "Value for $key contains a quote or newline" }
    $text = $text.Replace('${' + $key + '}', $values[$key])
}
$text = $text.Replace('${PILOT_LIST}', $listBlock)
$leftovers = [regex]::Matches($text, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') | ForEach-Object Value | Sort-Object -Unique
if ($leftovers) { throw "Unresolved placeholders: $($leftovers -join ', ')" }

# Keep the shared objects and only the selected job.
$documents = $text -split '(?m)^---\s*$' | Where-Object { $_.Trim() }
$selected = $documents | Where-Object {
    $_ -notmatch '(?m)^kind:\s*Job\s*$' -or $_ -match "crv\.pilot/job:\s*$Job\b"
}
if (@($selected | Where-Object { $_ -match '(?m)^kind:\s*Job\s*$' }).Count -ne 1) { throw "Job $Job not found." }
$rendered = ($selected | ForEach-Object { $_.Trim() }) -join "`n---`n"
if ($Job -eq 'index') {
    $schema = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $IndexSchema).Path, [Text.UTF8Encoding]::new($false))
    $null = $schema | ConvertFrom-Json
    $configMap = [ordered]@{
        apiVersion = 'v1'; kind = 'ConfigMap'
        metadata = [ordered]@{ name = 'crv-search-schema'; namespace = 'crv' }
        data = [ordered]@{ 'crv-evidence-index.json' = $schema }
    }
    $rendered += "`n---`n" + ($configMap | ConvertTo-Json -Depth 5)
}
$rendered += "`n"

$path = if ($RenderPath) { $RenderPath } else { Join-Path ([IO.Path]::GetTempPath()) "crv-pilot-$Job-$RunId.yaml" }
Set-Content -LiteralPath $path -Value $rendered -Encoding utf8 -NoNewline
Write-Output "Rendered $Job (run $RunId) to $path"
if ($Invoke) {
    $name = Split-Path -Leaf $path
    az aks command invoke -g $ResourceGroup -n $Cluster --command "kubectl apply -f $name" --file $path -o json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az aks command invoke failed with exit code $LASTEXITCODE" }
    Write-Output "Applied job crv-pilot-$Job-$RunId"
}
