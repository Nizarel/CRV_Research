<#
.SYNOPSIS
Render infra/kubernetes/pipeline/crv-pipeline.yaml and validate or apply it.

.DESCRIPTION
Substitutes every ${VAR}, refuses to continue if any placeholder survives, then runs
`kubectl apply --dry-run=server` (default) or a real apply with -Apply. Pass cluster access
arguments through -KubectlArgs (resolve the private API IP from private DNS each time).
With -RenderPath the rendered manifest is only written to that file (no cluster access).

-BillingPath is the pipeline's billing.json (USD budgets per billing account). External accounts
in it need one Key Vault secret each (-AccountKeySecrets name=secret, -KeyVaultName, -TenantId):
the IO workers mount them with the Secrets Store CSI driver under the worker identity. Without
external accounts the lines tagged external-accounts are dropped.

.EXAMPLE
./render-pipeline.ps1 -CoordinatorClientId <guid> -WorkerClientId <guid> -ImageTag pipeline-20261008a `
  -KubectlArgs @('--context','aks-crv-southcentralus','--server','https://<api-ip>:443','--tls-server-name','<fqdn>')
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [guid] $CoordinatorClientId,
    [Parameter(Mandatory)] [guid] $WorkerClientId,
    [Parameter(Mandatory)] [guid] $KedaClientId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_.-]{1,128}$')] [string] $ImageTag,
    [string] $AcrLoginServer = 'acrcrvscus.azurecr.io',
    [string] $PgHost = 'psql-crv-awmc6icu2kueq-ncus.postgres.database.azure.com',
    [string] $StorageAccountUrl = 'https://stawmc6icu2kueq.blob.core.windows.net',
    [ValidatePattern('^[a-z0-9][a-z0-9-]{2,62}$')] [string] $SourceContainer = 'crv-sources',
    [string] $ServiceBusNamespaceName = 'sb-awmc6icu2kueq',
    [string] $ApplicationInsightsConnectionString = '',
    [string] $DiEndpoint = 'https://cog-di-awmc6icu2kueq.cognitiveservices.azure.com/',
    [ValidateRange(0, 1000000)] [int] $DiPageBudget = 0,
    [ValidateRange(1, 64)] [int] $DiMaxInflight = 4,
    [string] $OpenAiEndpoint = 'https://cog-awmc6icu2kueq.cognitiveservices.azure.com/',
    [string] $EmbeddingDeployment = 'text-embedding-3-large',
    [string] $SearchEndpoint = 'https://gptkb-awmc6icu2kueq.search.windows.net',
    [string] $SearchIndexName = 'crv-evidence-v1',
    [string] $CuEndpoint = 'https://cu-awmc6icu2kueq.cognitiveservices.azure.com/',
    [ValidateRange(0, 1000000)] [int] $CuPageBudget = 0,
    [ValidateRange(0, 2000000000)] [long] $CuTokenBudget = 0,
    [ValidateRange(1, 32)] [int] $CuMaxInflight = 2,
    [ValidateRange(1000, 200000)] [int] $CuEstTokensPerPage = 12000,
    [string] $CuRegistryPath = (Join-Path $PSScriptRoot '../../content-understanding/crv-cu-registry.json'),
    [string] $PoliciesPath = (Join-Path $PSScriptRoot '../../content-understanding/crv-policies.json'),
    [ValidatePattern('^$|^[a-z0-9][a-z0-9.-]{0,62}$')] [string] $RoutingPolicy = 'crv-layout-v1',
    [ValidatePattern('^[a-z0-9][a-z0-9.-]{0,62}$')] [string] $SearchReleaseId = 'crv-pilot-v1',
    [ValidateRange(1, 8)] [int] $IoConcurrency = 1,
    [ValidateRange(1, 32)] [int] $IoMaxReplicas = 8,
    [string] $BillingPath = '',
    [hashtable] $AccountKeySecrets = @{},
    [ValidatePattern('^$|^[A-Za-z0-9-]{3,24}$')] [string] $KeyVaultName = '',
    [ValidatePattern('^$|^[0-9a-fA-F-]{36}$')] [string] $TenantId = '',
    [string[]] $KubectlArgs = @(),
    [string] $RenderPath = '',
    [switch] $Apply
)

$ErrorActionPreference = 'Stop'
$template = Join-Path $PSScriptRoot 'crv-pipeline.yaml'

$values = @{
    COORDINATOR_CLIENT_ID                 = $CoordinatorClientId.ToString()
    WORKER_CLIENT_ID                      = $WorkerClientId.ToString()
    KEDA_CLIENT_ID                        = $KedaClientId.ToString()
    IMAGE_TAG                             = $ImageTag
    ACR_LOGIN_SERVER                      = $AcrLoginServer
    PG_HOST                               = $PgHost
    STORAGE_ACCOUNT_URL                   = $StorageAccountUrl
    SOURCE_CONTAINER                      = $SourceContainer
    SERVICE_BUS_NAMESPACE_NAME            = $ServiceBusNamespaceName
    SERVICE_BUS_FQDN                      = "$ServiceBusNamespaceName.servicebus.windows.net"
    APPLICATIONINSIGHTS_CONNECTION_STRING = $ApplicationInsightsConnectionString
    DI_ENDPOINT                           = $DiEndpoint
    DI_PAGE_BUDGET                        = $DiPageBudget.ToString()
    DI_MAX_INFLIGHT                       = $DiMaxInflight.ToString()
    OPENAI_ENDPOINT                       = $OpenAiEndpoint
    EMBEDDING_DEPLOYMENT                  = $EmbeddingDeployment
    SEARCH_ENDPOINT                       = $SearchEndpoint
    SEARCH_INDEX_NAME                     = $SearchIndexName
    CU_ENDPOINT                           = $CuEndpoint
    CU_PAGE_BUDGET                        = $CuPageBudget.ToString()
    CU_TOKEN_BUDGET                       = $CuTokenBudget.ToString()
    CU_MAX_INFLIGHT                       = $CuMaxInflight.ToString()
    CU_EST_TOKENS_PER_PAGE                = $CuEstTokensPerPage.ToString()
    ROUTING_POLICY                        = $RoutingPolicy
    SEARCH_RELEASE_ID                     = $SearchReleaseId
    IO_CONCURRENCY                        = $IoConcurrency.ToString()
    IO_MAX_REPLICAS                       = $IoMaxReplicas.ToString()
}

$text = Get-Content -LiteralPath $template -Raw
foreach ($key in $values.Keys) {
    if ($values[$key] -match '["\r\n]') { throw "Value for $key contains a quote or newline" }
    $text = $text.Replace('${' + $key + '}', $values[$key])
}
$leftovers = [regex]::Matches($text, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') | ForEach-Object Value | Sort-Object -Unique
if ($leftovers) { throw "Unresolved placeholders: $($leftovers -join ', ')" }

$billing = if ($BillingPath) { [IO.File]::ReadAllText((Resolve-Path -LiteralPath $BillingPath).Path, [Text.UTF8Encoding]::new($false)) } else { '{}' }
$billingDocument = $billing | ConvertFrom-Json -AsHashtable
$accounts = @(if ($billingDocument.ContainsKey('accounts')) { $billingDocument.accounts })
$accountNames = @($accounts | ForEach-Object { $_.name } | Sort-Object)
if (($accountNames -join ',') -cne (@($AccountKeySecrets.Keys | Sort-Object) -join ',')) {
    throw "External accounts ($($accountNames -join ', ')) need exactly one -AccountKeySecrets entry each."
}
foreach ($account in $accounts) {
    $keyFile = "/var/run/secrets/crv/accounts/$($account.name)"
    if ($account.key_file -cne $keyFile) { throw "Account $($account.name) must read its key from $keyFile." }
    if ($AccountKeySecrets[$account.name] -notmatch '^[0-9A-Za-z-]{1,127}$') { throw "Invalid Key Vault secret name for $($account.name)." }
}
if ($accounts.Count) {
    if (-not ($KeyVaultName -and $TenantId)) { throw 'External accounts need -KeyVaultName and -TenantId.' }
    $text = [regex]::Replace($text, '[ \t]+# external-accounts', '')
}
else {
    $text = [regex]::Replace($text, '(?m)^[^\n]*# external-accounts\r?\n', '')
}

# Reviewed analyzer registry and routing policies, mounted read-only into every pipeline pod
# under fixed keys. JSON is valid YAML, so the ConfigMap document is appended as JSON.
$cuData = [ordered]@{}
foreach ($entry in @(@('ingestion-pilot.json', $CuRegistryPath), @('policies.json', $PoliciesPath))) {
    $content = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $entry[1]).Path, [Text.UTF8Encoding]::new($false))
    $parsed = $content | ConvertFrom-Json -AsHashtable
    $cuData[$entry[0]] = $content
}
if ($RoutingPolicy -and -not $parsed.policies.ContainsKey($RoutingPolicy)) {
    throw "Routing policy $RoutingPolicy is not in $PoliciesPath."
}
$cuData['billing.json'] = $billing
$cuConfigMap = [ordered]@{
    apiVersion = 'v1'; kind = 'ConfigMap'
    metadata = [ordered]@{ name = 'crv-pipeline-cu'; namespace = 'crv' }
    data = $cuData
}
$text = $text.TrimEnd() + "`n---`n" + ($cuConfigMap | ConvertTo-Json -Depth 5) + "`n"
if ($accounts.Count) {
    $objects = "array:`n" + (($accounts | ForEach-Object {
        "  - |`n    objectName: $($AccountKeySecrets[$_.name])`n    objectType: secret`n    objectAlias: $($_.name)`n"
    }) -join '')
    $secretProvider = [ordered]@{
        apiVersion = 'secrets-store.csi.x-k8s.io/v1'; kind = 'SecretProviderClass'
        metadata = [ordered]@{ name = 'crv-pipeline-accounts'; namespace = 'crv' }
        spec = [ordered]@{
            provider = 'azure'
            parameters = [ordered]@{
                usePodIdentity = 'false'
                clientID = $WorkerClientId.ToString()
                keyvaultName = $KeyVaultName
                tenantId = $TenantId
                objects = $objects
            }
        }
    }
    $text += "---`n" + ($secretProvider | ConvertTo-Json -Depth 5) + "`n"
}
if ($RenderPath) {
    Set-Content -LiteralPath $RenderPath -Value $text -Encoding utf8
    return
}

$rendered = Join-Path ([IO.Path]::GetTempPath()) "crv-pipeline-$([guid]::NewGuid()).yaml"
Set-Content -LiteralPath $rendered -Value $text -Encoding utf8
try {
    $mode = @(if (-not $Apply) { '--dry-run=server' })
    & kubectl @KubectlArgs apply @mode -f $rendered
    if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed with exit code $LASTEXITCODE" }
}
finally {
    Remove-Item -LiteralPath $rendered -Force -ErrorAction SilentlyContinue
}
