#Requires -Version 7.0
# -Endpoint/-ApiKeyFile target an external billing account's resource (API key read from a
# file, never from the command line) instead of the plan's account with an Entra token.
[CmdletBinding()]
param(
    [string]$PlanPath = (Join-Path $PSScriptRoot '../../.azure/ingestion-pilot-plan.json'),
    [string]$DefinitionPath = (Join-Path $PSScriptRoot 'ingestion-pilot.json'),
    [ValidatePattern('^$|^https://[A-Za-z0-9.-]+/?$')] [string]$Endpoint = '',
    [string]$ApiKeyFile = '',
    [switch]$ValidateOnly,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$plan = Get-Content -Raw $PlanPath | ConvertFrom-Json -AsHashtable
$definition = Get-Content -Raw $DefinitionPath | ConvertFrom-Json -AsHashtable

if ($plan.status -notin @('approved', 'deployed')) { throw 'Pilot plan is not approved.' }
if ($definition.apiVersion -ne $plan.contentUnderstanding.apiVersion) { throw 'CU API versions differ.' }
foreach ($analyzerId in $plan.contentUnderstanding.analyzers) {
    if (-not $definition.analyzers.ContainsKey($analyzerId)) { throw "Missing definition: $analyzerId" }
    $body = $definition.analyzers[$analyzerId]
    if ($body.models.completion -ne $plan.contentUnderstanding.completionModel) { throw "Completion mismatch: $analyzerId" }
    if ($body.models.embedding -ne $plan.contentUnderstanding.embeddingModel) { throw "Embedding mismatch: $analyzerId" }
    if ($body.config.omitContent -or -not $body.config.returnDetails) { throw "Evidence settings invalid: $analyzerId" }
    if ($body.ContainsKey('fieldSchema')) {
        foreach ($field in $body.fieldSchema.fields.Values) {
            if ($field.method -eq 'extract' -and -not $field.estimateSourceAndConfidence) {
                throw "Ungrounded extract field: $analyzerId"
            }
        }
    }
}
if ($ValidateOnly) {
    Write-Output "Validated $($definition.analyzers.Count) analyzer definitions and API $($definition.apiVersion)."
    return
}

if ([bool]$Endpoint -ne [bool]$ApiKeyFile) { throw '-Endpoint and -ApiKeyFile go together.' }
if ($ApiKeyFile) {
    $key = (Get-Content -Raw -LiteralPath $ApiKeyFile).Trim()
    if (-not $key) { throw 'The API key file is empty.' }
    $headers = @{ 'Ocp-Apim-Subscription-Key' = $key }
    $baseUrl = "$($Endpoint.TrimEnd('/'))/contentunderstanding"
}
else {
    $token = az account get-access-token --subscription $plan.subscriptionId --resource 'https://cognitiveservices.azure.com' --query accessToken --only-show-errors -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not acquire CU credential.' }
    $headers = @{ Authorization = "Bearer $($token.Trim())" }
    $baseUrl = "https://$($plan.contentUnderstanding.account).cognitiveservices.azure.com/contentunderstanding"
}
$apiVersion = $definition.apiVersion

function Invoke-CuRequest([string]$Method, [string]$Path, $Body = $null) {
    $parameters = @{
        Method = $Method
        Uri = "$baseUrl/${Path}?api-version=$apiVersion"
        Headers = $headers
        TimeoutSec = 120
    }
    if ($null -ne $Body) {
        $parameters.ContentType = 'application/json'
        $parameters.Body = $Body | ConvertTo-Json -Depth 40 -Compress
    }
    Invoke-RestMethod @parameters
}

function Test-DefinitionSubset($Expected, $Actual) {
    if ($Expected -is [System.Collections.IDictionary]) {
        if ($Actual -isnot [System.Collections.IDictionary]) { return $false }
        foreach ($key in $Expected.Keys) {
            if (-not $Actual.Contains($key)) { return $false }
            if (-not (Test-DefinitionSubset $Expected[$key] $Actual[$key])) { return $false }
        }
        return $true
    }
    if ($Expected -is [array]) {
        if ($Actual -isnot [array] -or $Expected.Count -ne $Actual.Count) { return $false }
        for ($index = 0; $index -lt $Expected.Count; $index++) {
            if (-not (Test-DefinitionSubset $Expected[$index] $Actual[$index])) { return $false }
        }
        return $true
    }
    return $Expected -ceq $Actual
}

if (-not $VerifyOnly) {
    Invoke-CuRequest PATCH 'defaults' @{ modelDeployments = $definition.modelDeployments } | Out-Null
}
$defaults = Invoke-CuRequest GET 'defaults' | ConvertTo-Json -Depth 40 | ConvertFrom-Json -AsHashtable
if (-not (Test-DefinitionSubset $definition.modelDeployments $defaults.modelDeployments)) {
    throw 'CU default model mappings do not match the pinned pilot.'
}

$pending = @()
foreach ($analyzerId in $plan.contentUnderstanding.analyzers) {
    $existing = $null
    try { $existing = Invoke-CuRequest GET "analyzers/$analyzerId" }
    catch {
        if ($_.Exception.Response.StatusCode -ne 404) { throw }
    }
    if ($null -eq $existing -and -not $VerifyOnly) {
        Invoke-CuRequest PUT "analyzers/$analyzerId" $definition.analyzers[$analyzerId] | Out-Null
        $existing = Invoke-CuRequest GET "analyzers/$analyzerId"
    }
    if ($null -eq $existing) { throw "Analyzer missing: $analyzerId" }
    $actual = $existing | ConvertTo-Json -Depth 40 | ConvertFrom-Json -AsHashtable
    if (-not (Test-DefinitionSubset $definition.analyzers[$analyzerId] $actual)) {
        throw "Analyzer definition mismatch; refusing to overwrite: $analyzerId"
    }
    Write-Output "$analyzerId : $($actual.status)"
    if ($actual.status -ne 'ready') { $pending += $analyzerId }
}
if ($pending.Count) {
    throw "Analyzer creation pending; rerun with -VerifyOnly after completion: $($pending -join ', ')"
}
Write-Output 'CU configuration verified. No analysis requests were submitted.'