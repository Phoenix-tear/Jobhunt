import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { requestJson } from '../api/client'
import type { SetupState, ReadinessChecklist } from '../api/types'

export interface WizardStep {
  id: string
  label: string
}

const WIZARD_STEPS: WizardStep[] = [
  { id: 'welcome', label: 'Welcome' },
  { id: 'gemini', label: 'Gemini Key' },
  { id: 'budget', label: 'Budget' },
  { id: 'resumes', label: 'Resumes' },
  { id: 'facts', label: 'Facts Review' },
  { id: 'profile', label: 'Target Profile' },
  { id: 'sources', label: 'Job Sources' },
  { id: 'schedule', label: 'Schedule' },
  { id: 'gmail', label: 'Gmail (Optional)' },
  { id: 'finish', label: 'Ready to Hunt' },
]

export function SetupWizardPage() {
  const navigate = useNavigate()
  const [currentStepIndex, setCurrentStepIndex] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Gemini state
  const [geminiKey, setGeminiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [keyTestStatus, setKeyTestStatus] = useState<{ testing: boolean; result?: any; error?: string }>({ testing: false })

  // Budget state (Free: 20/15/$1.00, Paid: 60/40/$5.00)
  const [budgetPreset, setBudgetPreset] = useState<'free' | 'paid' | 'custom'>('free')
  const [dailyCalls, setDailyCalls] = useState(20)
  const [interactiveCalls, setInteractiveCalls] = useState(15)
  const [monthlyUsd, setMonthlyUsd] = useState(1.0)

  // Resumes state
  const [uploadedBases, setUploadedBases] = useState<any[]>([])
  const [uploadingResume, setUploadingResume] = useState(false)
  const [uploadValidation, setUploadValidation] = useState<any | null>(null)
  const [buildProgress, setBuildProgress] = useState<any | null>(null)

  // Facts state
  const [facts, setFacts] = useState<any[]>([])
  const [factsHash, setFactsHash] = useState<string | null>(null)
  const [usedCount, setUsedCount] = useState<number>(0)
  const [unusedCount, setUnusedCount] = useState<number>(0)
  const [editingFactId, setEditingFactId] = useState<string | null>(null)
  const [editingFactText, setEditingFactText] = useState('')

  // Summary variants state
  const [suggestedSummaries, setSuggestedSummaries] = useState<any[]>([])
  const [suggestingSummaries, setSuggestingSummaries] = useState(false)
  const [summariesApproved, setSummariesApproved] = useState(false)

  // Profile state
  const [educationStage, setEducationStage] = useState('undergraduate')
  const [graduationYear, setGraduationYear] = useState('2026')
  const [selectedFamilies, setSelectedFamilies] = useState<string[]>(['fullstack', 'ai_ml'])
  const [familyPresets, setFamilyPresets] = useState<any[]>([])
  const [roleDescription, setRoleDescription] = useState('')
  const [suggestingRoles, setSuggestingRoles] = useState(false)
  const [showCustomRole, setShowCustomRole] = useState(false)
  const [customRole, setCustomRole] = useState({ id: '', label: '', keywords: '', title_patterns: '' })
  const [targetLocation, setTargetLocation] = useState('India, Remote')
  const [remotePreference, setRemotePreference] = useState('remote_or_hybrid')

  // Application answers state
  const [workAuth, setWorkAuth] = useState(true)
  const [sponsorship, setSponsorship] = useState(false)
  const [linkedinUrl, setLinkedinUrl] = useState('')
  const [githubUrl, setGithubUrl] = useState('')

  // Sources state & company discovery
  const [sourcesState, setSourcesState] = useState<Record<string, boolean>>({
    simplify: true,
    openroles: true,
    startup_jobs: true,
    ashby: true,
    greenhouse: true,
    lever: true,
    workable: true,
    smartrecruiters: true,
  })
  const [companyUrl, setCompanyUrl] = useState('')
  const [discoveringCompany, setDiscoveringCompany] = useState(false)
  const [discoveredCompany, setDiscoveredCompany] = useState<any | null>(null)

  // First ingest state & Ollama status
  const [ingesting, setIngesting] = useState(false)
  const [ingestProgress, setIngestProgress] = useState<any | null>(null)
  const [ingestResult, setIngestResult] = useState<any | null>(null)
  const [readiness, setReadiness] = useState<ReadinessChecklist | null>(null)
  const [ollamaOnline, setOllamaOnline] = useState<boolean>(false)

  useEffect(() => {
    // Fetch initial setup state & readiness
    requestJson<SetupState>('/api/setup/state')
      .then((state) => {
        const found = WIZARD_STEPS.findIndex((s) => s.id === state.step)
        if (found >= 0) {
          setCurrentStepIndex(found)
          if (state.step === 'facts') loadFacts()
          if (state.step === 'finish') loadReadiness()
        }
      })
      .catch(() => {})

    // Load family presets
    requestJson<{ presets: any[] }>('/api/profile/role-families/presets')
      .then((res) => setFamilyPresets(res.presets || []))
      .catch(() => {})

    // Check existing resumes
    requestJson<{ bases: any[] }>('/api/resumes/bases')
      .then((res) => setUploadedBases(res.bases || []))
      .catch(() => {})
  }, [])

  // Sync step changes with backend
  const goToStep = async (index: number) => {
    if (index < 0 || index >= WIZARD_STEPS.length) return
    setCurrentStepIndex(index)
    setError(null)
    const step = WIZARD_STEPS[index]
    if (!step) return
    const stepId = step.id
    try {
      await requestJson('/api/setup/step', {
        method: 'POST',
        body: JSON.stringify({ step: stepId }),
      })
    } catch {
      // non-blocking
    }

    if (stepId === 'facts') {
      loadFacts()
    } else if (stepId === 'finish') {
      loadReadiness()
    }
  }

  const loadFacts = async () => {
    try {
      const res = await requestJson<{ facts: any[]; facts_hash?: string; used_count?: number; unused_count?: number }>('/api/facts')
      setFacts(res.facts || [])
      if (res.facts_hash) setFactsHash(res.facts_hash)
      if (res.used_count !== undefined) setUsedCount(res.used_count)
      if (res.unused_count !== undefined) setUnusedCount(res.unused_count)
    } catch {
      setFacts([])
    }
  }

  const loadReadiness = async () => {
    try {
      const res = await requestJson<any>('/api/health')
      if (res.readiness) setReadiness(res.readiness)
      if (res.ollama_online !== undefined) setOllamaOnline(Boolean(res.ollama_online))
    } catch {
      // ignore
    }
  }

  // Option B: Enter Demo Mode
  const handleEnterDemo = async () => {
    setLoading(true)
    setError(null)
    try {
      await requestJson('/api/setup/demo/enter', { method: 'POST' })
      navigate('/today')
    } catch (err: any) {
      setError(err.message || 'Failed to enter demo mode')
      setLoading(false)
    }
  }

  // Gemini key test & save
  const handleTestKey = async () => {
    setKeyTestStatus({ testing: true })
    try {
      const res = await requestJson<any>('/api/settings/test-gemini-key', {
        method: 'POST',
        body: JSON.stringify({ key: geminiKey }),
      })
      setKeyTestStatus({ testing: false, result: res })
    } catch (err: any) {
      setKeyTestStatus({ testing: false, error: err.message || 'API key test failed' })
    }
  }

  const handleSaveGemini = async () => {
    if (!geminiKey.trim()) {
      goToStep(currentStepIndex + 1)
      return
    }
    setLoading(true)
    try {
      await requestJson('/api/settings/gemini-key', {
        method: 'POST',
        body: JSON.stringify({ key: geminiKey }),
      })
      goToStep(currentStepIndex + 1)
    } catch (err: any) {
      setError(err.message || 'Failed to save Gemini key')
    } finally {
      setLoading(false)
    }
  }

  // Budget preset saving
  const handleSaveBudget = async () => {
    setLoading(true)
    try {
      await requestJson('/api/settings/budget', {
        method: 'POST',
        body: JSON.stringify({
          preset: budgetPreset,
          daily_pipeline_calls: dailyCalls,
          daily_interactive_calls: interactiveCalls,
          monthly_budget_usd: monthlyUsd,
        }),
      })
      goToStep(currentStepIndex + 1)
    } catch (err: any) {
      setError(err.message || 'Failed to save budget settings')
    } finally {
      setLoading(false)
    }
  }

  // Resume upload handling
  const handleResumeUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (!file.name.endsWith('.tex')) {
      setError('Only .tex LaTeX files are supported')
      return
    }
    setUploadingResume(true)
    setError(null)
    setBuildProgress(null)
    const formData = new FormData()
    formData.append('file', file)
    formData.append('base_id', file.name.replace(/\.tex$/i, ''))

    try {
      const res = await requestJson<any>('/api/resumes/upload', {
        method: 'POST',
        body: formData,
      })
      setUploadValidation(res.validation)
      if (res.job_id) {
        const timer = setInterval(async () => {
          try {
            const p = await requestJson<any>(`/api/resumes/build-progress?job_id=${res.job_id}`)
            setBuildProgress(p)
            if (p.status === 'completed' || p.status === 'failed') {
              clearInterval(timer)
            }
          } catch {
            clearInterval(timer)
          }
        }, 800)
      }
      // refresh bases
      const basesRes = await requestJson<{ bases: any[] }>('/api/resumes/bases')
      setUploadedBases(basesRes.bases || [])
    } catch (err: any) {
      setError(err.message || 'Upload failed')
    } finally {
      setUploadingResume(false)
    }
  }

  // Facts tick / edit
  const handleToggleFact = async (id: string, currentTicked: boolean) => {
    try {
      await requestJson('/api/facts/tick', {
        method: 'POST',
        body: JSON.stringify({ fact_ids: [id], ticked: !currentTicked }),
      })
      setFacts((prev) => {
        const next = prev.map((f) => (f.id === id ? { ...f, ticked: !currentTicked } : f))
        const used = next.filter((f) => f.ticked).length
        setUsedCount(used)
        setUnusedCount(next.length - used)
        return next
      })
    } catch (err: any) {
      setError(err.message || 'Failed to update fact')
    }
  }

  const handleBulkToggleFacts = async (ticked: boolean) => {
    try {
      await requestJson('/api/facts/bulk-tick', {
        method: 'POST',
        body: JSON.stringify({ ticked }),
      })
      setFacts((prev) => {
        const next = prev.map((f) => ({ ...f, ticked }))
        setUsedCount(ticked ? next.length : 0)
        setUnusedCount(ticked ? 0 : next.length)
        return next
      })
    } catch (err: any) {
      setError(err.message || 'Failed to bulk-update facts')
    }
  }

  const handleSaveFactEdit = async (id: string) => {
    if (!editingFactText.trim()) return
    try {
      const res = await requestJson<any>('/api/facts/edit', {
        method: 'POST',
        body: JSON.stringify({ fact_id: id, text: editingFactText }),
      })
      setFacts((prev) =>
        prev.map((f) =>
          f.id === id ? { ...f, id: res.new_id, text: res.text, edited: true } : f
        )
      )
      setEditingFactId(null)
    } catch (err: any) {
      setError(err.message || 'Failed to save edit')
    }
  }

  // Summary variants handlers
  const handleSuggestSummaries = async () => {
    setSuggestingSummaries(true)
    setError(null)
    try {
      const res = await requestJson<{ variants: any[] }>('/api/profile/suggest-summaries', {
        method: 'POST',
        body: JSON.stringify({ base: 'ai' }),
      })
      setSuggestedSummaries(res.variants || [])
    } catch (err: any) {
      setError(err.message || 'Failed to suggest summaries: review and tick facts first')
    } finally {
      setSuggestingSummaries(false)
    }
  }

  const handleApproveSummaries = async () => {
    try {
      await requestJson('/api/profile/summaries', {
        method: 'POST',
        body: JSON.stringify({ variants: suggestedSummaries }),
      })
      setSummariesApproved(true)
    } catch (err: any) {
      setError(err.message || 'Failed to save summaries')
    }
  }

  // Role family suggestion & custom family
  const handleSuggestRoleFamilies = async () => {
    if (!roleDescription.trim()) return
    setSuggestingRoles(true)
    setError(null)
    try {
      const res = await requestJson<{ role_families: any[] }>('/api/profile/suggest-role-families', {
        method: 'POST',
        body: JSON.stringify({ description: roleDescription }),
      })
      const newFamilies = res.role_families || []
      if (newFamilies.length > 0) {
        setFamilyPresets((prev) => {
          const ids = new Set(prev.map((p) => p.id))
          const toAdd = newFamilies.filter((f) => !ids.has(f.id))
          return [...prev, ...toAdd]
        })
        setSelectedFamilies((prev) => Array.from(new Set([...prev, ...newFamilies.map((f) => f.id)])))
      }
    } catch (err: any) {
      setError(err.message || 'Failed to suggest role families')
    } finally {
      setSuggestingRoles(false)
    }
  }

  const handleAddCustomRole = () => {
    if (!customRole.id.trim() || !customRole.label.trim()) return
    const id = customRole.id.toLowerCase().replace(/[^a-z0-9_]/g, '_')
    const newEntry = {
      id,
      label: customRole.label.trim(),
      keywords: customRole.keywords.split(',').map((k) => k.trim()).filter(Boolean),
      title_patterns: customRole.title_patterns.split(',').map((p) => p.trim()).filter(Boolean),
      recipe: 'fullstack',
    }
    setFamilyPresets((prev) => [...prev, newEntry])
    setSelectedFamilies((prev) => [...prev, id])
    setShowCustomRole(false)
    setCustomRole({ id: '', label: '', keywords: '', title_patterns: '' })
  }

  // Company ATS discovery handler
  const handleDiscoverCompany = async () => {
    if (!companyUrl.trim()) return
    setDiscoveringCompany(true)
    setError(null)
    try {
      const res = await requestJson<any>('/api/settings/discover-company', {
        method: 'POST',
        body: JSON.stringify({ url: companyUrl }),
      })
      setDiscoveredCompany(res)
    } catch (err: any) {
      setError(err.message || 'Could not discover company ATS')
    } finally {
      setDiscoveringCompany(false)
    }
  }

  // Profile save
  const handleSaveProfile = async () => {
    setLoading(true)
    try {
      await requestJson('/api/profile/answers', {
        method: 'PUT',
        body: JSON.stringify({
          answers: {
            work_authorization: workAuth ? 'authorized' : 'not_authorized',
            sponsorship: sponsorship ? 'required' : 'not_required',
            linkedin_url: linkedinUrl,
            github_url: githubUrl,
          },
        }),
      })
      goToStep(currentStepIndex + 1)
    } catch (err: any) {
      setError(err.message || 'Failed to save profile')
    } finally {
      setLoading(false)
    }
  }

  // Run first ingest with polling
  const handleRunFirstIngest = async () => {
    setIngesting(true)
    setError(null)
    setIngestResult(null)
    try {
      const res = await requestJson<any>('/api/setup/first-ingest', { method: 'POST' })
      setIngestProgress(res)
      const timer = setInterval(async () => {
        try {
          const p = await requestJson<any>('/api/setup/ingest-progress')
          setIngestProgress(p)
          if (p.status === 'completed' || p.status === 'failed') {
            clearInterval(timer)
            setIngesting(false)
            setIngestResult(p)
            loadReadiness()
          }
        } catch {
          clearInterval(timer)
          setIngesting(false)
        }
      }, 800)
    } catch (err: any) {
      setError(err.message || 'Ingest encountered an error')
      setIngesting(false)
    }
  }

  const handleCancelFirstIngest = async () => {
    try {
      await requestJson('/api/setup/ingest-cancel', { method: 'POST' })
      setIngesting(false)
      setIngestProgress((prev: any) => prev ? { ...prev, status: 'cancelled', message: 'Cancelled by user' } : null)
    } catch {
      // ignore
    }
  }

  // Finish setup
  const handleCompleteSetup = async () => {
    setLoading(true)
    try {
      await requestJson('/api/setup/complete', { method: 'POST' })
      navigate('/today')
    } catch (err: any) {
      setError(err.message || 'Failed to mark setup complete')
      setLoading(false)
    }
  }

  const currentStep: WizardStep = WIZARD_STEPS[currentStepIndex] ?? WIZARD_STEPS[0]!

  return (
    <div className="min-h-screen bg-stone-50 dark:bg-stone-950 text-stone-900 dark:text-stone-100 flex flex-col justify-between">
      <div className="max-w-4xl mx-auto w-full px-4 py-8">
        {/* Header / Brand */}
        <div className="flex items-center justify-between border-b border-stone-200 dark:border-stone-800 pb-4 mb-6">
          <div className="flex items-center gap-3">
            <span className="grid size-9 place-items-center rounded-xl bg-brand-700 text-base font-bold text-white shadow-sm">
              j
            </span>
            <div>
              <h1 className="text-xl font-bold tracking-tight">jobhunt setup wizard</h1>
              <p className="text-xs text-stone-500">Configure your local pipeline from your browser</p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleEnterDemo}
            disabled={loading}
            className="rounded-md border border-amber-500/60 bg-amber-50 dark:bg-amber-950/40 px-3 py-1.5 text-xs font-semibold text-amber-900 dark:text-amber-200 hover:bg-amber-100 dark:hover:bg-amber-900/40 transition-colors cursor-pointer"
          >
            🎮 Try Demo Mode Instead
          </button>
        </div>

        {/* Progress Bar & Steps Indicator */}
        <div className="mb-8">
          <div className="flex items-center justify-between text-xs font-medium text-stone-500 mb-2">
            <span>Step {currentStepIndex + 1} of {WIZARD_STEPS.length}: {currentStep.label}</span>
            <span>{Math.round(((currentStepIndex + 1) / WIZARD_STEPS.length) * 100)}% Complete</span>
          </div>
          <div className="w-full bg-stone-200 dark:bg-stone-800 h-2 rounded-full overflow-hidden">
            <div
              className="bg-brand-600 h-full transition-all duration-300"
              style={{ width: `${((currentStepIndex + 1) / WIZARD_STEPS.length) * 100}%` }}
            />
          </div>
          <div className="hidden sm:flex justify-between items-center mt-3 overflow-x-auto text-[11px] gap-1">
            {WIZARD_STEPS.map((s, idx) => (
              <button
                key={s.id}
                type="button"
                onClick={() => goToStep(idx)}
                className={`px-1.5 py-0.5 rounded transition-colors whitespace-nowrap ${
                  idx === currentStepIndex
                    ? 'font-bold text-brand-600 dark:text-brand-400 bg-brand-50 dark:bg-brand-950/60'
                    : idx < currentStepIndex
                    ? 'text-stone-700 dark:text-stone-300 hover:text-stone-900'
                    : 'text-stone-400'
                }`}
              >
                {idx + 1}. {s.label}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div role="alert" className="mb-6 rounded-lg border border-rose-300 bg-rose-50 p-4 text-sm text-rose-900 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
            {error}
          </div>
        )}

        {/* Step 0: Welcome */}
        {currentStep.id === 'welcome' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-2xl font-bold">Welcome to jobhunt</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400 leading-relaxed">
              jobhunt is your private, automated, locally hosted job searching and resume tailoring copilot.
              Everything runs locally on your machine—your API keys, tailored resumes, and personal facts never leave your computer.
            </p>

            <div className="grid sm:grid-cols-2 gap-4 mt-6">
              <div
                onClick={() => goToStep(1)}
                className="p-5 rounded-xl border-2 border-brand-500 bg-brand-50/40 dark:bg-brand-950/20 hover:border-brand-600 transition-all cursor-pointer flex flex-col justify-between"
              >
                <div>
                  <div className="size-8 rounded-lg bg-brand-600 text-white font-bold grid place-items-center mb-3">1</div>
                  <h3 className="font-bold text-base text-brand-950 dark:text-brand-100">Set Up My Own Job Hunt</h3>
                  <p className="text-xs text-stone-600 dark:text-stone-400 mt-2 leading-relaxed">
                    Configure your Gemini API key, upload your LaTeX resume, review your verified facts, and pick your target role families.
                  </p>
                </div>
                <span className="mt-4 text-xs font-semibold text-brand-700 dark:text-brand-300">Start wizard →</span>
              </div>

              <div
                onClick={handleEnterDemo}
                className="p-5 rounded-xl border border-stone-300 dark:border-stone-700 bg-stone-50 dark:bg-stone-900/60 hover:border-amber-500 transition-all cursor-pointer flex flex-col justify-between"
              >
                <div>
                  <div className="size-8 rounded-lg bg-amber-500 text-stone-950 font-bold grid place-items-center mb-3">2</div>
                  <h3 className="font-bold text-base text-stone-900 dark:text-stone-100">Try Demo Mode (Alex Chen)</h3>
                  <p className="text-xs text-stone-600 dark:text-stone-400 mt-2 leading-relaxed">
                    Instant sandbox with pre-seeded jobs, sample systems & data engineering resumes, and a mock deterministic LLM client. Zero setup required ($0 spend).
                  </p>
                </div>
                <span className="mt-4 text-xs font-semibold text-amber-600 dark:text-amber-400">Launch sandbox →</span>
              </div>
            </div>
          </div>
        )}

        {/* Step 1: Gemini API Key */}
        {currentStep.id === 'gemini' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">1. Gemini API Key & Models</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Your key is saved in <code>.secrets/gemini_api_key</code> with owner-only filesystem permissions. It is never written to git, database, logs, or browser storage.
            </p>

            <div className="space-y-3">
              <label className="block text-xs font-semibold uppercase tracking-wider text-stone-500">
                Gemini API Key
              </label>
              <div className="flex gap-2">
                <input
                  type={showKey ? 'text' : 'password'}
                  value={geminiKey}
                  onChange={(e) => setGeminiKey(e.target.value)}
                  placeholder="AIzaSy..."
                  className="flex-1 rounded-md border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-950 px-3 py-2 text-sm font-mono focus:border-brand-500 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={() => setShowKey(!showKey)}
                  className="px-3 py-2 text-xs border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
                >
                  {showKey ? 'Hide' : 'Show'}
                </button>
                <button
                  type="button"
                  onClick={handleTestKey}
                  disabled={keyTestStatus.testing || !geminiKey.trim()}
                  className="px-4 py-2 text-xs font-semibold rounded-md bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900 hover:bg-stone-800 disabled:opacity-50 cursor-pointer"
                >
                  {keyTestStatus.testing ? 'Testing…' : 'Test Key'}
                </button>
              </div>

              <div className="flex justify-between items-center text-xs text-stone-500">
                <span>Don't have a key yet?</span>
                <a
                  href="https://aistudio.google.com/app/apikey"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-600 dark:text-brand-400 hover:underline font-medium"
                >
                  Get a free key from Google AI Studio →
                </a>
              </div>

              {keyTestStatus.result && (
                <div className="p-3 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-300 dark:border-emerald-800 rounded-md text-xs text-emerald-900 dark:text-emerald-100">
                  ✓ Key valid! Available models: {keyTestStatus.result.models?.slice(0, 4).join(', ')}…
                </div>
              )}
              {keyTestStatus.error && (
                <div className="p-3 bg-rose-50 dark:bg-rose-950/40 border border-rose-300 dark:border-rose-800 rounded-md text-xs text-rose-900 dark:text-rose-100">
                  ✕ Key test failed: {keyTestStatus.error}
                </div>
              )}
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(0)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={handleSaveGemini}
                disabled={loading}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Save & Continue →
              </button>
            </div>
          </div>
        )}

        {/* Step 2: Budget Presets */}
        {currentStep.id === 'budget' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">2. Monthly Budget & Rate Limits</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Jobhunt includes hard budget ceilings and daily rate-limits to guarantee you never incur surprise API charges.
            </p>

            <div className="grid sm:grid-cols-3 gap-4">
              <div
                onClick={() => {
                  setBudgetPreset('free')
                  setDailyCalls(20)
                  setInteractiveCalls(15)
                  setMonthlyUsd(1.0)
                }}
                className={`p-4 rounded-xl border-2 transition-all cursor-pointer ${
                  budgetPreset === 'free'
                    ? 'border-brand-600 bg-brand-50/50 dark:bg-brand-950/30'
                    : 'border-stone-200 dark:border-stone-800 hover:border-stone-400'
                }`}
              >
                <h3 className="font-bold text-sm">Free Tier Only</h3>
                <p className="text-xl font-bold text-brand-600 dark:text-brand-400 mt-1">$1.00 / mo</p>
                <ul className="text-xs text-stone-500 space-y-1 mt-3">
                  <li>• 20 daily pipeline calls</li>
                  <li>• 15 interactive clicks/day</li>
                  <li>• Zero credit card required</li>
                </ul>
              </div>

              <div
                onClick={() => {
                  setBudgetPreset('paid')
                  setDailyCalls(60)
                  setInteractiveCalls(40)
                  setMonthlyUsd(5.0)
                }}
                className={`p-4 rounded-xl border-2 transition-all cursor-pointer ${
                  budgetPreset === 'paid'
                    ? 'border-brand-600 bg-brand-50/50 dark:bg-brand-950/30'
                    : 'border-stone-200 dark:border-stone-800 hover:border-stone-400'
                }`}
              >
                <h3 className="font-bold text-sm">Paid Starter</h3>
                <p className="text-xl font-bold text-brand-600 dark:text-brand-400 mt-1">$5.00 / mo</p>
                <ul className="text-xs text-stone-500 space-y-1 mt-3">
                  <li>• 60 daily pipeline calls</li>
                  <li>• 40 interactive clicks/day</li>
                  <li>• Uses faster Gemini 2.5</li>
                </ul>
              </div>

              <div
                onClick={() => setBudgetPreset('custom')}
                className={`p-4 rounded-xl border-2 transition-all cursor-pointer ${
                  budgetPreset === 'custom'
                    ? 'border-brand-600 bg-brand-50/50 dark:bg-brand-950/30'
                    : 'border-stone-200 dark:border-stone-800 hover:border-stone-400'
                }`}
              >
                <h3 className="font-bold text-sm">Custom</h3>
                <p className="text-xl font-bold text-stone-600 dark:text-stone-400 mt-1">Manual limits</p>
                <ul className="text-xs text-stone-500 space-y-1 mt-3">
                  <li>• Set your own daily calls</li>
                  <li>• Set monthly spend cap</li>
                  <li>• Total rate control</li>
                </ul>
              </div>
            </div>

            {budgetPreset === 'custom' && (
              <div className="grid grid-cols-3 gap-3 p-4 bg-stone-50 dark:bg-stone-950 rounded-lg border border-stone-200 dark:border-stone-800">
                <div>
                  <label className="text-xs text-stone-500">Pipeline Calls / Day</label>
                  <input
                    type="number"
                    value={dailyCalls}
                    onChange={(e) => setDailyCalls(Number(e.target.value))}
                    className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-900 text-sm"
                  />
                </div>
                <div>
                  <label className="text-xs text-stone-500">Interactive Calls / Day</label>
                  <input
                    type="number"
                    value={interactiveCalls}
                    onChange={(e) => setInteractiveCalls(Number(e.target.value))}
                    className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-900 text-sm"
                  />
                </div>
                <div>
                  <label className="text-xs text-stone-500">Monthly Cap (USD)</label>
                  <input
                    type="number"
                    step="0.5"
                    value={monthlyUsd}
                    onChange={(e) => setMonthlyUsd(Number(e.target.value))}
                    className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-900 text-sm"
                  />
                </div>
              </div>
            )}

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(1)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={handleSaveBudget}
                disabled={loading}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Save & Continue →
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Base Resumes Upload */}
        {currentStep.id === 'resumes' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">3. Upload Base Resume (.tex)</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Upload your LaTeX master resume. We compile it offline in an untrusted sandbox, extract your experience/project bullets into facts, and check formatting.
            </p>

            <div className="border-2 border-dashed border-stone-300 dark:border-stone-700 rounded-xl p-8 text-center bg-stone-50/50 dark:bg-stone-950/40">
              <input
                type="file"
                accept=".tex"
                id="resume-upload"
                onChange={handleResumeUpload}
                disabled={uploadingResume}
                className="hidden"
              />
              <label
                htmlFor="resume-upload"
                className="inline-block px-5 py-2.5 bg-brand-600 text-white rounded-lg font-semibold text-xs hover:bg-brand-700 cursor-pointer transition-colors shadow-sm"
              >
                {uploadingResume ? 'Validating & Compiling…' : 'Choose .tex Resume File'}
              </label>
              <p className="text-xs text-stone-400 mt-2">Max 2 MB. Supports Custom macros, Jake's Resume, Awesome-CV, Moderncv, or Plain Itemize.</p>
            </div>

            {buildProgress && (
              <div className="p-3 bg-brand-50 dark:bg-brand-950/40 border border-brand-200 dark:border-brand-800 rounded-lg text-xs space-y-1">
                <div className="flex justify-between font-semibold text-brand-900 dark:text-brand-200">
                  <span>{buildProgress.message}</span>
                  <span>{buildProgress.progress}%</span>
                </div>
                <div className="w-full bg-brand-200 dark:bg-brand-900 h-1.5 rounded-full overflow-hidden">
                  <div className="bg-brand-600 h-full transition-all" style={{ width: `${buildProgress.progress}%` }} />
                </div>
                {buildProgress.facts_count > 0 && (
                  <span className="text-[11px] text-brand-700 dark:text-brand-300">Extracted {buildProgress.facts_count} facts from resume.</span>
                )}
              </div>
            )}

            {uploadValidation && (
              <div className="p-4 bg-stone-50 dark:bg-stone-950 border border-stone-200 dark:border-stone-800 rounded-lg text-xs space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-200 dark:border-stone-800 pb-2">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-stone-700 dark:text-stone-300">Template:</span>
                    <span className="font-bold text-stone-900 dark:text-stone-100">{uploadValidation.template || 'unknown'}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {uploadValidation.can_tailor ? (
                      <span className="px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200 font-semibold">
                        ✓ Full Tailoring Supported
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 rounded bg-stone-200 text-stone-700 dark:bg-stone-800 dark:text-stone-300 font-semibold">
                        Facts & Letters Only
                      </span>
                    )}
                    <span className="px-2 py-0.5 rounded bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300">
                      {uploadValidation.compile?.success ? '✓ 1-Page Compiled' : 'Compile Warning'}
                    </span>
                  </div>
                </div>

                {uploadValidation.reasons && uploadValidation.reasons.length > 0 && (
                  <div className="text-stone-600 dark:text-stone-400 space-y-0.5">
                    {uploadValidation.reasons.map((r: string, idx: number) => (
                      <p key={idx}>• {r}</p>
                    ))}
                  </div>
                )}

                {uploadValidation.sections_recognized && (
                  <div className="flex gap-2 flex-wrap">
                    {Object.entries(uploadValidation.sections_recognized).map(([sec, count]: any) => (
                      <span key={sec} className="bg-stone-200 dark:bg-stone-800 px-2 py-0.5 rounded text-[11px]">
                        {sec}: {String(count)}
                      </span>
                    ))}
                  </div>
                )}

                {uploadValidation.warnings && uploadValidation.warnings.length > 0 && (
                  <div className="mt-2 text-amber-700 dark:text-amber-400 space-y-1 border-t border-amber-200 dark:border-amber-900/40 pt-2">
                    <p className="font-bold">Template Warnings & Hints:</p>
                    {uploadValidation.warnings.map((w: any, idx: number) => (
                      <p key={idx}>
                        • Line {w.line || '–'}: {w.message} {w.hint ? `(Hint: ${w.hint})` : ''}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            )}

            {uploadedBases.length > 0 && (
              <div>
                <h4 className="text-xs font-bold uppercase tracking-wider text-stone-500 mb-2">Available Base Resumes</h4>
                <div className="space-y-1.5">
                  {uploadedBases.map((b) => (
                    <div key={b.id} className="p-2.5 rounded border border-stone-200 dark:border-stone-800 text-xs">
                      <div className="flex justify-between items-center">
                        <div>
                          <span className="font-mono font-medium mr-2">{b.id}.tex</span>
                          {b.template?.template && (
                            <span className="text-stone-500 text-[11px]">({b.template.template})</span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          {b.template?.can_tailor ? (
                            <span className="px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200 font-semibold text-[11px]">
                              Full Tailoring
                            </span>
                          ) : (
                            <span className="px-2 py-0.5 rounded bg-stone-200 text-stone-700 dark:bg-stone-800 dark:text-stone-300 font-semibold text-[11px]">
                              Facts & Letters Only
                            </span>
                          )}
                          <span className="text-stone-500 font-medium">{b.pages ? `${b.pages}p` : 'Active'}</span>
                        </div>
                      </div>
                      {b.template?.reasons && b.template.reasons.length > 0 && (
                        <div className="mt-1 text-[11px] text-stone-500 space-y-0.5">
                          {b.template.reasons.map((r: string, idx: number) => (
                            <p key={idx}>• {r}</p>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(2)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={() => goToStep(4)}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Continue to Facts Review →
              </button>
            </div>
          </div>
        )}

        {/* Step 4: Facts Review */}
        {currentStep.id === 'facts' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <div className="flex justify-between items-start">
              <div>
                <h2 className="text-xl font-bold">4. Resume Facts Review</h2>
                <p className="text-sm text-stone-600 dark:text-stone-400 mt-1">
                  Only ticked facts can be selected during automatic resume tailoring. Edit any bullet in-place to refine it.
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => handleBulkToggleFacts(true)}
                  className="px-2.5 py-1 text-xs border border-stone-300 dark:border-stone-700 rounded hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
                >
                  Tick All
                </button>
                <button
                  type="button"
                  onClick={() => handleBulkToggleFacts(false)}
                  className="px-2.5 py-1 text-xs border border-stone-300 dark:border-stone-700 rounded hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
                >
                  Untick All
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between text-xs text-stone-600 dark:text-stone-300 bg-stone-100 dark:bg-stone-800/60 p-2.5 rounded-lg">
              <div className="flex gap-4">
                <span className="font-semibold text-emerald-700 dark:text-emerald-400">✓ {usedCount} Ticked (Used)</span>
                <span>• {unusedCount} Unticked (Excluded)</span>
                <span>• {facts.length} Total Facts</span>
              </div>
              {factsHash && (
                <span className="font-mono text-[11px] text-stone-500">Hash: {factsHash.slice(0, 16)}…</span>
              )}
            </div>

            <div className="max-h-96 overflow-y-auto space-y-2 border border-stone-200 dark:border-stone-800 p-3 rounded-lg bg-stone-50/50 dark:bg-stone-950/40">
              {facts.length === 0 ? (
                <p className="text-xs text-stone-500 py-4 text-center">No facts found yet. Upload a base resume in the previous step.</p>
              ) : (
                facts.map((fact) => (
                  <div key={fact.id} className="p-2.5 bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded flex items-start gap-3 text-xs">
                    <input
                      type="checkbox"
                      checked={fact.ticked}
                      onChange={() => handleToggleFact(fact.id, fact.ticked)}
                      className="mt-1 size-4 rounded text-brand-600 cursor-pointer"
                    />
                    <div className="flex-1">
                      {editingFactId === fact.id ? (
                        <div className="space-y-2">
                          <textarea
                            value={editingFactText}
                            onChange={(e) => setEditingFactText(e.target.value)}
                            className="w-full p-2 border border-stone-300 dark:border-stone-700 rounded text-xs bg-white dark:bg-stone-950"
                            rows={3}
                          />
                          <div className="flex gap-2">
                            <button
                              type="button"
                              onClick={() => handleSaveFactEdit(fact.id)}
                              className="px-3 py-1 bg-brand-600 text-white rounded text-[11px] font-semibold"
                            >
                              Save Edit
                            </button>
                            <button
                              type="button"
                              onClick={() => setEditingFactId(null)}
                              className="px-2 py-1 text-stone-500 text-[11px]"
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="flex justify-between items-start">
                          <div>
                            <span className="font-mono text-[10px] bg-stone-100 dark:bg-stone-800 px-1 py-0.5 rounded text-stone-500 mr-1.5">
                              {fact.id}
                            </span>
                            <span className="text-stone-800 dark:text-stone-200">{fact.text}</span>
                            {fact.edited && (
                              <span className="ml-2 text-[10px] text-brand-600 dark:text-brand-400 font-semibold">
                                (edited by you)
                              </span>
                            )}
                          </div>
                          <button
                            type="button"
                            onClick={() => {
                              setEditingFactId(fact.id)
                              setEditingFactText(fact.text)
                            }}
                            className="text-stone-400 hover:text-stone-700 dark:hover:text-stone-200 text-xs ml-2 cursor-pointer"
                            title="Edit fact"
                          >
                            ✎
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>

            {/* Summary Variants Proposal Section */}
            <div className="border-t border-stone-200 dark:border-stone-800 pt-4 space-y-3">
              <div className="flex flex-wrap justify-between items-center gap-2">
                <div>
                  <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500">Summary Variants Proposal</h3>
                  <p className="text-xs text-stone-500">Generate 4-6 concise summary variants strictly supported by your ticked facts</p>
                </div>
                <button
                  type="button"
                  onClick={handleSuggestSummaries}
                  disabled={suggestingSummaries || usedCount === 0}
                  className="px-3 py-1.5 rounded bg-brand-600 text-white text-xs font-semibold hover:bg-brand-700 disabled:opacity-50 cursor-pointer"
                >
                  {suggestingSummaries ? 'Generating…' : 'Suggest Summary Variants ($0.005)'}
                </button>
              </div>

              {suggestedSummaries.length > 0 && (
                <div className="space-y-2 p-3 bg-stone-50 dark:bg-stone-950 rounded-lg border border-stone-200 dark:border-stone-800 text-xs">
                  {suggestedSummaries.map((v, idx) => (
                    <div key={v.id || idx} className="p-2 bg-white dark:bg-stone-900 rounded border border-stone-200 dark:border-stone-800 space-y-1">
                      <div className="flex justify-between items-center">
                        <span className="font-bold text-stone-800 dark:text-stone-200">{v.id}: {v.title}</span>
                      </div>
                      <textarea
                        value={v.text}
                        onChange={(e) => {
                          const next = [...suggestedSummaries]
                          next[idx] = { ...v, text: e.target.value }
                          setSuggestedSummaries(next)
                        }}
                        rows={2}
                        className="w-full p-1.5 border border-stone-200 dark:border-stone-700 rounded bg-white dark:bg-stone-950 text-xs"
                      />
                    </div>
                  ))}
                  <div className="flex justify-end pt-2">
                    <button
                      type="button"
                      onClick={handleApproveSummaries}
                      className="px-3 py-1.5 rounded bg-emerald-600 text-white font-semibold text-xs hover:bg-emerald-700 cursor-pointer"
                    >
                      {summariesApproved ? '✓ Summaries Saved to Profile' : 'Approve & Save Summary Variants'}
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(3)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={() => goToStep(5)}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Continue to Profile →
              </button>
            </div>
          </div>
        )}

        {/* Step 5: Profile & Answers */}
        {currentStep.id === 'profile' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">5. Target Profile & Roles</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Configure your degree, target role families, and standard job application answers.
            </p>

            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-semibold text-stone-500 uppercase">Degree Stage</label>
                <select
                  value={educationStage}
                  onChange={(e) => setEducationStage(e.target.value)}
                  className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                >
                  <option value="undergraduate">Undergraduate (B.S. / B.Tech)</option>
                  <option value="masters">Master's (M.S. / M.Tech)</option>
                  <option value="phd">PhD</option>
                  <option value="experienced">Experienced Professional</option>
                </select>
              </div>

              <div>
                <label className="text-xs font-semibold text-stone-500 uppercase">Expected Graduation Year</label>
                <input
                  type="text"
                  value={graduationYear}
                  onChange={(e) => setGraduationYear(e.target.value)}
                  className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                />
              </div>
            </div>

            <div>
              <label className="text-xs font-semibold text-stone-500 uppercase mb-2 block">
                Target Role Families
              </label>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {familyPresets.map((p) => {
                  const selected = selectedFamilies.includes(p.id)
                  return (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() =>
                        setSelectedFamilies(
                          selected
                            ? selectedFamilies.filter((x) => x !== p.id)
                            : [...selectedFamilies, p.id]
                        )
                      }
                      className={`p-2.5 text-left rounded border text-xs transition-colors cursor-pointer ${
                        selected
                          ? 'border-brand-600 bg-brand-50 dark:bg-brand-950/40 text-brand-900 dark:text-brand-100 font-bold'
                          : 'border-stone-200 dark:border-stone-800 hover:border-stone-400'
                      }`}
                    >
                      {p.label}
                    </button>
                  )
                })}
              </div>

              {/* Role Suggestions Input */}
              <div className="mt-3 p-3 bg-stone-50 dark:bg-stone-950 rounded-lg border border-stone-200 dark:border-stone-800 space-y-2">
                <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                  Describe what roles you want (or paste a target job blurb):
                </label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={roleDescription}
                    onChange={(e) => setRoleDescription(e.target.value)}
                    placeholder="e.g. Distributed backend systems, Go/Python microservices, and LLM pipelines"
                    className="flex-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-xs bg-white dark:bg-stone-900"
                  />
                  <button
                    type="button"
                    onClick={handleSuggestRoleFamilies}
                    disabled={suggestingRoles || !roleDescription.trim()}
                    className="px-3 py-1.5 rounded bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900 text-xs font-semibold hover:bg-stone-800 disabled:opacity-50 cursor-pointer"
                  >
                    {suggestingRoles ? 'Analyzing…' : 'Suggest Roles'}
                  </button>
                </div>
              </div>

              {/* Custom Role Family Toggle & Form */}
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  onClick={() => setShowCustomRole(!showCustomRole)}
                  className="text-xs text-brand-600 dark:text-brand-400 hover:underline font-semibold cursor-pointer"
                >
                  {showCustomRole ? '− Hide Custom Family Form' : '+ Add Custom Role Family'}
                </button>
              </div>

              {showCustomRole && (
                <div className="mt-2 p-3 bg-stone-50 dark:bg-stone-950 rounded-lg border border-stone-200 dark:border-stone-800 space-y-2 text-xs">
                  <h4 className="font-bold text-stone-800 dark:text-stone-200">New Role Family</h4>
                  <div className="grid sm:grid-cols-2 gap-2">
                    <div>
                      <label className="text-stone-500 text-[11px]">ID (slug)</label>
                      <input
                        type="text"
                        value={customRole.id}
                        onChange={(e) => setCustomRole({ ...customRole, id: e.target.value })}
                        placeholder="e.g. distributed_systems"
                        className="w-full p-1.5 border border-stone-300 dark:border-stone-700 rounded bg-white dark:bg-stone-900"
                      />
                    </div>
                    <div>
                      <label className="text-stone-500 text-[11px]">Label</label>
                      <input
                        type="text"
                        value={customRole.label}
                        onChange={(e) => setCustomRole({ ...customRole, label: e.target.value })}
                        placeholder="e.g. Distributed Systems"
                        className="w-full p-1.5 border border-stone-300 dark:border-stone-700 rounded bg-white dark:bg-stone-900"
                      />
                    </div>
                  </div>
                  <div>
                    <label className="text-stone-500 text-[11px]">Keywords (comma-separated)</label>
                    <input
                      type="text"
                      value={customRole.keywords}
                      onChange={(e) => setCustomRole({ ...customRole, keywords: e.target.value })}
                      placeholder="e.g. raft, kafka, distributed, consensus"
                      className="w-full p-1.5 border border-stone-300 dark:border-stone-700 rounded bg-white dark:bg-stone-900"
                    />
                  </div>
                  <div>
                    <label className="text-stone-500 text-[11px]">Title Patterns (comma-separated)</label>
                    <input
                      type="text"
                      value={customRole.title_patterns}
                      onChange={(e) => setCustomRole({ ...customRole, title_patterns: e.target.value })}
                      placeholder="e.g. distributed systems engineer, infrastructure engineer"
                      className="w-full p-1.5 border border-stone-300 dark:border-stone-700 rounded bg-white dark:bg-stone-900"
                    />
                  </div>
                  <div className="flex justify-end pt-1">
                    <button
                      type="button"
                      onClick={handleAddCustomRole}
                      disabled={!customRole.id.trim() || !customRole.label.trim()}
                      className="px-3 py-1 bg-brand-600 text-white rounded font-semibold disabled:opacity-50"
                    >
                      Add & Select Family
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="border-t border-stone-200 dark:border-stone-800 pt-4 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500">Target Location & Preferences</h3>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-stone-500 uppercase">Target Location / Workplace</label>
                  <input
                    type="text"
                    value={targetLocation}
                    onChange={(e) => setTargetLocation(e.target.value)}
                    placeholder="e.g. India, Remote"
                    className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                  />
                </div>
                <div>
                  <label className="text-xs font-semibold text-stone-500 uppercase">Remote Preference</label>
                  <select
                    value={remotePreference}
                    onChange={(e) => setRemotePreference(e.target.value)}
                    className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                  >
                    <option value="remote_or_hybrid">Remote or Hybrid</option>
                    <option value="remote_only">Remote Only</option>
                    <option value="onsite_or_hybrid">Onsite or Hybrid</option>
                    <option value="any">Any</option>
                  </select>
                </div>
              </div>

              <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500 pt-2">Standard Application Answers</h3>
              <div className="grid sm:grid-cols-2 gap-4">
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={workAuth}
                    onChange={(e) => setWorkAuth(e.target.checked)}
                    className="size-4 rounded text-brand-600"
                  />
                  <span>Legally authorized to work in target country</span>
                </label>
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={sponsorship}
                    onChange={(e) => setSponsorship(e.target.checked)}
                    className="size-4 rounded text-brand-600"
                  />
                  <span>Require visa sponsorship now or in future</span>
                </label>
              </div>

              <div className="grid sm:grid-cols-2 gap-4 pt-2">
                <div>
                  <label className="text-xs font-semibold text-stone-500 uppercase">LinkedIn Profile URL</label>
                  <input
                    type="url"
                    value={linkedinUrl}
                    onChange={(e) => setLinkedinUrl(e.target.value)}
                    placeholder="https://linkedin.com/in/username"
                    className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                  />
                </div>
                <div>
                  <label className="text-xs font-semibold text-stone-500 uppercase">GitHub Profile URL</label>
                  <input
                    type="url"
                    value={githubUrl}
                    onChange={(e) => setGithubUrl(e.target.value)}
                    placeholder="https://github.com/username"
                    className="w-full mt-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-sm bg-white dark:bg-stone-950"
                  />
                </div>
              </div>
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(4)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={handleSaveProfile}
                disabled={loading}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Save & Continue →
              </button>
            </div>
          </div>
        )}

        {/* Step 6: Job Sources */}
        {currentStep.id === 'sources' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">6. Job Sources & ATS Adapters</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Select which job boards and applicant tracking systems jobhunt should poll during automatic ingests.
            </p>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {Object.entries(sourcesState).map(([src, enabled]) => (
                <label
                  key={src}
                  className={`p-3 rounded-lg border flex items-center justify-between cursor-pointer transition-colors ${
                    enabled
                      ? 'border-brand-500 bg-brand-50/30 dark:bg-brand-950/20'
                      : 'border-stone-200 dark:border-stone-800 opacity-60'
                  }`}
                >
                  <span className="text-xs font-semibold capitalize">{src.replace('_', ' ')}</span>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(e) =>
                      setSourcesState({ ...sourcesState, [src]: e.target.checked })
                    }
                    className="size-4 rounded text-brand-600"
                  />
                </label>
              ))}
            </div>

            {/* Discover Company ATS Board */}
            <div className="mt-4 p-4 rounded-xl border border-stone-200 dark:border-stone-800 bg-stone-50/50 dark:bg-stone-950/40 space-y-2">
              <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500">Discover Company ATS Board</h3>
              <p className="text-xs text-stone-500">
                Enter any company careers URL or job posting link to auto-detect its ATS adapter (Greenhouse, Lever, Ashby, Workable, etc.).
              </p>
              <div className="flex gap-2">
                <input
                  type="url"
                  value={companyUrl}
                  onChange={(e) => setCompanyUrl(e.target.value)}
                  placeholder="https://boards.greenhouse.io/stripe or careers.airbnb.com"
                  className="flex-1 p-2 border border-stone-300 dark:border-stone-700 rounded text-xs bg-white dark:bg-stone-900"
                />
                <button
                  type="button"
                  onClick={handleDiscoverCompany}
                  disabled={discoveringCompany || !companyUrl.trim()}
                  className="px-4 py-2 rounded bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900 text-xs font-semibold hover:bg-stone-800 disabled:opacity-50 cursor-pointer"
                >
                  {discoveringCompany ? 'Discovering…' : 'Discover ATS'}
                </button>
              </div>

              {discoveredCompany && (
                <div className="p-3 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-300 dark:border-emerald-800 rounded-lg text-xs space-y-1 mt-2 text-emerald-900 dark:text-emerald-100">
                  <div className="font-semibold">✓ Discovered {discoveredCompany.company_name || 'Company'}!</div>
                  <div className="text-[11px] text-stone-600 dark:text-stone-300">
                    ATS Adapter: <span className="font-mono font-bold uppercase text-brand-700 dark:text-brand-300">{discoveredCompany.ats}</span> · Board URL: <span className="font-mono">{discoveredCompany.board_url}</span>
                  </div>
                </div>
              )}
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(5)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={() => goToStep(7)}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Continue to Schedule →
              </button>
            </div>
          </div>
        )}

        {/* Step 7: Schedule */}
        {currentStep.id === 'schedule' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">7. Automated Schedule</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Jobhunt can run automatically in the background using Windows Task Scheduler or cron.
            </p>

            <div className="p-4 bg-stone-50 dark:bg-stone-950 border border-stone-200 dark:border-stone-800 rounded-lg space-y-2 text-xs">
              <p className="font-semibold text-stone-900 dark:text-stone-100">Configured Ingest Times (Local Time):</p>
              <p className="font-mono text-stone-600 dark:text-stone-400">06:30, 09:30, 12:30, 15:30, 18:30</p>
              <p className="mt-3 font-semibold text-stone-900 dark:text-stone-100">One-click background install:</p>
              <p className="font-mono text-brand-600 dark:text-brand-400">python -m jobhunt schedule-install</p>
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(6)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={() => goToStep(8)}
                className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
              >
                Continue to Gmail Alerts →
              </button>
            </div>
          </div>
        )}

        {/* Step 8: Gmail Alerts (Optional) */}
        {currentStep.id === 'gmail' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-xl font-bold">8. Gmail Job Alerts (Optional)</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Connect your Gmail account with read-only access to automatically ingest job alert emails from LinkedIn, Simplify, and job boards.
            </p>

            <div className="p-4 rounded-lg border border-amber-200 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/20 text-xs text-amber-900 dark:text-amber-200 space-y-2">
              <p className="font-bold">Instructions:</p>
              <p>1. Go to Google Cloud Console and create an OAuth Client (Desktop App).</p>
              <p>2. Download <code>credentials.json</code> and save it in <code>.secrets/credentials.json</code>.</p>
              <p>3. Run <code>python -m jobhunt gmail-auth</code> in your terminal to complete the one-time browser login.</p>
            </div>

            <div className="pt-4 flex justify-between">
              <button
                type="button"
                onClick={() => goToStep(7)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => goToStep(9)}
                  className="px-4 py-2 text-xs font-medium text-stone-500 hover:text-stone-800 cursor-pointer"
                >
                  Skip for Now
                </button>
                <button
                  type="button"
                  onClick={() => goToStep(9)}
                  className="px-5 py-2 text-xs font-semibold rounded-md bg-brand-600 text-white hover:bg-brand-700 cursor-pointer"
                >
                  Continue →
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Step 9: Ready to Hunt & First Ingest */}
        {currentStep.id === 'finish' && (
          <div className="space-y-6 bg-white dark:bg-stone-900 p-6 rounded-xl border border-stone-200 dark:border-stone-800 shadow-sm">
            <h2 className="text-2xl font-bold">🎉 Almost Ready!</h2>
            <p className="text-sm text-stone-600 dark:text-stone-400">
              Run your first ingest to verify your connection to job sources and confirm your pipeline readiness.
            </p>

            <div className="p-5 border border-stone-200 dark:border-stone-800 rounded-xl bg-stone-50/50 dark:bg-stone-950/50 space-y-4">
              <div className="flex flex-wrap justify-between items-center gap-2">
                <div>
                  <h3 className="text-sm font-bold">Quick First Ingest</h3>
                  <p className="text-xs text-stone-500">Fast sources first, time-capped, parses new job postings into your database</p>
                </div>
                <div className="flex items-center gap-2">
                  {ollamaOnline ? (
                    <span className="px-2.5 py-1 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200 font-semibold text-[11px]">
                      Ollama: Online
                    </span>
                  ) : (
                    <span className="px-2.5 py-1 rounded bg-stone-200 text-stone-700 dark:bg-stone-800 dark:text-stone-300 font-semibold text-[11px]">
                      Ollama: Offline (Gracefully Skipped)
                    </span>
                  )}
                  {ingesting ? (
                    <button
                      type="button"
                      onClick={handleCancelFirstIngest}
                      className="px-3 py-1.5 rounded bg-rose-600 text-white text-xs font-semibold hover:bg-rose-700 cursor-pointer"
                    >
                      Cancel Ingest
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={handleRunFirstIngest}
                      className="px-4 py-2 rounded-lg bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900 text-xs font-semibold hover:bg-stone-800 cursor-pointer"
                    >
                      Run Quick First Ingest
                    </button>
                  )}
                </div>
              </div>

              {ingestProgress && ingesting && (
                <div className="p-3 bg-brand-50 dark:bg-brand-950/40 border border-brand-200 dark:border-brand-800 rounded-lg text-xs space-y-2">
                  <div className="flex justify-between font-semibold text-brand-900 dark:text-brand-200">
                    <span>{ingestProgress.message}</span>
                    <span>{ingestProgress.progress}%</span>
                  </div>
                  <div className="w-full bg-brand-200 dark:bg-brand-900 h-2 rounded-full overflow-hidden">
                    <div
                      className="bg-brand-600 h-full transition-all duration-300"
                      style={{ width: `${ingestProgress.progress}%` }}
                    />
                  </div>
                </div>
              )}

              {ingestResult && (
                <div className="p-3 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-300 dark:border-emerald-800 rounded text-xs text-emerald-900 dark:text-emerald-100 space-y-1">
                  <div className="font-semibold">
                    ✓ First Ingest completed in {ingestResult.time_seconds || 0}s!
                  </div>
                  <div className="text-[11px] text-stone-600 dark:text-stone-300">
                    Processed {ingestResult.total_jobs || 0} job postings across {(ingestResult.sources || []).length} sources.
                  </div>
                </div>
              )}
            </div>

            {readiness && (
              <div className="space-y-2">
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-500">Readiness Checklist</h3>
                <div className="grid sm:grid-cols-2 gap-2 text-xs">
                  {Object.entries(readiness).filter(([k]) => k !== 'ready').map(([k, item]: any) => (
                    <div key={k} className="p-2.5 rounded border border-stone-200 dark:border-stone-800 flex items-center gap-2">
                      <span className={`size-4 rounded-full flex items-center justify-center font-bold text-[10px] ${item.passed ? 'bg-emerald-500 text-white' : 'bg-amber-500 text-stone-950'}`}>
                        {item.passed ? '✓' : '!'}
                      </span>
                      <span className="font-medium">{item.label}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="pt-6 border-t border-stone-200 dark:border-stone-800 flex justify-between items-center">
              <button
                type="button"
                onClick={() => goToStep(8)}
                className="px-4 py-2 text-xs font-medium border border-stone-300 dark:border-stone-700 rounded-md hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
              >
                ← Back
              </button>
              <button
                type="button"
                onClick={handleCompleteSetup}
                disabled={loading}
                className="px-6 py-2.5 rounded-lg bg-emerald-600 text-white font-bold text-sm hover:bg-emerald-700 transition-colors cursor-pointer shadow-md"
              >
                Complete Setup & Go to Dashboard →
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
