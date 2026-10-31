import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
  try {
    window.localStorage.clear() // remembered filters must not leak from one test into the next
  } catch {
    /* no storage in this environment */
  }
})
