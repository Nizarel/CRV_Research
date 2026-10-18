targetScope = 'resourceGroup'

@description('Location of the PostgreSQL Flexible Server.')
param postgresLocation string = 'northcentralus'

@description('Name of the PostgreSQL Flexible Server.')
param postgresServerName string

@description('SKU name for the PostgreSQL Flexible Server.')
param postgresSkuName string = 'Standard_B1ms'

@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
@description('SKU tier for PostgreSQL.')
param postgresSkuTier string = 'Burstable'

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@minValue(32)
@description('Storage size in GB.')
param postgresStorageSizeGB int = 32

@description('Name of the ingestion catalog database.')
param ingestionCatalogDatabaseName string = 'ingestion-catalog'

@description('Name of the agent memory database.')
param agentMemoryDatabaseName string = 'agent-memory'

@description('Name of the isolated PostgreSQL hybrid retrieval evaluation database.')
param retrievalEvalDatabaseName string = 'retrieval-eval'

@description('Resource ID of the private endpoints subnet in the existing VNet.')
param privateEndpointsSubnetId string

@description('Name of the existing VNet used for private DNS links.')
param virtualNetworkName string

@description('Principal object ID for PostgreSQL Entra admin (AKS workload identity principalId).')
param postgresAdminObjectId string

@description('Principal display name for PostgreSQL Entra admin (AKS workload identity name).')
param postgresAdminPrincipalName string

@allowed(['User', 'Group', 'ServicePrincipal', 'Unknown'])
@description('Principal type for PostgreSQL Entra admin.')
param postgresAdminPrincipalType string = 'ServicePrincipal'

@description('Tags applied to resources.')
param tags object = {}

var tenantId = tenant().tenantId
var privateDnsZoneName = 'privatelink.postgres.database.azure.com'
var privateEndpointName = '${postgresServerName}-pe'

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: postgresLocation
  tags: tags
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    version: postgresVersion
    createMode: 'Default'
    storage: {
      storageSizeGB: postgresStorageSizeGB
      autoGrow: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    network: {
      publicNetworkAccess: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
  }
}

resource postgresAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgresServer
  name: postgresAdminObjectId
  properties: {
    principalName: postgresAdminPrincipalName
    principalType: postgresAdminPrincipalType
    tenantId: tenantId
  }
}

resource extensionAllowlist 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'azure.extensions'
  properties: {
    value: 'vector,pg_trgm,unaccent,pg_stat_statements,age'
  }
}

resource sharedPreloadLibraries 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'shared_preload_libraries'
  dependsOn: [
    extensionAllowlist
  ]
  properties: {
    value: 'pg_cron,pg_stat_statements,age'
  }
}

resource ingestionCatalogDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: ingestionCatalogDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresAdmin
  ]
}

resource agentMemoryDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: agentMemoryDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresAdmin
  ]
}

resource retrievalEvalDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: retrievalEvalDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresAdmin
  ]
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: privateDnsZoneName
  location: 'global'
  tags: tags
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource vnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDnsZone
  name: '${virtualNetworkName}-dnslink'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: vnet.id
    }
    registrationEnabled: false
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: privateEndpointName
  location: resourceGroup().location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointsSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${privateEndpointName}-connection'
        properties: {
          privateLinkServiceId: postgresServer.id
          groupIds: [
            'postgresqlServer'
          ]
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: '${privateEndpointName}-group'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'postgres'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    vnetLink
  ]
}

output postgresServer string = postgresServer.name
output postgresFqdn string = postgresServer.properties.fullyQualifiedDomainName
output postgresLocationOutput string = postgresServer.location
output ingestionCatalogDatabase string = ingestionCatalogDb.name
output agentMemoryDatabase string = agentMemoryDb.name
output privateEndpoint string = privateEndpoint.name
