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
    # Per-role overrides; empty uses -ChatDeployment. Defaults = adopted R3w (2026-10-22 dev eval).
    [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $PlannerDeployment = '',
    [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $WorkerDeployment = 'model-router',
    [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $AnswerDeployment = '',
    [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $VerifierDeployment = '',
    # Per-role reasoning effort; empty sends none (deployment default).
    [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $PlannerEffort = 'low',
    [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $WorkerEffort = 'low',
    [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $AnswerEffort = 'medium',
    [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $VerifierEffort = 'low',
    [ValidateRange(1, 6)] [int] $FacetConcurrency = 4,
    [ValidateRange(1, 24)] [int] $WorkerMaxClaims = 8,
    [ValidateRange(1, 4)] [int] $ExtractBatches = 1,
    [ValidateSet('recompose', 'prune')] [string] $RepairMode = 'recompose',
    # KG-2 pilot: released studies choose the documents of one strict search (needs 0021 + a release).
    [switch] $KgResults,
    # answer-eval: scorer arm label of this configuration, and the exposed/ledger funnel arms.
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')] [string] $AnswerArm = 'AGENT',
    [switch] $Diagnostics,
    # Also print the stages file (exposed source text, gzip+base64) to the pod log for a local audit.
    [switch] $ExportStages,
    [ValidatePattern('^[a-z0-9][a-z0-9.-]{0,62}$')] [string] $SearchRelease = 'crv-v1',
    [ValidatePattern('^crvkg-[0-9]{8}-[a-f0-9]{12}$')] [string] $KgRelease = 'crvkg-20261021-adb13a21c7da',
    [string] $AcrLoginServer = 'acrcrvscus.azurecr.io',
    [ValidatePattern('^[a-z0-9]{1,12}$')] [string] $RunId = (Get-Date -Format 'MMddHHmm'),
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [ValidateSet('A0', 'A1', 'LEX', 'P16', 'P12', 'P24', 'P16W2000', 'CAP', 'LLM', 'FULL50', 'DEC', 'KGB', 'CEIL', 'AAR')] [string[]] $Arm = @(),
    [ValidateSet('dev', 'heldout')] [string] $Split = 'dev',
    [switch] $AllowHeldout,
    [ValidatePattern('^[A-Za-z0-9]{1,8}$')] [string[]] $Only = @(),
    [ValidatePattern('^$|^[a-z0-9][a-z0-9-]{1,62}$')] [string] $KnowledgeBase = '',
    [ValidateRange(1, 8)] [int] $Concurrency = 3,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')] [string] $JudgeDeployment = 'gpt-5.4-mini',
    [ValidateRange(1, 5)] [int] $JudgeRepeats = 3,
    # Judge calls in flight; exposed-passage contexts (~50K tokens each) need 1-2 under 504K TPM.
    [ValidateRange(1, 8)] [int] $ScoreConcurrency = 6,
    [ValidateRange(900, 86400)] [int] $DeadlineSeconds = 14400,
    [ValidateRange(30, 600)] [int] $TurnSeconds = 180,
    # Evidence search of the evaluation jobs (the web app stays on Azure AI Search). postgres reads
    # crv_retrieval (pipeline migration 0022, retrieval-backfill); a Cohere cross-encoder needs
    # its Foundry rerank URL and model. -ArmLabel prefixes retrieval-eval arms (PG -> PG_A1).
    [ValidateSet('azure', 'postgres')] [string] $SearchBackend = 'azure',
    [ValidateSet('none', 'cohere')] [string] $PgReranker = 'none',
    [ValidatePattern('^$|^https://[A-Za-z0-9.\-/_]+$')] [string] $PgRerankerUrl = '',
    [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $PgRerankerModel = '',
    [ValidatePattern('^$|^[A-Za-z0-9-]{1,16}$')] [string] $ArmLabel = '',
    # retrieval-eval: the same arms on Azure (AZ_*) then PostgreSQL (PG_*), scored in one pass with
    # each PG arm paired to its AZ arm. -AnchorsOnly scores anchors without any judge call.
    [switch] $CompareBackends,
    [switch] $AnchorsOnly,
    [string] $GoldenPath = (Join-Path $PSScriptRoot '../../../golden/crv_golden.jsonl'),
    [string] $RenderPath = '',
    [switch] $Invoke
)

$ErrorActionPreference = 'Stop'
if ($Job -eq 'kg-login' -and -not $PipelineDigest) { throw '-Job kg-login needs -PipelineDigest.' }
if ($Job -ne 'kg-login' -and -not $AgentDigest) { throw "-Job $Job needs -AgentDigest." }
if ($Job -in 'retrieval-eval', 'answer-eval' -and -not $PipelineDigest) { throw "-Job $Job needs -PipelineDigest (scorer)." }
if ($Split -eq 'heldout' -and -not $AllowHeldout) { throw 'The held-out split needs -AllowHeldout (run once, for the frozen winner).' }
if (($CompareBackends -or $AnchorsOnly) -and $Job -ne 'retrieval-eval') { throw '-CompareBackends and -AnchorsOnly apply to -Job retrieval-eval.' }
if ($CompareBackends) {
    if ($SearchBackend -ne 'azure' -or $ArmLabel) { throw '-CompareBackends labels the runs itself (AZ, PG); drop -SearchBackend/-ArmLabel.' }
    if (-not $Arm) { throw '-CompareBackends needs explicit -Arm values (each arm runs twice).' }
}
$evalArgs = ''
foreach ($item in $Arm) { $evalArgs += ", --arm, $item" }
foreach ($item in $Only) { $evalArgs += ", --only, $item" }
if ($KnowledgeBase) { $evalArgs += ", --knowledge-base, $KnowledgeBase" }
if ($Split -eq 'heldout') { $evalArgs += ', --allow-heldout' }
$compareArgs = "$evalArgs, --label, PG"
if ($CompareBackends) { $evalArgs += ', --label, AZ' }
elseif ($ArmLabel) { $evalArgs += ", --label, $ArmLabel" }
if ($PgReranker -eq 'cohere' -and -not ($PgRerankerUrl -and $PgRerankerModel)) { throw '-PgReranker cohere needs -PgRerankerUrl and -PgRerankerModel.' }
$scoreReferences = if ($CompareBackends) { ($Arm | ForEach-Object { ", --reference, AZ_$_" }) -join '' } else { ', --reference, P16, --reference, A1' }
$answerArgs = ''
foreach ($item in $Only) { $answerArgs += ", --question-id, $item" }
if ($Split -eq 'heldout') { $answerArgs += ', --allow-heldout' }
if ($Diagnostics) { $answerArgs += ', --stages-out, /results/stages.jsonl' }
if ($ExportStages) {
    if (-not $Diagnostics) { throw '-ExportStages needs -Diagnostics.' }
    $answerArgs += ', --print-stages'
}
$zero = 'sha256:' + ('0' * 64)
$values = [ordered]@{
    AGENT_CLIENT_ID     = $AgentClientId.ToString()
    AGENT_PRINCIPAL_ID  = $AgentPrincipalId.ToString()
    AGENT_IDENTITY_NAME = $AgentIdentityName
    ADMIN_IDENTITY_NAME = $AdminIdentityName
    PG_HOST             = $PgHost
    SEARCH_ENDPOINT     = $SearchEndpoint
    OPENAI_ENDPOINT     = $OpenAiEndpoint
    PLANNER_DEPLOYMENT  = if ($PlannerDeployment) { $PlannerDeployment } else { $ChatDeployment }
    WORKER_DEPLOYMENT   = if ($WorkerDeployment) { $WorkerDeployment } else { $ChatDeployment }
    ANSWER_DEPLOYMENT   = if ($AnswerDeployment) { $AnswerDeployment } else { $ChatDeployment }
    VERIFIER_DEPLOYMENT = if ($VerifierDeployment) { $VerifierDeployment } else { $ChatDeployment }
    PLANNER_EFFORT      = $PlannerEffort
    WORKER_EFFORT       = $WorkerEffort
    ANSWER_EFFORT       = $AnswerEffort
    VERIFIER_EFFORT     = $VerifierEffort
    FACET_CONCURRENCY   = $FacetConcurrency.ToString()
    WORKER_MAX_CLAIMS   = $WorkerMaxClaims.ToString()
    EXTRACT_BATCHES     = $ExtractBatches.ToString()
    REPAIR_MODE         = $RepairMode
    KG_RESULTS          = if ($KgResults) { 'true' } else { 'false' }
    ANSWER_ARM          = $AnswerArm
    EVAL_DIAGNOSTICS    = if ($Diagnostics) { 'true' } else { 'false' }
    SCORE_CONTEXTS      = if ($Diagnostics) { '/results/stages.jsonl' } else { '/results/answers.jsonl' }
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
    JUDGE_DEPLOYMENT    = if ($AnchorsOnly) { '' } else { $JudgeDeployment }
    JUDGE_REPEATS       = $JudgeRepeats.ToString()
    SCORE_CONCURRENCY   = $ScoreConcurrency.ToString()
    DEADLINE_SECONDS    = $DeadlineSeconds.ToString()
    SEARCH_BACKEND      = $SearchBackend
    PG_RERANKER         = $PgReranker
    PG_RERANKER_URL     = $PgRerankerUrl
    PG_RERANKER_MODEL   = $PgRerankerModel
    COMPARE_EVAL_ARGS   = $compareArgs
    COMPARE_CONTEXTS    = if ($CompareBackends) { ', --contexts, /results/contexts-pg.jsonl' } else { '' }
    SCORE_REFERENCES    = $scoreReferences
}
foreach ($login in $AgentIdentityName, $AdminIdentityName) {
    if ($login -notmatch '^[a-z][a-z0-9-]{2,62}$') { throw "Unsafe login name: $login" }
}
$text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'research-agent.yaml') -Raw
$text = if ($CompareBackends) {
    [regex]::Replace($text, '(?m)^[ \t]*# (BEGIN|END) compare-backends[^\n]*\n', '')
} else {
    [regex]::Replace($text, '(?ms)^[ \t]*# BEGIN compare-backends.*?# END compare-backends[^\n]*\n', '')
}
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
