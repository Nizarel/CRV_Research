targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
// microsoft.insights/components has restricted regions
@allowed([
  'eastus'
  'southcentralus'
  'northeurope'
  'westeurope'
  'southeastasia'
  'westus2'
  'uksouth'
  'canadacentral'
  'centralindia'
  'japaneast'
  'australiaeast'
  'koreacentral'
  'francecentral'
  'centralus'
  'eastus2'
  'eastasia'
  'westus'
  'southafricanorth'
  'northcentralus'
  'brazilsouth'
  'switzerlandnorth'
  'norwayeast'
  'norwaywest'
  'australiasoutheast'
  'australiacentral2'
  'germanywestcentral'
  'switzerlandwest'
  'uaecentral'
  'ukwest'
  'japanwest'
  'brazilsoutheast'
  'uaenorth'
  'australiacentral'
  'southindia'
  'westus3'
  'koreasouth'
  'swedencentral'
  'canadaeast'
  'jioindiacentral'
  'jioindiawest'
  'qatarcentral'
  'southafricawest'
  'germanynorth'
  'polandcentral'
  'israelcentral'
  'italynorth'
  'mexicocentral'
  'spaincentral'
  'newzealandnorth'
  'chilecentral'
  'indonesiacentral'
  'malaysiawest'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

param resourceGroupName string = '' // Set in main.parameters.json

param applicationInsightsDashboardName string = '' // Set in main.parameters.json
param applicationInsightsName string = '' // Set in main.parameters.json
param logAnalyticsName string = '' // Set in main.parameters.json

param searchServiceName string = '' // Set in main.parameters.json
param searchServiceResourceGroupName string = '' // Set in main.parameters.json
param searchServiceLocation string = '' // Set in main.parameters.json
// The free tier does not support managed identity (required) or semantic search (optional)
@allowed(['free', 'basic', 'standard', 'standard2', 'standard3', 'storage_optimized_l1', 'storage_optimized_l2'])
param searchServiceSkuName string // Set in main.parameters.json
param searchIndexName string // Set in main.parameters.json
param knowledgeBaseName string = useAgenticKnowledgeBase ? '${searchIndexName}-agent-upgrade' : ''
param searchQueryLanguage string // Set in main.parameters.json
param searchQuerySpeller string // Set in main.parameters.json
param searchServiceSemanticRankerLevel string // Set in main.parameters.json
param searchFieldNameEmbedding string // Set in main.parameters.json
var actualSearchServiceSemanticRankerLevel = (searchServiceSkuName == 'free')
  ? 'disabled'
  : searchServiceSemanticRankerLevel
param searchServiceQueryRewriting string // Set in main.parameters.json
param storageAccountName string = '' // Set in main.parameters.json
param storageResourceGroupName string = '' // Set in main.parameters.json
param storageResourceGroupLocation string = location
param storageContainerName string = 'content'
param storageSkuName string // Set in main.parameters.json

param defaultReasoningEffort string // Set in main.parameters.json
@description('Controls the default retrieval reasoning effort for agentic retrieval (minimal, low, or medium).')
param defaultRetrievalReasoningEffort string = 'minimal'
param useAgenticKnowledgeBase bool // Set in main.parameters.json

param userStorageAccountName string = ''
param userStorageContainerName string = 'user-content'

param tokenStorageContainerName string = 'tokens'

param imageStorageContainerName string = 'images'

@allowed(['azure', 'openai', 'azure_custom'])
param openAiHost string // Set in main.parameters.json
param isAzureOpenAiHost bool = startsWith(openAiHost, 'azure')
param deployAzureOpenAi bool = openAiHost == 'azure'
param azureOpenAiCustomUrl string = ''
@secure()
param azureOpenAiApiKey string = ''
param azureOpenAiDisableKeys bool = true
param openAiServiceName string = ''
param openAiResourceGroupName string = ''

@description('Name of the Microsoft Foundry project to create inside the Foundry account. Leave empty to generate one.')
param foundryProjectName string = ''

param speechServiceResourceGroupName string = ''
param speechServiceLocation string = ''
param speechServiceName string = ''
param speechServiceSkuName string // Set in main.parameters.json
param speechServiceVoice string = ''
param useMultimodal bool = false
param useEval bool = false
param useCloudIngestion bool = false

// When greater than 0, CSV rows are grouped into pages of up to this many characters
// during ingestion instead of one page per row (avoids out-of-memory on large CSV files).
param csvMaxPageChars int = 0

@description('Restore soft-deleted Cognitive Services accounts instead of creating new ones. Set to true after running azd down.')
param restoreCognitiveServices bool = false
param useCloudIngestionAcls bool = false
@description('Use an existing ADLS Gen2 storage account instead of provisioning a new one')
param useExistingAdlsStorage bool = false
// Must be specified when useExistingAdlsStorage is true. Bicep assert is experimental so we can't validate at compile-time yet.
param adlsStorageAccountName string = ''
param adlsStorageResourceGroupName string = ''

@allowed(['free', 'provisioned', 'serverless'])
param cosmosDbSkuName string // Set in main.parameters.json
param cosmodDbResourceGroupName string = ''
param cosmosDbLocation string = ''
param cosmosDbAccountName string = ''
param cosmosDbThroughput int = 400
param chatHistoryDatabaseName string = 'chat-database'
param chatHistoryContainerName string = 'chat-history-v2'
param chatHistoryVersion string = 'cosmosdb-v2'

// -------------------------------------------------------------------------
// Ingestion pipeline resources (Architecture 1: AKS Job-based ingestion/indexing)
// Provisions Azure Service Bus (work queue + DLQ), Azure Key Vault, a PostgreSQL
// Flexible Server (ingestion catalog + agent memory), and an extraction-cache blob container.
@description('Provision the ingestion pipeline resources: Service Bus, Key Vault, and the PostgreSQL catalog/memory server.')
param useIngestionPipeline bool = false
@description('Name of the Service Bus namespace. Leave empty to generate one.')
param serviceBusNamespaceName string = ''
@allowed(['Basic', 'Standard', 'Premium'])
param serviceBusSkuName string = 'Standard'
@description('Name of the ingestion work queue.')
param ingestionQueueName string = 'ingestion-work'
@description('Name of the Key Vault for the ingestion pipeline. Leave empty to generate one.')
param ingestionKeyVaultName string = ''
@description('Name of the PostgreSQL Flexible Server (ingestion catalog + agent memory). Leave empty to generate one.')
param postgresServerName string = ''
@description('Location for the PostgreSQL Flexible Server. Leave empty to use the deployment location. Set to an unrestricted region if the primary region is offer-restricted for this subscription.')
param postgresLocation string = ''
@description('PostgreSQL compute SKU name.')
param postgresSkuName string = 'Standard_B1ms'
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
@description('PostgreSQL compute tier. Burstable avoids zone-redundant capacity constraints.')
param postgresSkuTier string = 'Burstable'
@description('PostgreSQL major version.')
param postgresVersion string = '16'
@minValue(32)
@description('PostgreSQL storage size in GB (minimum 32).')
param postgresStorageSizeGB int = 32
@description('Name of the ingestion catalog database (source_objects, placements, logical_documents, dead-letter, state).')
param ingestionCatalogDatabaseName string = 'ingestion-catalog'
@description('Name of the agent memory database (LangGraph checkpointer + store, pgvector).')
param agentMemoryDatabaseName string = 'agent-memory'
@description('Name of the isolated PostgreSQL hybrid retrieval evaluation database.')
param retrievalEvalDatabaseName string = 'retrieval-eval'
@description('Name of the extraction cache blob container.')
param extractionCacheContainerName string = 'extraction-cache'

// https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?tabs=global-standard-aoai%2Cglobal-standard&pivots=azure-openai#models-by-deployment-type
@description('Location for the OpenAI resource group')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadaeast'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'japaneast'
  'koreacentral'
  'northcentralus'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southcentralus'
  'southindia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'uaenorth'
  'uksouth'
  'westeurope'
  'westus'
  'westus3'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param openAiLocation string

param openAiSkuName string = 'S0'

@secure()
param openAiApiKey string = ''
param openAiApiOrganization string = ''

param documentIntelligenceServiceName string = '' // Set in main.parameters.json
param documentIntelligenceResourceGroupName string = '' // Set in main.parameters.json

// Limited regions for new version:
// https://learn.microsoft.com/azure/ai-services/document-intelligence/concept-layout
@description('Location for the Document Intelligence resource group')
@allowed(['eastus', 'westus2', 'westeurope', 'australiaeast'])
@metadata({
  azd: {
    type: 'location'
  }
})
param documentIntelligenceResourceGroupLocation string

param documentIntelligenceSkuName string // Set in main.parameters.json

param visionServiceName string = '' // Set in main.parameters.json
param visionResourceGroupName string = '' // Set in main.parameters.json
param visionResourceGroupLocation string = location

param contentUnderstandingServiceName string = '' // Set in main.parameters.json
param contentUnderstandingResourceGroupName string = '' // Set in main.parameters.json

param chatGptModelName string = ''
param chatGptDeploymentName string = ''
param chatGptDeploymentVersion string = ''
param chatGptDeploymentSkuName string = ''
param chatGptDeploymentCapacity int = 0

var chatGpt = {
  modelName: !empty(chatGptModelName) ? chatGptModelName : 'gpt-5.4-mini'
  deploymentName: !empty(chatGptDeploymentName) ? chatGptDeploymentName : 'gpt-5.4-mini'
  deploymentVersion: !empty(chatGptDeploymentVersion) ? chatGptDeploymentVersion : '2026-03-17'
  deploymentSkuName: !empty(chatGptDeploymentSkuName) ? chatGptDeploymentSkuName : 'GlobalStandard'
  deploymentCapacity: chatGptDeploymentCapacity != 0 ? chatGptDeploymentCapacity : 30
}

param embeddingModelName string = ''
param embeddingDeploymentName string = ''
param embeddingDeploymentVersion string = ''
param embeddingDeploymentSkuName string = ''
param embeddingDeploymentCapacity int = 0
param embeddingDimensions int = 0
var embedding = {
  modelName: !empty(embeddingModelName) ? embeddingModelName : 'text-embedding-3-large'
  deploymentName: !empty(embeddingDeploymentName) ? embeddingDeploymentName : 'text-embedding-3-large'
  deploymentVersion: !empty(embeddingDeploymentVersion) ? embeddingDeploymentVersion : (embeddingModelName == 'text-embedding-ada-002' ? '2' : '1')
  deploymentSkuName: !empty(embeddingDeploymentSkuName) ? embeddingDeploymentSkuName : (embeddingModelName == 'text-embedding-ada-002' ? 'Standard' : 'GlobalStandard')
  deploymentCapacity: embeddingDeploymentCapacity != 0 ? embeddingDeploymentCapacity : 200
  dimensions: embeddingDimensions != 0 ? embeddingDimensions : 3072
}

param evalModelName string = ''
param evalDeploymentName string = ''
param evalModelVersion string = ''
param evalDeploymentSkuName string = ''
param evalDeploymentCapacity int = 0
var eval = {
  modelName: !empty(evalModelName) ? evalModelName : 'gpt-5.4'
  deploymentName: !empty(evalDeploymentName) ? evalDeploymentName : 'eval'
  deploymentVersion: !empty(evalModelVersion) ? evalModelVersion : '2026-03-05'
  deploymentSkuName: !empty(evalDeploymentSkuName) ? evalDeploymentSkuName : 'GlobalStandard'
  deploymentCapacity: evalDeploymentCapacity != 0 ? evalDeploymentCapacity : 30
}

param knowledgeBaseModelName string = ''
param knowledgeBaseDeploymentName string = ''
param knowledgeBaseModelVersion string = ''
param knowledgeBaseDeploymentSkuName string = ''
param knowledgeBaseDeploymentCapacity int = 0
var knowledgeBase = {
  modelName: !empty(knowledgeBaseModelName) ? knowledgeBaseModelName : 'gpt-5.4'
  deploymentName: !empty(knowledgeBaseDeploymentName) ? knowledgeBaseDeploymentName : 'knowledgebase'
  deploymentVersion: !empty(knowledgeBaseModelVersion) ? knowledgeBaseModelVersion : '2026-03-05'
  deploymentSkuName: !empty(knowledgeBaseDeploymentSkuName) ? knowledgeBaseDeploymentSkuName : 'GlobalStandard'
  deploymentCapacity: knowledgeBaseDeploymentCapacity != 0 ? knowledgeBaseDeploymentCapacity : 100
}


param tenantId string = tenant().tenantId
param authTenantId string = ''

// Used for the optional login and document level access control system
param useAuthentication bool = false
param enforceAccessControl bool = false
// Force using MSAL app authentication instead of built-in App Service authentication
// https://learn.microsoft.com/azure/app-service/overview-authentication-authorization
param disableAppServicesAuthentication bool = false
param enableGlobalDocuments bool = false
param enableUnauthenticatedAccess bool = false
param serverAppId string = ''
@secure()
param serverAppSecret string = ''
param clientAppId string = ''
@secure()
param clientAppSecret string = ''

// Used for optional CORS support for alternate frontends
param allowedOrigin string = '' // should start with https://, shouldn't end with a /

@allowed(['None', 'AzureServices'])
@description('If allowedIp is set, whether azure services are allowed to bypass the storage and AI services firewall.')
param bypass string = 'AzureServices'

@description('Public network access value for all deployed resources')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

@description('Add a private endpoints for network connectivity')
param usePrivateEndpoint bool = false

// A workload using private endpoints must not retain public data-plane access.
var usePrivateNetworking = true
var effectivePublicNetworkAccess = 'Disabled'
var applicationInsightsPublicNetworkAccessForIngestion = 'Enabled'
// Service Bus private endpoints require Premium. Keep a Standard namespace reachable until it
// is explicitly upgraded, then the existing private-endpoint wiring closes public access.
var serviceBusPublicNetworkAccess = serviceBusSkuName == 'Premium' ? effectivePublicNetworkAccess : 'Enabled'

@description('Use a P2S VPN Gateway for secure access to the private endpoints')
param useVpnGateway bool = false

@description('Id of the user or app to assign application roles')
param principalId string = ''

@description('Use Application Insights for monitoring and performance tracing')
param useApplicationInsights bool = false

@description('Optional Action Group resource IDs notified by Exploration observability alerts.')
param explorationAlertActionGroupResourceIds array = []

@description('Optional Action Group resource IDs notified by Maintenance agent observability alerts.')
param maintenanceAlertActionGroupResourceIds array = []

@description('Enable language picker')
param enableLanguagePicker bool = false
@description('Use speech recognition feature in browser')
param useSpeechInputBrowser bool = false
@description('Use speech synthesis in browser')
param useSpeechOutputBrowser bool = false
@description('Use Azure speech service for reading out text')
param useSpeechOutputAzure bool = false
@description('Use chat history feature in browser')
param useChatHistoryBrowser bool = false
@description('Use chat history feature in CosmosDB')
param useChatHistoryCosmos bool = false
@description('Show options to use vector embeddings for searching in the app UI')
param useVectors bool = false
@description('Use Built-in integrated Vectorization feature of AI Search to vectorize and ingest documents')
param useIntegratedVectorization bool = false

@description('Use media description feature with Azure Content Understanding during ingestion')
param useMediaDescriberAzureCU bool = true

@description('Enable user document upload feature')
param useUserUpload bool = false
param useLocalPdfParser bool = false
param useLocalHtmlParser bool = false

var abbrs = loadJsonContent('abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

var tenantIdForAuth = !empty(authTenantId) ? authTenantId : tenantId
var authenticationIssuerUri = '${environment().authentication.loginEndpoint}${tenantIdForAuth}/v2.0'

@description('Whether the deployment is running on GitHub Actions')
param runningOnGh string = ''

@description('Whether the deployment is running on Azure DevOps Pipeline')
param runningOnAdo string = ''

@description('Name of the private AKS cluster. Leave empty to generate one.')
param aksClusterName string = ''
@description('Name of the private Azure Container Registry used by AKS. Leave empty to generate one.')
param aksContainerRegistryName string = ''
@description('VM size for the AKS system and application node pool.')
param aksNodeVmSize string = 'Standard_D4ds_v5'
@minValue(2)
param aksMinNodeCount int = 2
@minValue(2)
param aksMaxNodeCount int = 5
@description('Whether this deployment manages the AKS Container Insights addon (omsagent). Set false to avoid extension-operation conflicts during retries.')
param aksEnableMonitoringAddon bool = true
@description('Name of the shared user-assigned managed identity used by all AKS workloads and AGIC.')
param aksWorkloadIdentityName string = ''
@description('Name of the public WAF_v2 Application Gateway. Leave empty to generate one.')
param applicationGatewayName string = ''
@description('Name of the public IP assigned to Application Gateway. Leave empty to generate one.')
param applicationGatewayPublicIpName string = ''
@minValue(0)
@maxValue(125)
param applicationGatewayMinCapacity int = 1
@minValue(2)
@maxValue(125)
param applicationGatewayMaxCapacity int = 2

// RAG Configuration Parameters
@description('Whether to use text embeddings for RAG search')
param ragSearchTextEmbeddings bool = true
@description('Whether to use image embeddings for RAG search')
param ragSearchImageEmbeddings bool = true
@description('Whether to send text sources to LLM for RAG responses')
param ragSendTextSources bool = true
@description('Whether to send image sources to LLM for RAG responses')
param ragSendImageSources bool = true
@description('Whether to enable web sources for agentic retrieval')
param useWebSource bool = false
@description('Whether to enable SharePoint sources for agentic retrieval')
param useSharePointSource bool = false

// Organize resources in a resource group
resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: !empty(resourceGroupName) ? resourceGroupName : '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

// Name of the resource group hosting each service; falls back to the main resource
// group when the *ResourceGroupName param is empty.
// Kept as plain string vars (used via `scope: az.resourceGroup(<name>)`) rather than
// `existing` resourceGroups resources, which would collide with the main resource group
// under Bicep languageVersion 2.0. See #3146.
var openAiResourceGroupNameActual = !empty(openAiResourceGroupName) ? openAiResourceGroupName : resourceGroup.name
var documentIntelligenceResourceGroupNameActual = !empty(documentIntelligenceResourceGroupName)
  ? documentIntelligenceResourceGroupName
  : resourceGroup.name
var visionResourceGroupNameActual = !empty(visionResourceGroupName) ? visionResourceGroupName : resourceGroup.name
var contentUnderstandingResourceGroupNameActual = !empty(contentUnderstandingResourceGroupName)
  ? contentUnderstandingResourceGroupName
  : resourceGroup.name
var searchServiceResourceGroupNameActual = !empty(searchServiceResourceGroupName)
  ? searchServiceResourceGroupName
  : resourceGroup.name
var storageResourceGroupNameActual = !empty(storageResourceGroupName) ? storageResourceGroupName : resourceGroup.name
var speechResourceGroupNameActual = !empty(speechServiceResourceGroupName)
  ? speechServiceResourceGroupName
  : resourceGroup.name
var cosmosDbResourceGroupNameActual = !empty(cosmodDbResourceGroupName) ? cosmodDbResourceGroupName : resourceGroup.name
// ADLS resource group - defaults to main resource group if not specified
var adlsStorageResourceGroupNameActual = !empty(adlsStorageResourceGroupName)
  ? adlsStorageResourceGroupName
  : resourceGroup.name

// Monitor application with Azure Monitor
module monitoring 'core/monitor/monitoring.bicep' = if (useApplicationInsights) {
  name: 'monitoring'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    applicationInsightsName: !empty(applicationInsightsName)
      ? applicationInsightsName
      : '${abbrs.insightsComponents}${resourceToken}'
    logAnalyticsName: !empty(logAnalyticsName)
      ? logAnalyticsName
      : '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    publicNetworkAccess: effectivePublicNetworkAccess
    applicationInsightsPublicNetworkAccessForIngestion: applicationInsightsPublicNetworkAccessForIngestion
    applicationInsightsPublicNetworkAccessForQuery: effectivePublicNetworkAccess
  }
}

module applicationInsightsDashboard 'backend-dashboard.bicep' = if (useApplicationInsights) {
  name: 'application-insights-dashboard'
  scope: resourceGroup
  params: {
    name: !empty(applicationInsightsDashboardName)
      ? applicationInsightsDashboardName
      : '${abbrs.portalDashboards}${resourceToken}'
    location: location
    applicationInsightsName: useApplicationInsights ? monitoring!.outputs.applicationInsightsName : ''
  }
}

module explorationObservability 'exploration-observability.bicep' = if (useApplicationInsights) {
  name: 'exploration-observability'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    applicationInsightsResourceId: monitoring!.outputs.applicationInsightsId
    logAnalyticsWorkspaceResourceId: monitoring!.outputs.logAnalyticsWorkspaceId
    actionGroupResourceIds: explorationAlertActionGroupResourceIds
  }
}

module maintenanceObservability 'maintenance-observability.bicep' = if (useApplicationInsights) {
  name: 'maintenance-observability'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    applicationInsightsResourceId: monitoring!.outputs.applicationInsightsId
    logAnalyticsWorkspaceResourceId: monitoring!.outputs.logAnalyticsWorkspaceId
    actionGroupResourceIds: maintenanceAlertActionGroupResourceIds
  }
}

// Determine which ADLS storage account name to use (existing or provisioned)
var adlsStorageAccountNameResolved = useExistingAdlsStorage ? existingAdlsStorage.name : (useCloudIngestionAcls ? adlsStorage!.outputs.name : '')
module aksWorkloadIdentity 'core/security/workload-identity.bicep' = {
  name: 'aks-workload-identity'
  scope: resourceGroup
  params: {
    name: !empty(aksWorkloadIdentityName) ? aksWorkloadIdentityName : '${environmentName}-aks-workload'
    location: location
    tags: tags
  }
}

var defaultOpenAiDeployments = [
  {
    name: chatGpt.deploymentName
    model: {
      format: 'OpenAI'
      name: chatGpt.modelName
      version: chatGpt.deploymentVersion
    }
    sku: {
      name: chatGpt.deploymentSkuName
      capacity: chatGpt.deploymentCapacity
    }
  }
  {
    name: embedding.deploymentName
    model: {
      format: 'OpenAI'
      name: embedding.modelName
      version: embedding.deploymentVersion
    }
    sku: {
      name: embedding.deploymentSkuName
      capacity: embedding.deploymentCapacity
    }
  }
]

var openAiDeployments = concat(
  defaultOpenAiDeployments,
  useEval
    ? [
      {
        name: eval.deploymentName
        model: {
          format: 'OpenAI'
          name: eval.modelName
          version: eval.deploymentVersion
        }
        sku: {
          name: eval.deploymentSkuName
          capacity: eval.deploymentCapacity
        }
      }
    ] : [],
  useAgenticKnowledgeBase
    ? [
        {
          name: knowledgeBase.deploymentName
          model: {
            format: 'OpenAI'
            name: knowledgeBase.modelName
            version: knowledgeBase.deploymentVersion
          }
          sku: {
            name: knowledgeBase.deploymentSkuName
            capacity: knowledgeBase.deploymentCapacity
          }
        }
      ]
    : []
)

// Provision a Foundry account + project only when we own the account (deployFoundryAccount):
//   - openAiHost == 'azure': create a Microsoft Foundry account (AIServices + project
//     management) hosting the models, plus a Foundry project. A set openAiServiceName (reused
//     from a prior output or user-chosen) is reused idempotently so redeploys stay stable.
//   - openAiHost == 'azure_custom' (or non-azure): bring-your-own — account, deployments,
//     project, private endpoint, and role assignments are all skipped; the backend uses
//     AZURE_OPENAI_CUSTOM_URL and you manage everything.
// openAiServiceName can't signal bring-your-own: AZURE_OPENAI_SERVICE is also an output azd
// writes back after every deploy, so it's set on redeploys of our own account too. openAiHost
// is the intent signal.
var deployFoundryAccount = isAzureOpenAiHost && deployAzureOpenAi

// Microsoft Foundry account (AIServices) with project management enabled, so the model
// deployments are hosted on the account and a Foundry project can be created inside it.
module foundryAccount 'br/public:avm/res/cognitive-services/account:0.15.0' = if (deployFoundryAccount) {
  name: 'foundry-account'
  scope: az.resourceGroup(openAiResourceGroupNameActual)
  params: {
    name: !empty(openAiServiceName) ? openAiServiceName : '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    location: openAiLocation
    tags: tags
    kind: 'AIServices'
    allowProjectManagement: true
    customSubDomainName: !empty(openAiServiceName) ? openAiServiceName : '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    publicNetworkAccess: effectivePublicNetworkAccess
    networkAcls: {
      defaultAction: 'Allow'
      bypass: bypass
    }
    sku: openAiSkuName
    deployments: openAiDeployments
    disableLocalAuth: azureOpenAiDisableKeys
    restore: restoreCognitiveServices
  }
}

// Microsoft Foundry project, hosted inside the Foundry (AIServices) account above.
// A project is required to open the account in the Microsoft Foundry portal and to use
// the Foundry / Agents SDKs against FOUNDRY_PROJECT_ENDPOINT.
var foundryProjectNameResolved = !empty(foundryProjectName) ? foundryProjectName : 'proj-${resourceToken}'

var foundryProjectRoleAssignments = concat(
  empty(principalId)
    ? []
    : [
        {
          principalId: principalId
          roleDefinitionId: 'eadc314b-1a2d-4efa-be10-5d325db5065e' // Azure AI Project Manager
          principalType: principalType
        }
      ],
  [
    {
      principalId: backendPrincipalId
      roleDefinitionId: '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Azure AI User
      principalType: 'ServicePrincipal'
    }
  ]
)

module foundryProject 'core/ai/ai-foundry-project.bicep' = if (deployFoundryAccount) {
  name: 'foundry-project'
  scope: az.resourceGroup(openAiResourceGroupNameActual)
  params: {
    accountName: foundryAccount!.outputs.name
    projectName: foundryProjectNameResolved
    location: openAiLocation
    tags: tags
    roleAssignments: foundryProjectRoleAssignments
    applicationInsightsResourceId: useApplicationInsights ? monitoring!.outputs.applicationInsightsId : ''
    applicationInsightsConnectionString: useApplicationInsights
      ? monitoring!.outputs.applicationInsightsConnectionString
      : ''
  }
}
// Formerly known as Form Recognizer
// Does not support bypass
module documentIntelligence 'br/public:avm/res/cognitive-services/account:0.7.2' = {
  name: 'documentintelligence'
  scope: az.resourceGroup(documentIntelligenceResourceGroupNameActual)
  params: {
    name: !empty(documentIntelligenceServiceName)
      ? documentIntelligenceServiceName
      : '${abbrs.cognitiveServicesDocumentIntelligence}${resourceToken}'
    kind: 'FormRecognizer'
    customSubDomainName: !empty(documentIntelligenceServiceName)
      ? documentIntelligenceServiceName
      : '${abbrs.cognitiveServicesDocumentIntelligence}${resourceToken}'
    publicNetworkAccess: effectivePublicNetworkAccess
    networkAcls: {
      defaultAction: 'Allow'
    }
    location: documentIntelligenceResourceGroupLocation
    disableLocalAuth: true
    tags: tags
    sku: documentIntelligenceSkuName
    restore: restoreCognitiveServices
  }
}

module vision 'br/public:avm/res/cognitive-services/account:0.7.2' = if (useMultimodal) {
  name: 'vision'
  scope: az.resourceGroup(visionResourceGroupNameActual)
  params: {
    name: !empty(visionServiceName)
      ? visionServiceName
      : '${abbrs.cognitiveServicesVision}${resourceToken}'
    kind: 'CognitiveServices'
    networkAcls: {
      defaultAction: 'Allow'
    }
    publicNetworkAccess: effectivePublicNetworkAccess
    customSubDomainName: !empty(visionServiceName)
      ? visionServiceName
      : '${abbrs.cognitiveServicesVision}${resourceToken}'
    location: visionResourceGroupLocation
    tags: tags
    sku: 'S0'
    restore: restoreCognitiveServices
  }
}


module contentUnderstanding 'br/public:avm/res/cognitive-services/account:0.7.2' = if (useMediaDescriberAzureCU) {
  name: 'content-understanding'
  scope: az.resourceGroup(contentUnderstandingResourceGroupNameActual)
  params: {
    name: !empty(contentUnderstandingServiceName)
      ? contentUnderstandingServiceName
      : '${abbrs.cognitiveServicesContentUnderstanding}${resourceToken}'
    kind: 'AIServices'
    networkAcls: {
      defaultAction: 'Allow'
    }
    publicNetworkAccess: effectivePublicNetworkAccess
    customSubDomainName: !empty(contentUnderstandingServiceName)
      ? contentUnderstandingServiceName
      : '${abbrs.cognitiveServicesContentUnderstanding}${resourceToken}'
    // Hard-coding to westus for now, due to limited availability and no overlap with Document Intelligence
    location: 'westus'
    tags: tags
    sku: 'S0'
    restore: restoreCognitiveServices
  }
}

module speech 'br/public:avm/res/cognitive-services/account:0.7.2' = if (useSpeechOutputAzure) {
  name: 'speech-service'
  scope: az.resourceGroup(speechResourceGroupNameActual)
  params: {
    name: !empty(speechServiceName) ? speechServiceName : '${abbrs.cognitiveServicesSpeech}${resourceToken}'
    kind: 'SpeechServices'
    networkAcls: {
      defaultAction: 'Allow'
    }
    customSubDomainName: !empty(speechServiceName)
      ? speechServiceName
      : '${abbrs.cognitiveServicesSpeech}${resourceToken}'
    location: !empty(speechServiceLocation) ? speechServiceLocation : location
    tags: tags
    sku: speechServiceSkuName
    restore: restoreCognitiveServices
  }
}
module searchService 'core/search/search-services.bicep' = {
  name: 'search-service'
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  params: {
    name: !empty(searchServiceName) ? searchServiceName : 'gptkb-${resourceToken}'
    location: !empty(searchServiceLocation) ? searchServiceLocation : location
    identityLocation: location
    tags: tags
    disableLocalAuth: true
    sku: {
      name: searchServiceSkuName
    }
    semanticSearch: actualSearchServiceSemanticRankerLevel
    publicNetworkAccess: effectivePublicNetworkAccess == 'Enabled'
      ? 'enabled'
      : (effectivePublicNetworkAccess == 'Disabled' ? 'disabled' : null)
    sharedPrivateLinkStorageAccounts: (usePrivateNetworking && useIntegratedVectorization) ? [storage.outputs.id] : []
  }
}

module searchDiagnostics 'core/search/search-diagnostics.bicep' = if (useApplicationInsights) {
  name: 'search-diagnostics'
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  params: {
    searchServiceName: searchService.outputs.name
    workspaceId: useApplicationInsights ? monitoring!.outputs.logAnalyticsWorkspaceId : ''
  }
}

module storage 'core/storage/storage-account.bicep' = {
  name: 'storage'
  scope: az.resourceGroup(storageResourceGroupNameActual)
  params: {
    name: !empty(storageAccountName) ? storageAccountName : '${abbrs.storageStorageAccounts}${resourceToken}'
    location: storageResourceGroupLocation
    tags: tags
    publicNetworkAccess: effectivePublicNetworkAccess
    bypass: bypass
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    sku: {
      name: storageSkuName
    }
    deleteRetentionPolicy: {
      enabled: true
      days: 2
    }
    containers: [
      {
        name: storageContainerName
        publicAccess: 'None'
      }
      {
        name: imageStorageContainerName
        publicAccess: 'None'
      }
      {
        name: tokenStorageContainerName
        publicAccess: 'None'
      }
      {
        name: extractionCacheContainerName
        publicAccess: 'None'
      }
    ]
  }
}

module userStorage 'core/storage/storage-account.bicep' = if (useUserUpload) {
  name: 'user-storage'
  scope: az.resourceGroup(storageResourceGroupNameActual)
  params: {
    name: !empty(userStorageAccountName)
      ? userStorageAccountName
      : 'user${abbrs.storageStorageAccounts}${resourceToken}'
    location: storageResourceGroupLocation
    tags: tags
    publicNetworkAccess: effectivePublicNetworkAccess
    bypass: bypass
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    isHnsEnabled: true
    sku: {
      name: storageSkuName
    }
    containers: [
      {
        name: userStorageContainerName
        publicAccess: 'None'
      }
    ]
  }
}

// Reference existing ADLS Gen2 storage account when bringing your own
resource existingAdlsStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (useExistingAdlsStorage && !empty(adlsStorageAccountName)) {
  name: adlsStorageAccountName
  scope: az.resourceGroup(adlsStorageResourceGroupNameActual)
}

// ADLS Gen2 storage account for cloud ingestion with ACL support
// Only provision if using cloud ingestion ACLs AND not using an existing ADLS account
module adlsStorage 'core/storage/storage-account.bicep' = if (useCloudIngestionAcls && !useExistingAdlsStorage) {
  name: 'adls-storage'
  scope: az.resourceGroup(storageResourceGroupNameActual)
  params: {
    name: 'adls${abbrs.storageStorageAccounts}${resourceToken}'
    location: storageResourceGroupLocation
    tags: tags
    publicNetworkAccess: effectivePublicNetworkAccess
    bypass: bypass
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    isHnsEnabled: true
    sku: {
      name: storageSkuName
    }
    deleteRetentionPolicy: {
      enabled: true
      days: 2
    }
    containers: [
      {
        name: storageContainerName
        publicAccess: 'None'
      }
    ]
  }
}

var provisionCosmos = (useAuthentication && useChatHistoryCosmos)

// WARNING: Cosmos DB partition keys are immutable. If you originally deployed with the v1 container
// (chat-history with a single /userId partition key), re-deploying with the v2 container schema
// (chat-history-v2 with MultiHash /entra_oid + /session_id) will fail with:
//   "Document collection partition key cannot be changed."
// To migrate from v1 to v2, you must either:
//   1. Delete the old container manually (Azure Portal > Cosmos DB > Data Explorer) and re-deploy, OR
//   2. Override chatHistoryContainerName with a new name (e.g. 'chat-history-v3') via azd env set.
// This is an ARM/Cosmos DB platform limitation — Bicep cannot conditionally skip container updates.
module cosmosDb 'br/public:avm/res/document-db/database-account:0.6.1' = if (provisionCosmos) {
  name: 'cosmosdb'
  scope: az.resourceGroup(cosmosDbResourceGroupNameActual)
  params: {
    name: !empty(cosmosDbAccountName) ? cosmosDbAccountName : '${abbrs.documentDBDatabaseAccounts}${resourceToken}'
    location: !empty(cosmosDbLocation) ? cosmosDbLocation : location
    locations: [
      {
        locationName: !empty(cosmosDbLocation) ? cosmosDbLocation : location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    enableFreeTier: cosmosDbSkuName == 'free'
    capabilitiesToAdd: cosmosDbSkuName == 'serverless' ? ['EnableServerless'] : []
    networkRestrictions: {
      ipRules: []
      networkAclBypass: bypass
      publicNetworkAccess: effectivePublicNetworkAccess
      virtualNetworkRules: []
    }
    sqlDatabases: (useAuthentication && useChatHistoryCosmos) ? [
      {
        name: chatHistoryDatabaseName
        throughput: (cosmosDbSkuName == 'serverless') ? null : cosmosDbThroughput
        containers: [
          {
            name: chatHistoryContainerName
            kind: 'MultiHash'
            paths: [
              '/entra_oid'
              '/session_id'
            ]
            indexingPolicy: {
              indexingMode: 'consistent'
              automatic: true
              includedPaths: [
                {
                  path: '/entra_oid/?'
                }
                {
                  path: '/session_id/?'
                }
                {
                  path: '/timestamp/?'
                }
                {
                  path: '/type/?'
                }
              ]
              excludedPaths: [
                {
                  path: '/*'
                }
              ]
            }
          }
        ]
      }
    ] : []
  }
}

// -------------------------------------------------------------------------
// Ingestion pipeline: Service Bus, Key Vault, and RBAC for the AKS workload identity
module serviceBus 'core/host/service-bus.bicep' = if (useIngestionPipeline) {
  name: 'service-bus'
  scope: resourceGroup
  params: {
    name: !empty(serviceBusNamespaceName) ? serviceBusNamespaceName : '${abbrs.serviceBusNamespaces}${resourceToken}'
    location: location
    tags: tags
    skuName: serviceBusSkuName
    queueName: ingestionQueueName
    publicNetworkAccess: serviceBusPublicNetworkAccess
  }
}

module keyVault 'core/security/keyvault.bicep' = if (useIngestionPipeline) {
  name: 'key-vault'
  scope: resourceGroup
  params: {
    name: !empty(ingestionKeyVaultName) ? ingestionKeyVaultName : '${abbrs.keyVaultVaults}${resourceToken}'
    location: location
    tags: tags
    publicNetworkAccess: effectivePublicNetworkAccess
  }
}

// PostgreSQL Flexible Server: ingestion catalog/state + agent memory (LangGraph checkpointer + store).
// Entra-only auth; the AKS workload identity is the Entra administrator so the pipeline and agent
// connect with managed identity (no passwords). Burstable tier avoids zone-redundant capacity limits.
module postgresServer 'core/database/postgresql-flexible-server.bicep' = if (useIngestionPipeline) {
  name: 'postgres-server'
  scope: resourceGroup
  params: {
    name: !empty(postgresServerName) ? postgresServerName : '${abbrs.dBforPostgreSQLServers}${resourceToken}'
    location: !empty(postgresLocation) ? postgresLocation : location
    tags: tags
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    version: postgresVersion
    storageSizeGB: postgresStorageSizeGB
    publicNetworkAccess: effectivePublicNetworkAccess
    databaseNames: [
      ingestionCatalogDatabaseName
      agentMemoryDatabaseName
      retrievalEvalDatabaseName
    ]
    administrators: [
      {
        objectId: aksWorkloadIdentity.outputs.principalId
        principalName: aksWorkloadIdentity.outputs.name
        principalType: 'ServicePrincipal'
      }
    ]
  }
}

// Azure Service Bus Data Owner for the AKS workload identity (enqueue/dequeue/dead-letter)
module serviceBusDataOwnerRole 'core/security/role.bicep' = if (useIngestionPipeline) {
  scope: resourceGroup
  name: 'sb-data-owner-aks'
  params: {
    principalId: aksWorkloadIdentity.outputs.principalId
    roleDefinitionId: '090c5cfd-751d-490a-894a-3ce6f1109419' // Azure Service Bus Data Owner
    principalType: 'ServicePrincipal'
  }
}

// Key Vault Secrets User for the AKS workload identity
module keyVaultSecretsUserRole 'core/security/role.bicep' = if (useIngestionPipeline) {
  scope: resourceGroup
  name: 'kv-secrets-user-aks'
  params: {
    principalId: aksWorkloadIdentity.outputs.principalId
    roleDefinitionId: '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User
    principalType: 'ServicePrincipal'
  }
}

// USER ROLES
var principalType = empty(runningOnGh) && empty(runningOnAdo) ? 'User' : 'ServicePrincipal'

module openAiRoleUser 'core/security/role.bicep' = if (isAzureOpenAiHost && deployAzureOpenAi) {
  scope: az.resourceGroup(openAiResourceGroupNameActual)
  name: 'openai-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
    principalType: principalType
  }
}

// For both Document Intelligence and AI vision
module cognitiveServicesRoleUser 'core/security/role.bicep' = {
  scope: resourceGroup
  name: 'cognitiveservices-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908'
    principalType: principalType
  }
}

module speechRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(speechResourceGroupNameActual)
  name: 'speech-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: 'f2dc8367-1007-4938-bd23-fe263f013447' // Cognitive Services Speech User
    principalType: principalType
  }
}

module storageRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
    principalType: principalType
  }
}

module storageContribRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-contrib-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
    principalType: principalType
  }
}

module storageOwnerRoleUser 'core/security/role.bicep' = if (useUserUpload) {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-owner-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Storage Blob Data Owner
    principalType: principalType
  }
}

module searchRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '1407120a-92aa-4202-b7e9-c0e197c71c8f' // Search Index Data Reader
    principalType: principalType
  }
}

module searchContribRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-contrib-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
    principalType: principalType
  }
}

module searchSvcContribRoleUser 'core/security/role.bicep' = {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-svccontrib-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '7ca78c08-252a-4471-8644-bb5ff32d4ba0' // Search Service Contributor
    principalType: principalType
  }
}

module cosmosDbAccountContribRoleUser 'core/security/role.bicep' = if (useAuthentication && useChatHistoryCosmos) {
  scope: az.resourceGroup(cosmosDbResourceGroupNameActual)
  name: 'cosmosdb-account-contrib-role-user'
  params: {
    principalId: principalId
    roleDefinitionId: '5bd9cd88-fe45-4216-938b-f97437e15450'
    principalType: principalType
  }
}

// RBAC for Cosmos DB
// https://learn.microsoft.com/azure/cosmos-db/nosql/security/how-to-grant-data-plane-role-based-access
module cosmosDbDataContribRoleUser 'core/security/documentdb-sql-role.bicep' = if (useAuthentication && useChatHistoryCosmos) {
  scope: az.resourceGroup(cosmosDbResourceGroupNameActual)
  name: 'cosmosdb-data-contrib-role-user'
  params: {
    databaseAccountName: (useAuthentication && useChatHistoryCosmos) ? cosmosDb!.outputs.name : ''
    principalId: principalId
    // Cosmos DB Built-in Data Contributor role
    roleDefinitionId: (useAuthentication && useChatHistoryCosmos)
      ? '/${subscription().id}/resourceGroups/${cosmosDb!.outputs.resourceGroupName}/providers/Microsoft.DocumentDB/databaseAccounts/${cosmosDb!.outputs.name}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
      : ''
  }
}

// SYSTEM IDENTITIES
module openAiRoleBackend 'core/security/role.bicep' = if (isAzureOpenAiHost && deployAzureOpenAi) {
  scope: az.resourceGroup(openAiResourceGroupNameActual)
  name: 'openai-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
    principalType: 'ServicePrincipal'
  }
}

module openAiRoleSearchService 'core/security/role.bicep' = if (isAzureOpenAiHost && deployAzureOpenAi && searchServiceSkuName != 'free') {
  scope: az.resourceGroup(openAiResourceGroupNameActual)
  name: 'openai-role-searchservice'
  params: {
    principalId: searchService.outputs.systemAssignedPrincipalId
    roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
    principalType: 'ServicePrincipal'
  }
}

module visionRoleSearchService 'core/security/role.bicep' = if (useMultimodal && searchServiceSkuName != 'free') {
  scope: az.resourceGroup(visionResourceGroupNameActual)
  name: 'vision-role-searchservice'
  params: {
    principalId: searchService.outputs.systemAssignedPrincipalId
    roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908'
    principalType: 'ServicePrincipal'
  }
}

module storageRoleBackend 'core/security/role.bicep' = {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
    principalType: 'ServicePrincipal'
  }
}

// The ingestion Jobs read source blobs and write immutable extraction-cache results and figure crops.
module storageContribRoleAksIngestion 'core/security/storage-role.bicep' = if (useIngestionPipeline) {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-contributor-role-aks-ingestion'
  params: {
    storageAccountName: storage.outputs.name
    principalId: aksWorkloadIdentity.outputs.principalId
    roleDefinitionId: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
    principalType: 'ServicePrincipal'
  }
}

module storageOwnerRoleBackend 'core/security/role.bicep' = if (useUserUpload) {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-owner-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Storage Blob Data Owner
    principalType: 'ServicePrincipal'
  }
}

// Search service needs blob read access for both integrated vectorization and cloud ingestion indexer data source
module storageRoleSearchService 'core/security/role.bicep' = if ((useIntegratedVectorization || useCloudIngestion) && searchServiceSkuName != 'free') {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-role-searchservice'
  params: {
    principalId: searchService.outputs.systemAssignedPrincipalId
    roleDefinitionId: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
    principalType: 'ServicePrincipal'
  }
}

module storageRoleContributorSearchService 'core/security/role.bicep' = if ((useIntegratedVectorization && useMultimodal) && searchServiceSkuName != 'free') {
  scope: az.resourceGroup(storageResourceGroupNameActual)
  name: 'storage-role-contributor-searchservice'
  params: {
    principalId: searchService.outputs.systemAssignedPrincipalId
    roleDefinitionId: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
    principalType: 'ServicePrincipal'
  }
}

// ADLS Gen2 storage role assignments for cloud ingestion with ACLs
// These are scoped to the ADLS storage account itself, so they work for both
// provisioned and bring-your-own (BYO) ADLS storage accounts
module adlsStorageRoleSearchService 'core/security/storage-role.bicep' = if (useCloudIngestionAcls && searchServiceSkuName != 'free') {
  scope: az.resourceGroup(adlsStorageResourceGroupNameActual)
  name: 'adls-storage-role-searchservice'
  params: {
    storageAccountName: adlsStorageAccountNameResolved
    principalId: searchService.outputs.systemAssignedPrincipalId
    roleDefinitionId: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Owner on ADLS storage for user to manage ACLs
module adlsStorageOwnerRoleUser 'core/security/storage-role.bicep' = if (useCloudIngestionAcls) {
  scope: az.resourceGroup(adlsStorageResourceGroupNameActual)
  name: 'adls-storage-owner-role-user'
  params: {
    storageAccountName: adlsStorageAccountNameResolved
    principalId: principalId
    roleDefinitionId: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Storage Blob Data Owner
    principalType: principalType
  }
}

module adlsStorageRoleAksIngestion 'core/security/storage-role.bicep' = if (useCloudIngestionAcls && useCloudIngestion) {
  scope: az.resourceGroup(adlsStorageResourceGroupNameActual)
  name: 'adls-storage-role-aks-ingestion'
  params: {
    storageAccountName: adlsStorageAccountNameResolved
    principalId: aksWorkloadIdentity!.outputs.principalId
    roleDefinitionId: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
    principalType: 'ServicePrincipal'
  }
}

// Used to issue search queries
// https://learn.microsoft.com/azure/search/search-security-rbac
module searchRoleBackend 'core/security/role.bicep' = {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
    principalType: 'ServicePrincipal'
  }
}

module speechRoleBackend 'core/security/role.bicep' = {
  scope: az.resourceGroup(speechResourceGroupNameActual)
  name: 'speech-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: 'f2dc8367-1007-4938-bd23-fe263f013447' // Cognitive Services Speech User
    principalType: 'ServicePrincipal'
  }
}

// RBAC for Cosmos DB
// https://learn.microsoft.com/azure/cosmos-db/nosql/security/how-to-grant-data-plane-role-based-access
module cosmosDbRoleBackend 'core/security/documentdb-sql-role.bicep' = if (useAuthentication && useChatHistoryCosmos) {
  scope: az.resourceGroup(cosmosDbResourceGroupNameActual)
  name: 'cosmosdb-role-backend'
  params: {
    databaseAccountName: (useAuthentication && useChatHistoryCosmos) ? cosmosDb!.outputs.name : ''
    principalId: backendPrincipalId
    // Cosmos DB Built-in Data Contributor role
    roleDefinitionId: (useAuthentication && useChatHistoryCosmos)
      ? '/${subscription().id}/resourceGroups/${cosmosDb!.outputs.resourceGroupName}/providers/Microsoft.DocumentDB/databaseAccounts/${cosmosDb!.outputs.name}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
      : ''
  }
}

module isolation 'network-isolation.bicep' = if (usePrivateNetworking) {
  name: 'networks'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    vnetName: '${abbrs.virtualNetworks}${resourceToken}'
    useVpnGateway: useVpnGateway
    vpnGatewayName: useVpnGateway ? '${abbrs.networkVpnGateways}${resourceToken}' : ''
    dnsResolverName: useVpnGateway ? '${abbrs.privateDnsResolver}${resourceToken}' : ''
  }
}

module aksCluster 'core/host/aks.bicep' = {
  name: 'aks-cluster'
  scope: resourceGroup
  params: {
    name: !empty(aksClusterName) ? aksClusterName : '${abbrs.containerServiceManagedClusters}${resourceToken}'
    location: location
    tags: tags
    subnetId: usePrivateNetworking ? isolation!.outputs.aksSubnetId : ''
    logAnalyticsWorkspaceResourceId: useApplicationInsights ? monitoring!.outputs.logAnalyticsWorkspaceId : ''
    enableMonitoringAddon: aksEnableMonitoringAddon
    nodeVmSize: aksNodeVmSize
    minNodeCount: aksMinNodeCount
    maxNodeCount: aksMaxNodeCount
  }
}

module aksContainerRegistry 'core/host/container-registry.bicep' = {
  name: 'aks-container-registry'
  scope: resourceGroup
  params: {
    name: !empty(aksContainerRegistryName)
      ? aksContainerRegistryName
      : '${abbrs.containerRegistryRegistries}${replace(toLower(environmentName), '-', '')}${take(resourceToken, 8)}'
    location: location
    tags: tags
    useVnet: true
  }
}

module aksWorkloadFederation 'core/security/workload-identity-federation.bicep' = {
  name: 'aks-workload-federation'
  scope: resourceGroup
  params: {
    identityName: aksWorkloadIdentity!.outputs.name
    credentialName: 'crv'
    oidcIssuerUrl: aksCluster!.outputs.oidcIssuerUrl
    subject: 'system:serviceaccount:crv:crv'
  }
}

module aksAcrPullRole 'core/security/role.bicep' = {
  name: 'aks-acr-pull'
  scope: resourceGroup
  params: {
    principalId: aksCluster!.outputs.kubeletPrincipalId
    roleDefinitionId: '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
    principalType: 'ServicePrincipal'
  }
}

module applicationGateway 'core/host/application-gateway.bicep' = {
  name: 'application-gateway'
  scope: resourceGroup
  params: {
    name: !empty(applicationGatewayName) ? applicationGatewayName : '${abbrs.networkApplicationGateways}${resourceToken}'
    publicIpName: !empty(applicationGatewayPublicIpName)
      ? applicationGatewayPublicIpName
      : '${abbrs.networkPublicIPAddresses}${resourceToken}'
    location: location
    tags: tags
    subnetId: isolation.outputs.applicationGatewaySubnetId
    minCapacity: applicationGatewayMinCapacity
    maxCapacity: applicationGatewayMaxCapacity
    agicPrincipalId: aksWorkloadIdentity.outputs.principalId
  }
}

module agicResourceGroupReader 'core/security/role.bicep' = {
  name: 'agic-resource-group-reader'
  scope: resourceGroup
  params: {
    principalId: aksWorkloadIdentity.outputs.principalId
    roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7' // Reader
    principalType: 'ServicePrincipal'
  }
}

module agicWorkloadFederation 'core/security/workload-identity-federation.bicep' = {
  name: 'agic-workload-federation'
  scope: resourceGroup
  params: {
    identityName: aksWorkloadIdentity.outputs.name
    credentialName: 'agic'
    oidcIssuerUrl: aksCluster.outputs.oidcIssuerUrl
    subject: 'system:serviceaccount:kube-system:ingress-azure'
  }
  dependsOn: [aksWorkloadFederation]
}

var backendPrincipalId = aksWorkloadIdentity.outputs.principalId

var environmentData = environment()

var openAiPrivateEndpointConnection = (usePrivateNetworking && deployFoundryAccount)
  ? [
      {
        groupId: 'account'
        // Microsoft Foundry (AIServices) accounts resolve the services.ai.azure.com zone in
        // addition to openai.azure.com and cognitiveservices.azure.com.
        // https://learn.microsoft.com/azure/private-link/private-endpoint-dns#ai--machine-learning
        dnsZoneNames: [
          'privatelink.services.ai.azure.com'
          'privatelink.openai.azure.com'
          'privatelink.cognitiveservices.azure.com'
        ]
        resourceIds: [foundryAccount!.outputs.resourceId]
      }
    ]
  : []

var cognitiveServicesPrivateEndpointConnection = (usePrivateNetworking && (!useLocalPdfParser || useMultimodal || useMediaDescriberAzureCU))
  ? [
      {
        groupId: 'account'
        dnsZoneNames: ['privatelink.cognitiveservices.azure.com']
        // Only include generic Cognitive Services-based resources (Form Recognizer / Vision / Content Understanding)
        // Azure OpenAI uses its own privatelink.openai.azure.com zone and already has a separate private endpoint above.
        resourceIds: concat(
          !useLocalPdfParser ? [documentIntelligence.outputs.resourceId] : [],
          useMultimodal ? [vision!.outputs.resourceId] : [],
          useMediaDescriberAzureCU ? [contentUnderstanding!.outputs.resourceId] : []
        )
      }
    ]
  : []

var aksContainerRegistryPrivateEndpointConnection = usePrivateNetworking
  ? [
      {
        groupId: 'registry'
        dnsZoneNames: ['privatelink.azurecr.io']
        resourceIds: [aksContainerRegistry!.outputs.resourceId]
      }
    ]
  : []

var speechPrivateEndpointConnection = (usePrivateNetworking && useSpeechOutputAzure)
  ? [
      {
        groupId: 'account'
        dnsZoneNames: ['privatelink.cognitiveservices.azure.com']
        resourceIds: [speech!.outputs.resourceId]
      }
    ]
  : []
var otherPrivateEndpointConnections = (usePrivateNetworking)
  ? [
      {
        groupId: 'blob'
        dnsZoneNames: ['privatelink.blob.${environmentData.suffixes.storage}']
        resourceIds: concat([storage.outputs.id], useUserUpload ? [userStorage!.outputs.id] : [])
      }
      {
        groupId: 'searchService'
        dnsZoneNames: ['privatelink.search.windows.net']
        resourceIds: [searchService.outputs.id]
      }
      {
        groupId: 'sql'
        dnsZoneNames: ['privatelink.documents.azure.com']
        resourceIds: provisionCosmos ? [cosmosDb!.outputs.resourceId] : []
      }
    ]
  : []

var ingestionPrivateEndpointConnections = (usePrivateNetworking && useIngestionPipeline)
  ? [
      {
        groupId: 'namespace'
        dnsZoneNames: ['privatelink.servicebus.windows.net']
        // Service Bus private endpoints require Premium SKU.
        resourceIds: serviceBusSkuName == 'Premium' ? [serviceBus!.outputs.id] : []
      }
      {
        groupId: 'vault'
        dnsZoneNames: ['privatelink.vaultcore.azure.net']
        resourceIds: [keyVault!.outputs.id]
      }
      {
        groupId: 'postgresqlServer'
        dnsZoneNames: ['privatelink.postgres.database.azure.com']
        resourceIds: [postgresServer!.outputs.id]
      }
    ]
  : []

var privateEndpointConnections = concat(
  otherPrivateEndpointConnections,
  openAiPrivateEndpointConnection,
  cognitiveServicesPrivateEndpointConnection,
  aksContainerRegistryPrivateEndpointConnection,
  speechPrivateEndpointConnection,
  ingestionPrivateEndpointConnections
)

module privateEndpoints 'private-endpoints.bicep' = if (usePrivateNetworking) {
  name: 'privateEndpoints'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    privateEndpointConnections: privateEndpointConnections
    applicationInsightsId: useApplicationInsights ? monitoring!.outputs.applicationInsightsId : ''
    logAnalyticsWorkspaceId: useApplicationInsights ? monitoring!.outputs.logAnalyticsWorkspaceId : ''
    vnetName: isolation!.outputs.vnetName
    vnetPeSubnetId: isolation.outputs.privateEndpointsSubnetId
  }
  // Wait for the Foundry project to be created before creating the account private endpoint.
  // The account PUT returns before it is fully provisioned; project creation is the convergence
  // point that avoids AccountProvisioningStateInvalid errors on the private endpoint.
  dependsOn: [foundryProject]
}

// Used to read index definitions (required when using authentication)
// https://learn.microsoft.com/azure/search/search-security-rbac
module searchReaderRoleBackend 'core/security/role.bicep' = if (useAuthentication) {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-reader-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
    principalType: 'ServicePrincipal'
  }
}

// Used to add/remove documents from index (required for user upload feature)
module searchContribRoleBackend 'core/security/role.bicep' = if (useUserUpload) {
  scope: az.resourceGroup(searchServiceResourceGroupNameActual)
  name: 'search-contrib-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
    principalType: 'ServicePrincipal'
  }
}

// For Azure AI Vision access by the backend
module visionRoleBackend 'core/security/role.bicep' = if (useMultimodal) {
  scope: az.resourceGroup(visionResourceGroupNameActual)
  name: 'vision-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908'
    principalType: 'ServicePrincipal'
  }
}

// For document intelligence access by the backend
module documentIntelligenceRoleBackend 'core/security/role.bicep' = if (useUserUpload) {
  scope: az.resourceGroup(documentIntelligenceResourceGroupNameActual)
  name: 'documentintelligence-role-backend'
  params: {
    principalId: backendPrincipalId
    roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908'
    principalType: 'ServicePrincipal'
  }
}

// The ingestion Layout Jobs call Document Intelligence with the AKS workload identity.
// Document Intelligence has disableLocalAuth=true, so Entra RBAC (Cognitive Services User) is required.
module documentIntelligenceRoleAksIngestion 'core/security/role.bicep' = if (useIngestionPipeline) {
  scope: az.resourceGroup(documentIntelligenceResourceGroupNameActual)
  name: 'documentintelligence-role-aks-ingestion'
  params: {
    principalId: aksWorkloadIdentity.outputs.principalId
    roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User
    principalType: 'ServicePrincipal'
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenantId
output AZURE_AUTH_TENANT_ID string = authTenantId
output AZURE_RESOURCE_GROUP string = resourceGroup.name

// Shared by all OpenAI deployments
output OPENAI_HOST string = openAiHost
output AZURE_OPENAI_EMB_MODEL_NAME string = embedding.modelName
output AZURE_OPENAI_EMB_DIMENSIONS int = embedding.dimensions
output AZURE_OPENAI_CHATGPT_MODEL string = chatGpt.modelName

// Specific to Azure OpenAI
output AZURE_OPENAI_SERVICE string = deployFoundryAccount ? foundryAccount!.outputs.name : ''
output AZURE_OPENAI_ENDPOINT string = deployFoundryAccount ? foundryAccount!.outputs.endpoint : ''
output AZURE_OPENAI_RESOURCE_GROUP string = isAzureOpenAiHost ? openAiResourceGroupNameActual : ''

// Microsoft Foundry project hosted inside the Foundry (AIServices) account above.
output FOUNDRY_PROJECT_NAME string = deployFoundryAccount ? foundryProject!.outputs.name : ''
output FOUNDRY_PROJECT_ENDPOINT string = deployFoundryAccount ? foundryProject!.outputs.endpoint : ''

output AZURE_OPENAI_CHATGPT_DEPLOYMENT string = isAzureOpenAiHost ? chatGpt.deploymentName : ''
output AZURE_OPENAI_CHATGPT_DEPLOYMENT_VERSION string = isAzureOpenAiHost ? chatGpt.deploymentVersion : ''
output AZURE_OPENAI_CHATGPT_DEPLOYMENT_SKU string = isAzureOpenAiHost ? chatGpt.deploymentSkuName : ''
output AZURE_OPENAI_EMB_DEPLOYMENT string = isAzureOpenAiHost ? embedding.deploymentName : ''
output AZURE_OPENAI_EMB_DEPLOYMENT_VERSION string = isAzureOpenAiHost ? embedding.deploymentVersion : ''
output AZURE_OPENAI_EMB_DEPLOYMENT_SKU string = isAzureOpenAiHost ? embedding.deploymentSkuName : ''
output AZURE_OPENAI_EVAL_DEPLOYMENT string = isAzureOpenAiHost && useEval ? eval.deploymentName : ''
output AZURE_OPENAI_EVAL_DEPLOYMENT_SKU string = isAzureOpenAiHost && useEval ? eval.deploymentSkuName : ''
output AZURE_OPENAI_EVAL_MODEL string = isAzureOpenAiHost && useEval ? eval.modelName : ''
output AZURE_OPENAI_KNOWLEDGEBASE_DEPLOYMENT string = isAzureOpenAiHost && useAgenticKnowledgeBase ? knowledgeBase.deploymentName : ''
output AZURE_OPENAI_KNOWLEDGEBASE_MODEL string = isAzureOpenAiHost && useAgenticKnowledgeBase ? knowledgeBase.modelName : ''
output AZURE_OPENAI_REASONING_EFFORT string  = defaultReasoningEffort
output AZURE_SEARCH_KNOWLEDGEBASE_RETRIEVAL_REASONING_EFFORT string = defaultRetrievalReasoningEffort
output AZURE_SPEECH_SERVICE_ID string = useSpeechOutputAzure ? speech!.outputs.resourceId : ''
output AZURE_SPEECH_SERVICE_LOCATION string = useSpeechOutputAzure ? speech!.outputs.location : ''

output AZURE_VISION_ENDPOINT string = useMultimodal ? vision!.outputs.endpoint : ''
output AZURE_CONTENTUNDERSTANDING_ENDPOINT string = useMediaDescriberAzureCU ? contentUnderstanding!.outputs.endpoint : ''

output AZURE_DOCUMENTINTELLIGENCE_SERVICE string = documentIntelligence.outputs.name
output AZURE_DOCUMENTINTELLIGENCE_RESOURCE_GROUP string = documentIntelligenceResourceGroupNameActual

output AZURE_SEARCH_INDEX string = searchIndexName
output AZURE_SEARCH_KNOWLEDGEBASE_NAME string = knowledgeBaseName
output AZURE_SEARCH_SERVICE string = searchService.outputs.name
output AZURE_SEARCH_SERVICE_RESOURCE_GROUP string = searchServiceResourceGroupNameActual
output AZURE_SEARCH_SEMANTIC_RANKER string = actualSearchServiceSemanticRankerLevel
output AZURE_SEARCH_FIELD_NAME_EMBEDDING string = searchFieldNameEmbedding
output AZURE_SEARCH_USER_ASSIGNED_IDENTITY_RESOURCE_ID string = searchService.outputs.userAssignedIdentityResourceId

output AZURE_COSMOSDB_ACCOUNT string = (useAuthentication && useChatHistoryCosmos) ? cosmosDb!.outputs.name : ''
output AZURE_CHAT_HISTORY_DATABASE string = chatHistoryDatabaseName
output AZURE_CHAT_HISTORY_CONTAINER string = chatHistoryContainerName
output AZURE_CHAT_HISTORY_VERSION string = chatHistoryVersion

// Ingestion pipeline (Architecture 1) resource outputs
output AZURE_INGESTION_PIPELINE object = useIngestionPipeline ? {
  enabled: true
  postgresServer: postgresServer!.outputs.name
  postgresFqdn: postgresServer!.outputs.fqdn
  catalogDatabase: ingestionCatalogDatabaseName
  agentMemoryDatabase: agentMemoryDatabaseName
  serviceBusNamespace: serviceBus!.outputs.name
  serviceBusQueue: ingestionQueueName
  keyVault: keyVault!.outputs.name
  keyVaultUri: keyVault!.outputs.uri
  extractionCacheContainer: extractionCacheContainerName
} : { enabled: false }

output AZURE_STORAGE_ACCOUNT string = storage.outputs.name
output AZURE_STORAGE_CONTAINER string = storageContainerName
output AZURE_STORAGE_RESOURCE_GROUP string = storageResourceGroupNameActual

output AZURE_ADLS_STORAGE_ACCOUNT string = useCloudIngestionAcls ? adlsStorageAccountNameResolved : ''
output AZURE_CLOUD_INGESTION_STORAGE_ACCOUNT string = useCloudIngestionAcls ? adlsStorageAccountNameResolved : storage.outputs.name
output AZURE_CLOUD_INGESTION_STORAGE_RESOURCE_GROUP string = useCloudIngestionAcls ? adlsStorageResourceGroupNameActual : storageResourceGroupNameActual
output USE_CLOUD_INGESTION_ACLS bool = useCloudIngestionAcls

output AZURE_USERSTORAGE_ACCOUNT string = useUserUpload ? userStorage!.outputs.name : ''
output AZURE_USERSTORAGE_CONTAINER string = userStorageContainerName
output AZURE_USERSTORAGE_RESOURCE_GROUP string = storageResourceGroupNameActual


output AZURE_IMAGESTORAGE_CONTAINER string = useMultimodal ? imageStorageContainerName : ''

output AZURE_USE_AUTHENTICATION bool = useAuthentication

output BACKEND_URI string = 'http://${applicationGateway.outputs.publicIpAddress}'
output APPLICATION_GATEWAY_NAME string = applicationGateway.outputs.name
output APPLICATION_GATEWAY_PUBLIC_IP string = applicationGateway.outputs.publicIpAddress
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = aksContainerRegistry.outputs.loginServer
output AKS_CLUSTER_NAME string = aksCluster.outputs.name
output AKS_OIDC_ISSUER_URL string = aksCluster.outputs.oidcIssuerUrl
output AKS_WORKLOAD_IDENTITY_CLIENT_ID string = aksWorkloadIdentity.outputs.clientId

output AZURE_VPN_CONFIG_DOWNLOAD_LINK string = useVpnGateway ? 'https://portal.azure.com/#@${tenant().tenantId}/resource${isolation!.outputs.virtualNetworkGatewayId}/pointtositeconfiguration' : ''
