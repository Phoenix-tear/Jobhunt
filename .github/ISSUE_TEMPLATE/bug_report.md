---
name: Bug Report
about: Create a report to help us improve Jobhunt
title: '[BUG] '
labels: bug
assignees: ''
---

## 1. Describe the Bug
A clear and concise description of what the bug is.

## 2. Environment Information
- **Operating System:** [e.g. macOS 14 Sonoma (Apple Silicon M2) / Windows 11 64-bit / Ubuntu 24.04]
- **Python Version:** [e.g. 3.12.3]
- **Node.js Version:** [e.g. 20.12.0]
- **Installation Method:** [e.g. Fresh clone with `setup.ps1` / `setup.sh` or `jobhunt upgrade`]
- **Mode:** [Pure Offline ($0) / Gemini Cloud API]

## 3. Steps to Reproduce
Steps to reproduce the behavior:
1. Run command '...'
2. Open page '...'
3. Click on '....'
4. See error

## 4. Expected vs. Actual Behavior
- **Expected:** What should have happened.
- **Actual:** What actually happened (include error messages or stack traces).

## 5. System Doctor Report
Please attach your scrubbed `doctor_report.txt` generated with:
```bash
jobhunt doctor --report
```
*(The report is automatically scrubbed of all API keys, secrets, email addresses, and resume text)*

## 6. Additional Context
Add any other context about the problem here (e.g. resume template type, scheduler backend).

