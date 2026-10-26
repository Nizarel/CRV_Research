#!/usr/bin/env pwsh
#Requires -Version 7.0
<#
.SYNOPSIS
Captures the full stakeholder suite and compares it using the official Copilot SDK.
.DESCRIPTION
Uses a previously provisioned isolated backend, freezes evaluator hashes, records
all 466 generation outcomes, and runs three Haiku 5.5 comparisons per valid answer.
No Azure-based judge is invoked. Existing answers and reports are never overwritten.
.PARAMETER RunId
The unique run ID of the prepared isolated backend and deployment snapshot.
.PARAMETER BaseUrl
Loopback port-forward URL of that backend.
.EXAMPLE
./Invoke-BusinessEval.ps1 -RunId manadry-20261024-190257 -BaseUrl http://127.0.0.1:21883
.NOTES
Logs and incremental JSONL remain in the run directory if execution is interrupted.
Unmeasured tool traces and attributable costs are not represented as zero.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z][a-z0-9-]{1,42}$')]
    [string] $RunId,
    [Parameter(Mandatory)]
    [ValidatePattern('^http://127\.0\.0\.1:[0-9]+$')]
    [string] $BaseUrl
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$Output = Join-Path $Repo "Docs/evaluation/copilot-extension/$RunId"
$Dataset = Join-Path $Repo 'golden/crv_golden_extension.jsonl'
$SnapshotPath = Join-Path $Output 'deployment-snapshot.json'
$Answers = Join-Path $Output 'answers.jsonl'
$Comparison = Join-Path $Output 'comparison-haiku55'
$BackendPython = Join-Path $Repo 'src/backend/.venv/Scripts/python.exe'
$JudgePython = Join-Path $Repo 'src/ingestion/.venv312/Scripts/python.exe'
if (-not (Test-Path $SnapshotPath)) { throw 'Prepare the isolated backend first' }
$Snapshot = Get-Content $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable
if ($Snapshot.agent_model_endpoint -ne 'https://manadry.cognitiveservices.azure.com/' -or
    $Snapshot.judge.model -ne 'claude-haiku-5.5' -or -not $Snapshot.package_root) {
    throw 'Snapshot must identify the verified external-agent / SDK-judge runtime'
}
$Questions = @(Get-Content $Dataset | ForEach-Object { $_ | ConvertFrom-Json })
if ($Questions.Count -ne 466 -or @($Questions.id | Sort-Object -Unique).Count -ne 466) {
    throw 'Expected exactly 466 unique stakeholder questions'
}
if ((Test-Path $Answers) -or (Test-Path $Comparison) -or
    (Test-Path (Join-Path $Output 'evaluation-config.json'))) {
    throw 'Refusing to overwrite an existing evaluation; use its documented resume procedure'
}
if (-not $PSCmdlet.ShouldProcess($RunId, 'Generate 466 Manadry answers and judge with SDK Haiku 5.5')) {
    return
}
$TrackedFiles = @(
    'golden/crv_golden_extension.jsonl',
    'src/backend/crv_agent/evaluation/cli.py',
    'src/backend/crv_agent/evaluation/runner.py',
    'src/ingestion/ingest_pipeline/tools/golden/answer_compare.py',
    'src/ingestion/ingest_pipeline/tools/golden/copilot_judge.py',
    'src/ingestion/ingest_pipeline/tools/golden/copilot_sdk_bridge.mjs'
)
$Hashes = @{}
foreach ($RelativePath in $TrackedFiles) {
    $Hashes[$RelativePath] = (Get-FileHash (Join-Path $Repo $RelativePath) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$Configuration = [ordered]@{
    run_id = $RunId; started_at = [DateTime]::UtcNow.ToString('o')
    questions = 466; arm = 'MANADRY'; base_url = $BaseUrl
    generation_concurrency = 2; judge_concurrency = 2; judge_repeats = 3
    judge_model = 'claude-haiku-5.5'; judge_transport = 'sdk-node'; sdk_version = '1.0.26'
    evaluator_sha256 = $Hashes
    runtime_snapshot_sha256 = (Get-FileHash $SnapshotPath -Algorithm SHA256).Hash.ToLowerInvariant()
    tool_trace_status = 'unmeasured'; attributable_agent_cost_status = 'unmeasured'
}
$Configuration | ConvertTo-Json -Depth 10 |
    Set-Content (Join-Path $Output 'evaluation-config.json') -Encoding utf8
Push-Location $Repo
try {
    Write-Output "Generation started: $RunId (466 questions, concurrency 2)"
    & $BackendPython -m crv_agent.evaluation.cli --base-url $BaseUrl --dataset $Dataset `
        --output (Join-Path $Output 'generation-summary.json') --answers-out $Answers `
        --attempts-out (Join-Path $Output 'generation-attempts.jsonl') `
        --arm MANADRY --concurrency 2 --wait-ready 30 --confirm-live `
        *> (Join-Path $Output 'generation.log')
    if ($LASTEXITCODE -ne 0) { throw 'Generation failed; inspect generation.log and incremental attempts' }
    $Records = @(Get-Content $Answers | ForEach-Object { $_ | ConvertFrom-Json })
    if ($Records.Count -ne 466 -or @($Records.id | Sort-Object -Unique).Count -ne 466) {
        throw 'Generation output must retain all 466 outcomes, including errors'
    }
    foreach ($RelativePath in $TrackedFiles) {
        if ((Get-FileHash (Join-Path $Repo $RelativePath) -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            $Hashes[$RelativePath]) { throw "Evaluator changed during capture: $RelativePath" }
    }
    Write-Output 'All 466 outcomes captured; official SDK judging started (three repeats, concurrency 2)'
    & $JudgePython -m ingest_pipeline.tools.golden compare-answers --golden $Dataset `
        --answers $Answers --out-dir $Comparison --transport sdk-node --model claude-haiku-5.5 `
        --repeats 3 --concurrency 2 --timeout 120 --confirm-copilot `
        *> (Join-Path $Output 'comparison.log')
    if ($LASTEXITCODE -ne 0) { throw 'SDK comparison failed; inspect comparison.log and judge-attempts.jsonl' }
    Write-Output "Full evaluation finished: $Output"
    Get-Content (Join-Path $Output 'generation.log')
    Get-Content (Join-Path $Output 'comparison.log')
}
finally {
    Pop-Location
}