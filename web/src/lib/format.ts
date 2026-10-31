const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** "3h ago", "2d ago", "5w ago". Returns '' for missing/invalid dates. */
export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ''
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ''
  const elapsed = Math.max(0, now - then)
  if (elapsed < HOUR) return `${Math.max(1, Math.floor(elapsed / MINUTE))}m ago`
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)}h ago`
  if (elapsed < 14 * DAY) return `${Math.floor(elapsed / DAY)}d ago`
  if (elapsed < 120 * DAY) return `${Math.floor(elapsed / (7 * DAY))}w ago`
  return `${Math.floor(elapsed / (30 * DAY))}mo ago`
}

export const LEVEL_LABELS: Record<string, string> = {
  intern: 'Intern',
  new_grad: 'New grad',
  entry: 'Entry',
  mid: 'Mid',
  senior: 'Senior',
  unknown: 'Level unknown',
}

export const TYPE_LABELS: Record<string, string> = {
  full_time: 'Full-time',
  internship: 'Internship',
  contract: 'Contract',
  part_time: 'Part-time',
}

export const SOURCE_LABELS: Record<string, string> = {
  greenhouse: 'Greenhouse',
  lever: 'Lever',
  ashby: 'Ashby',
  workable: 'Workable',
  smartrecruiters: 'SmartRecruiters',
  recruitee: 'Recruitee',
  workday: 'Careers site (Workday)',
  oracle: 'Careers site (Oracle)',
  amazon_jobs: 'Careers site (amazon.jobs)',
  remotive: 'Remotive',
  remoteok: 'RemoteOK',
  weworkremotely: 'We Work Remotely',
  himalayas: 'Himalayas',
  jobicy: 'Jobicy',
  arbeitnow: 'Arbeitnow',
  adzuna: 'Adzuna',
  hackernews: 'Hacker News',
  simplify_internships: 'Simplify internships',
  simplify_newgrad: 'Simplify new-grad',
  manual: 'Added by me',
  gmail: 'Gmail alerts',
}

export function sourceLabel(source: string): string {
  if (source.startsWith('email_')) return `${source.slice(6).replace(/^./, (c) => c.toUpperCase())} alert`
  return SOURCE_LABELS[source] ?? source
}

const regionNames = typeof Intl !== 'undefined' && 'DisplayNames' in Intl ? new Intl.DisplayNames(['en'], { type: 'region' }) : null

export function countryLabel(code: string): string {
  if (code === 'unknown') return 'Unknown'
  try {
    return regionNames?.of(code) ?? code
  } catch {
    return code
  }
}

/** Is a follow-up date (YYYY-MM-DD or ISO datetime) in the past? Date-only values are overdue after that day. */
export function isOverdue(followUp: string | null | undefined, now: Date = new Date()): boolean {
  if (!followUp) return false
  const dateOnly = followUp.length === 10
  const due = new Date(dateOnly ? `${followUp}T23:59:59` : followUp)
  return !Number.isNaN(due.getTime()) && due.getTime() < now.getTime()
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value.length === 10 ? `${value}T00:00:00` : value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

export function formatSalary(min: number | null, max: number | null, currency: string | null): string {
  if (min === null && max === null) return ''
  const fmt = (value: number) => new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value)
  const range = min !== null && max !== null && min !== max ? `${fmt(min)}–${fmt(max)}` : fmt((min ?? max) as number)
  return `${currency ? `${currency} ` : ''}${range} / yr`
}
