import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { JobFilters } from '../lib/filters'
import { jobsQueryString, requestJson } from './client'
import type {
  Application,
  Board,
  CompanyRuleList,
  CompanyRulePreview,
  Facets,
  FeedbackReason,
  FeedbackResult,
  Health,
  JobDetail,
  JobPage,
  Liveness,
  ManualJobInput,
  ManualJobResult,
  Stage,
  DecisionResult,
  LearningView,
  ResumeDetail,
  ResumeList,
  TailorEstimate,
  TailoredResume,
  TailoringStatus,
  TodayView,
  KnockoutItem,
  SkillGaps,
  WritingEstimate,
  WritingText,
  SetupState,
  AuditReviewState,
} from './types'

export const PAGE_SIZE = 25

export const queryKeys = {
  jobs: (filters: JobFilters) => ['jobs', filters] as const,
  job: (id: number) => ['job', id] as const,
  facets: ['facets'] as const,
  board: ['board'] as const,
  health: ['health'] as const,
}

export function useJobs(filters: JobFilters) {
  return useQuery({
    queryKey: queryKeys.jobs(filters),
    queryFn: () => requestJson<JobPage>(`/api/jobs?${jobsQueryString(filters, PAGE_SIZE)}`),
    placeholderData: keepPreviousData, // keep the old list on screen while the next page loads
  })
}

export function useJob(id: number | null) {
  return useQuery({
    queryKey: queryKeys.job(id ?? -1),
    queryFn: () => requestJson<JobDetail>(`/api/jobs/${id}`),
    enabled: id !== null,
  })
}

export function useFacets() {
  return useQuery({
    queryKey: queryKeys.facets,
    queryFn: () => requestJson<Facets>('/api/facets'),
    staleTime: 60_000,
  })
}

export function useBoard() {
  return useQuery({ queryKey: queryKeys.board, queryFn: () => requestJson<Board>('/api/applications') })
}

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => requestJson<Health>('/api/health'),
    refetchInterval: 60_000,
  })
}

/** After any application change: the board, the job lists and the open detail all need a refresh. */
function useInvalidateApplicationViews() {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: queryKeys.board })
    void client.invalidateQueries({ queryKey: ['jobs'] })
    void client.invalidateQueries({ queryKey: ['job'] })
  }
}

export interface FeedbackInput {
  jobId: number
  action: 'dismiss' | 'like' | 'undo'
  reason?: FeedbackReason
}

export function useFeedback() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ jobId, action, reason }: FeedbackInput) =>
      requestJson<FeedbackResult>(`/api/jobs/${jobId}/feedback`, {
        method: 'POST',
        body: JSON.stringify({ action, ...(reason ? { reason } : {}) }),
      }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['jobs'] })
      void client.invalidateQueries({ queryKey: ['job'] })
      void client.invalidateQueries({ queryKey: queryKeys.facets })
    },
  })
}

export function useSaveJob() {
  const invalidate = useInvalidateApplicationViews()
  return useMutation({
    mutationFn: (jobId: number) =>
      requestJson<Application>('/api/applications', { method: 'POST', body: JSON.stringify({ job_id: jobId }) }),
    onSettled: invalidate,
  })
}

export interface ApplicationPatch {
  stage?: Stage
  notes?: string | null
  follow_up_at?: string | null
  applied_at?: string | null
  tailored_resume_id?: number | null
  contact_name?: string | null
  contact_email?: string | null
  contact_phone?: string | null
}

export function usePatchApplication() {
  const client = useQueryClient()
  const invalidate = useInvalidateApplicationViews()
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: ApplicationPatch }) =>
      requestJson<Application>(`/api/applications/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    // Optimistic: move the card (or update its notes/date) immediately, roll back if the call fails.
    onMutate: async ({ id, patch }) => {
      await client.cancelQueries({ queryKey: queryKeys.board })
      const previous = client.getQueryData<Board>(queryKeys.board)
      if (previous) client.setQueryData<Board>(queryKeys.board, applyPatchToBoard(previous, id, patch))
      return { previous }
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) client.setQueryData(queryKeys.board, context.previous)
    },
    onSettled: invalidate,
  })
}

export function useDeleteApplication() {
  const client = useQueryClient()
  const invalidate = useInvalidateApplicationViews()
  return useMutation({
    mutationFn: (id: number) => requestJson<void>(`/api/applications/${id}`, { method: 'DELETE' }),
    onMutate: async (id) => {
      await client.cancelQueries({ queryKey: queryKeys.board })
      const previous = client.getQueryData<Board>(queryKeys.board)
      if (previous) client.setQueryData<Board>(queryKeys.board, removeFromBoard(previous, id))
      return { previous }
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) client.setQueryData(queryKeys.board, context.previous)
    },
    onSettled: invalidate,
  })
}

export function useAddManualJob() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: ManualJobInput) =>
      requestJson<ManualJobResult>('/api/jobs/manual', { method: 'POST', body: JSON.stringify(input) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['jobs'] })
      void client.invalidateQueries({ queryKey: queryKeys.facets })
    },
  })
}

// --- pure board transforms (unit-tested) ---------------------------------------------------------

export function applyPatchToBoard(board: Board, applicationId: number, patch: ApplicationPatch): Board {
  const now = new Date().toISOString()
  let moved: Board['columns'][number]['cards'][number] | undefined
  const stripped = board.columns.map((column) => {
    const cards = column.cards.filter((card) => {
      if (card.application.id !== applicationId) return true
      moved = card
      return false
    })
    return { ...column, cards, count: cards.length }
  })
  if (!moved) return board
  const updated = {
    ...moved,
    application: {
      ...moved.application,
      ...(patch.notes !== undefined ? { notes: patch.notes } : {}),
      ...(patch.follow_up_at !== undefined ? { follow_up_at: patch.follow_up_at } : {}),
      ...(patch.applied_at !== undefined ? { applied_at: patch.applied_at } : {}),
      ...(patch.stage !== undefined
        ? {
            stage: patch.stage,
            applied_at:
              patch.stage === 'applied' && !moved.application.applied_at && patch.applied_at === undefined
                ? now
                : (patch.applied_at ?? moved.application.applied_at),
          }
        : {}),
      updated_at: now,
    },
  }
  const targetStage = patch.stage ?? moved.application.stage
  return {
    ...board,
    columns: stripped.map((column) => {
      if (column.stage !== targetStage) return column
      const cards = [updated, ...column.cards]
      return { ...column, cards, count: cards.length }
    }),
  }
}

export function removeFromBoard(board: Board, applicationId: number): Board {
  const columns = board.columns.map((column) => {
    const cards = column.cards.filter((card) => card.application.id !== applicationId)
    return { ...column, cards, count: cards.length }
  })
  return { ...board, total: columns.reduce((sum, column) => sum + column.count, 0), columns }
}

// --- resume tailoring ----------------------------------------------------------------------------

export function useTailored(jobId: number | null) {
  return useQuery({
    queryKey: ['tailored', jobId],
    queryFn: () => requestJson<TailoredResume[]>(`/api/jobs/${jobId}/tailored`),
    enabled: jobId !== null,
  })
}

export function useTailorJob() {
  const client = useQueryClient()
  return useMutation({
    // `force: true` is the explicit "Re-tailor": it creates a NEW version and keeps the old ones
    mutationFn: ({ jobId, base, force }: { jobId: number; base?: 'ai' | 'fullstack' | 'backend'; force?: boolean }) =>
      requestJson<TailoredResume>(`/api/jobs/${jobId}/tailor`, {
        method: 'POST',
        body: JSON.stringify({ ...(base ? { base } : {}), ...(force ? { force: true } : {}) }),
      }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['resumes'] })
      void client.invalidateQueries({ queryKey: ['tailored'] })
      void client.invalidateQueries({ queryKey: ['tailoring-status'] })
      void client.invalidateQueries({ queryKey: ['health'] })
    },
  })
}

export function useApproveTailored() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, approved }: { id: number; approved: boolean }) =>
      requestJson<TailoredResume>(`/api/tailored/${id}/approve`, { method: 'POST', body: JSON.stringify({ approved }) }),
    onSettled: () => void client.invalidateQueries({ queryKey: ['tailored'] }),
  })
}

export function useTailoringStatus() {
  return useQuery({
    queryKey: ['tailoring-status'],
    queryFn: () => requestJson<TailoringStatus>('/api/tailoring/status'),
    refetchInterval: 60_000,
  })
}

// --- Today ---------------------------------------------------------------------------------------

export function useToday() {
  return useQuery({
    queryKey: ['today'],
    queryFn: () => requestJson<TodayView>('/api/today'),
    staleTime: 0,
    refetchOnMount: 'always',
  })
}

export function useMarkSeen() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => requestJson<void>('/api/today/seen', { method: 'POST' }),
    onSettled: () => void client.invalidateQueries({ queryKey: ['today'] }),
  })
}

export function useMarkOpened() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) =>
      requestJson<{ job_id: number; opened_at: string }>(`/api/jobs/${jobId}/opened`, { method: 'POST', body: '{}' }),
    onSettled: () => void client.invalidateQueries({ queryKey: ['today'] }),
  })
}

// --- cover letters and answers -------------------------------------------------------------------

export function useWriting(jobId: number | null) {
  return useQuery({
    queryKey: ['writing', jobId],
    queryFn: () => requestJson<WritingText[]>(`/api/jobs/${jobId}/writing`),
    enabled: jobId !== null,
  })
}

function useInvalidateWriting() {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: ['writing'] })
    void client.invalidateQueries({ queryKey: ['health'] })
  }
}

export function useCoverLetter() {
  const invalidate = useInvalidateWriting()
  return useMutation({
    mutationFn: ({ jobId, regenerate }: { jobId: number; regenerate?: boolean }) =>
      requestJson<WritingText>(`/api/jobs/${jobId}/cover-letter`, {
        method: 'POST',
        body: JSON.stringify({ regenerate: Boolean(regenerate) }),
      }),
    onSettled: invalidate,
  })
}

export function useWritingEstimate(jobId: number, kind: 'cover_letter' | 'answer', enabled: boolean) {
  return useQuery({
    queryKey: ['writing-estimate', jobId, kind],
    queryFn: () => requestJson<WritingEstimate>(`/api/jobs/${jobId}/writing/estimate?kind=${kind}`),
    enabled,
    staleTime: 0,
    gcTime: 0,
  })
}

export function useSaveEdit() {
  const invalidate = useInvalidateWriting()
  return useMutation({
    mutationFn: ({ textId, text }: { textId: number; text: string }) =>
      requestJson<WritingText>(`/api/writing/${textId}/edit`, { method: 'POST', body: JSON.stringify({ text }) }),
    onSettled: invalidate,
  })
}

export function useUseVersion() {
  const invalidate = useInvalidateWriting()
  return useMutation({
    mutationFn: ({ textId }: { textId: number }) =>
      requestJson<WritingText>(`/api/writing/${textId}/use`, { method: 'POST', body: JSON.stringify({}) }),
    onSettled: invalidate,
  })
}

export function useKnockouts() {
  return useMutation({
    mutationFn: ({ jobId, questions }: { jobId: number; questions: string[] }) =>
      requestJson<KnockoutItem[]>(`/api/jobs/${jobId}/knockouts`, {
        method: 'POST',
        body: JSON.stringify({ questions }),
      }),
  })
}

export function useAnswer() {
  const invalidate = useInvalidateWriting()
  return useMutation({
    mutationFn: ({ jobId, question, regenerate }: { jobId: number; question: string; regenerate?: boolean }) =>
      requestJson<WritingText>(`/api/jobs/${jobId}/answer`, {
        method: 'POST',
        body: JSON.stringify({ question, regenerate: Boolean(regenerate) }),
      }),
    onSettled: invalidate,
  })
}

// --- Resumes page --------------------------------------------------------------------------------

export interface ResumeFilterState {
  company: string
  status: string
  base: string
  since: string
  includeSuperseded: boolean
  includeArchived: boolean
  jobId?: number
}

export function resumesQueryString(filters: Partial<ResumeFilterState>): string {
  const params = new URLSearchParams()
  if (filters.company) params.set('company', filters.company)
  if (filters.status) params.set('status', filters.status)
  if (filters.base) params.set('base', filters.base)
  if (filters.since) params.set('since', filters.since)
  if (filters.includeSuperseded) params.set('include_superseded', 'true')
  if (filters.includeArchived) params.set('include_archived', 'true')
  if (filters.jobId !== undefined) params.set('job_id', String(filters.jobId))
  return params.toString()
}

export function useResumes(filters: Partial<ResumeFilterState> = {}) {
  return useQuery({
    queryKey: ['resumes', filters],
    queryFn: () => requestJson<ResumeList>(`/api/resumes?${resumesQueryString(filters)}`),
    placeholderData: keepPreviousData,
  })
}

export function useResume(id: number | null) {
  return useQuery({
    queryKey: ['resume', id],
    queryFn: () => requestJson<ResumeDetail>(`/api/resumes/${id}`),
    enabled: id !== null,
  })
}

export function useSkillGaps(enabled: boolean) {
  return useQuery({
    queryKey: ['skill-gaps'],
    queryFn: () => requestJson<SkillGaps>('/api/skill-gaps?top=25'),
    enabled,
    staleTime: 600_000,
  })
}

/** Cost estimate for a tailor / re-tailor click: only fetched when the confirm box is opened. */
export function useTailorEstimate(jobId: number, enabled: boolean) {
  return useQuery({
    queryKey: ['tailor-estimate', jobId],
    queryFn: () => requestJson<TailorEstimate>(`/api/jobs/${jobId}/tailor-estimate`),
    enabled,
    staleTime: 0,
  })
}

function useInvalidateResumes() {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: ['resumes'] })
    void client.invalidateQueries({ queryKey: ['resume'] })
    void client.invalidateQueries({ queryKey: ['tailored'] })
    void client.invalidateQueries({ queryKey: ['board'] })
  }
}

export function useMarkResumeSent() {
  const invalidate = useInvalidateResumes()
  return useMutation({
    mutationFn: (id: number) => requestJson<{ application_id: number; sent_at: string }>(`/api/resumes/${id}/sent`, { method: 'POST' }),
    onSettled: invalidate,
  })
}

export function useArchiveResume() {
  const invalidate = useInvalidateResumes()
  return useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) =>
      requestJson<{ id: number; archived: boolean }>(`/api/resumes/${id}/archive`, {
        method: 'POST',
        body: JSON.stringify({ archived }),
      }),
    onSettled: invalidate,
  })
}

export function useApproveResume() {
  const invalidate = useInvalidateResumes()
  return useMutation({
    mutationFn: ({ id, approved }: { id: number; approved: boolean }) =>
      requestJson<TailoredResume>(`/api/tailored/${id}/approve`, { method: 'POST', body: JSON.stringify({ approved }) }),
    onSettled: invalidate,
  })
}

// --- decisions (Save / Applied / Not applying / Undo) and learning --------------------------------

function useInvalidateDecisions() {
  const client = useQueryClient()
  return () => {
    for (const key of ['jobs', 'job', 'today', 'board', 'resumes', 'resume', 'tailored', 'facets', 'learning']) {
      void client.invalidateQueries({ queryKey: [key] })
    }
  }
}

export interface AppliedInput {
  jobId: number
  applied_at?: string
  how: string
  resume_id?: number | null
  note?: string
}

export function useSaveDecision() {
  const invalidate = useInvalidateDecisions()
  return useMutation({
    mutationFn: (jobId: number) => requestJson<DecisionResult>(`/api/jobs/${jobId}/decision/save`, { method: 'POST' }),
    onSettled: invalidate,
  })
}

export function useAppliedDecision() {
  const invalidate = useInvalidateDecisions()
  return useMutation({
    mutationFn: ({ jobId, ...body }: AppliedInput) =>
      requestJson<DecisionResult>(`/api/jobs/${jobId}/decision/applied`, { method: 'POST', body: JSON.stringify(body) }),
    onSettled: invalidate,
  })
}

export function useNotApplying() {
  const invalidate = useInvalidateDecisions()
  return useMutation({
    mutationFn: ({ jobId, reason, note }: { jobId: number; reason: string; note?: string }) =>
      requestJson<DecisionResult>(`/api/jobs/${jobId}/decision/not-applying`, {
        method: 'POST',
        body: JSON.stringify({ reason, ...(note ? { note } : {}) }),
      }),
    onSettled: invalidate,
  })
}

export function useBulkNotApplying() {
  const invalidate = useInvalidateDecisions()
  return useMutation({
    mutationFn: ({ jobIds, reason, note }: { jobIds: number[]; reason: string; note?: string }) =>
      requestJson<DecisionResult[]>('/api/decisions/not-applying', {
        method: 'POST',
        body: JSON.stringify({ job_ids: jobIds, reason, ...(note ? { note } : {}) }),
      }),
    onSettled: invalidate,
  })
}

export function useUndoDecision() {
  const invalidate = useInvalidateDecisions()
  return useMutation({
    mutationFn: (jobId: number) => requestJson<DecisionResult>(`/api/jobs/${jobId}/decision/undo`, { method: 'POST' }),
    onSettled: invalidate,
  })
}

export function useLearning() {
  return useQuery({ queryKey: ['learning'], queryFn: () => requestJson<LearningView>('/api/learning') })
}

export function useLearningActions() {
  const client = useQueryClient()
  const done = () => void client.invalidateQueries({ queryKey: ['learning'] })
  return {
    approve: useMutation({
      mutationFn: (key: string) => requestJson<{ id: string }>('/api/learning/proposals/approve', { method: 'POST', body: JSON.stringify({ key }) }),
      onSettled: done,
    }),
    reject: useMutation({
      mutationFn: (key: string) => requestJson<{ key: string }>('/api/learning/proposals/reject', { method: 'POST', body: JSON.stringify({ key }) }),
      onSettled: done,
    }),
    remove: useMutation({
      mutationFn: (id: string) => requestJson<{ id: string }>(`/api/learning/rules/${id}`, { method: 'DELETE' }),
      onSettled: done,
    }),
  }
}

// --- freshness, still-open checks, company grade rules ----------------------------------------------------------------

export function useCheckOpen() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) => requestJson<Liveness & { job_id: number }>(`/api/jobs/${jobId}/check-open`, { method: 'POST' }),
    onSettled: () => {
      for (const key of ['jobs', 'job', 'facets', 'health']) void client.invalidateQueries({ queryKey: [key] })
    },
  })
}

export function useRestoreJob() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) => requestJson<{ job_id: number; restored: boolean }>(`/api/jobs/${jobId}/restore`, { method: 'POST' }),
    onSettled: () => {
      for (const key of ['jobs', 'job', 'facets', 'health', 'today']) void client.invalidateQueries({ queryKey: [key] })
    },
  })
}

export function useVerdictWrong() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (args: { jobId: number; label: string; reason?: string }) =>
      requestJson<{ job_id: number; human_label: string }>(`/api/jobs/${args.jobId}/llm-verdict-wrong`, {
        method: 'POST',
        body: JSON.stringify({ label: args.label, reason: args.reason || null }),
      }),
    onSettled: () => {
      for (const key of ['jobs', 'job', 'facets', 'health', 'today']) void client.invalidateQueries({ queryKey: [key] })
    },
  })
}

export function useUndoVerdictWrong() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) =>
      requestJson<{ job_id: number }>(`/api/jobs/${jobId}/llm-verdict-wrong`, { method: 'DELETE' }),
    onSettled: () => {
      for (const key of ['jobs', 'job', 'facets', 'health', 'today']) void client.invalidateQueries({ queryKey: [key] })
    },
  })
}

export function useCompanyRules() {
  return useQuery({ queryKey: ['company-rules'], queryFn: () => requestJson<CompanyRuleList>('/api/company-rules') })
}

export function useCompanyRuleActions() {
  const client = useQueryClient()
  const done = () => void client.invalidateQueries({ queryKey: ['company-rules'] })
  return {
    /** Shows the exact text that "Remember for this company" would write; writes nothing. */
    preview: useMutation({
      mutationFn: (input: { jobId: number; pattern?: string }) =>
        requestJson<CompanyRulePreview>('/api/company-rules/preview', {
          method: 'POST',
          body: JSON.stringify({ job_id: input.jobId, ...(input.pattern ? { pattern: input.pattern } : {}) }),
        }),
    }),
    /** Writes the previewed text; only ever called from the confirm button. */
    write: useMutation({
      mutationFn: (input: { jobId: number; pattern: string; text: string }) =>
        requestJson<CompanyRuleList>('/api/company-rules', {
          method: 'POST',
          body: JSON.stringify({ job_id: input.jobId, pattern: input.pattern, text: input.text, confirmed: true }),
        }),
      onSettled: done,
    }),
    remove: useMutation({
      mutationFn: (index: number) => requestJson<CompanyRuleList>(`/api/company-rules/${index}`, { method: 'DELETE' }),
      onSettled: done,
    }),
  }
}

export function useRunTriageNow() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => requestJson<Record<string, unknown>>('/api/health/triage-llm-run', { method: 'POST' }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: queryKeys.health })
      void client.invalidateQueries({ queryKey: ['jobs'] })
      void client.invalidateQueries({ queryKey: ['job'] })
    },
  })
}

export function useSetupState() {
  return useQuery({
    queryKey: ['setup-state'],
    queryFn: () => requestJson<SetupState>('/api/setup/state'),
    staleTime: 10_000,
  })
}

export function useTriageAudit() {
  return useQuery({
    queryKey: ['triage-audit'],
    queryFn: () => requestJson<AuditReviewState>('/api/triage/audit'),
  })
}

export function useLabelAuditItem() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: { jobId: number; myVerdict: string; myNote?: string }) =>
      requestJson<AuditReviewState>('/api/triage/audit/label', {
        method: 'POST',
        body: JSON.stringify({
          job_id: input.jobId,
          my_verdict: input.myVerdict,
          my_note: input.myNote || '',
        }),
      }),
    onSuccess: (data) => {
      client.setQueryData(['triage-audit'], data)
      void client.invalidateQueries({ queryKey: queryKeys.health })
    },
  })
}

export function useUndoAuditItem() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) =>
      requestJson<AuditReviewState>('/api/triage/audit/undo', {
        method: 'POST',
        body: JSON.stringify({ job_id: jobId }),
      }),
    onSuccess: (data) => {
      client.setQueryData(['triage-audit'], data)
      void client.invalidateQueries({ queryKey: queryKeys.health })
    },
  })
}



