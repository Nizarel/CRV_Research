targetScope = 'resourceGroup'

param contentUnderstandingAccountName string = 'cu-nyuwyhzhjriuu'
param postgresServerName string = 'psql-nyuwyhzhjriuu-ncus-085512'
param completionDeploymentName string = 'crv-gpt-5.4-mini'
param embeddingDeploymentName string = 'crv-embedding-3-large'

@minValue(1)
param completionCapacity int = 10

@minValue(1)
param embeddingCapacity int = 20

param extensionAllowlist string = 'vector,pg_trgm,unaccent,pg_stat_statements,age'
param preloadLibraries string = 'pg_cron,pg_stat_statements,age'

resource cu 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: contentUnderstandingAccountName
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

resource completion 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: cu
  name: completionDeploymentName
  sku: {
    name: 'DataZoneStandard'
    capacity: completionCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: '2026-03-17'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: cu
  name: embeddingDeploymentName
  dependsOn: [
    completion
  ]
  sku: {
    name: 'DataZoneStandard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-large'
      version: '1'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: extensionAllowlist
    source: 'user-override'
  }
}

resource preload 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'shared_preload_libraries'
  dependsOn: [
    extensions
  ]
  properties: {
    value: preloadLibraries
    source: 'user-override'
  }
}

output completionDeployment string = completion.name
output embeddingDeployment string = embedding.name
output contentUnderstandingEndpoint string = cu.properties.endpoint