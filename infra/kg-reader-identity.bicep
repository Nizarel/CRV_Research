// Knowledge-graph read identity for the maintenance agent backend.
//
// It holds no Azure role: its only use is an Entra login on the PostgreSQL catalog, mapped by
// `tizagent-pipeline kg-grant-reader` to a role that may only EXECUTE the crv_kg read functions.
// It federates with the backend's existing service account, so the backend requests its
// token explicitly by client ID while keeping its main workload identity for everything else.

@description('Name of the user-assigned identity')
param identityName string = 'id-crv-kg-reader'

@description('Azure region')
param location string = resourceGroup().location

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes namespace of the agent backend')
param namespace string = 'crv'

@description('Kubernetes service account of the agent backend')
param serviceAccountName string = 'crv'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'crv-backend-aks'
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
  }
}

output identityName string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
