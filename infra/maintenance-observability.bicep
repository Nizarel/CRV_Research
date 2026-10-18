// Maintenance agent workbook and Azure Monitor log alerts (roles maintenance-agent, maintenance-web,
// maintenance-evaluation in the shared Application Insights resource). Thresholds start from the
// 2026-10 benchmark baseline (TTFT p95 16-22 s, turn p95 ~12 s); tune after two weeks of pilot data.

param location string = resourceGroup().location
param applicationInsightsResourceId string
@description('Log Analytics workspace resource ID; workbook and alert queries read its App* tables.')
param logAnalyticsWorkspaceResourceId string
param tags object = {}

@description('Optional Action Group resource IDs notified when a Maintenance alert fires.')
param actionGroupResourceIds array = []

var turnsQuery = '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| extend Outcome=tostring(Properties["maintenance.outcome"])
| summarize Turns=count() by Outcome, bin(TimeGenerated, 15m)
| order by TimeGenerated asc
'''

var latencyQuery = '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| extend TtftMs=toreal(Properties["maintenance.ttft_ms"])
| summarize TurnP50Ms=percentile(DurationMs, 50), TurnP95Ms=percentile(DurationMs, 95), TtftP50Ms=percentile(TtftMs, 50), TtftP95Ms=percentile(TtftMs, 95) by bin(TimeGenerated, 1h)
| order by TimeGenerated asc
'''

var stageQuery = '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "maintenance."
| extend Stage=tostring(Properties["maintenance.stage"])
| where isnotempty(Stage)
| summarize Runs=count(), Failures=countif(Success == false), P50Ms=percentile(DurationMs, 50), P95Ms=percentile(DurationMs, 95) by Stage
| order by P95Ms desc
'''

var modelQuery = '''
AppDependencies
| where AppRoleName == "maintenance-agent"
| where Name startswith "chat " or Target has "cognitiveservices" or Target has "openai"
| summarize Calls=count(), Failures=countif(Success == false), Throttled=countif(ResultCode == "429"), P50Ms=percentile(DurationMs, 50), P95Ms=percentile(DurationMs, 95) by Name
| order by Calls desc
'''

var retrievalQuery = '''
let legs = AppDependencies
| where AppRoleName == "maintenance-agent" and Name == "execute_tool evidence_search"
| extend Leg=tostring(Properties["maintenance.leg"]), Hits=toint(Properties["maintenance.hits"])
| summarize Runs=count(), Failures=countif(Success == false), MeanHits=avg(Hits), P95Ms=percentile(DurationMs, 95) by Component=strcat("leg:", Leg);
let fallbacks = AppMetrics
| where AppRoleName == "maintenance-agent" and Name == "maint.kg.fallbacks"
| extend Operation=tostring(Properties["operation"])
| summarize Failures=tolong(sum(Sum)) by Component=strcat("kg:", Operation)
| extend Runs=long(null), MeanHits=real(null), P95Ms=real(null);
union legs, fallbacks
| project Component, Runs, Failures, MeanHits, P95Ms
| order by Component asc
'''

var guardrailQuery = '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| extend P=Properties
| summarize Turns=count(),
    Abstained=countif(tostring(P["maintenance.outcome"]) == "abstained"),
    Clarification=countif(tostring(P["maintenance.outcome"]) == "clarification"),
    InvalidCitations=countif(tobool(P["maintenance.guardrail.invalid_citations"])),
    UnverifiedNumbers=countif(tobool(P["maintenance.guardrail.unverified_numbers"])),
    BindingConflicts=countif(tobool(P["maintenance.guardrail.binding_conflicts"])),
    PresentationGaps=countif(tobool(P["maintenance.guardrail.presentation_gaps"])),
    HazardEscalation=countif(tobool(P["maintenance.guardrail.hazard_escalation"])),
    RetrievalFailed=countif(tobool(P["maintenance.retrieval_failed"])),
    PlannerDegraded=countif(tobool(P["maintenance.planner_degraded"]))
    by Day=bin(TimeGenerated, 1d)
| order by Day asc
'''

var tokensQuery = '''
AppMetrics
| where AppRoleName == "maintenance-agent" and Name == "maint.tokens"
| extend Role=tostring(Properties["role"]), Kind=tostring(Properties["kind"])
| summarize Tokens=sum(Sum) by Role, Kind, Day=bin(TimeGenerated, 1d)
| order by Day asc, Role asc, Kind asc
'''

var qualityQuery = '''
AppEvents
| where AppRoleName in ("maintenance-agent", "maintenance-evaluation") and Name == "gen_ai.evaluation.result"
| extend P=Properties
| extend Evaluation=tostring(P["gen_ai.evaluation.name"]), Label=tostring(P["gen_ai.evaluation.score.label"]), Score=todouble(P["gen_ai.evaluation.score.value"])
| extend Source=case(tostring(P["microsoft.gen_ai.human_evaluation.source"]) == "end_user", "user", isnotempty(tostring(P["maintenance.eval.suite"])), tostring(P["maintenance.eval.suite"]), "other")
| summarize Samples=count(), PassRate=round(100.0 * countif(Label == "pass") / count(), 1), MeanScore=round(avg(Score), 3) by Source, Evaluation, Day=bin(TimeGenerated, 1d)
| order by Day asc, Source asc, Evaluation asc
'''

var releaseQuery = '''
let turns = AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| extend Version=tostring(Properties["maintenance.app_version"]), ResponseId=tostring(Properties["gen_ai.response.id"]), TtftMs=toreal(Properties["maintenance.ttft_ms"]), Outcome=tostring(Properties["maintenance.outcome"]);
let votes = AppEvents
| where AppRoleName == "maintenance-agent" and Name == "gen_ai.evaluation.result"
| where tostring(Properties["microsoft.gen_ai.human_evaluation.source"]) == "end_user"
| project ResponseId=tostring(Properties["gen_ai.response.id"]), Label=tostring(Properties["gen_ai.evaluation.score.label"]);
turns
| join kind=leftouter votes on ResponseId
| summarize Turns=dcount(OperationId), ErrorRate=round(100.0 * countif(Outcome == "error") / count(), 1), TtftP95Ms=percentile(TtftMs, 95), TurnP95Ms=percentile(DurationMs, 95), Votes=countif(isnotempty(Label)), UpVotes=countif(Label == "pass"), FirstSeen=min(TimeGenerated), LastSeen=max(TimeGenerated) by Version
| extend FeedbackPassRate=iff(Votes > 0, round(100.0 * UpVotes / Votes, 1), real(null))
| order by FirstSeen desc
'''

var evalRunsQuery = '''
AppEvents
| where AppRoleName == "maintenance-evaluation" and Name == "maintenance.eval.run"
| extend P=Properties
| project TimeGenerated, RunId=tostring(P["maintenance.eval.run_id"]), Suite=tostring(P["maintenance.eval.suite"]), Version=tostring(P["maintenance.app_version"]), Gate=tostring(P["maintenance.eval.gate"]), Cases=toint(P["maintenance.eval.total"]), Eligible=toint(P["maintenance.eval.eligible"]), AnswerPassRate=todouble(P["maintenance.eval.answer_pass_rate"]), SourceMatchRate=todouble(P["maintenance.eval.source_match_rate"]), TtftP95S=todouble(P["maintenance.eval.first_token_seconds_p95"]), JudgePromptSha=tostring(P["maintenance.eval.judge_prompt_sha"])
| order by TimeGenerated desc
'''

var browserQuery = '''
AppEvents
| where AppRoleName == "maintenance-web"
| extend Reason=tostring(Properties["reason"]), Outcome=tostring(Properties["outcome"])
| summarize Events=count(), FirstTokenP95Ms=percentile(todouble(Measurements["first_token_ms"]), 95) by Name, Detail=iff(isnotempty(Reason), Reason, Outcome), Day=bin(TimeGenerated, 1d)
| order by Day asc, Name asc
'''

var exceptionsQuery = '''
AppExceptions
| where AppRoleName == "maintenance-agent"
| summarize Count=count(), LastSeen=max(TimeGenerated), SampleOperation=any(OperationId) by ExceptionType, OuterMessage=substring(OuterMessage, 0, 120)
| order by Count desc
'''

var sections = [
  { name: 'turns', title: 'Turns by outcome', query: turnsQuery, chart: 'timechart', durationMs: 86400000 }
  { name: 'latency', title: 'Turn duration and time to first token (ms)', query: latencyQuery, chart: 'timechart', durationMs: 604800000 }
  { name: 'stages', title: 'Graph node latency', query: stageQuery, chart: 'table', durationMs: 86400000 }
  { name: 'models', title: 'Model and HTTP dependency latency, failures and throttling', query: modelQuery, chart: 'table', durationMs: 86400000 }
  { name: 'retrieval', title: 'Retrieval legs and knowledge graph fallbacks', query: retrievalQuery, chart: 'table', durationMs: 86400000 }
  { name: 'guardrails', title: 'Guardrail flags and degraded turns', query: guardrailQuery, chart: 'table', durationMs: 1209600000 }
  { name: 'tokens', title: 'Model tokens by role', query: tokensQuery, chart: 'table', durationMs: 1209600000 }
  { name: 'quality', title: 'Quality: user feedback, golden and online evaluations', query: qualityQuery, chart: 'table', durationMs: 2592000000 }
  { name: 'releases', title: 'Release comparison', query: releaseQuery, chart: 'table', durationMs: 2592000000 }
  { name: 'eval-runs', title: 'Evaluation runs and promotion gates', query: evalRunsQuery, chart: 'table', durationMs: 2592000000 }
  { name: 'browser', title: 'Browser: completed turns, failures and gateway rejections', query: browserQuery, chart: 'table', durationMs: 604800000 }
  { name: 'exceptions', title: 'Backend exceptions', query: exceptionsQuery, chart: 'table', durationMs: 604800000 }
]

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(applicationInsightsResourceId, 'maintenance-observability')
  location: location
  kind: 'shared'
  tags: union(tags, {
    'hidden-title': 'Maintenance Agent Observability'
  })
  properties: {
    displayName: 'Maintenance Agent Observability'
    category: 'workbook'
    sourceId: applicationInsightsResourceId
    serializedData: string({
      version: 'Notebook/1.0'
      items: concat(
        [
          {
            type: 1
            name: 'overview'
            content: {
              json: '# Maintenance agent observability\nOne `invoke_agent` trace per chat turn (`gen_ai.response.id` = answer id), user feedback and evaluations as `gen_ai.evaluation.result`. Question and answer text is only in the protected AppGenAIContent table, when capture is enabled.'
            }
          }
        ],
        map(sections, section => {
          type: 3
          name: section.name
          content: {
            version: 'KqlItem/1.0'
            title: section.title
            query: section.query
            size: 0
            timeContext: {
              durationMs: section.durationMs
            }
            queryType: 0
            resourceType: 'microsoft.operationalinsights/workspaces'
            crossComponentResources: [logAnalyticsWorkspaceResourceId]
            visualization: section.chart
          }
        })
      )
      isLocked: false
      fallbackResourceIds: [applicationInsightsResourceId]
    })
  }
}

var alertDefinitions = [
  {
    name: 'maintenance-agent-error-rate'
    displayName: 'Maintenance agent error rate'
    description: 'At least five turns and 20 percent or more ended in an agent failure within 15 minutes.'
    severity: 2
    frequency: 'PT5M'
    window: 'PT15M'
    query: '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| summarize Total=count(), Errors=countif(tostring(Properties["maintenance.outcome"]) == "error")
| where Total >= 5 and 100.0 * Errors / Total >= 20.0
'''
  }
  {
    name: 'maintenance-agent-slow-first-token'
    displayName: 'Maintenance agent slow first token'
    description: 'Time to first answer token p95 exceeded 25 seconds over at least five turns in 30 minutes.'
    severity: 3
    frequency: 'PT15M'
    window: 'PT30M'
    query: '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| extend TtftMs=toreal(Properties["maintenance.ttft_ms"])
| where isnotnull(TtftMs)
| summarize Turns=count(), TtftP95Ms=percentile(TtftMs, 95)
| where Turns >= 5 and TtftP95Ms > 25000.0
'''
  }
  {
    name: 'maintenance-agent-retrieval-legs-failing'
    displayName: 'Maintenance evidence retrieval legs failing'
    description: 'At least ten retrieval legs ran and 20 percent or more failed or timed out within 30 minutes.'
    severity: 2
    frequency: 'PT15M'
    window: 'PT30M'
    query: '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name == "execute_tool evidence_search"
| summarize Legs=count(), Failures=countif(Success == false)
| where Legs >= 10 and 100.0 * Failures / Legs >= 20.0
'''
  }
  {
    name: 'maintenance-agent-invalid-citations'
    displayName: 'Maintenance answer cited an unknown source'
    description: 'An answer cited a source id that was not retrieved for the turn (zero tolerance).'
    severity: 2
    frequency: 'PT15M'
    window: 'PT15M'
    query: '''
AppDependencies
| where AppRoleName == "maintenance-agent" and Name startswith "invoke_agent"
| where tobool(Properties["maintenance.guardrail.invalid_citations"])
| summarize Turns=count()
| where Turns > 0
'''
  }
  {
    name: 'maintenance-agent-kg-fallbacks'
    displayName: 'Maintenance knowledge graph falling back to snapshot'
    description: 'Knowledge graph calls fell back to the snapshot at least five times within 30 minutes.'
    severity: 3
    frequency: 'PT15M'
    window: 'PT30M'
    query: '''
AppMetrics
| where AppRoleName == "maintenance-agent" and Name == "maint.kg.fallbacks"
| summarize Fallbacks=sum(Sum)
| where Fallbacks >= 5
'''
  }
  {
    name: 'maintenance-agent-throttling'
    displayName: 'Maintenance agent throttled by a dependency'
    description: 'At least five dependency calls returned HTTP 429 within 15 minutes.'
    severity: 3
    frequency: 'PT5M'
    window: 'PT15M'
    query: '''
AppDependencies
| where AppRoleName == "maintenance-agent" and ResultCode == "429"
| summarize Throttled=count()
| where Throttled >= 5
'''
  }
  {
    name: 'maintenance-eval-gate-failed'
    displayName: 'Maintenance evaluation gate failed'
    description: 'A golden-set evaluation run reported a failed promotion gate.'
    severity: 1
    frequency: 'PT1H'
    window: 'PT1H'
    query: '''
AppEvents
| where AppRoleName == "maintenance-evaluation" and Name == "maintenance.eval.run"
| where tostring(Properties["maintenance.eval.gate"]) == "fail"
| summarize Runs=count()
| where Runs > 0
'''
  }
  {
    name: 'maintenance-agent-telemetry-silent'
    displayName: 'Maintenance agent telemetry missing'
    description: 'Backend pods are running but sent no telemetry for 15 minutes (exporter or ingestion broken).'
    severity: 3
    frequency: 'PT15M'
    window: 'PT15M'
    query: '''
let items = toscalar(union isfuzzy=true AppMetrics, AppPerformanceCounters, AppRequests
    | where AppRoleName == "maintenance-agent"
    | count);
KubePodInventory
| where Namespace == "crv" and Name startswith "backend-" and PodStatus == "Running"
| summarize Pods=dcount(Name)
| where Pods > 0 and items == 0
'''
  }
  {
    name: 'maintenance-agent-negative-feedback'
    displayName: 'Maintenance user feedback mostly negative'
    description: 'At least five user votes in 24 hours and fewer than 60 percent were positive.'
    severity: 3
    frequency: 'PT6H'
    window: 'P1D'
    query: '''
AppEvents
| where AppRoleName == "maintenance-agent" and Name == "gen_ai.evaluation.result"
| where tostring(Properties["microsoft.gen_ai.human_evaluation.source"]) == "end_user"
| summarize Votes=count(), Up=countif(tostring(Properties["gen_ai.evaluation.score.label"]) == "pass")
| where Votes >= 5 and 100.0 * Up / Votes < 60.0
'''
  }
  {
    name: 'maintenance-online-groundedness-drop'
    displayName: 'Maintenance online groundedness drop'
    description: 'At least ten sampled live answers were judged in 24 hours and fewer than 80 percent were grounded.'
    severity: 3
    frequency: 'PT6H'
    window: 'P1D'
    query: '''
AppEvents
| where AppRoleName == "maintenance-evaluation" and Name == "gen_ai.evaluation.result"
| where tostring(Properties["maintenance.eval.suite"]) == "online" and tostring(Properties["gen_ai.evaluation.name"]) == "groundedness"
| summarize Judged=count(), Passed=countif(tostring(Properties["gen_ai.evaluation.score.label"]) == "pass")
| where Judged >= 10 and 100.0 * Passed / Judged < 80.0
'''
  }
  {
    name: 'maintenance-backend-restarts'
    displayName: 'Maintenance backend container restarted'
    description: 'A backend container restarted (crash or OOM kill) within 30 minutes.'
    severity: 2
    frequency: 'PT15M'
    window: 'PT30M'
    query: '''
KubePodInventory
| where Namespace == "crv" and Name startswith "backend-"
| summarize Restarts=max(ContainerRestartCount) - min(ContainerRestartCount) by Name
| where Restarts > 0
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

output workbookId string = workbook.id
