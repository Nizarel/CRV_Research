#Requires -Version 7.0
<#
.SYNOPSIS
    In-cluster benchmark of the deployed maintenance agent on approved golden cases.

.DESCRIPTION
    start   Captures a fresh release-scoped index inventory (trimmed to the declared sources),
            stages photo/drawing assets in blob storage by SHA-256, then runs the evaluator
            (from this checkout) as a Job against the backend Service, with the answer judge.
    status  Shows the Job and the last log lines.
    fetch   Saves the summaries, per-case results and the inventory under
            Docs/evidence/agent-benchmark/<run-id>/.
    cleanup Deletes the Job and its ConfigMap.

    -Evaluator graph runs evaluate_graph_api.py on the synthetic RDF registry pack instead
    (entity, graph-retrieval and answer layers; no index inventory or assets), optionally one -Split.

    -Publish sends per-case gen_ai.evaluation.result events and a maintenance.eval.run summary to
    Application Insights (role maintenance-evaluation). -Gate checks gates.json on its golden file;
    a failed gate makes that evaluator exit 1 and is recorded in the summary (deploy.ps1 -RequireGate).

    Uses direct kubectl to the private API server (VPN required).

.EXAMPLE
    .\infra\kubernetes\agent-benchmark\run-benchmark.ps1 -Mode start
    .\infra\kubernetes\agent-benchmark\run-benchmark.ps1 -Mode start -Publish -Gate
    .\infra\kubernetes\agent-benchmark\run-benchmark.ps1 -Mode fetch
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'fetch', 'cleanup', 'schedule', 'unschedule')] [string] $Mode = 'start',
    [ValidateSet('golden', 'graph')] [string] $Evaluator = 'golden',
    [ValidateSet('', 'dev', 'heldout')] [string] $Split = '',
    [string] $RunId = "agent-bench-$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmm'))",
    [string[]] $GoldenFiles = @('data/Golden/deep_eval/question_drafts.jsonl', 'data/Golden/maintenance_agent_golden.jsonl'),
    [string] $BaseUrl = 'http://backend.crv.svc.cluster.local',
    [string[]] $QuestionIds = @(),
    [int] $Concurrency = 2,
    [int] $TimeoutSeconds = 240,
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [string] $StorageAccount = 'stnyuwyhzhjriuu',
    [string] $AssetContainer = 'images',
    [switch] $Publish,
    [switch] $Gate,
    # schedule: weekdays while the cluster runs (it is stopped outside site hours).
    [string] $Schedule = '0 13 * * 1-5'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$name = 'crv-agent-benchmark'
$python = Join-Path $repoRoot 'src\backend\.venv312\Scripts\python.exe'

function Invoke-Az { $output = '' | & az.cmd @args --only-show-errors; if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed" }; $output }

function Get-KubectlArgs {
    # The API server IP moves across stop/start, so resolve it each time (see deploy.ps1).
    $nodeRg = (Invoke-Az aks show -g $ResourceGroup -n $Cluster --query nodeResourceGroup -o tsv).Trim()
    $zone = (Invoke-Az network private-dns zone list -g $nodeRg --query "[?ends_with(name, 'azmk8s.io')].name | [0]" -o tsv).Trim()
    $ip = (Invoke-Az network private-dns record-set a list -g $nodeRg -z $zone --query '[0].aRecords[0].ipv4Address' -o tsv).Trim()
    @("--server=https://${ip}:443", '--insecure-skip-tls-verify=true', '-n', 'crv')
}

$k = Get-KubectlArgs

# schedule stages the same pack into a CronJob instead of a Job. Each run names itself when it
# starts and uploads its results to <asset container>/eval/results/<run-id>/ (no log packing).
# The evaluator, golden files and index inventory are snapshots: re-run schedule to refresh them.
$scheduled = $Mode -in 'schedule', 'unschedule'
$stageId = $RunId
if ($scheduled) {
    $name = 'crv-agent-benchmark-nightly'
    $stageId = 'nightly'
    $RunId = '$RUN_ID'
}

switch ($Mode) {
    { $_ -in 'start', 'schedule' } {
        $stage = Join-Path $env:TEMP "crv-diag\agent-benchmark\$stageId"
        $evidence = if ($scheduled) { Join-Path $stage 'evidence' } else { Join-Path $repoRoot "Docs\evidence\agent-benchmark\$RunId" }
        if ($scheduled) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force $stage, $evidence | Out-Null
        $select = if ($QuestionIds) { " --question-ids $($QuestionIds -join ',')" } else { '' }
        $steps = @('set -u', 'python /evaluation/stage_assets.py --mounted /evaluation --work /work || exit 2', 'cd /work')
        if ($scheduled) { $steps = @('RUN_ID=nightly-$(date -u +%Y%m%dT%H%M)') + $steps }

        if ($Evaluator -eq 'graph') {
            # Only the chosen split goes into the ConfigMap; the whole pack is close to its size limit.
            $pack = Join-Path $repoRoot 'data\Golden\rdf_synthetic\questions.jsonl'
            $lines = @([IO.File]::ReadLines($pack) | Where-Object { $_.Trim() -and (-not $Split -or ($_ | ConvertFrom-Json).split -eq $Split) })
            [IO.File]::WriteAllLines((Join-Path $stage 'questions.jsonl'), [string[]]$lines, [Text.UTF8Encoding]::new($false))
            Set-Content -Encoding utf8NoBOM (Join-Path $stage 'assets.json') '{"container": "", "files": {}}'
            Copy-Item (Join-Path $repoRoot 'src\backend\evaluate_golden_api.py'), (Join-Path $repoRoot 'src\backend\evaluate_graph_api.py'), (Join-Path $PSScriptRoot 'stage_assets.py') $stage -Force
            $splitArg = if ($Split) { " --split $Split" } else { '' }
            $steps += "python /work/evaluate_graph_api.py --base-url $BaseUrl --questions /work/questions.jsonl --output /work/results-graph.json --timeout $TimeoutSeconds --concurrency $Concurrency --judge --run-id $RunId$splitArg$select > /work/summary-graph.json; echo `"EXIT graph `$?`"; echo `"SUMMARY graph `$(cat /work/summary-graph.json)`""
        }
        else {
            $golden = @($GoldenFiles | ForEach-Object { Get-Item (Join-Path $repoRoot $_) })

            $inventoryArgs = @((Join-Path $repoRoot 'src\backend\capture_index_inventory.py'), '--output', (Join-Path $stage 'inventory.json'))
            foreach ($file in $golden) { $inventoryArgs += @('--golden-file', $file.FullName) }
            & $python @inventoryArgs
            if ($LASTEXITCODE -ne 0) { throw 'Index inventory capture failed.' }

            # Attachments resolve relative to their golden file; the Job copies every file into one /work dir.
            $files = [ordered]@{}
            foreach ($file in $golden) {
                Copy-Item $file.FullName $stage -Force
                foreach ($line in [IO.File]::ReadLines($file.FullName)) {
                    if (-not $line.Trim()) { continue }
                    $photos = ($line | ConvertFrom-Json).PSObject.Properties['photos']
                    foreach ($relative in @(if ($photos) { $photos.Value })) {
                        if (-not $relative) { continue }
                        $local = Join-Path $file.DirectoryName $relative
                        $sha = (Get-FileHash -Algorithm SHA256 $local).Hash.ToLowerInvariant()
                        if ($files.Contains($relative) -and $files[$relative].sha256 -ne $sha) { throw "Conflicting assets for $relative" }
                        $files[$relative] = [ordered]@{ sha256 = $sha; blob = "eval/assets/$sha$([IO.Path]::GetExtension($local))"; local = $local }
                    }
                }
            }
            foreach ($entry in $files.Values) {
                $exists = (Invoke-Az storage blob exists --account-name $StorageAccount -c $AssetContainer -n $entry.blob --auth-mode login --query exists -o tsv).Trim()
                if ($exists -ne 'true') {
                    Invoke-Az storage blob upload --account-name $StorageAccount -c $AssetContainer -n $entry.blob -f $entry.local --auth-mode login --overwrite false | Out-Null
                }
            }
            $manifest = [ordered]@{ container = $AssetContainer; files = [ordered]@{} }
            foreach ($key in $files.Keys) { $manifest.files[$key] = [ordered]@{ sha256 = $files[$key].sha256; blob = $files[$key].blob } }
            $manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8NoBOM (Join-Path $stage 'assets.json')
            Copy-Item (Join-Path $repoRoot 'src\backend\evaluate_golden_api.py'), (Join-Path $PSScriptRoot 'stage_assets.py') $stage -Force
            Copy-Item (Join-Path $stage 'inventory.json'), (Join-Path $stage 'assets.json') $evidence -Force
            $gateFile = ''
            if ($Gate) {
                Copy-Item (Join-Path $PSScriptRoot 'gates.json') $stage -Force
                $gateFile = (Get-Content (Join-Path $PSScriptRoot 'gates.json') -Raw | ConvertFrom-Json).golden_file
                if (-not ($golden | Where-Object Name -eq $gateFile)) { throw "-Gate needs $gateFile in -GoldenFiles." }
            }

            foreach ($file in $golden) {
                $stem = $file.BaseName
                $extra = ''
                if ($Publish) { $extra += ' --publish' }
                if ($Gate -and $file.Name -eq $gateFile) { $extra += ' --gates /work/gates.json' }
                $steps += "python /work/evaluate_golden_api.py --base-url $BaseUrl --golden-file /work/$($file.Name) --index-inventory /work/inventory.json --output /work/results-$stem.json --timeout $TimeoutSeconds --concurrency $Concurrency --judge --run-id $RunId-$stem$select$extra > /work/summary-$stem.json; echo `"EXIT $stem `$?`"; echo `"SUMMARY $stem `$(head -n 1 /work/summary-$stem.json)`""
            }
        }

        $bytes = (Get-ChildItem $stage -File | Measure-Object Length -Sum).Sum
        if ($bytes -gt 900KB) { throw "Benchmark ConfigMap would be $bytes bytes (limit 1 MiB)." }
        $steps += if ($scheduled) {
            @('python /work/stage_assets.py --work /work --upload "eval/results/$RUN_ID"')
        } else {
            @('echo RESULTS-BEGIN', 'python /work/stage_assets.py --work /work --pack', 'echo RESULTS-END')
        }
        $image = (kubectl @k get deploy backend -o 'jsonpath={.spec.template.spec.containers[0].image}')
        $labels = @{ 'app.kubernetes.io/part-of' = 'crv'; 'app.kubernetes.io/component' = 'agent-benchmark' }
        $job = [ordered]@{
            apiVersion = 'batch/v1'; kind = 'Job'
            metadata = @{ name = $name; namespace = 'crv'; labels = $labels; annotations = @{ 'crv/run-id' = $RunId } }
            spec = @{
                backoffLimit = 0; activeDeadlineSeconds = 7200; ttlSecondsAfterFinished = 86400
                template = @{
                    metadata = @{ labels = $labels + @{ 'azure.workload.identity/use' = 'true' } }
                    spec = @{
                        restartPolicy = 'Never'; serviceAccountName = 'crv'
                        securityContext = @{ runAsNonRoot = $true; runAsUser = 1001; runAsGroup = 1001; seccompProfile = @{ type = 'RuntimeDefault' } }
                        containers = @(@{
                            name = 'benchmark'; image = $image; command = @('/bin/sh', '-c'); args = @($steps -join "`n")
                            envFrom = @(@{ configMapRef = @{ name = 'crv-agent-settings' } })
                            env = @(@{ name = 'OTEL_SERVICE_NAME'; value = 'maintenance-evaluation' })
                            resources = @{ requests = @{ cpu = '250m'; memory = '256Mi' }; limits = @{ cpu = '1'; memory = '1Gi' } }
                            volumeMounts = @(
                                @{ name = 'evaluation'; mountPath = '/evaluation'; readOnly = $true },
                                @{ name = 'work'; mountPath = '/work' },
                                @{ name = 'tmp'; mountPath = '/tmp' })
                            securityContext = @{ allowPrivilegeEscalation = $false; readOnlyRootFilesystem = $true; capabilities = @{ drop = @('ALL') } }
                        })
                        volumes = @(
                            @{ name = 'evaluation'; configMap = @{ name = $name } },
                            @{ name = 'work'; emptyDir = @{ sizeLimit = '1Gi' } },
                            @{ name = 'tmp'; emptyDir = @{} })
                    }
                }
            }
        }
        $jobPath = Join-Path (Split-Path $stage) "$stageId-job.json"
        if ($scheduled) {
            $cron = [ordered]@{
                apiVersion = 'batch/v1'; kind = 'CronJob'
                metadata = @{ name = $name; namespace = 'crv'; labels = $labels }
                spec = @{
                    schedule = $Schedule; timeZone = 'Etc/UTC'; concurrencyPolicy = 'Forbid'
                    startingDeadlineSeconds = 3600; successfulJobsHistoryLimit = 3; failedJobsHistoryLimit = 3
                    jobTemplate = @{ spec = $job.spec }
                }
            }
            $cron | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8NoBOM $jobPath
        }
        else {
            $job | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8NoBOM $jobPath
            kubectl @k delete job $name --ignore-not-found --wait=true | Out-Null
        }

        # Server-side apply: a client-side apply copies the data into a 256 KiB annotation.
        kubectl @k create configmap $name --from-file=$stage --dry-run=client -o yaml | kubectl @k apply --server-side --force-conflicts -f - | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'ConfigMap apply failed.' }
        if ($scheduled) {
            kubectl @k apply -f $jobPath
            if ($LASTEXITCODE -ne 0) { throw 'CronJob apply failed.' }
            "scheduled: $name '$Schedule' UTC  image: $image  results: $AssetContainer/eval/results/<run-id>/"
            break
        }
        kubectl @k create -f $jobPath
        if ($LASTEXITCODE -ne 0) { throw 'Job creation failed.' }
        "run-id: $RunId  image: $image  evidence: $evidence"
    }
    'unschedule' {
        kubectl @k delete cronjob $name --ignore-not-found
        kubectl @k delete configmap $name --ignore-not-found
    }
    'status' {
        kubectl @k get job $name -o wide
        kubectl @k logs "job/$name" --tail=6 2>$null | ForEach-Object { $_.Substring(0, [Math]::Min(300, $_.Length)) }
    }
    'fetch' {
        $runId = kubectl @k get job $name -o 'jsonpath={.metadata.annotations.crv/run-id}'
        $evidence = Join-Path $repoRoot "Docs\evidence\agent-benchmark\$runId"
        New-Item -ItemType Directory -Force $evidence | Out-Null
        $logs = (kubectl @k logs "job/$name") -join "`n"
        $logs -split "`n" | Where-Object { $_ -match '^(EXIT|SUMMARY) ' } | ForEach-Object { $_ }
        $match = [regex]::Match($logs, 'RESULTS-BEGIN\s+(\S+)\s+RESULTS-END')
        if (-not $match.Success) { throw 'No packed results in the Job log yet.' }
        $buffer = [IO.MemoryStream]::new([Convert]::FromBase64String($match.Groups[1].Value))
        $gzip = [IO.Compression.GZipStream]::new($buffer, [IO.Compression.CompressionMode]::Decompress)
        $reader = [IO.StreamReader]::new($gzip, [Text.Encoding]::UTF8)
        $bundle = Join-Path $evidence 'results-bundle.json'
        Set-Content -Encoding utf8NoBOM -NoNewline -Path $bundle -Value $reader.ReadToEnd()
        $reader.Dispose()
        & $python -c "import json,pathlib,sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); [pathlib.Path(sys.argv[2], k).write_text(json.dumps(v, ensure_ascii=False, indent=2) + chr(10), encoding='utf-8') for k, v in d.items()]" $bundle $evidence
        Remove-Item $bundle
        $logs -split "`n" | Where-Object { $_ -match '^SUMMARY ' } | Set-Content -Encoding utf8NoBOM (Join-Path $evidence 'summaries.txt')
        $evidence
    }
    'cleanup' {
        kubectl @k delete job $name --ignore-not-found
        kubectl @k delete configmap $name --ignore-not-found
    }
}
