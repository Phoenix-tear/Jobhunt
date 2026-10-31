import { useState, useEffect } from 'react'
import { useSearchParams, useNavigate, Link } from 'react-router-dom'
import { requestJson } from '../api/client'

const SETTINGS_TABS = [
  { id: 'gemini', label: 'Gemini' },
  { id: 'budget', label: 'Budget' },
  { id: 'profile', label: 'Profile' },
  { id: 'resumes', label: 'Resumes' },
  { id: 'facts', label: 'Facts' },
  { id: 'summaries', label: 'Summaries & Answers' },
  { id: 'sources', label: 'Sources' },
  { id: 'schedule', label: 'Schedule' },
  { id: 'gmail', label: 'Gmail' },
  { id: 'danger', label: 'Danger Zone' },
]

export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const activeTab = searchParams.get('tab') || 'gemini'

  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<{ text: string; tone: 'ok' | 'err' } | null>(null)

  // 1. Gemini state
  const [secretInfo, setSecretInfo] = useState<{ present: boolean; last4: string }>({ present: false, last4: '' })
  const [newKey, setNewKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [keyTest, setKeyTest] = useState<any>(null)
  const [modelsData, setModelsData] = useState<any>(null)

  // 2. Budget state
  const [budgetView, setBudgetView] = useState<any>(null)
  const [budgetPreset, setBudgetPreset] = useState('free')
  const [dailyCalls, setDailyCalls] = useState(15)
  const [interactiveCalls, setInteractiveCalls] = useState(5)
  const [monthlyCap, setMonthlyCap] = useState(0.0)

  // 3. Profile state
  const [familyPresets, setFamilyPresets] = useState<any[]>([])
  const [selectedFamilies, setSelectedFamilies] = useState<string[]>(['fullstack', 'ai_ml'])
  const [educationStage, setEducationStage] = useState('undergraduate')

  // 4. Resumes state
  const [bases, setBases] = useState<any[]>([])
  const [uploadingResume, setUploadingResume] = useState(false)
  const [uploadDiag, setUploadDiag] = useState<any>(null)

  // 5. Facts state
  const [facts, setFacts] = useState<any[]>([])
  const [editingFactId, setEditingFactId] = useState<string | null>(null)
  const [editingFactText, setEditingFactText] = useState('')

  // 6. Summaries & Answers state
  const [summaries, setSummaries] = useState<Record<string, string>>({})
  const [answers, setAnswers] = useState<Record<string, any>>({})

  // 7. Sources state
  const [sources, setSources] = useState<Record<string, boolean>>({
    simplify: true,
    openroles: true,
    startup_jobs: true,
    ashby: true,
    greenhouse: true,
    lever: true,
    workable: true,
    smartrecruiters: true,
  })

  // 8. Schedule state
  const [scheduleData, setScheduleData] = useState<any>(null)

  // 9. Gmail state
  const [gmailStatus, setGmailStatus] = useState<any>(null)

  // 10. Danger zone state
  const [moveAsideModal, setMoveAsideModal] = useState(false)
  const [includeSecrets, setIncludeSecrets] = useState(false)
  const [confirmPhrase, setConfirmPhrase] = useState('')
  const [restoreModal, setRestoreModal] = useState(false)
  const [restoreTimestamp, setRestoreTimestamp] = useState('')

  const setTab = (id: string) => {
    setSearchParams({ tab: id })
    setMessage(null)
  }

  useEffect(() => {
    loadAll()
  }, [])

  const loadAll = () => {
    // Load Gemini info
    requestJson<{ gemini_key: { present: boolean; last4: string } }>('/api/settings/secrets-status')
      .then((res) => setSecretInfo(res.gemini_key || { present: false, last4: '' }))
      .catch(() => {})

    // Load models
    requestJson<any>('/api/settings/models')
      .then((res) => setModelsData(res))
      .catch(() => {})

    // Load budget
    requestJson<any>('/api/settings/budget')
      .then((res) => {
        setBudgetView(res)
        if (res.budget) {
          setBudgetPreset(res.budget.preset || 'free')
          setDailyCalls(res.budget.daily_calls || 15)
          setInteractiveCalls(res.budget.interactive_daily_calls || 5)
          setMonthlyCap(res.budget.monthly_usd || 0.0)
        }
      })
      .catch(() => {})

    // Load presets & bases
    requestJson<{ presets: any[] }>('/api/profile/role-families/presets')
      .then((res) => setFamilyPresets(res.presets || []))
      .catch(() => {})

    requestJson<{ bases: any[] }>('/api/resumes/bases')
      .then((res) => setBases(res.bases || []))
      .catch(() => {})

    // Load facts
    requestJson<{ facts: any[] }>('/api/facts')
      .then((res) => setFacts(res.facts || []))
      .catch(() => {})

    // Load answers & summaries
    requestJson<{ answers: any }>('/api/profile/answers')
      .then((res) => setAnswers(res.answers || {}))
      .catch(() => {})

    requestJson<{ summaries: any }>('/api/profile/summaries')
      .then((res) => setSummaries(res.summaries || {}))
      .catch(() => {})

    // Load schedule & gmail
    requestJson<any>('/api/settings/schedule')
      .then((res) => setScheduleData(res))
      .catch(() => {})

    requestJson<any>('/api/settings/gmail/status')
      .then((res) => setGmailStatus(res))
      .catch(() => {})
  }

  // Gemini Key save
  const handleSaveGeminiKey = async () => {
    if (!newKey.trim()) return
    setLoading(true)
    try {
      const res = await requestJson<any>('/api/settings/gemini-key', {
        method: 'POST',
        body: JSON.stringify({ key: newKey }),
      })
      setSecretInfo(res)
      setNewKey('')
      setMessage({ text: 'Gemini API key updated successfully.', tone: 'ok' })
    } catch (err: any) {
      setMessage({ text: err.message || 'Failed to update key', tone: 'err' })
    } finally {
      setLoading(false)
    }
  }

  const handleTestKey = async () => {
    setLoading(true)
    setKeyTest(null)
    try {
      const res = await requestJson<any>('/api/settings/test-gemini-key', {
        method: 'POST',
        body: JSON.stringify({ key: newKey }),
      })
      setKeyTest(res)
    } catch (err: any) {
      setKeyTest({ success: false, error: err.message })
    } finally {
      setLoading(false)
    }
  }

  // Budget save
  const handleSaveBudget = async () => {
    setLoading(true)
    try {
      await requestJson('/api/settings/budget', {
        method: 'POST',
        body: JSON.stringify({
          preset: budgetPreset,
          daily_pipeline_calls: dailyCalls,
          daily_interactive_calls: interactiveCalls,
          monthly_budget_usd: monthlyCap,
        }),
      })
      setMessage({ text: 'Budget preferences saved.', tone: 'ok' })
      loadAll()
    } catch (err: any) {
      setMessage({ text: err.message || 'Failed to save budget', tone: 'err' })
    } finally {
      setLoading(false)
    }
  }

  // Resume upload
  const handleUploadResume = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploadingResume(true)
    setUploadDiag(null)
    const formData = new FormData()
    formData.append('file', file)
    formData.append('base_id', file.name.replace(/\.tex$/i, ''))
    try {
      const res = await requestJson<any>('/api/resumes/upload', {
        method: 'POST',
        body: formData,
      })
      setUploadDiag(res.validation)
      loadAll()
      setMessage({ text: 'Resume uploaded and validated.', tone: 'ok' })
    } catch (err: any) {
      setMessage({ text: err.message || 'Upload failed', tone: 'err' })
    } finally {
      setUploadingResume(false)
    }
  }

  // Facts handlers
  const handleToggleFact = async (id: string, currentTicked: boolean) => {
    try {
      await requestJson('/api/facts/tick', {
        method: 'POST',
        body: JSON.stringify({ fact_ids: [id], ticked: !currentTicked }),
      })
      setFacts((prev) => prev.map((f) => (f.id === id ? { ...f, ticked: !currentTicked } : f)))
    } catch (err: any) {
      setMessage({ text: err.message, tone: 'err' })
    }
  }

  const handleBulkToggle = async (ticked: boolean) => {
    try {
      await requestJson('/api/facts/bulk-tick', {
        method: 'POST',
        body: JSON.stringify({ ticked }),
      })
      setFacts((prev) => prev.map((f) => ({ ...f, ticked })))
    } catch (err: any) {
      setMessage({ text: err.message, tone: 'err' })
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
        prev.map((f) => (f.id === id ? { ...f, id: res.new_id, text: res.text, edited: true } : f))
      )
      setEditingFactId(null)
      setMessage({ text: 'Fact updated and re-hashed.', tone: 'ok' })
    } catch (err: any) {
      setMessage({ text: err.message, tone: 'err' })
    }
  }

  // Danger Zone actions
  const handleRunSetupAgain = async () => {
    setLoading(true)
    try {
      await requestJson('/api/settings/run-setup-again', { method: 'POST' })
      navigate('/setup')
    } catch (err: any) {
      setMessage({ text: err.message || 'Failed to restart setup', tone: 'err' })
      setLoading(false)
    }
  }

  const handleMoveDataAside = async () => {
    if (confirmPhrase !== 'MOVE DATA ASIDE') return
    setLoading(true)
    try {
      const res = await requestJson<any>('/api/settings/danger/move-aside', {
        method: 'POST',
        body: JSON.stringify({
          confirmation_phrase: confirmPhrase,
          include_secrets: includeSecrets,
        }),
      })
      setMoveAsideModal(false)
      setConfirmPhrase('')
      setMessage({
        text: `Data safely moved aside to ${res.trash_path}. Use timestamp ${res.timestamp} to restore if needed.`,
        tone: 'ok',
      })
    } catch (err: any) {
      setMessage({ text: err.message || 'Move aside failed', tone: 'err' })
    } finally {
      setLoading(false)
    }
  }

  const handleRestoreAside = async () => {
    if (!restoreTimestamp.trim()) return
    setLoading(true)
    try {
      const res = await requestJson<any>('/api/settings/danger/restore-aside', {
        method: 'POST',
        body: JSON.stringify({ timestamp: restoreTimestamp.trim() }),
      })
      setRestoreModal(false)
      setRestoreTimestamp('')
      setMessage({ text: res.message || 'Data restored successfully', tone: 'ok' })
      loadAll()
    } catch (err: any) {
      setMessage({ text: err.message || 'Restore failed', tone: 'err' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-stone-500">Configure your local pipeline, models, keys, and preferences</p>
      </div>

      {message && (
        <div
          role="alert"
          className={`mb-6 p-4 rounded-lg text-sm border ${
            message.tone === 'ok'
              ? 'bg-emerald-50 text-emerald-900 border-emerald-300 dark:bg-emerald-950/40 dark:text-emerald-100 dark:border-emerald-800'
              : 'bg-rose-50 text-rose-900 border-rose-300 dark:bg-rose-950/40 dark:text-rose-100 dark:border-rose-800'
          }`}
        >
          {message.text}
        </div>
      )}

      {/* Tabs Layout */}
      <div className="flex flex-col md:flex-row gap-6">
        {/* Tab List */}
        <nav aria-label="Settings navigation" className="w-full md:w-56 shrink-0 flex md:flex-col gap-1 overflow-x-auto border-b md:border-b-0 md:border-r border-stone-200 dark:border-stone-800 pb-2 md:pb-0 md:pr-4">
          {SETTINGS_TABS.map((tab) => {
            const isActive = activeTab === tab.id
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setTab(tab.id)}
                className={`text-left px-3 py-2 rounded-md text-xs font-medium transition-colors whitespace-nowrap cursor-pointer ${
                  isActive
                    ? 'bg-brand-600 text-white font-bold shadow-sm'
                    : 'text-stone-600 dark:text-stone-400 hover:bg-stone-100 dark:hover:bg-stone-800'
                }`}
              >
                {tab.label}
              </button>
            )
          })}
          <div className="pt-2 my-1 border-t border-stone-200 dark:border-stone-800">
            <Link
              to="/audit"
              className="text-left px-3 py-2 rounded-md text-xs font-semibold text-brand-600 dark:text-brand-400 hover:bg-stone-100 dark:hover:bg-stone-800 transition-colors block"
            >
              Triage Audit →
            </Link>
          </div>
        </nav>

        {/* Tab Content Panels */}
        <div className="flex-1 min-w-0 bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl p-6 shadow-sm">
          {/* 1. Gemini */}
          {activeTab === 'gemini' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Gemini API Key & Models</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Saved locally under <code>.secrets/gemini_api_key</code> with owner-only ACL permissions.
                </p>
              </div>

              <div className="p-4 bg-stone-50 dark:bg-stone-950 rounded-lg border border-stone-200 dark:border-stone-800 flex justify-between items-center text-xs">
                <div>
                  <span className="font-semibold text-stone-700 dark:text-stone-300">Current Key: </span>
                  {secretInfo.present ? (
                    <span className="font-mono text-emerald-600 dark:text-emerald-400 font-bold ml-1">
                      Present (ending in {secretInfo.last4})
                    </span>
                  ) : (
                    <span className="text-amber-600 dark:text-amber-400 font-bold ml-1">Not configured</span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={handleTestKey}
                  disabled={loading}
                  className="px-3 py-1.5 border border-stone-300 dark:border-stone-700 rounded text-xs font-medium hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
                >
                  Test Stored Key
                </button>
              </div>

              <div className="space-y-3">
                <label className="text-xs font-semibold uppercase text-stone-500">Update API Key</label>
                <div className="flex gap-2">
                  <input
                    type={showKey ? 'text' : 'password'}
                    value={newKey}
                    onChange={(e) => setNewKey(e.target.value)}
                    placeholder="Enter new AIzaSy... key"
                    className="flex-1 p-2 rounded border border-stone-300 dark:border-stone-700 text-xs font-mono bg-white dark:bg-stone-950"
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey(!showKey)}
                    className="px-3 py-1.5 border border-stone-300 dark:border-stone-700 rounded text-xs"
                  >
                    {showKey ? 'Hide' : 'Show'}
                  </button>
                  <button
                    type="button"
                    onClick={handleSaveGeminiKey}
                    disabled={loading || !newKey.trim()}
                    className="px-4 py-1.5 bg-brand-600 text-white rounded text-xs font-semibold hover:bg-brand-700 disabled:opacity-50 cursor-pointer"
                  >
                    Save Key
                  </button>
                </div>
              </div>

              {keyTest && (
                <div className={`p-3 rounded text-xs border ${keyTest.success ? 'bg-emerald-50 border-emerald-300 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-100' : 'bg-rose-50 border-rose-300 text-rose-900 dark:bg-rose-950/40 dark:text-rose-100'}`}>
                  {keyTest.success ? `✓ Test passed! Models: ${keyTest.models?.slice(0, 5).join(', ')}` : `✕ Test failed: ${keyTest.error}`}
                </div>
              )}

              {modelsData && (
                <div className="border-t border-stone-200 dark:border-stone-800 pt-4">
                  <h3 className="text-xs font-bold uppercase text-stone-500 mb-2">Active Task Models</h3>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    {Object.entries(modelsData.current || {}).map(([task, model]: any) => (
                      <div key={task} className="p-2 border rounded bg-stone-50 dark:bg-stone-950 flex justify-between">
                        <span className="capitalize">{task}:</span>
                        <span className="font-mono font-medium">{String(model)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 2. Budget */}
          {activeTab === 'budget' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Usage, Rates & Monthly Budget</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Protected by rate limits and spend caps. Reset happens at 00:00:00 UTC.
                </p>
              </div>

              {budgetView && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  <div className="p-3 border rounded-lg bg-stone-50 dark:bg-stone-950">
                    <p className="text-[11px] uppercase text-stone-500">Pipeline Today</p>
                    <p className="text-xl font-bold mt-1">
                      {budgetView.usage?.today_calls} / {budgetView.budget?.daily_calls}
                    </p>
                  </div>
                  <div className="p-3 border rounded-lg bg-stone-50 dark:bg-stone-950">
                    <p className="text-[11px] uppercase text-stone-500">Interactive Today</p>
                    <p className="text-xl font-bold mt-1">
                      {budgetView.usage?.today_interactive} / {budgetView.budget?.interactive_daily_calls}
                    </p>
                  </div>
                  <div className="p-3 border rounded-lg bg-stone-50 dark:bg-stone-950">
                    <p className="text-[11px] uppercase text-stone-500">Today Cost</p>
                    <p className="text-xl font-bold mt-1">${budgetView.usage?.today_cost_usd?.toFixed(4)}</p>
                  </div>
                  <div className="p-3 border rounded-lg bg-stone-50 dark:bg-stone-950">
                    <p className="text-[11px] uppercase text-stone-500">Month Spend</p>
                    <p className="text-xl font-bold mt-1">
                      ${budgetView.usage?.month_cost_usd?.toFixed(2)} / ${budgetView.budget?.monthly_usd?.toFixed(2)}
                    </p>
                  </div>
                </div>
              )}

              <div className="space-y-3 border-t border-stone-200 dark:border-stone-800 pt-4">
                <label className="text-xs font-semibold uppercase text-stone-500">Preset</label>
                <div className="flex gap-3">
                  {['free', 'paid', 'custom'].map((p) => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => setBudgetPreset(p)}
                      className={`px-4 py-2 rounded-lg border text-xs font-bold capitalize cursor-pointer ${
                        budgetPreset === p
                          ? 'border-brand-600 bg-brand-50 text-brand-900 dark:bg-brand-950/40 dark:text-brand-100'
                          : 'border-stone-200 dark:border-stone-800'
                      }`}
                    >
                      {p}
                    </button>
                  ))}
                </div>

                <div className="grid grid-cols-3 gap-3 pt-2">
                  <div>
                    <label className="text-xs text-stone-500">Pipeline Calls / Day</label>
                    <input
                      type="number"
                      value={dailyCalls}
                      onChange={(e) => setDailyCalls(Number(e.target.value))}
                      className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-950 text-xs"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-stone-500">Interactive Calls / Day</label>
                    <input
                      type="number"
                      value={interactiveCalls}
                      onChange={(e) => setInteractiveCalls(Number(e.target.value))}
                      className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-950 text-xs"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-stone-500">Monthly USD Cap</label>
                    <input
                      type="number"
                      step="0.5"
                      value={monthlyCap}
                      onChange={(e) => setMonthlyCap(Number(e.target.value))}
                      className="w-full mt-1 p-2 rounded border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-950 text-xs"
                    />
                  </div>
                </div>

                <button
                  type="button"
                  onClick={handleSaveBudget}
                  disabled={loading}
                  className="mt-3 px-4 py-2 bg-brand-600 text-white rounded text-xs font-semibold hover:bg-brand-700 cursor-pointer"
                >
                  Save Budget Settings
                </button>
              </div>
            </div>
          )}

          {/* 3. Profile */}
          {activeTab === 'profile' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Target Profile & Role Families</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Customize the roles and parameters jobhunt matches your resumes against.
                </p>
              </div>

              <div>
                <label className="text-xs font-semibold uppercase text-stone-500 mb-2 block">
                  Select Active Role Families
                </label>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                  {familyPresets.map((p) => {
                    const sel = selectedFamilies.includes(p.id)
                    return (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() =>
                          setSelectedFamilies(
                            sel ? selectedFamilies.filter((x) => x !== p.id) : [...selectedFamilies, p.id]
                          )
                        }
                        className={`p-2.5 text-left rounded border text-xs cursor-pointer ${
                          sel
                            ? 'border-brand-600 bg-brand-50 text-brand-900 dark:bg-brand-950/40 dark:text-brand-100 font-bold'
                            : 'border-stone-200 dark:border-stone-800'
                        }`}
                      >
                        {p.label}
                      </button>
                    )
                  })}
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold uppercase text-stone-500 mb-2 block">
                  Education Stage
                </label>
                <select
                  value={educationStage}
                  onChange={(e) => setEducationStage(e.target.value)}
                  className="p-2 border rounded text-xs bg-white dark:bg-stone-900 border-stone-300 dark:border-stone-700"
                >
                  <option value="undergraduate">Undergraduate</option>
                  <option value="masters">Master's</option>
                  <option value="phd">PhD</option>
                  <option value="bootcamp">Bootcamp Graduate</option>
                  <option value="self_taught">Self Taught / Industry Experience</option>
                </select>
              </div>
            </div>
          )}

          {/* 4. Resumes */}
          {activeTab === 'resumes' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Base Resumes (.tex)</h2>
                <p className="text-xs text-stone-500 mt-1">
                  LaTeX templates stored under <code>data/resumes/</code> and compiled with Tectonic.
                </p>
              </div>

              <div className="space-y-2">
                {bases.map((b) => (
                  <div key={b.id} className="p-3 border rounded-lg flex justify-between items-center text-xs">
                    <div>
                      <span className="font-mono font-bold text-stone-800 dark:text-stone-200">{b.id}.tex</span>
                      <span className="ml-2 text-stone-400">({b.path})</span>
                    </div>
                    <span className="px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300 font-semibold">
                      Active
                    </span>
                  </div>
                ))}
              </div>

              <div className="border-t border-stone-200 dark:border-stone-800 pt-4">
                <label className="text-xs font-semibold uppercase text-stone-500 block mb-2">Upload New Base (.tex)</label>
                <input
                  type="file"
                  accept=".tex"
                  onChange={handleUploadResume}
                  disabled={uploadingResume}
                  className="text-xs"
                />
                {uploadDiag && (
                  <div className="mt-3 p-3 bg-stone-50 dark:bg-stone-950 rounded border text-xs">
                    {uploadDiag.compile?.success ? '✓ Sandboxed Tectonic compilation succeeded' : 'Compilation notice: PDF engine skipped or offline'}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 5. Facts */}
          {activeTab === 'facts' && (
            <div className="space-y-6">
              <div className="flex justify-between items-start">
                <div>
                  <h2 className="text-lg font-bold">Extracted Facts Review</h2>
                  <p className="text-xs text-stone-500 mt-1">
                    Verified bullets stored in <code>data/profile/facts_tex.md</code>. Only ticked facts are eligible for tailoring.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => handleBulkToggle(true)}
                    className="px-2.5 py-1 text-xs border rounded cursor-pointer"
                  >
                    Tick All
                  </button>
                  <button
                    type="button"
                    onClick={() => handleBulkToggle(false)}
                    className="px-2.5 py-1 text-xs border rounded cursor-pointer"
                  >
                    Untick All
                  </button>
                </div>
              </div>

              <div className="max-h-96 overflow-y-auto space-y-2 border p-3 rounded-lg bg-stone-50/50 dark:bg-stone-950/40">
                {facts.map((fact) => (
                  <div key={fact.id} className="p-2.5 bg-white dark:bg-stone-900 border rounded flex items-start gap-3 text-xs">
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
                            className="w-full p-2 border rounded text-xs bg-white dark:bg-stone-950"
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
                            className="text-stone-400 hover:text-stone-700 text-xs ml-2 cursor-pointer"
                          >
                            ✎
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 6. Summaries & Answers */}
          {activeTab === 'summaries' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Profile Summaries & Application Answers</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Manage introductory summaries and common question answers.
                </p>
              </div>

              <div className="space-y-3">
                <h3 className="text-xs font-bold uppercase text-stone-500">Application Answers</h3>
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div className="p-3 border rounded bg-stone-50 dark:bg-stone-950">
                    <span className="text-stone-500 block">Work Authorization:</span>
                    <span className="font-medium">{answers.work_authorization || 'Authorized'}</span>
                  </div>
                  <div className="p-3 border rounded bg-stone-50 dark:bg-stone-950">
                    <span className="text-stone-500 block">Visa Sponsorship:</span>
                    <span className="font-medium">{answers.sponsorship || 'Not Required'}</span>
                  </div>
                </div>
              </div>

              <div className="space-y-3">
                <h3 className="text-xs font-bold uppercase text-stone-500">Role Summaries</h3>
                {Object.keys(summaries).length === 0 ? (
                  <p className="text-xs text-stone-400">No role family summaries configured yet.</p>
                ) : (
                  <div className="space-y-2">
                    {Object.entries(summaries).map(([fam, sum]) => (
                      <div key={fam} className="p-3 border rounded text-xs">
                        <span className="font-semibold block capitalize mb-1">{fam.replace('_', ' ')}</span>
                        <p className="text-stone-600 dark:text-stone-300">{sum}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 7. Sources */}
          {activeTab === 'sources' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Job Feeds & ATS Adapters</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Toggle which scrapers run during automated ingestion.
                </p>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {Object.entries(sources).map(([src, enabled]) => (
                  <label key={src} className="p-3 border rounded flex justify-between items-center text-xs capitalize cursor-pointer">
                    <span>{src.replace('_', ' ')}</span>
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={(e) => setSources({ ...sources, [src]: e.target.checked })}
                      className="size-4 rounded text-brand-600"
                    />
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* 8. Schedule */}
          {activeTab === 'schedule' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Background Schedule Times</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Runs configured for Windows Task Scheduler or cron.
                </p>
              </div>

              {scheduleData && (
                <div className="p-4 bg-stone-50 dark:bg-stone-950 border rounded text-xs space-y-2">
                  <p>
                    <strong>Ingest Times: </strong>
                    <span className="font-mono">{scheduleData.ingest_times?.join(', ')}</span>
                  </p>
                  <p>
                    <strong>Match Time: </strong>
                    <span className="font-mono">{scheduleData.match_time}</span>
                  </p>
                  <p>
                    <strong>Timezone: </strong>
                    <span>{scheduleData.timezone}</span>
                  </p>
                </div>
              )}
            </div>
          )}

          {/* 9. Gmail */}
          {activeTab === 'gmail' && (
            <div className="space-y-6">
              <div>
                <h2 className="text-lg font-bold">Gmail Integration</h2>
                <p className="text-xs text-stone-500 mt-1">
                  Read-only email alert parsing.
                </p>
              </div>

              {gmailStatus && (
                <div className="p-4 bg-stone-50 dark:bg-stone-950 border rounded text-xs space-y-2">
                  <p>
                    <strong>Status: </strong>
                    <span className="capitalize font-semibold text-brand-600">{gmailStatus.status}</span>
                  </p>
                  <p>
                    <strong>OAuth Credentials: </strong>
                    <span>{gmailStatus.client_present ? '✓ Present' : '✕ Missing (credentials.json)'}</span>
                  </p>
                  <p>
                    <strong>Token: </strong>
                    <span>{gmailStatus.token_present ? '✓ Authorized (token.json)' : '✕ Not Authorized'}</span>
                  </p>
                  <p className="text-stone-400 mt-2">{gmailStatus.note}</p>
                </div>
              )}
            </div>
          )}

          {/* 10. Danger Zone */}
          {activeTab === 'danger' && (
            <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-rose-600 dark:text-rose-400">Danger Zone</h2>
        <p className="text-xs text-stone-500 mt-1">
          Safe workspace archives, data moves to trash, and non-destructive setup resets. No silent file deletions.
        </p>
      </div>

      <div className="p-4 border border-stone-200 dark:border-stone-800 rounded-lg flex justify-between items-center">
        <div>
          <h3 className="text-sm font-semibold">Run Setup Again</h3>
          <p className="text-xs text-stone-500">
            Re-opens the first-run wizard non-destructively so you can walk through setup on a copy.
          </p>
        </div>
        <button
          type="button"
          onClick={handleRunSetupAgain}
          disabled={loading}
          className="px-4 py-2 border border-stone-300 dark:border-stone-700 rounded-md text-xs font-semibold hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
        >
          Run Setup Again
        </button>
      </div>

      <div className="p-4 border border-amber-300 dark:border-amber-900 bg-amber-50/30 dark:bg-amber-950/20 rounded-lg flex justify-between items-center">
        <div>
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-300">Move My Data Aside</h3>
          <p className="text-xs text-stone-500">
            Moves <code>data/</code> contents (except backups) into <code>data/trash/&lt;timestamp&gt;/</code> after confirmation. Never deletes silently.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setMoveAsideModal(true)}
          className="px-4 py-2 bg-amber-600 text-white rounded-md text-xs font-semibold hover:bg-amber-700 cursor-pointer"
        >
          Move Data Aside…
        </button>
      </div>

      <div className="p-4 border border-stone-200 dark:border-stone-800 rounded-lg flex justify-between items-center">
        <div>
          <h3 className="text-sm font-semibold">Restore Data from Trash</h3>
          <p className="text-xs text-stone-500">
            Restore previously archived data from a <code>data/trash/&lt;timestamp&gt;/</code> folder back into active workspace.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setRestoreModal(true)}
          className="px-4 py-2 border border-stone-300 dark:border-stone-700 rounded-md text-xs font-semibold hover:bg-stone-100 dark:hover:bg-stone-800 cursor-pointer"
        >
          Restore Aside…
        </button>
      </div>
    </div>
  )}
        </div>
      </div>

      {moveAsideModal && (
        <div className="fixed inset-0 bg-stone-950/60 flex items-center justify-center p-4 z-50">
          <div className="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl max-w-md w-full p-6 shadow-xl space-y-4">
            <h3 className="text-base font-bold text-amber-600">Move My Data Aside</h3>
            <p className="text-xs text-stone-600 dark:text-stone-400 leading-relaxed">
              This safely moves active files in <code>data/</code> to <code>data/trash/&lt;timestamp&gt;/</code> without deleting anything.
              Type <strong className="font-mono text-amber-600">MOVE DATA ASIDE</strong> to confirm.
            </p>
            <input
              type="text"
              value={confirmPhrase}
              onChange={(e) => setConfirmPhrase(e.target.value)}
              placeholder="Type MOVE DATA ASIDE"
              className="w-full p-2 border border-stone-300 dark:border-stone-700 rounded text-xs font-mono bg-white dark:bg-stone-950"
            />
            <label className="flex items-center gap-2 text-xs text-stone-600 dark:text-stone-400 cursor-pointer">
              <input
                type="checkbox"
                checked={includeSecrets}
                onChange={(e) => setIncludeSecrets(e.target.checked)}
                className="size-4 rounded text-amber-600"
              />
              <span>Also move <code>.secrets/</code> (optional)</span>
            </label>
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => {
                  setMoveAsideModal(false)
                  setConfirmPhrase('')
                }}
                className="px-3 py-1.5 border rounded text-xs text-stone-600"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleMoveDataAside}
                disabled={confirmPhrase !== 'MOVE DATA ASIDE' || loading}
                className="px-4 py-1.5 bg-amber-600 text-white rounded text-xs font-semibold disabled:opacity-50 hover:bg-amber-700 cursor-pointer"
              >
                Move Data Aside
              </button>
            </div>
          </div>
        </div>
      )}

      {restoreModal && (
        <div className="fixed inset-0 bg-stone-950/60 flex items-center justify-center p-4 z-50">
          <div className="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl max-w-md w-full p-6 shadow-xl space-y-4">
            <h3 className="text-base font-bold text-stone-900 dark:text-stone-100">Restore Data from Trash</h3>
            <p className="text-xs text-stone-600 dark:text-stone-400 leading-relaxed">
              Enter the folder timestamp name located in <code>data/trash/</code> (e.g. <code>20261030_120000</code>).
            </p>
            <input
              type="text"
              value={restoreTimestamp}
              onChange={(e) => setRestoreTimestamp(e.target.value)}
              placeholder="e.g. 20261030_120000"
              className="w-full p-2 border border-stone-300 dark:border-stone-700 rounded text-xs font-mono bg-white dark:bg-stone-950"
            />
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => {
                  setRestoreModal(false)
                  setRestoreTimestamp('')
                }}
                className="px-3 py-1.5 border rounded text-xs text-stone-600"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleRestoreAside}
                disabled={!restoreTimestamp.trim() || loading}
                className="px-4 py-1.5 bg-brand-600 text-white rounded text-xs font-semibold disabled:opacity-50 hover:bg-brand-700 cursor-pointer"
              >
                Restore
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
