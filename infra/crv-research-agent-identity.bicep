// Runtime identity of the CRV research agent backend (development/evaluation).
//
// Least privilege, resource-scoped:
// - Search Index Data Reader on the evidence search service (query only, no writes).
// - Cognitive Services OpenAI User on the model account (inference only).
// - PostgreSQL access is granted inside the database, not here: run
//   `crv-pipeline kg-grant-reader` (execute-only crv_kg_reader) and
//   `crv-agent grant-memory-runtime` (agent-memory DML only).
// The identity federates with one Kubernetes service account; no secrets are created.
// Evidence ACLs remain deferred: this identity must not back a shared/production service.

@description('Name of the user-assigned identity')
param identityName string = 'id-crv-research-agent'

@description('Azure region of the identity')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL (az aks show --query oidcIssuerProfile.issuerUrl)')
param oidcIssuerUrl string

@description('Kubernetes namespace of the research agent')
param namespace string = 'crv'

@description('Kubernetes service account of the research agent')
param serviceAccountName string = 'crv-research-agent'

@description('Existing Azure AI Search service holding crv-evidence-v1')
param searchServiceName string

@description('Existing Azure OpenAI / Foundry account with the agent deployments')
param openAiAccountName string

param tags object = {}

var searchIndexDataReader = '1407120a-92aa-4202-b7e9-c0e197c71c8f'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
  tags: tags
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: identity
  name: 'crv-research-agent-aks'
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
  }
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchServiceName
}

resource openAi 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: openAiAccountName
}

resource searchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identity.id, searchIndexDataReader)
  scope: search
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReader)
  }
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, identity.id, cognitiveServicesOpenAIUser)
  scope: openAi
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
  }
}

output identityName string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
