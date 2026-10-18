// Exploration AGENT workload identity, federation and least-privilege role assignments.
//
// Distinct from infra/exploration-identity.bicep, which is the LOADER identity. The loader writes
// (blob cache, search index); the agent only reads. They must not share a credential.
//
// NOT expressible here: `Search Index Data Reader` scoped to a single index. Azure has no ARM
// resource type for a Search index, so that assignment is applied out of band:
//   az role assignment create --assignee <principalId> \
//     --role "Search Index Data Reader" \
//     --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Search/searchServices/<svc>/indexes/expl-content
// Reader, never Contributor: the agent must be unable to write the index it reads.

@description('Name of the user-assigned identity for the exploration agent')
param identityName string

@description('Azure region')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes namespace running the exploration agent')
param namespace string = 'crv-exploration'

@description('Kubernetes service account used by the exploration agent')
param serviceAccountName string = 'exploration-agent'

@description('Name of the existing container registry')
param containerRegistryName string

@description('Name of the existing Foundry / Cognitive Services account')
param foundryAccountName string

@description('Name of the existing storage account holding exploration documents and renders')
param storageAccountName string

@description('Blob container holding exploration source documents')
param explorationContainerName string = 'explorastore'

@description('Blob container holding cached extraction results and page renders')
param explorationCacheContainerName string = 'explorastore-cache'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var cognitiveServicesOpenAIUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'exploration-agent-aks'
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

// Chat completions only. The query-time vectorizer runs under the SEARCH service's identity,
// not this one, so no embeddings permission is needed here.
resource foundryOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, identity.id, cognitiveServicesOpenAIUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUserRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Source documents served by the image proxy — read-only, container-scoped.
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

// Page renders and figure crops — read-only. The loader holds Contributor on this container;
// the agent must not be able to overwrite the evidence it cites.
resource explorationCacheReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage::blobService::explorationCacheContainer
  name: guid(
    storage::blobService::explorationCacheContainer.id,
    identity.id,
    storageBlobDataReaderRoleId
  )
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataReaderRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Single object output: main.bicep is at the ARM 64-output cap.
output explorationAgentIdentity object = {
  name: identity.name
  clientId: identity.properties.clientId
  principalId: identity.properties.principalId
  namespace: namespace
  serviceAccountName: serviceAccountName
}
