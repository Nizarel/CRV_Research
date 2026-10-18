#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$PlanPath = (Join-Path $PSScriptRoot '../../.azure/ingestion-pilot-plan.json'),
    [switch]$Apply,
    [switch]$VerifyServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$plan = Get-Content -Raw $PlanPath | ConvertFrom-Json -AsHashtable
if ($plan.status -notin @('approved', 'deployed')) { throw 'Pilot plan is not approved.' }
$subscription = $plan.subscriptionId
$resourceGroup = $plan.resourceGroup
$clusterName = $plan.aks.cluster
$cluster = az aks show --subscription $subscription -g $resourceGroup -n $clusterName --only-show-errors -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not read AKS state.' }
if ($cluster.powerState.code -ne 'Running' -or $cluster.provisioningState -ne 'Succeeded') { throw 'AKS is not ready.' }
$zone = $cluster.privateFqdn.Split('.', 2)[1]
$recordName = $cluster.privateFqdn.Split('.')[0]
$address = az network private-dns record-set a show --subscription $subscription -g $cluster.nodeResourceGroup -z $zone -n $recordName --query 'aRecords[0].ipv4Address' --only-show-errors -o tsv
if ($LASTEXITCODE -ne 0 -or -not $address) { throw 'Could not resolve private AKS address.' }
$kubectlArguments = @('--context', $clusterName, "--server=https://$($address.Trim()):443", "--tls-server-name=$($cluster.privateFqdn)", '-n', 'crv')
$scaledJob = kubectl @kubectlArguments get scaledjob ingestion-consumer -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not read ingestion ScaledJob.' }
if ($scaledJob.metadata.annotations.'autoscaling.keda.sh/paused' -ne 'true') { throw 'Legacy ingestion must remain paused.' }
$clientId = az identity show --subscription $subscription -g $resourceGroup -n $plan.retainedResources.workloadIdentity --query clientId --only-show-errors -o tsv
if ($LASTEXITCODE -ne 0 -or -not $clientId) { throw 'Could not resolve workload identity.' }
$image = $scaledJob.spec.jobTargetRef.template.spec.containers[0].image
if ($image -notmatch '@sha256:') {
    $reference = $image.Substring($image.IndexOf('/') + 1)
    $registry = $image.Split('.')[0]
    $digest = az acr repository show --subscription $subscription -n $registry --image $reference --query digest --only-show-errors -o tsv
    if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Could not pin bootstrap image digest.' }
    $image = ($image -replace ':[^/]+$', '') + '@' + $digest.Trim()
}
$sqlDirectory = Join-Path $PSScriptRoot '../sql/ingestion-catalog'
kubectl @kubectlArguments create configmap ingestion-pilot-bootstrap --from-file="apply-pilot.py=$sqlDirectory/apply-pilot.py" --from-file="prerequisites.sql=$sqlDirectory/0002_knowledge_prerequisites.sql" --dry-run=client -o json | kubectl @kubectlArguments apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Could not stage bootstrap scripts.' }
$arguments = @('/bootstrap/apply-pilot.py', '--sql', '/bootstrap/prerequisites.sql')
$mode = 'apply'
if (-not $Apply) { $arguments += '--dry-run'; $mode = 'dryrun' }
if ($VerifyServices) { $arguments += '--verify-services' }
$job = @{
    apiVersion = 'batch/v1'
    kind = 'Job'
    metadata = @{ generateName = "ingestion-pilot-db-$mode-"; namespace = 'crv' }
    spec = @{
        backoffLimit = 0
        activeDeadlineSeconds = 300
        ttlSecondsAfterFinished = 600
        template = @{
            metadata = @{ labels = @{ 'azure.workload.identity/use' = 'true'; 'app.kubernetes.io/name' = 'ingestion-pilot-bootstrap' } }
            spec = @{
                restartPolicy = 'Never'
                serviceAccountName = $scaledJob.spec.jobTargetRef.template.spec.serviceAccountName
                securityContext = @{ runAsNonRoot = $true; runAsUser = 1000; runAsGroup = 1000; fsGroup = 1000; seccompProfile = @{ type = 'RuntimeDefault' } }
                containers = @(@{
                    name = 'bootstrap'
                    image = $image
                    command = @('python')
                    args = $arguments
                    env = @(
                        @{ name = 'AZURE_CLIENT_ID'; value = $clientId.Trim() },
                        @{ name = 'PG_HOST'; value = "$($plan.postgres.server).postgres.database.azure.com" },
                        @{ name = 'PG_USER'; value = $plan.retainedResources.workloadIdentity },
                        @{ name = 'PG_DATABASE'; value = $plan.postgres.database },
                        @{ name = 'CU_ENDPOINT'; value = "https://$($plan.contentUnderstanding.account).cognitiveservices.azure.com" },
                        @{ name = 'SEARCH_ENDPOINT'; value = "https://$($plan.retainedResources.search).search.windows.net" },
                        @{ name = 'SEARCH_INDEX'; value = 'maint-evidence-pilot-20261007' },
                        @{ name = 'FUSEKI_ENDPOINT'; value = 'http://crv-fuseki-pilot.crv.svc.cluster.local:3030' },
                        @{ name = 'PYTHONDONTWRITEBYTECODE'; value = '1' }
                    )
                    resources = @{ requests = @{ cpu = '100m'; memory = '128Mi' }; limits = @{ cpu = '500m'; memory = '512Mi' } }
                    securityContext = @{ allowPrivilegeEscalation = $false; readOnlyRootFilesystem = $true; capabilities = @{ drop = @('ALL') } }
                    volumeMounts = @(@{ name = 'bootstrap'; mountPath = '/bootstrap'; readOnly = $true }, @{ name = 'scratch'; mountPath = '/tmp' })
                })
                volumes = @(@{ name = 'bootstrap'; configMap = @{ name = 'ingestion-pilot-bootstrap' } }, @{ name = 'scratch'; emptyDir = @{ sizeLimit = '64Mi' } })
            }
        }
    }
}
$created = $job | ConvertTo-Json -Depth 20 | kubectl @kubectlArguments create -f - -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not create bootstrap Job.' }
$jobName = $created.metadata.name
Write-Output "Bootstrap job: $jobName ($mode), image: $image"
kubectl @kubectlArguments wait --for=condition=complete "job/$jobName" --timeout=270s
$completed = $LASTEXITCODE -eq 0
kubectl @kubectlArguments logs "job/$jobName"
if (-not $completed) { throw "Bootstrap did not complete: $jobName" }