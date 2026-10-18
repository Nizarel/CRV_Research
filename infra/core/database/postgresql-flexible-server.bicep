metadata description = 'Creates an Azure Database for PostgreSQL Flexible Server with Microsoft Entra-only authentication, private-endpoint-ready networking, and one or more databases. Used as the ingestion catalog/state store and the agent memory backend (LangGraph Postgres checkpointer + store with pgvector).'

@description('Name of the PostgreSQL Flexible Server.')
param name string

@description('Location for the server.')
param location string = resourceGroup().location

@description('Tags to apply to the server.')
param tags object = {}

@description('Compute SKU name. Burstable avoids zone-redundant capacity constraints.')
param skuName string = 'Standard_B1ms'

@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
@description('Compute tier for the SKU.')
param skuTier string = 'Burstable'

@description('PostgreSQL major version.')
param version string = '16'

@minValue(32)
@description('Storage size in GB (minimum 32).')
param storageSizeGB int = 32

@allowed(['Enabled', 'Disabled'])
@description('Public network access. Disabled when fronted by a private endpoint.')
param publicNetworkAccess string = 'Disabled'

@description('Backup retention in days.')
param backupRetentionDays int = 7

@description('Databases to create on the server.')
param databaseNames array = []

@description('Microsoft Entra administrators: array of objects with objectId, principalName, and principalType.')
param administrators array = []

var tenantId = tenant().tenantId
var allowedExtensions = 'vector,pg_trgm,unaccent,pg_stat_statements'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: version
    createMode: 'Default'
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    network: {
      publicNetworkAccess: publicNetworkAccess
    }
    highAvailability: {
      mode: 'Disabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
  }
}

resource extensionAllowlist 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: allowedExtensions
    source: 'user-override'
  }
}

// Microsoft Entra administrators (required at creation because password auth is disabled).
resource entraAdmins 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = [
  for admin in administrators: {
    parent: server
    name: admin.objectId
    properties: {
      principalName: admin.principalName
      principalType: admin.principalType
      tenantId: tenantId
    }
  }
]

// Databases. Depend on the admin so the server has finished its auth configuration first.
resource databases 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = [
  for dbName in databaseNames: {
    parent: server
    name: dbName
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
    dependsOn: [
      entraAdmins
    ]
  }
]

output id string = server.id
output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
