<#
.SYNOPSIS
Render (and optionally apply) the CRV research agent web pod: nginx + SPA + research backend.

.DESCRIPTION
Substitutes every ${VAR} in research-agent-web.yaml and refuses to continue if any placeholder
survives. Images are referenced only by digest. The cluster is private: -Invoke uses
`az aks command invoke`, applies the manifest and waits for the rollout.
-Remove deletes the Deployment, Service and Ingress (the kill switch; no sign-in protects the app).
#>
[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Deploy')] [ValidatePattern('^sha256:[a-f0-9]{64}$')] [string] $WebDigest,
    [Parameter(Mandatory, ParameterSetName = 'Deploy')] [ValidatePattern('^sha256:[a-f0-9]{64}$')] [string] $AgentDigest,
    [Parameter(ParameterSetName = 'Deploy')] [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')] [string] $ChatDeployment = 'gpt-6.1-sol',
    [Parameter(ParameterSetName = 'Deploy')] [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $PlannerDeployment = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $WorkerDeployment = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $AnswerDeployment = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidatePattern('^$|^[A-Za-z0-9._-]{1,64}$')] [string] $VerifierDeployment = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $PlannerEffort = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $WorkerEffort = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $AnswerEffort = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidateSet('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh')] [string] $VerifierEffort = '',
    [Parameter(ParameterSetName = 'Deploy')] [ValidateRange(1, 6)] [int] $FacetConcurrency = 4,
    [Parameter(ParameterSetName = 'Deploy')] [ValidateRange(30, 280)] [int] $TurnSeconds = 180,
    [Parameter(ParameterSetName = 'Deploy')] [string] $AcrLoginServer = 'acrcrvscus.azurecr.io',
    [Parameter(ParameterSetName = 'Deploy')] [string] $RenderPath = '',
    [Parameter(Mandatory, ParameterSetName = 'Remove')] [switch] $Remove,
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $Cluster = 'aks-crv-southcentralus',
    [switch] $Invoke
)

$ErrorActionPreference = 'Stop'

function Invoke-Cluster([string] $Command, [string] $File = '') {
    $arguments = @('aks', 'command', 'invoke', '-g', $ResourceGroup, '-n', $Cluster, '--command', $Command, '-o', 'json', '--only-show-errors')
    if ($File) { $arguments += @('--file', $File) }
    az @arguments
    if ($LASTEXITCODE -ne 0) { throw "az aks command invoke failed with exit code $LASTEXITCODE" }
}

if ($Remove) {
    $selector = 'app.kubernetes.io/name=crv-research-web'
    $command = "kubectl -n crv delete ingress,service,deployment -l $selector --ignore-not-found"
    if ($Invoke) { Invoke-Cluster $command; Write-Output 'Removed crv-research-web.' }
    else { Write-Output "Would run: $command (add -Invoke)" }
    return
}

# TurnSeconds must stay below the gateway request-timeout annotation (300 s) and nginx read timeout.
$chat = $ChatDeployment
$values = [ordered]@{
    ACR_LOGIN_SERVER    = $AcrLoginServer
    WEB_DIGEST          = $WebDigest
    AGENT_DIGEST        = $AgentDigest
    PLANNER_DEPLOYMENT  = if ($PlannerDeployment) { $PlannerDeployment } else { $chat }
    WORKER_DEPLOYMENT   = if ($WorkerDeployment) { $WorkerDeployment } else { $chat }
    ANSWER_DEPLOYMENT   = if ($AnswerDeployment) { $AnswerDeployment } else { $chat }
    VERIFIER_DEPLOYMENT = if ($VerifierDeployment) { $VerifierDeployment } else { $chat }
    PLANNER_EFFORT      = $PlannerEffort
    WORKER_EFFORT       = $WorkerEffort
    ANSWER_EFFORT       = $AnswerEffort
    VERIFIER_EFFORT     = $VerifierEffort
    FACET_CONCURRENCY   = $FacetConcurrency.ToString()
    TURN_SECONDS        = $TurnSeconds.ToString()
}
$text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'research-agent-web.yaml') -Raw
foreach ($key in $values.Keys) {
    if ($values[$key] -match '["\r\n]') { throw "Value for $key contains a quote or newline" }
    $text = $text.Replace('${' + $key + '}', $values[$key])
}
$leftovers = [regex]::Matches($text, '\$\{[A-Za-z_][A-Za-z0-9_]*\}') | ForEach-Object Value | Sort-Object -Unique
if ($leftovers) { throw "Unresolved placeholders: $($leftovers -join ', ')" }

$path = if ($RenderPath) { $RenderPath } else { Join-Path ([IO.Path]::GetTempPath()) 'crv-research-web.yaml' }
Set-Content -LiteralPath $path -Value $text -Encoding utf8 -NoNewline
Write-Output "Rendered crv-research-web to $path"
if ($Invoke) {
    $name = Split-Path -Leaf $path
    Invoke-Cluster "kubectl apply -f $name && kubectl -n crv rollout status deployment/crv-research-web --timeout=420s" $path
    Write-Output 'Applied crv-research-web.'
}
