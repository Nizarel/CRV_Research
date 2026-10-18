// Durable ingestion pipeline (v2), Phase 1 prerequisites. Additive; nothing existing is modified.
//
// - Four pool queues on the EXISTING Service Bus namespace (wake-ups only; the ledger is authoritative).
// - Write-once artifact container on the EXISTING storage account.
// - Two least-privilege workload identities:
//     coordinator: sends outbox events to the pool queues;
//     worker:      receives from the pool queues, reads sources, writes artifacts.
// - Phase 2 (only when the account/service names are set): the worker calls Document
//   Intelligence and Azure OpenAI embeddings on their accounts and writes the shadow
//   evidence index. Search RBAC cannot target a single index from ARM, so the assignment is on
//   the service; the worker only ever writes the configured shadow index.
// - KEDA keeps using the existing `azure-servicebus-auth` TriggerAuthentication: reading queue
//   metrics needs Manage rights, which the runtime identities deliberately do not get.
//
// NOT expressible here: PostgreSQL principals. After deployment an Entra admin maps both
// identities and grants the `tizert_ingest_worker` role created by migration 0003
// (see infra/sql/ingestion-catalog/grant-pipeline-roles.sql).

targetScope = 'resourceGroup'

@description('Existing Service Bus namespace')
param serviceBusNamespaceName string = 'sb-nyuwyhzhjriuu'

@description('Existing storage account holding sources')
param storageAccountName string = 'stnyuwyhzhjriuu'

@description('Existing source container (read-only for the pipeline)')
param sourceContainerName string = 'content'

@description('New write-once artifact container')
param artifactContainerName string = 'ingest-artifacts'

@description('Azure region for the identities')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes namespace running the pipeline')
param kubernetesNamespace string = 'crv'

param coordinatorIdentityName string = 'id-crv-pipeline-coordinator'
param workerIdentityName string = 'id-crv-pipeline-worker'
param coordinatorServiceAccountName string = 'crv-pipeline-coordinator'
param workerServiceAccountName string = 'crv-pipeline-worker'

@description('Worker pools; each gets queue ingest-<pool>')
param pools array = [
  'io'
  'cpu'
  'heavy'
  'publish'
  'poll'
]

@description('Transport-level redeliveries before dead-lettering; business retries live in the ledger')
@minValue(1)
param maxDeliveryCount int = 10

@description('Existing Document Intelligence (or AI Services) account; empty skips the role')
param documentIntelligenceAccountName string = ''

@description('Existing Foundry/AI Services account hosting Content Understanding analyzers; empty skips the role. Workers analyze and read analyzers; they never create them.')
param contentUnderstandingAccountName string = ''

@description('Existing AI Services / Azure OpenAI account hosting the embedding deployment; empty skips the role')
param embeddingAccountName string = ''

@description('Existing Azure AI Search service holding the shadow evidence index; empty skips the role')
param searchServiceName string = ''

@description('Existing Key Vault holding the API keys of external billing accounts')
param keyVaultName string = ''

@description('Key Vault secrets the worker reads through the CSI driver: one API key per external billing account')
param accountKeySecretNames array = []

var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var searchIndexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var useDocumentIntelligence = !empty(documentIntelligenceAccountName)
var useContentUnderstanding = !empty(contentUnderstandingAccountName)
var useEmbeddings = !empty(embeddingAccountName)
var useSearch = !empty(searchServiceName)
var queueNames = [for pool in pools: 'ingest-${pool}']

resource bus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' existing = {
  name: serviceBusNamespaceName
}

// Serialized: parallel child writes on one namespace previously failed with RequestConflict.
@batchSize(1)
resource queues 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = [
  for pool in pools: {
    parent: bus
    name: 'ingest-${pool}'
    properties: {
      lockDuration: 'PT5M'
      maxDeliveryCount: maxDeliveryCount
      requiresDuplicateDetection: true
      duplicateDetectionHistoryTimeWindow: 'PT10M'
      deadLetteringOnMessageExpiration: true
      defaultMessageTimeToLive: 'P14D'
      requiresSession: false
      enablePartitioning: false
      enableBatchedOperations: true
    }
  }
]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName

  resource blobService 'blobServices' existing = {
    name: 'default'

    resource sourceContainer 'containers' existing = {
      name: sourceContainerName
    }
  }
}

resource artifactContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: storage::blobService
  name: artifactContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource coordinator 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: coordinatorIdentityName
  location: location
}

resource worker 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: workerIdentityName
  location: location
}

resource coordinatorFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: coordinator
  name: 'crv-pipeline-coordinator-aks'
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${kubernetesNamespace}:${coordinatorServiceAccountName}'
  }
}

resource workerFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: worker
  name: 'crv-pipeline-worker-aks'
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${kubernetesNamespace}:${workerServiceAccountName}'
  }
}

resource coordinatorSend 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (pool, index) in pools: {
    scope: queues[index]
    name: guid(queues[index].id, coordinator.id, serviceBusDataSenderRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataSenderRoleId)
      principalId: coordinator.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource workerReceive 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (pool, index) in pools: {
    scope: queues[index]
    name: guid(queues[index].id, worker.id, serviceBusDataReceiverRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataReceiverRoleId)
      principalId: worker.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Sources are evidence: the pipeline can read them but never modify them.
resource workerSourceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage::blobService::sourceContainer
  name: guid(storage::blobService::sourceContainer.id, worker.id, storageBlobDataReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workerArtifactWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: artifactContainer
  name: guid(artifactContainer.id, worker.id, storageBlobDataContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (useDocumentIntelligence) {
  name: documentIntelligenceAccountName
}

resource workerDocumentIntelligence 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useDocumentIntelligence) {
  scope: documentIntelligence
  name: guid(documentIntelligence.id, worker.id, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource contentUnderstanding 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (useContentUnderstanding) {
  name: contentUnderstandingAccountName
}

resource workerContentUnderstanding 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useContentUnderstanding) {
  scope: contentUnderstanding
  name: guid(contentUnderstanding.id, worker.id, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource embeddingAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (useEmbeddings) {
  name: embeddingAccountName
}

resource workerEmbeddings 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useEmbeddings) {
  scope: embeddingAccount
  name: guid(embeddingAccount.id, worker.id, cognitiveServicesOpenAiUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' existing = if (useSearch) {
  name: searchServiceName
}

resource workerSearchWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useSearch) {
  scope: search
  name: guid(search.id, worker.id, searchIndexDataContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributorRoleId)
    principalId: worker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource accountKeys 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [
  for name in accountKeySecretNames: {
    parent: keyVault
    name: name
  }
]

// Scoped to each secret: the worker cannot read anything else in the vault.
resource workerAccountKeys 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (name, index) in accountKeySecretNames: {
    scope: accountKeys[index]
    name: guid(accountKeys[index].id, worker.id, keyVaultSecretsUserRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
      principalId: worker.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output ingestionPipeline object = {
  coordinatorIdentity: {
    name: coordinator.name
    clientId: coordinator.properties.clientId
    principalId: coordinator.properties.principalId
    serviceAccountName: coordinatorServiceAccountName
  }
  workerIdentity: {
    name: worker.name
    clientId: worker.properties.clientId
    principalId: worker.properties.principalId
    serviceAccountName: workerServiceAccountName
  }
  queues: queueNames
  artifactContainer: artifactContainer.name
}
