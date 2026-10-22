<#
.SYNOPSIS
Render (and optionally apply) one CRV research-agent job from research-agent.yaml.

.DESCRIPTION
Shared objects (service account, ConfigMap) are always rendered; exactly one job is kept with -Job,
so the steps run in order: kg-login -> memory-setup -> memory-grant -> agent-check ->
retrieval-eval / answer-eval (dev split; the held-out split needs -AllowHeldout and is run once).
Every ${VAR} is substituted and the script refuses to continue if any placeholder survives.
Images are referenced only by digest. The cluster is private: -Invoke uses `az aks command invoke`.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('kg-login', 'memory-setup', 'memory-grant', 'agent-check', 'retrieval-eval', 'answer-eval')] [string] $Job,
    [ValidatePattern('^$|^sha256:[a-f0-9]{64}$')] [string] $AgentDigest = '',
    [ValidatePattern('^$|^sha256:[a-f0-9]{64}$')] [string] $PipelineDigest = '',
    [guid] $AgentClientId = 'd965e54c-3f16-4521-b384-bd6924b98dd4',
    [guid] $AgentPrincipalId = '27f5fcdb-8296-42d0-ac54-6bef2d2a98a6',
    [string] $AgentIdentityName = 'id-crv-research-agent',
    [string] $AdminIdentityName = 'id-crv-aks-workload',
    [string] $PgHost = 'psql-crv-awmc6icu2kueq-ncus.postgres.database.azure.com',
    [string] $SearchEndpoint = 'https://gptkb-awmc6icu2kueq.search.windows.net',
    [string] $OpenAiEndpoint = 'https://cog-awmc6icu2kueq.cognitiveservices.azure.com/',
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')] [string] $ChatDeployment = 'gpt-6.1-sol',
    [ValidatePattern('^[a-z0-9][a-z0-9.-]{0,62}$')] [string] $SearchRelease = 'crv-v1',
    [ValidatePattern('^crvkg-[0-9]{8}-[a-f0-9]{12}$')] [string] $KgRelease = 'crvkg-20261021-adb13a21c7da',
    [string] $AcrLoginServer = 'acrcrvscus.azurecr.io',
    [ValidatePattern('^[a-z0-9]{1,12}$')] [string] $RunId = (Get-Date -Format 'MMddHHmm'),
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [ValidateSet('A0', 'A1', 'P16', 'P12', 'P24', 'P16W2000', 'CAP', 'LLM', 'FULL50', 'DEC', 'KGB', 'CEIL', 'AAR')] [string[]] $Arm = @(),
    [ValidateSet('dev', 'heldout')] [string] $Split = 'dev',
    [switch] $AllowHeldout,
    [ValidatePattern('^[A-Za-z0-9]{1,8}$')] [string[]] $Only = @(),
    [ValidatePattern('^$|^[a-z0-9][a-z0-9-]{1,62}$')] [string] $KnowledgeBase = '',
    [ValidateRange(1, 8)] [int] $Concurrency = 3,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')] [string] $JudgeDeployment = 'gpt-5.4-mini',
    [ValidateRange(1, 5)] [int] $JudgeRepeats = 3,
    [ValidateRange(900, 86400)] [int] $DeadlineSeconds = 14400,
    [ValidateRange(30, 600)] [int] $TurnSeconds = 180,
    [string] $GoldenPath = (Join-Path $PSScriptRoot '../../../golden/crv_golden.jsonl'),
    [string] $RenderPath = '',
    [switch] $Invoke
)

$ErrorActionPreference = 'Stop'
if ($Job -eq 'kg-login' -and -not $PipelineDigest) { throw '-Job kg-login needs -PipelineDigest.' }
if ($Job -ne 'kg-login' -and -not $AgentDigest) { throw "-Job $Job needs -AgentDigest." }
if ($Job -in 'retrieval-eval', 'answer-eval' -and -not $PipelineDigest) { throw "-Job $Job needs -PipelineDigest (scorer)." }
if ($Split -eq 'heldout' -and -not $AllowHeldout) { throw 'The held-out split needs -AllowHeldout (run once, for the frozen winner).' }
$evalArgs = ''
foreach ($item in $Arm) { $evalArgs += ", --arm, $item" }
foreach ($item in $Only) { $evalArgs += ", --only, $item" }
if ($KnowledgeBase) { $evalArgs += ", --knowledge-base, $KnowledgeBase" }
if ($Split -eq 'heldout') { $evalArgs += ', --allow-heldout' }
$answerArgs = ''
foreach ($item in $Only) { $answerArgs += ", --question-id, $item" }
if ($Split -eq 'heldout') { $answerArgs += ', --allow-heldout' }
$zero = 'sha256:' + ('0' * 64)
$values = [ordered]@{
    AGENT_CLIENT_ID     = $AgentClientId.ToString()
    AGENT_PRINCIPAL_ID  = $AgentPrincipalId.ToString()
    AGENT_IDENTITY_NAME = $AgentIdentityName
    ADMIN_IDENTITY_NAME = $AdminIdentityName
    PG_HOST             = $PgHost
    SEARCH_ENDPOINT     = $SearchEndpoint
    OPENAI_ENDPOINT     = $OpenAiEndpoint
    CHAT_DEPLOYMENT     = $ChatDeployment
    SEARCH_RELEASE      = $SearchRelease
    KG_RELEASE          = $KgRelease
    ACR_LOGIN_SERVER    = $AcrLoginServer
    AGENT_DIGEST        = if ($AgentDigest) { $AgentDigest } else { $zero }
    PIPELINE_DIGEST     = if ($PipelineDigest) { $PipelineDigest } else { $zero }
    RUN_ID              = $RunId
    EVAL_SPLIT          = $Split
    EVAL_CONCURRENCY    = $Concurrency.ToString()
    EVAL_ARGS           = $evalArgs
    ANSWER_ARGS         = $answerArgs
    TURN_SECONDS        = $TurnSeconds.ToString()
    JUDGE_DEPLOYMENT    = $JudgeDeployment
    JUDGE_REPEATS       = $JudgeRepeats.ToString()
    DEADLINE_SECONDS    = $DeadlineSeconds.ToString()
}
foreach ($login in $AgentIdentityName, $AdminIdentityName) {
    if ($login -notmatch '^[a-z][a-z0-9-]{2,62}$') { throw "Unsafe login name: $login" }
}
$text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'research-agent.yaml') -Raw
foreach ($key in $values.Keys) {
    if ($values[$key] -match '["\r\n]') { throw "Value for $key contains a quote or newline" }
    $text = $text.Replace('${' + $key + '}', $values[$key])
}
$leftovers = [regex]::Matches($text, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') | ForEach-Object Value | Sort-Object -Unique
if ($leftovers) { throw "Unresolved placeholders: $($leftovers -join ', ')" }

$documents = $text -split '(?m)^---\s*$' | Where-Object { $_.Trim() }
$selected = $documents | Where-Object {
    $_ -notmatch '(?m)^kind:\s*Job\s*$' -or $_ -match "crv\.agent/job:\s*$Job\b"
}
if (@($selected | Where-Object { $_ -match '(?m)^kind:\s*Job\s*$' }).Count -ne 1) { throw "Job $Job not found." }
$rendered = (($selected | ForEach-Object { $_.Trim() }) -join "`n---`n") + "`n"
if ($Job -in 'retrieval-eval', 'answer-eval') {
    $golden = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $GoldenPath).Path, [Text.UTF8Encoding]::new($false))
    $configMap = [ordered]@{
        apiVersion = 'v1'; kind = 'ConfigMap'
        metadata = [ordered]@{ name = 'crv-golden-agent'; namespace = 'crv' }
        data = [ordered]@{ 'crv_golden.jsonl' = $golden }
    }
    $rendered += "---`n" + ($configMap | ConvertTo-Json -Depth 5) + "`n"
}

$path = if ($RenderPath) { $RenderPath } else { Join-Path ([IO.Path]::GetTempPath()) "crv-agent-$Job-$RunId.yaml" }
Set-Content -LiteralPath $path -Value $rendered -Encoding utf8 -NoNewline
Write-Output "Rendered $Job (run $RunId) to $path"
if ($Invoke) {
    $name = Split-Path -Leaf $path
    az aks command invoke -g $ResourceGroup -n $Cluster --command "kubectl apply -f $name" --file $path -o json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az aks command invoke failed with exit code $LASTEXITCODE" }
    Write-Output "Applied job crv-agent-$Job-$RunId"
}
