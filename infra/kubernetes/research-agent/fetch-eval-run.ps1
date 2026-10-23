<#
.SYNOPSIS
Fetch an answer-eval run's artifacts from the private cluster into a local folder.

.DESCRIPTION
Reads the job's container logs through `az aks command invoke` in base64 chunks (the command
output is capped near 512 KB and decoded as cp1252 otherwise) and writes, when present:
report.json and answers.jsonl (answers container), stages.jsonl (gzip+base64 block printed with
-ExportStages; contains exposed source text, keep local) and score.json (score container).

.EXAMPLE
./fetch-eval-run.ps1 -RunId aud1022 -OutDir $env:TEMP/crv-eval/aud
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z0-9]{1,12}$')] [string] $RunId,
    [Parameter(Mandatory)] [string] $OutDir,
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [ValidateRange(10000, 360000)] [int] $ChunkBytes = 300000
)

$ErrorActionPreference = 'Stop'

function Invoke-Cluster([string] $Command) {
    $output = az aks command invoke -g $ResourceGroup -n $Cluster --command $Command --query logs -o tsv --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az aks command invoke failed with exit code $LASTEXITCODE" }
    ($output -join '').Trim()
}

function Get-ContainerLog([string] $Container) {
    $logs = "kubectl -n crv logs job/crv-agent-answer-eval-$RunId -c $Container --tail=-1"
    $size = [int](Invoke-Cluster "$logs | wc -c")
    $buffer = [IO.MemoryStream]::new()
    for ($offset = 1; $offset -le $size; $offset += $ChunkBytes) {
        $chunk = [Convert]::FromBase64String((Invoke-Cluster "$logs | tail -c +$offset | head -c $ChunkBytes | base64 -w0"))
        $buffer.Write($chunk, 0, $chunk.Length)
    }
    [Text.Encoding]::UTF8.GetString($buffer.ToArray())
}

function Get-Block([string] $Text, [string] $Name) {
    $match = [regex]::Match($Text, "=== $Name BEGIN ===\s*(.*?)\s*=== $Name END ===", 'Singleline')
    if ($match.Success) { $match.Groups[1].Value } else { $null }
}

$utf8 = [Text.UTF8Encoding]::new($false)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$written = @()

$answersLog = Get-ContainerLog 'answers'
if ($report = Get-Block $answersLog 'AGENT EVAL REPORT') {
    [IO.File]::WriteAllText((Join-Path $OutDir 'report.json'), $report, $utf8); $written += 'report.json'
}
if ($answers = Get-Block $answersLog 'AGENT ANSWERS') {
    $lines = @($answers -split "`n" | Where-Object { $_.Trim().StartsWith('{') })
    [IO.File]::WriteAllLines((Join-Path $OutDir 'answers.jsonl'), [string[]]$lines, $utf8); $written += "answers.jsonl ($($lines.Count))"
}
if ($stages = Get-Block $answersLog 'AGENT STAGES GZIP') {
    $compressed = [IO.MemoryStream]::new([Convert]::FromBase64String($stages.Trim()))
    $gzip = [IO.Compression.GZipStream]::new($compressed, [IO.Compression.CompressionMode]::Decompress)
    $file = [IO.File]::Create((Join-Path $OutDir 'stages.jsonl'))
    try { $gzip.CopyTo($file) } finally { $file.Dispose(); $gzip.Dispose() }
    $written += 'stages.jsonl'
}
if ($score = Get-Block (Get-ContainerLog 'score') 'AGENT SCORE') {
    [IO.File]::WriteAllText((Join-Path $OutDir 'score.json'), $score, $utf8); $written += 'score.json'
}
Write-Output "Fetched $RunId to ${OutDir}: $($written -join ', ')"
