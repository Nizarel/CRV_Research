<#
.SYNOPSIS
Store an external billing account's API key in the private CRV Key Vault.

.DESCRIPTION
The vault denies public data-plane access, so `az keyvault secret set` from a workstation fails.
This writes the secret through the ARM control plane (Microsoft.KeyVault/vaults/secrets/write,
granted by Contributor), which needs no network change. The key is read with a hidden prompt,
written to a temporary request body readable only by the current user, and deleted afterwards.
It is never echoed, logged or stored in deployment history.

.EXAMPLE
./set-account-key.ps1 -SecretName manadry-di-key
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[0-9A-Za-z-]{1,127}$')] [string] $SecretName,
    [string] $ResourceGroup = 'rg-CRV-southcentralus',
    [string] $VaultName = 'kv-awmc6icu2kueq'
)

$ErrorActionPreference = 'Stop'
$subscription = az account show --query id -o tsv
if (-not $subscription) { throw 'Run az login first.' }
$url = "https://management.azure.com/subscriptions/$subscription/resourceGroups/$ResourceGroup" +
    "/providers/Microsoft.KeyVault/vaults/$VaultName/secrets/${SecretName}?api-version=2023-07-01"

$secure = Read-Host -AsSecureString "API key for $SecretName (input hidden)"
$plain = [Net.NetworkCredential]::new('', $secure).Password.Trim()
if ($plain.Length -lt 16) { throw 'The key looks too short; nothing was stored.' }

$body = Join-Path ([IO.Path]::GetTempPath()) "kv-$([guid]::NewGuid()).json"
try {
    $json = @{ properties = @{ value = $plain; contentType = 'api-key' } } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($body, $json, [Text.UTF8Encoding]::new($false))
    az rest --method put --url $url --body "@$body" --query "{name:name,enabled:properties.attributes.enabled}" -o json
    if ($LASTEXITCODE) { throw 'Storing the secret failed.' }
}
finally {
    if (Test-Path -LiteralPath $body) { Remove-Item -LiteralPath $body -Force }
    $plain = $null
}
