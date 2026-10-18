metadata description = 'Creates an Azure Key Vault with RBAC authorization for the ingestion pipeline.'

@description('Name of the Key Vault.')
param name string

@description('Location for the Key Vault.')
param location string = resourceGroup().location

@description('Tags to apply to the Key Vault.')
param tags object = {}

@description('Tenant ID for the Key Vault.')
param tenantId string = subscription().tenantId

@allowed(['Enabled', 'Disabled'])
@description('Public network access. Set to Disabled when using private endpoints.')
param publicNetworkAccess string = 'Enabled'

@allowed(['Allow', 'Deny'])
@description('Default network ACL action when public access is enabled.')
param networkDefaultAction string = 'Allow'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // RBAC authorization instead of access policies (managed-identity-first).
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccess == 'Enabled' ? networkDefaultAction : 'Deny'
    }
  }
}

output id string = keyVault.id
output name string = keyVault.name
output uri string = keyVault.properties.vaultUri
