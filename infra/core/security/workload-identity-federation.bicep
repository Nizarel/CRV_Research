@description('Name of the existing user-assigned identity')
param identityName string

@description('Name of the federated identity credential')
param credentialName string

@description('AKS OIDC issuer URL')
param oidcIssuerUrl string

@description('Kubernetes service-account subject')
param subject string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: credentialName
  properties: {
    audiences: ['api://AzureADTokenExchange']
    issuer: oidcIssuerUrl
    subject: subject
  }
}