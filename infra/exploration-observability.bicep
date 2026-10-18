metadata description = 'Creates the Exploration workload workbook and Azure Monitor log alerts.'

param location string = resourceGroup().location
param applicationInsightsResourceId string
@description('Log Analytics workspace resource ID used by workspace-native alert queries.')
param logAnalyticsWorkspaceResourceId string
param tags object = {}

@description('Optional Action Group resource IDs notified when an Exploration alert fires.')
param actionGroupResourceIds array = []

var agentRunsQuery = '''
AppDependencies
| where AppRoleName == "exploration-agent" and Name == "invoke_agent"
| summarize Runs=count(), Failures=countif(Success == false), P95DurationMs=percentile(DurationMs, 95) by bin(TimeGenerated, 15m)
| order by TimeGenerated asc
'''

var stageLatencyQuery = '''
AppMetrics
| where AppRoleName == "exploration-agent" and Name == "expl.stage.duration_ms"
| extend Stage=tostring(todynamic(Properties)["stage"])
| summarize P50Ms=percentile(Sum, 50), P95Ms=percentile(Sum, 95) by Stage, bin(TimeGenerated, 15m)
| order by TimeGenerated asc
'''

var qualityQuery = '''
AppEvents
| where Name == "gen_ai.evaluation.result"
| extend Evaluation=tostring(todynamic(Properties)["gen_ai.evaluation.name"]), Label=tostring(todynamic(Properties)["gen_ai.evaluation.score.label"]), Score=todouble(todynamic(Properties)["gen_ai.evaluation.score.value"])
| summarize Samples=count(), PassRate=100.0 * countif(Label == "pass") / count(), MeanScore=avg(Score) by Evaluation, bin(TimeGenerated, 1h)
| order by TimeGenerated asc
'''

var browserQuery = '''
AppEvents
| where AppRoleName == "exploration-web"
| where Name startswith "exploration.chat." or Name == "exploration.feedback.submitted"
| summarize Events=count() by Name, bin(TimeGenerated, 15m)
| order by TimeGenerated asc
'''

var backendComparisonQuery = '''
let responseTime = AppDependencies
| where AppRoleName in ("exploration-agent", "exploration-agent-postgres") and Name == "invoke_agent"
| extend Provider=tostring(todynamic(Properties)["exploration.retrieval_provider"])
| where Provider in ("azure", "postgres")
| summarize Samples=count(), P50Ms=percentile(DurationMs, 50), P95Ms=percentile(DurationMs, 95) by Provider
| extend Metric="End-to-end response time", Tokens=long(null), QualityMean=real(null), QualityPassRate=real(null);
let retrievalTime = AppMetrics
| where AppRoleName in ("exploration-agent", "exploration-agent-postgres") and Name == "expl.stage.duration_ms"
| extend Dimensions=todynamic(Properties)
| extend Provider=tostring(Dimensions["retrieval_provider"]), Stage=tostring(Dimensions["stage"])
| where Provider in ("azure", "postgres") and Stage == "retrieve"
| summarize Samples=sum(ItemCount), P50Ms=percentile(Sum, 50), P95Ms=percentile(Sum, 95) by Provider
| extend Metric="Retrieval stage time", Tokens=long(null), QualityMean=real(null), QualityPassRate=real(null);
let tokens = AppMetrics
| where AppRoleName in ("exploration-agent", "exploration-agent-postgres") and Name == "expl.tokens"
| extend Dimensions=todynamic(Properties)
| extend Provider=tostring(Dimensions["retrieval_provider"])
| where Provider in ("azure", "postgres")
| summarize Tokens=sum(Sum) by Provider
| extend Metric="Model token consumption", Samples=long(null), P50Ms=real(null), P95Ms=real(null), QualityMean=real(null), QualityPassRate=real(null);
let groundedQuality = AppEvents
| where Name == "gen_ai.evaluation.result"
| extend Dimensions=todynamic(Properties)
| extend Provider=tostring(Dimensions["exploration.retrieval_provider"])
| where Dimensions["gen_ai.evaluation.name"] == "answer_quality" and Provider in ("azure", "postgres")
| summarize Samples=count(), QualityMean=avg(todouble(Dimensions["gen_ai.evaluation.score.value"])), QualityPassRate=100.0 * countif(Dimensions["gen_ai.evaluation.score.label"] == "pass") / count() by Provider
| extend Metric="Grounded answer quality", P50Ms=real(null), P95Ms=real(null), Tokens=long(null);
union responseTime, retrievalTime, tokens, groundedQuality
| project Provider, Metric, Samples, P50Ms, P95Ms, Tokens, QualityMean, QualityPassRate
| order by Metric asc, Provider asc
'''

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(applicationInsightsResourceId, 'exploration-observability')
  location: location
  kind: 'shared'
  tags: union(tags, {
    'hidden-title': 'Exploration Observability'
  })
  properties: {
    displayName: 'Exploration Observability'
    category: 'workbook'
    sourceId: applicationInsightsResourceId
    serializedData: string({
      version: 'Notebook/1.0'
      items: [
        {
          type: 1
          name: 'overview'
          content: {
            json: '# Exploration observability\nAgent, retrieval, quality, and browser telemetry from the shared Application Insights resource.'
          }
        }
        {
          type: 3
          name: 'agent-runs'
          content: {
            version: 'KqlItem/1.0'
            title: 'Agent runs, failures, and p95 duration'
            query: agentRunsQuery
            size: 0
            timeContext: {
              durationMs: 86400000
            }
            queryType: 0
            resourceType: 'microsoft.insights/components'
            visualization: 'timechart'
          }
        }
        {
          type: 3
          name: 'stage-latency'
          content: {
            version: 'KqlItem/1.0'
            title: 'Agent stage latency'
            query: stageLatencyQuery
            size: 0
            timeContext: {
              durationMs: 86400000
            }
            queryType: 0
            resourceType: 'microsoft.insights/components'
            visualization: 'timechart'
          }
        }
        {
          type: 3
          name: 'quality'
          content: {
            version: 'KqlItem/1.0'
            title: 'Human and golden-set quality'
            query: qualityQuery
            size: 0
            timeContext: {
              durationMs: 604800000
            }
            queryType: 0
            resourceType: 'microsoft.insights/components'
            visualization: 'timechart'
          }
        }
        {
          type: 3
          name: 'browser-events'
          content: {
            version: 'KqlItem/1.0'
            title: 'Browser chat lifecycle and feedback'
            query: browserQuery
            size: 0
            timeContext: {
              durationMs: 86400000
            }
            queryType: 0
            resourceType: 'microsoft.insights/components'
            visualization: 'timechart'
          }
        }
        {
          type: 3
          name: 'backend-comparison'
          content: {
            version: 'KqlItem/1.0'
            title: 'Azure AI Search versus PostgreSQL comparison'
            query: backendComparisonQuery
            size: 0
            timeContext: {
              durationMs: 604800000
            }
            queryType: 0
            resourceType: 'microsoft.insights/components'
            visualization: 'table'
          }
        }
      ]
      isLocked: false
      fallbackResourceIds: [applicationInsightsResourceId]
    })
  }
}

var alertDefinitions = [
  {
    name: 'exploration-agent-failure-rate-workspace'
    displayName: 'Exploration agent failure rate'
    description: 'At least five chat requests occurred and 20 percent or more failed within 15 minutes.'
    severity: 2
    frequency: 'PT5M'
    window: 'PT15M'
    query: '''
  AppDependencies
  | where AppRoleName == "exploration-agent" and Name == "invoke_agent"
  | summarize Total=count(), Failures=countif(Success == false)
| where Total >= 5 and 100.0 * Failures / Total >= 20.0
'''
  }
  {
    name: 'exploration-agent-high-latency-workspace'
    displayName: 'Exploration agent high p95 latency'
    description: 'Agent-run p95 duration exceeded two minutes within 30 minutes.'
    severity: 2
    frequency: 'PT5M'
    window: 'PT30M'
    query: '''
  AppDependencies
  | where AppRoleName == "exploration-agent" and Name == "invoke_agent"
  | summarize P95DurationMs=percentile(DurationMs, 95)
| where P95DurationMs > 120000.0
'''
  }
  {
    name: 'exploration-golden-quality-regression-workspace'
    displayName: 'Exploration golden-set quality regression'
    description: 'At least one answer-quality golden-set evaluation failed within 24 hours.'
    severity: 1
    frequency: 'PT1H'
    window: 'P1D'
    query: '''
  AppEvents
  | where Name == "gen_ai.evaluation.result"
  | extend Properties=todynamic(Properties)
  | where tostring(Properties["gen_ai.evaluation.name"]) == "answer_quality"
  | where tostring(Properties["gen_ai.evaluation.score.label"]) == "fail"
| summarize Failures=count()
| where Failures > 0
'''
  }
]

resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [
  for alert in alertDefinitions: {
    name: alert.name
    location: location
    tags: tags
    properties: {
      displayName: alert.displayName
      description: alert.description
      severity: alert.severity
      enabled: true
      evaluationFrequency: alert.frequency
      scopes: [logAnalyticsWorkspaceResourceId]
      windowSize: alert.window
      criteria: {
        allOf: [
          {
            query: alert.query
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      actions: {
        actionGroups: actionGroupResourceIds
      }
    }
  }
]