import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { DecisionProvider } from './components/DecisionDialogs'
import { ToastProvider } from './components/Toast'
import { HealthPage } from './pages/HealthPage'
import { JobsPage } from './pages/JobsPage'
import { TodayPage } from './pages/TodayPage'
import { ResumesPage } from './pages/ResumesPage'
import { TrackerPage } from './pages/TrackerPage'
import { SettingsPage } from './pages/SettingsPage'
import { SetupWizardPage } from './pages/SetupWizardPage'
import { TriageAuditPage } from './pages/TriageAuditPage'
import { useSetupState } from './api/hooks'

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // 4xx answers will not change on retry; only retry network blips / 5xx a couple of times.
          const status = (error as { status?: number }).status ?? 0
          return failureCount < 2 && (status === 0 || status >= 500)
        },
      },
    },
  })
}

function SetupRedirect() {
  const location = useLocation()
  const navigate = useNavigate()
  const { data: setupState } = useSetupState()

  useEffect(() => {
    if (location.pathname === '/setup') return
    if (setupState && !setupState.completed && !setupState.demo_mode) {
      navigate('/setup', { replace: true })
    }
  }, [location.pathname, setupState, navigate])

  return null
}

export function App() {
  const [queryClient] = useState(createQueryClient)
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <DecisionProvider>
          <SetupRedirect />
          <Routes>
            <Route path="setup" element={<SetupWizardPage />} />
            <Route element={<AppShell />}>
              <Route index element={<Navigate to="/today" replace />} />
              <Route path="today" element={<TodayPage />} />
              <Route path="jobs" element={<JobsPage />} />
              <Route path="resumes" element={<ResumesPage />} />
              <Route path="tracker" element={<TrackerPage />} />
              <Route path="health" element={<HealthPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="audit" element={<TriageAuditPage />} />
              <Route path="triage/audit" element={<Navigate to="/audit" replace />} />
              <Route path="*" element={<Navigate to="/today" replace />} />
            </Route>
          </Routes>
        </DecisionProvider>
      </ToastProvider>
    </QueryClientProvider>
  )
}
