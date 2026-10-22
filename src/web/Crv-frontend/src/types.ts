/** Types mirroring the backend's Pydantic models (src/backend/crv_agent/domain, ports/persistence.py)
 * and the SSE events emitted by POST /api/v1/chat (src/backend/crv_agent/api/app.py). */

export type AnswerStatus =
  | 'answered'
  | 'partial'
  | 'insufficient_evidence'
  | 'needs_clarification'
  | 'out_of_scope'

export interface Evidence {
  /** Opaque id; the answer text cites it as `[<id>]`. */
  id: string
  release: string
  chunk_id: string
  title: string
  /** Full text of the cited chunk. */
  content: string
  source_uri: string
  /** Original corpus path of the document. */
  source_path: string
  document_code: string | null
  pages: number[]
  locators: string[]
  generated: boolean
  document_class: string
  extraction_route: string
  source_role: 'unclassified' | 'secondary_summary'
}

export interface Claim {
  text: string
  evidence_ids: string[]
  /** Exact quotation from the source supporting the claim. */
  quote: string
  subject: string
  quantity: string | null
  /** Decimals are serialised as strings by the backend. */
  value: string | number | null
  unit: string | null
  date: string | null
  conditions: string | null
  scope: string | null
  calculation_index: number | null
  qualifier: '<' | '<=' | '>' | '>=' | '~' | null
}

export interface Conflict {
  subject: string
  quantity: string
  /** Indices into the answer's `claims`. */
  claim_indices: number[]
}

export interface Calculation {
  operation: string
  /** Indices into the answer's `claims`. */
  input_claims: number[]
  value: string | number
  unit: string
  evidence_ids: string[]
}

export interface Coverage {
  matched: number
  visited: number
  enumerated: number
  catalogue_complete: boolean
  gaps: string[]
}

export interface Answer {
  status: AnswerStatus
  content: string
  claims: Claim[]
  citations: Evidence[]
  conflicts: Conflict[]
  calculations: Calculation[]
  coverage: Coverage
  reasons: string[]
  search_release: string
  kg_release: string
  trace_id: string
  usage: Record<string, number>
  retrieval_mode: 'kg' | 'search'
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled'

/** One persisted question/answer turn (`GET /api/v1/conversations/{id}`). */
export interface Run {
  id: string
  thread_id: string
  question: string
  status: RunStatus
  result: Answer | { error?: string } | null
}

export interface Thread {
  id: string
  search_release: string
  kg_release: string
}

/** Research graph steps reported by `progress` events, in execution order. */
export type ProgressStep = 'started' | 'understand' | 'research' | 'reconcile' | 'answer'

/** `plan` event: the sub-questions the agent will research. */
export interface ResearchPlan {
  intent: string
  facets: string[]
}

export interface FacetSource {
  id: string
  title: string
  source: string
  pages: number[]
  document_code: string | null
  source_role: Evidence['source_role']
}

/** `facet` event: claims found for one sub-question, quotes located verbatim but not yet
 * verified as an answer. */
export interface FacetFinding {
  facet: string
  status: 'completed' | 'budget_exhausted' | 'failed'
  claims: Claim[]
  sources: FacetSource[]
}

export type ChatRole = 'user' | 'assistant'

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  /** Present on a finished assistant message that carries a sourced answer. */
  answer?: Answer
  pending?: boolean
  step?: ProgressStep
  /** Sub-questions and provisional findings shown while the turn runs. */
  plan?: ResearchPlan
  findings?: FacetFinding[]
  /** Why a turn produced no answer (failed, cancelled, interrupted). */
  failure?: string
  threadId?: string
  /** The run id; votes are recorded against it. */
  runId?: string
}

/** Locally-persisted (localStorage) index of past conversations. The backend lists thread ids
 * only, without titles, so the browser keeps the first question as the title. */
export interface ConversationSummary {
  threadId: string
  title: string
  updatedAt: string
}
