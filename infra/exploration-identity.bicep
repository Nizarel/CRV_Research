// Exploration workload identity, federation and least-privilege role assignments.

@description('Name of the user-assigned identity for the exploration workstream')
param identityName string

@description('Azure region')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes namespace running the exploration jobs')
param namespace string = 'crv-exploration'

@description('Kubernetes service account used by the exploration jobs')
param serviceAccountName string = 'crv-exploration'

@description('Name of the existing container registry')
param containerRegistryName string

@description('Name of the existing Foundry / Cognitive Services account')
param foundryAccountName string

@description('Name of the existing storage account holding exploration source documents')
param storageAccountName string

@description('Blob container holding exploration source documents')
param explorationContainerName string = 'explorastore'

@description('Blob container holding cached DI/CU extraction results, exploration-scoped')
param explorationCacheContainerName string = 'explorastore-cache'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'exploration-aks'
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName

  resource blobService 'blobServices' existing = {
    name: 'default'

    resource explorationContainer 'containers' existing = {
      name: explorationContainerName
    }

    resource explorationCacheContainer 'containers' existing = {
      name: explorationCacheContainerName
    }
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Embeddings and Document Intelligence.
resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, identity.id, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Read-only, and scoped to the exploration container rather than the storage account, so the
// job cannot reach maintenance source documents.
resource explorationBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage::blobService::explorationContainer
  name: guid(storage::blobService::explorationContainer.id, identity.id, storageBlobDataReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataReaderRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Read-write, and scoped to the cache container only, so the job can write DI/CU results and
// renders without gaining access to the maintenance extraction-cache or exploration source docs.
resource explorationCacheContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage::blobService::explorationCacheContainer
  name: guid(storage::blobService::explorationCacheContainer.id, identity.id, storageBlobDataContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output identity object = {
  name: identity.name
  clientId: identity.properties.clientId
  principalId: identity.properties.principalId
  namespace: namespace
  serviceAccountName: serviceAccountName
}
