#Requires -Version 7.0
<#
.SYNOPSIS
Prepare and run the Phase 2 evidence smoke run or the Phase 3 bake-off, one gated step at a time.

.DESCRIPTION
Reads .azure/ingestion-smoke-plan.json (or -PlanPath .azure/ingestion-bakeoff-plan.json).
Preflight, CuCheck, Status and Bakeoff are read-only. Every step that changes Azure, the
registry, the database, the cluster or source storage needs -Apply, plan status 'approved' and
its own scopeApproval flag; without -Apply each step only previews.

.EXAMPLE
./invoke-evidence-smoke.ps1 -Step Preflight
./invoke-evidence-smoke.ps1 -Step Infra            # what-if only
./invoke-evidence-smoke.ps1 -Step Infra -Apply     # needs scopeApproval.deployPipelineInfra
./invoke-evidence-smoke.ps1 -Step Replay -JobId urn:crv:job:... -Reason 'fix deployed' -Actor me -Apply
./invoke-evidence-smoke.ps1 -PlanPath ../../../.azure/ingestion-bakeoff-plan.json -Step EnqueueArm -Policy bakeoff-cu-first-v1 -Apply
./invoke-evidence-smoke.ps1 -PlanPath ../../../.azure/ingestion-rollout-plan.json -Step SearchIndex   # preview; -Apply needs scopeApproval.createSearchIndex
./invoke-evidence-smoke.ps1 -PlanPath ../../../.azure/ingestion-rollout-plan.json -Step EnqueueZone -Zone '12-Pont bascule' -Apply
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Preflight', 'Start', 'Infra', 'Image', 'Database', 'SearchIndex', 'Deploy', 'CuCheck', 'Upload', 'Enqueue', 'EnqueueArm', 'EnqueueZone', 'Status', 'Replay', 'Bakeoff')]
    [string] $Step,
    [string] $PlanPath = (Join-Path $PSScriptRoot '../../../.azure/ingestion-smoke-plan.json'),
    [string] $JobId,
    [string] $Reason,
    [string] $Actor,
    [string] $Policy,
    [string] $Zone,
    [switch] $Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$plan = Get-Content -Raw -LiteralPath $PlanPath | ConvertFrom-Json -AsHashtable
$sub = $plan.subscriptionId
$rg = $plan.resourceGroup
$ns = $plan.aks.namespace
$image = "$($plan.image.registry).azurecr.io/$($plan.image.repository):$($plan.image.tag)"
$workerIdentity = 'id-crv-pipeline-worker'
$bakeoff = if ($plan.ContainsKey('bakeoff')) { $plan.bakeoff } else { $null }
$rollout = if ($plan.ContainsKey('rollout')) { $plan.rollout } else { $null }
$billing = if ($plan.ContainsKey('billing')) { $plan.billing } else { $null }
$cuEndpoint = if ($plan.providers.ContainsKey('contentUnderstandingEndpoint')) { $plan.providers.contentUnderstandingEndpoint } else { '' }

function Get-Sample {
    if (-not $bakeoff) { throw 'This plan has no bakeoff section.' }
    return Get-Content -Raw -LiteralPath (Join-Path $repo $bakeoff.samplePath) -Encoding utf8 | ConvertFrom-Json -AsHashtable
}

function Get-PlanRender {
    $render = @{}
    if ($cuEndpoint) {
        $render.CuEndpoint = $cuEndpoint
        $render.CuPageBudget = $plan.spendCap.contentUnderstandingPages
        $render.CuTokenBudget = $plan.spendCap.contentUnderstandingTokens
        $render.CuMaxInflight = $plan.providers.contentUnderstandingMaxInflight
        if ($plan.spendCap.ContainsKey('estTokensPerPage')) { $render.CuEstTokensPerPage = $plan.spendCap.estTokensPerPage }
    }
    if ($plan.providers.ContainsKey('documentIntelligenceMaxInflight')) { $render.DiMaxInflight = $plan.providers.documentIntelligenceMaxInflight }
    if ($rollout) {
        $render.RoutingPolicy = $rollout.routingPolicy
        $render.PoliciesPath = Join-Path $repo $rollout.policiesPath
    }
    if ($billing) {
        $accounts = @(Get-BillingAccounts)
        $document = [ordered]@{
            budget_usd = $billing.budgetUsd
            di_page_usd = $billing.prices.diPageUsd
            cu_page_estimate_usd = $billing.prices.cuPageEstimateUsd
            cu_meter_usd = $billing.prices.cuMeterUsd
            accounts = @($accounts | ForEach-Object {
                $entry = [ordered]@{ name = $_.name; budget_usd = $_.budgetUsd; key_file = "/var/run/secrets/crv/accounts/$($_.name)" }
                if ($_.diEndpoint) { $entry.di_endpoint = $_.diEndpoint }
                if ($_.cuEndpoint) { $entry.cu_endpoint = $_.cuEndpoint }
                if ($_.ContainsKey('cuMaxInflight')) { $entry.cu_max_inflight = $_.cuMaxInflight }
                if ($_.ContainsKey('diMaxInflight')) { $entry.di_max_inflight = $_.diMaxInflight }
                $entry
            })
        }
        $path = Join-Path ([IO.Path]::GetTempPath()) "crv-billing-$([guid]::NewGuid()).json"
        [IO.File]::WriteAllText($path, ($document | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
        $render.BillingPath = $path
        $render.AccountKeySecrets = @{}
        foreach ($account in $accounts) { $render.AccountKeySecrets[$account.name] = $account.keySecretName }
        if ($accounts.Count) {
            $render.KeyVaultName = $billing.keyVault
            $render.TenantId = $plan.tenantId
        }
    }
    return $render
}

# External billing accounts take part only once their endpoints are filled in.
function Get-BillingAccounts {
    return @($billing.accounts | Where-Object { $_.diEndpoint -or $_.cuEndpoint })
}

function Assert-Approved([string] $Flag) {
    if ($plan.status -ne 'approved') { throw "Plan status is '$($plan.status)'; review it and set status to 'approved' first." }
    if (-not $plan.scopeApproval[$Flag]) { throw "scopeApproval.$Flag is false in $PlanPath." }
}

# Piped stdin keeps az non-interactive: an unreachable registry otherwise waits on an admin "Username:" prompt.
function Invoke-Az {
    $output = '' | & az @args --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed: $output" }
    return $output
}

function Get-OptionalAz {
    $output = '' | & az @args --only-show-errors 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $output
}

function Get-ClientId([string] $Name) {
    return (Invoke-Az identity show --subscription $sub -g $rg -n $Name --query clientId -o tsv).Trim()
}

function Get-KubectlArgs {
    $cluster = Invoke-Az aks show --subscription $sub -g $rg -n $plan.aks.cluster -o json | ConvertFrom-Json
    if ($cluster.powerState.code -ne 'Running' -or $cluster.provisioningState -ne 'Succeeded') { throw 'AKS is not running; use -Step Start.' }
    $zone = $cluster.privateFqdn.Split('.', 2)[1]
    $record = $cluster.privateFqdn.Split('.')[0]
    $address = (Invoke-Az network private-dns record-set a show --subscription $sub -g $cluster.nodeResourceGroup -z $zone -n $record --query 'aRecords[0].ipv4Address' -o tsv).Trim()
    return @('--context', $plan.aks.cluster, "--server=https://${address}:443", "--tls-server-name=$($cluster.privateFqdn)", '-n', $ns)
}

function Assert-LegacyPaused([string[]] $Kubectl) {
    $scaledJob = kubectl @Kubectl get scaledjob $plan.aks.legacyScaledJob -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the legacy ScaledJob.' }
    if ($scaledJob.metadata.annotations.'autoscaling.keda.sh/paused' -ne 'true') { throw 'Legacy ingestion must stay paused.' }
}

function Write-BlobList {
    $names = if ($bakeoff) { @((Get-Sample).documents | ForEach-Object { $_.blob_name }) } else { @($plan.sample | ForEach-Object { $_.blobName }) }
    if ($names.Count -ne (@($names | Sort-Object -Unique)).Count) { throw 'Duplicate blob names in the sample.' }
    $path = Join-Path ([IO.Path]::GetTempPath()) 'crv-smoke-blobs.txt'
    [IO.File]::WriteAllText($path, (($names -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
    return $path
}

function Set-FileConfigMap([string[]] $Kubectl, [string] $Name, [string] $Key, [string] $Path) {
    Set-FilesConfigMap $Kubectl $Name @{ $Key = $Path }
}

function Set-FilesConfigMap([string[]] $Kubectl, [string] $Name, [hashtable] $Files) {
    # kubectl reads the files itself: piping through PowerShell can re-encode non-ASCII blob names.
    kubectl @Kubectl delete configmap $Name --ignore-not-found | Out-Null
    $sources = @($Files.Keys | ForEach-Object { "--from-file=$_=$($Files[$_])" })
    kubectl @Kubectl create configmap $Name @sources | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create ConfigMap $Name." }
}

function Invoke-ClusterJob {
    param([string[]] $Kubectl, [string] $Prefix, [string] $ServiceAccount, [string] $ClientId,
          [string[]] $Command, [string[]] $Arguments, [hashtable] $Env = @{}, [string] $ConfigMap = '',
          [switch] $UsePipelineConfig, [int] $TimeoutSeconds = 600)
    $envList = @(@{ name = 'AZURE_CLIENT_ID'; value = $ClientId })
    foreach ($key in $Env.Keys) { $envList += @{ name = $key; value = [string]$Env[$key] } }
    $mounts = @(@{ name = 'scratch'; mountPath = '/tmp' })
    $volumes = @(@{ name = 'scratch'; emptyDir = @{ sizeLimit = '256Mi' } })
    if ($ConfigMap) {
        $mounts += @{ name = 'smoke'; mountPath = '/smoke'; readOnly = $true }
        $volumes += @{ name = 'smoke'; configMap = @{ name = $ConfigMap } }
    }
    $container = @{
        name = 'smoke'; image = $image; args = $Arguments; env = $envList; volumeMounts = $mounts
        resources = @{ requests = @{ cpu = '100m'; memory = '256Mi' }; limits = @{ cpu = '1'; memory = '1Gi' } }
        securityContext = @{ allowPrivilegeEscalation = $false; readOnlyRootFilesystem = $true; capabilities = @{ drop = @('ALL') } }
    }
    if ($Command) { $container.command = $Command }
    if ($UsePipelineConfig) {
        $container.envFrom = @(@{ configMapRef = @{ name = 'crv-pipeline' } })
        # The pipeline ConfigMap points at the mounted analyzer registry and routing policies.
        $mounts += @{ name = 'cu-config'; mountPath = '/etc/crv/cu'; readOnly = $true }
        $volumes += @{ name = 'cu-config'; configMap = @{ name = 'crv-pipeline-cu' } }
        $container.volumeMounts = $mounts
    }
    $job = @{
        apiVersion = 'batch/v1'; kind = 'Job'
        metadata = @{ generateName = "$Prefix-"; namespace = $ns;         labels = @{ 'app.kubernetes.io/part-of' = 'crv-pipeline' } }
        spec = @{
            backoffLimit = 0; activeDeadlineSeconds = $TimeoutSeconds; ttlSecondsAfterFinished = 3600
            template = @{
                metadata = @{ labels = @{ 'azure.workload.identity/use' = 'true';                 'app.kubernetes.io/part-of' = 'crv-pipeline' } }
                spec = @{
                    restartPolicy = 'Never'; serviceAccountName = $ServiceAccount
                    securityContext = @{ runAsNonRoot = $true; runAsUser = 10001; runAsGroup = 10001; seccompProfile = @{ type = 'RuntimeDefault' } }
                    containers = @($container); volumes = $volumes
                }
            }
        }
    }
    $created = $job | ConvertTo-Json -Depth 20 -EscapeHandling EscapeNonAscii | kubectl @Kubectl create -f - -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not create $Prefix Job." }
    $name = $created.metadata.name
    Write-Output "Job $name started"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds + 30)
    do {
        Start-Sleep -Seconds 5
        $state = kubectl @Kubectl get job $name -o json | ConvertFrom-Json
        $done = ($state.status.PSObject.Properties.Name -contains 'succeeded') -or ($state.status.PSObject.Properties.Name -contains 'failed')
    } until ($done -or (Get-Date) -gt $deadline)
    kubectl @Kubectl logs "job/$name"
    if (-not ($state.status.PSObject.Properties.Name -contains 'succeeded')) { throw "Job $name did not succeed." }
}

switch ($Step) {
    'Preflight' {
        $checks = [System.Collections.Generic.List[object]]::new()
        function Add-Check([string] $Name, [string] $Status, [string] $Detail = '') { $checks.Add([pscustomobject]@{ Check = $Name; Status = $Status; Detail = $Detail }) }
        $account = (Invoke-Az account show --query id -o tsv).Trim()
        Add-Check 'subscription' ($(if ($account -eq $sub) { 'ok' } else { 'WRONG' })) $account
        $aks = Invoke-Az aks show --subscription $sub -g $rg -n $plan.aks.cluster --query '{p:powerState.code,s:provisioningState}' -o json | ConvertFrom-Json
        Add-Check 'aks' ($(if ($aks.p -eq 'Running' -and $aks.s -eq 'Succeeded') { 'ok' } else { 'stopped' })) "$($aks.p)/$($aks.s)"
        $pg = (Invoke-Az postgres flexible-server show --subscription $sub -g $rg -n $plan.postgres.server --query state -o tsv).Trim()
        Add-Check 'postgres' ($(if ($pg -eq 'Ready') { 'ok' } else { 'stopped' })) $pg
        foreach ($name in @($plan.infra.parameters.documentIntelligenceAccountName, $plan.infra.parameters.embeddingAccountName, $plan.infra.parameters.contentUnderstandingAccountName) | Where-Object { $_ } | Sort-Object -Unique) {
            $found = Get-OptionalAz cognitiveservices account show --subscription $sub -g $rg -n $name --query properties.endpoint -o tsv
            Add-Check "account $name" ($(if ($found) { 'ok' } else { 'MISSING' })) "$found"
        }
        $deployment = Get-OptionalAz cognitiveservices account deployment show --subscription $sub -g $rg -n $plan.infra.parameters.embeddingAccountName --deployment-name $plan.providers.embeddingDeployment --query properties.provisioningState -o tsv
        Add-Check 'embedding deployment' ($(if ($deployment -match 'Succeeded') { 'ok' } else { 'MISSING' })) "$($plan.providers.embeddingDeployment) $deployment"
        $search = Get-OptionalAz search service show --subscription $sub -g $rg -n $plan.infra.parameters.searchServiceName --query status -o tsv
        Add-Check 'search service' ($(if ($search) { 'ok' } else { 'MISSING' })) "$search"
        foreach ($name in $plan.infra.identities) {
            $found = Get-OptionalAz identity show --subscription $sub -g $rg -n $name --query clientId -o tsv
            Add-Check "identity $name" ($(if ($found) { 'ok' } else { 'not deployed' })) "$found"
        }
        foreach ($pool in @('io', 'cpu', 'heavy', 'publish')) {
            $found = Get-OptionalAz servicebus queue show --subscription $sub -g $rg --namespace-name $plan.infra.parameters.serviceBusNamespaceName -n "ingest-$pool" --query status -o tsv
            Add-Check "queue ingest-$pool" ($(if ($found) { 'ok' } else { 'not deployed' })) "$found"
        }
        $tags = Get-OptionalAz acr repository show-tags --subscription $sub -n $plan.image.registry --repository $plan.image.repository -o tsv
        Add-Check "image $($plan.image.tag)" ($(if ($tags -and ($tags -contains $plan.image.tag)) { 'ok' } else { 'not pushed' })) $image
        & az bicep build --file (Join-Path $repo $plan.infra.template) --stdout *> $null
        Add-Check 'bicep build' ($(if ($LASTEXITCODE -eq 0) { 'ok' } else { 'FAILED' }))
        $rendered = Join-Path ([IO.Path]::GetTempPath()) "crv-smoke-render-$([guid]::NewGuid()).yaml"
        try {
            $planRender = Get-PlanRender
            & (Join-Path $PSScriptRoot 'render-pipeline.ps1') -CoordinatorClientId ([guid]::Empty) -WorkerClientId ([guid]::Empty) -ImageTag $plan.image.tag `
                -DiEndpoint $plan.providers.documentIntelligenceEndpoint -DiPageBudget $plan.spendCap.documentIntelligencePages `
                -OpenAiEndpoint $plan.providers.embeddingEndpoint -EmbeddingDeployment $plan.providers.embeddingDeployment `
                -SearchEndpoint $plan.providers.searchEndpoint -SearchIndexName $plan.providers.searchIndex -RenderPath $rendered `
                @planRender
            Add-Check 'manifest render' 'ok' 'offline, placeholder identities'
        }
        catch { Add-Check 'manifest render' 'FAILED' $_.Exception.Message }
        finally { Remove-Item -LiteralPath $rendered -Force -ErrorAction SilentlyContinue }
        if ($billing) {
            $ready = @(Get-BillingAccounts)
            $order = @("this subscription USD $($billing.budgetUsd)") + @($ready | ForEach-Object { "$($_.name) USD $(if ($null -ne $_.budgetUsd) { $_.budgetUsd } else { 'unlimited' })" })
            Add-Check 'billing order' 'ok' ($order -join ', then ')
            foreach ($account in @($billing.accounts)) {
                Add-Check "billing account $($account.name)" ($(if ($ready -contains $account) { 'ok' } else { 'no endpoint' })) "key $($billing.keyVault)/$($account.keySecretName); DI $($account.diEndpoint); CU $($account.cuEndpoint)"
            }
            $vault = Get-OptionalAz keyvault show --subscription $sub -g $rg -n $billing.keyVault --query name -o tsv
            Add-Check "key vault $($billing.keyVault)" ($(if ($vault) { 'ok' } else { 'MISSING' })) 'secrets are checked by the IO workers at startup'
        }
        if ($rollout) {
            $policies = (Get-Content -Raw -LiteralPath (Join-Path $repo $rollout.policiesPath) | ConvertFrom-Json -AsHashtable).policies
            Add-Check "routing policy $($rollout.routingPolicy)" ($(if ($policies.ContainsKey($rollout.routingPolicy)) { 'ok' } else { 'UNKNOWN' })) "release $($policies[$rollout.routingPolicy].release_id)"
            $unsafe = @($rollout.zones | Where-Object { $zone = $_; @($rollout.excludedPrefixes | Where-Object { $zone.StartsWith($_) }).Count })
            Add-Check 'zones' ($(if ($unsafe.Count) { 'EXCLUDED PREFIX' } else { 'ok' })) "$(@($rollout.zones).Count) zones; excluded $($rollout.excludedPrefixes -join ', ')"
        }
        elseif ($bakeoff) {
            $list = Write-BlobList
            $sample = Get-Sample
            foreach ($document in @($sample.documents | Where-Object { $_.upload_from })) {
                $local = Join-Path $repo $document.upload_from
                $hash = if (Test-Path -LiteralPath $local) { (Get-FileHash -LiteralPath $local -Algorithm SHA256).Hash.ToLowerInvariant() } else { 'missing' }
                Add-Check "upload source $($document.id)" ($(if ($hash -eq $document.sha256) { 'ok' } else { 'MISMATCH' })) "$($document.upload_from) -> $($document.blob_name)"
            }
            Add-Check 'sample' 'ok' "$(@($sample.documents).Count) documents, $($sample.estimate.pages) pages, CU worst case $($sample.estimate.cu_pages_upper_bound) of cap $($plan.spendCap.contentUnderstandingPages); list at $list"
            foreach ($arm in $bakeoff.armsToEnqueue) {
                $known = (Get-Content -Raw -LiteralPath (Join-Path $repo $bakeoff.policiesPath) | ConvertFrom-Json -AsHashtable).policies.ContainsKey($arm)
                Add-Check "arm $arm" ($(if ($known) { 'ok' } else { 'UNKNOWN' }))
            }
        }
        else {
            $list = Write-BlobList
            $hashes = @($plan.sample | ForEach-Object { $_.contentHash } | Sort-Object -Unique).Count
            Add-Check 'sample' 'ok' "$(@($plan.sample).Count) placements, $hashes contents, list at $list"
        }
        Add-Check 'plan approval' ($(if ($plan.status -eq 'approved') { 'ok' } else { 'pending' })) "status=$($plan.status)"
        $checks | Format-Table -AutoSize | Out-String -Width 200
    }
    'Start' {
        if (-not $Apply) { Write-Output 'Would run start-environment.ps1 and start PostgreSQL (use -Apply).'; return }
        Assert-Approved 'startEnvironment'
        & (Join-Path $PSScriptRoot '../start-environment.ps1') -ResourceGroup $rg -AksClusterName $plan.aks.cluster
        Invoke-Az postgres flexible-server start --subscription $sub -g $rg -n $plan.postgres.server | Out-Null
        Write-Output 'AKS and PostgreSQL started.'
    }
    'Infra' {
        $oidc = (Invoke-Az aks show --subscription $sub -g $rg -n $plan.aks.cluster --query oidcIssuerProfile.issuerUrl -o tsv).Trim()
        $parameters = @("oidcIssuerUrl=$oidc") + @($plan.infra.parameters.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
        $accountKeys = @(if ($billing) { Get-BillingAccounts | ForEach-Object { $_.keySecretName } })
        if ($accountKeys.Count) {
            # Array parameters go through a file: az.cmd mangles quoted JSON on the command line.
            $keyParameters = Join-Path ([IO.Path]::GetTempPath()) "crv-account-keys-$([guid]::NewGuid()).json"
            $content = @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'
                parameters = @{ keyVaultName = @{ value = $billing.keyVault }; accountKeySecretNames = @{ value = $accountKeys } } }
            [IO.File]::WriteAllText($keyParameters, ($content | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
            $parameters += "@$keyParameters"
        }
        $template = Join-Path $repo $plan.infra.template
        if (-not $Apply) {
            & az deployment group what-if --subscription $sub -g $rg -n $plan.infra.deploymentName -f $template --parameters @parameters
            if ($LASTEXITCODE -ne 0) { throw 'what-if failed.' }
            return
        }
        Assert-Approved 'deployPipelineInfra'
        Invoke-Az deployment group create --subscription $sub -g $rg -n $plan.infra.deploymentName -f $template --parameters @parameters --query properties.outputs -o json
    }
    'Image' {
        $build = @('buildx', 'build', '--platform', 'linux/amd64', '--target', 'runtime', '-t', $image, '--push', '-f', (Join-Path $repo 'Dockerfile.pipeline'), $repo)
        if (-not $Apply) { Write-Output "Would run: az acr login -n $($plan.image.registry); docker $($build -join ' ')"; return }
        Assert-Approved 'pushImage'
        Invoke-Az acr login --subscription $sub -n $plan.image.registry | Out-Null
        & docker @build
        if ($LASTEXITCODE -ne 0) { throw 'Image build or push failed.' }
    }
    'Database' {
        Assert-Approved 'databaseMigrations'
        $kubectl = Get-KubectlArgs
        $adminClientId = Get-ClientId $plan.postgres.adminIdentity
        $pgHost = "$($plan.postgres.server).postgres.database.azure.com"
        $migrate = @('migrate'); if (-not $Apply) { $migrate += '--dry-run' }
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-smoke-migrate' -ServiceAccount $plan.postgres.adminServiceAccount -ClientId $adminClientId `
            -Arguments $migrate -Env @{ TIZ_DB__HOST = $pgHost; TIZ_DB__USER = $plan.postgres.adminIdentity; TIZ_DB__DATABASE = $plan.postgres.database }
        Assert-Approved 'databasePrincipals'
        $script = Join-Path $repo 'infra/sql/ingestion-catalog/grant-pipeline-roles.py'
        Set-FileConfigMap $kubectl 'crv-smoke-scripts' 'grant-pipeline-roles.py' $script
        $grant = @('/smoke/grant-pipeline-roles.py') + @($plan.infra.identities | ForEach-Object { '--principal'; $_ })
        if ($Apply) { $grant += '--apply' }
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-smoke-grants' -ServiceAccount $plan.postgres.adminServiceAccount -ClientId $adminClientId `
            -Command @('python') -Arguments $grant -ConfigMap 'crv-smoke-scripts' -Env @{ PG_HOST = $pgHost; PG_USER = $plan.postgres.adminIdentity }
    }
    'SearchIndex' {
        # Create-only: analyzers and field attributes are immutable, so a schema change ships as a
        # new index name. The search identity calls its own query deployment for query vectors so agent
        # queries never share throttling with ingestion embeddings.
        if (-not $plan.providers.ContainsKey('searchIndexDefinition')) { throw 'This plan has no providers.searchIndexDefinition.' }
        $definition = Get-Content -Raw -LiteralPath (Join-Path $repo $plan.providers.searchIndexDefinition) -Encoding utf8
        $definition = $definition.Replace('${EMBEDDING_RESOURCE_URI}', $plan.providers.embeddingEndpoint.TrimEnd('/'))
        $definition = $definition.Replace('${QUERY_EMBEDDING_DEPLOYMENT}', $plan.providers.queryEmbeddingDeployment)
        if ($definition -match '\$\{\w+\}') { throw "Unrendered placeholder $($Matches[0]) in the index definition." }
        $index = $definition | ConvertFrom-Json -AsHashtable
        if ($index.name -ne $plan.providers.searchIndex) { throw "Definition $($index.name) is not providers.searchIndex $($plan.providers.searchIndex)." }
        # az.cmd breaks on the indexes('name') form: cmd.exe treats the parentheses as syntax.
        $url = "$($plan.providers.searchEndpoint.TrimEnd('/'))/indexes/$($index.name)?api-version=2026-04-01"
        $exists = Get-OptionalAz rest --method get --url $url --resource 'https://search.azure.com' --query name -o tsv
        $service = Invoke-Az search service show --subscription $sub -g $rg -n $plan.infra.parameters.searchServiceName --query '{principal:identity.principalId, semantic:semanticSearch}' -o json | ConvertFrom-Json
        $account = (Invoke-Az cognitiveservices account show --subscription $sub -g $rg -n $plan.infra.parameters.embeddingAccountName --query id -o tsv).Trim()
        $role = 'Cognitive Services OpenAI User'
        $granted = Get-OptionalAz role assignment list --assignee $service.principal --role $role --scope $account --query '[0].id' -o tsv
        Write-Output "Index $($index.name): $(if ($exists) { 'exists' } else { 'absent' }); $(@($index.fields).Count) fields; semantic ranker plan $($service.semantic)"
        Write-Output "Vectorizer role '$role' for the search identity: $(if ($granted) { 'granted' } else { 'missing' })"
        if (-not $Apply) { return }
        Assert-Approved 'createSearchIndex'
        if (-not $granted) {
            Invoke-Az role assignment create --assignee-object-id $service.principal --assignee-principal-type ServicePrincipal --role $role --scope $account | Out-Null
            Write-Output 'Vectorizer role granted.'
        }
        if ($exists) { Write-Output 'Index exists; nothing created.'; return }
        $body = Join-Path ([IO.Path]::GetTempPath()) "$($index.name).json"
        [IO.File]::WriteAllText($body, $definition, [Text.UTF8Encoding]::new($false))
        Invoke-Az rest --method put --url $url --resource 'https://search.azure.com' --headers 'If-None-Match=*' 'Content-Type=application/json' --body "@$body" | Out-Null
        Write-Output "Created $($index.name)."
    }
    'Deploy' {
        $kubectl = Get-KubectlArgs
        Assert-LegacyPaused $kubectl
        if ($Apply) { Assert-Approved 'deployWorkloads' }
        $render = @{
            CoordinatorClientId = Get-ClientId $plan.infra.identities[0]
            WorkerClientId = Get-ClientId $plan.infra.identities[1]
            ImageTag = $plan.image.tag
            DiEndpoint = $plan.providers.documentIntelligenceEndpoint
            DiPageBudget = $plan.spendCap.documentIntelligencePages
            OpenAiEndpoint = $plan.providers.embeddingEndpoint
            EmbeddingDeployment = $plan.providers.embeddingDeployment
            SearchEndpoint = $plan.providers.searchEndpoint
            SearchIndexName = $plan.providers.searchIndex
            KubectlArgs = $kubectl
        }
        $render += Get-PlanRender
        & (Join-Path $PSScriptRoot 'render-pipeline.ps1') @render -Apply:$Apply
    }
    'Enqueue' {
        $list = Write-BlobList
        if (-not $Apply) { Write-Output "Would enqueue $(@($plan.sample).Count) placements (page cap $($plan.spendCap.documentIntelligencePages)) from $list"; Get-Content -LiteralPath $list -Encoding utf8; return }
        Assert-Approved 'paidAnalysis'
        $kubectl = Get-KubectlArgs
        Assert-LegacyPaused $kubectl
        Set-FileConfigMap $kubectl         'crv-smoke-blobs' 'blobs.txt' $list
                Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-smoke-enqueue' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments @('enqueue-snapshots', '--blob-list', '/smoke/blobs.txt') -ConfigMap 'crv-smoke-blobs' -UsePipelineConfig `
            -Env @{ TIZ_DB__USER = $workerIdentity }
    }
    'EnqueueZone' {
        # Bulk rollout: one top-level zone folder at a time, planned by the deployed routing policy.
        if (-not $rollout) { throw 'EnqueueZone needs the rollout plan.' }
        if ($Zone -cnotin $rollout.zones) { throw "-Zone must be one of: $($rollout.zones -join ' | ')" }
        foreach ($excluded in $rollout.excludedPrefixes) { if ($Zone.StartsWith($excluded)) { throw "Zone $Zone is under the excluded prefix $excluded." } }
        $arguments = @('enqueue-snapshots', '--prefix', "$($Zone.TrimEnd('/'))/")
        if (-not $Apply) { Write-Output "Would run: tizagent-pipeline $($arguments -join ' ') under routing policy $($rollout.routingPolicy)"; return }
        Assert-Approved 'paidAnalysis'
        Assert-Approved 'bulkIngestion'
        $kubectl = Get-KubectlArgs
        Assert-LegacyPaused $kubectl
        $deployed = kubectl @kubectl get configmap crv-pipeline -o 'jsonpath={.data.TIZ_ROUTING_POLICY}'
        if ($LASTEXITCODE -ne 0 -or $deployed -cne $rollout.routingPolicy) { throw "Deployed routing policy is '$deployed', not '$($rollout.routingPolicy)'; run Deploy first." }
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-rollout-enqueue' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments $arguments -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity } -TimeoutSeconds 1800
    }
    'Status' {
        if ($plan.status -ne 'approved') { throw 'Plan is not approved.' }
        $kubectl = Get-KubectlArgs
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-smoke-status' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments @('status') -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity } -TimeoutSeconds 180
    }
    'Replay' {
        if (-not ($JobId -and $Reason -and $Actor)) { throw 'Replay needs -JobId, -Reason and -Actor.' }
        $replay = @('replay', '--job-id', $JobId, '--reason', $Reason, '--actor', $Actor)
        if (-not $Apply) { Write-Output "Would run: tizagent-pipeline $($replay -join ' ')"; return }
        Assert-Approved 'paidAnalysis'
        $kubectl = Get-KubectlArgs
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-smoke-replay' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments $replay -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity } -TimeoutSeconds 180
    }
    'CuCheck' {
        # Read-only: reads model defaults and analyzer definitions with the worker identity.
        if (-not $cuEndpoint) { throw 'The plan has no contentUnderstandingEndpoint.' }
        if ($plan.status -ne 'approved') { throw 'Plan is not approved.' }
        $kubectl = Get-KubectlArgs
        Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-cu-check' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments @('cu-check') -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity } -TimeoutSeconds 180
    }
    'Upload' {
        # Sources not in the corpus are uploaded once under the pilot prefix; never overwritten.
        $sample = Get-Sample
        $account = $plan.infra.parameters.storageAccountName
        $container = $plan.infra.parameters.sourceContainerName
        foreach ($document in @($sample.documents | Where-Object { $_.upload_from })) {
            $local = Join-Path $repo $document.upload_from
            $hash = (Get-FileHash -LiteralPath $local -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($hash -ne $document.sha256) { throw "$($document.upload_from) hash $hash differs from the sample's $($document.sha256)." }
            if (-not $Apply) { Write-Output "Would upload $local -> $container/$($document.blob_name) (content_hash=$hash, no overwrite)"; continue }
            Assert-Approved 'uploadPilotSource'
            Invoke-Az storage blob upload --subscription $sub --auth-mode login --account-name $account -c $container -n $document.blob_name `
                -f $local --metadata "content_hash=$hash" --overwrite false | Out-Null
            Write-Output "Uploaded $($document.blob_name)"
        }
    }
    'EnqueueArm' {
        if (-not $bakeoff) { throw 'EnqueueArm needs the bake-off plan.' }
        if ($Policy -notin $bakeoff.armsToEnqueue) { throw "-Policy must be one of: $($bakeoff.armsToEnqueue -join ', ')" }
        $list = Write-BlobList
        $arguments = @('enqueue-arm', '--policy', $Policy, '--blob-list', '/smoke/blobs.txt')
        if (-not $Apply) { Write-Output "Would run: tizagent-pipeline $($arguments -join ' ') (CU caps $($plan.spendCap.contentUnderstandingPages) pages / $($plan.spendCap.contentUnderstandingTokens) tokens)"; return }
        Assert-Approved 'paidAnalysis'
        $kubectl = Get-KubectlArgs
        Assert-LegacyPaused $kubectl
        Set-FileConfigMap $kubectl 'crv-smoke-blobs' 'blobs.txt' $list
                Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-bakeoff-arm' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Arguments $arguments -ConfigMap 'crv-smoke-blobs' -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity }
    }
    'Bakeoff' {
        # Read-only: collects each arm's evidence and spend from the ledger and scores it.
        if (-not $bakeoff) { throw 'Bakeoff needs the bake-off plan.' }
        if ($plan.status -ne 'approved') { throw 'Plan is not approved.' }
        $kubectl = Get-KubectlArgs
        Set-FilesConfigMap $kubectl 'crv-bakeoff-inputs' @{
            'sample.json' = (Join-Path $repo $bakeoff.samplePath)
            'gold.jsonl' = (Join-Path $repo $bakeoff.goldPath)
        }
        $output = Invoke-ClusterJob -Kubectl $kubectl -Prefix 'crv-bakeoff-score' -ServiceAccount 'crv-pipeline-worker' -ClientId (Get-ClientId $workerIdentity) `
            -Command @('python', '-m', 'tizertmaint_pipeline.tools.bakeoff') -Arguments @('score', '--sample', '/smoke/sample.json', '--gold', '/smoke/gold.jsonl') `
            -ConfigMap 'crv-bakeoff-inputs' -UsePipelineConfig -Env @{ TIZ_DB__USER = $workerIdentity } -TimeoutSeconds 900
        $dir = Join-Path $repo $bakeoff.reportDir
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $logs = Join-Path $dir 'score-job.log'
        [IO.File]::WriteAllText($logs, (($output | ForEach-Object { "$_" }) -join "`n"), [Text.UTF8Encoding]::new($false))
        $python = Join-Path $repo 'src/ingestion/.venv312/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
        Push-Location (Join-Path $repo 'src/ingestion')
        try {
            & $python -m tizertmaint_pipeline.tools.bakeoff render --report $logs --out (Join-Path $dir 'report.md') --json-out (Join-Path $dir 'report.json')
            if ($LASTEXITCODE -ne 0) { throw 'Rendering the bake-off report failed.' }
        }
        finally { Pop-Location }
        Write-Output "Report written to $($bakeoff.reportDir)"
    }
}
