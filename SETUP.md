# Gmail setup (phase 4: job-alert emails)

This lets `jobhunt` read the job-alert emails that LinkedIn, Naukri, Internshala, Indeed, Wellfound, Unstop,
Foundit, Cutshort, Instahyre and Glassdoor send to **you@example.com**, so those sites never need to be
scraped. Access is **read-only** (`gmail.readonly`): the code has no call that can send, label, modify, trash or
delete anything, and refuses any token carrying another scope.

Time needed: about 15 minutes, once. Google's console labels change now and then; the intent of each step is
what matters.

## 0. Reminders before you start

- Set `user_agent_contact` in `config.yaml` (a URL or email). It is blank on purpose and is sent in the
  User-Agent of every request to job boards and APIs so operators can reach you. `jobhunt` warns on every
  run until it is set.
- Install the optional Gmail dependencies: `.\.venv\Scripts\python.exe -m pip install -e ".[gmail]"`.
- Secrets stay in `.secrets/` (gitignored). Never commit `credentials.json` or `token.json`.

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/> signed in as you@example.com.
2. Project picker (top bar) -> **New project** -> name it `jobhunt` -> **Create**, then select it.

## 2. Enable the Gmail API

1. **APIs & Services -> Library** -> search **Gmail API** -> **Enable**.

## 3. Configure the OAuth consent screen (Google Auth Platform)

1. **APIs & Services -> OAuth consent screen** (newer consoles: **Google Auth Platform**, with Branding /
   Audience / Data Access tabs).
2. **User type: External** (Internal is only for Google Workspace organizations).
3. App name `jobhunt`, user support email and developer contact: your address. No logo or domains needed.
4. **Scopes / Data Access -> Add or remove scopes** -> add exactly
   `https://www.googleapis.com/auth/gmail.readonly` ("View your email messages and settings").
   Google classifies it as a *restricted* scope; that matters only if you distribute the app (see step 7).
5. **Test users / Audience -> Add users** -> add **you@example.com** (yourself).
6. Leave **Publishing status = Testing** for now.

## 4. Create the OAuth client and download `credentials.json`

1. **APIs & Services -> Credentials -> Create credentials -> OAuth client ID**.
2. **Application type: Desktop app**, name `jobhunt-desktop` -> **Create**.
3. **Download JSON**, and save it as `jobhunt\.secrets\credentials.json` (create the folder if needed).

## 5. Authorize

```powershell
.\.venv\Scripts\python.exe -m jobhunt gmail-auth
```

A browser opens. Sign in as you@example.com. Because the app is unverified you will see **"Google hasn't
verified this app"**: choose **Advanced -> Go to jobhunt (unsafe)** (it is your own app) and allow
**View your email messages and settings**. The command then stores `.secrets\token.json` and checks that the
authorized account matches `email.account` in `config.yaml`; on a mismatch it deletes the token again.

Check it works (reads only; nothing is modified):

```powershell
.\.venv\Scripts\python.exe -m jobhunt ingest-email --since-days 3
```

## 6. Day-to-day use

```powershell
.\.venv\Scripts\python.exe -m jobhunt ingest-email
```

Each run lists messages newer than the stored position from the allowlisted senders (`email.platforms` in
`config.yaml`), skips messages already in `processed_emails`, parses the job cards, stores only the parsed
fields (title, company, location, link), and:

- links that already point at a supported ATS (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
  Recruitee) add the company to discovery and the posting is fetched through the ATS's public API;
- every other link is stored as the apply URL with `needs_description = 1`. It is never opened, not even to
  follow a tracking redirect.

### Tuning on real emails (once they arrive)

The parsers were written against synthetic fixtures modeled on each platform's typical layout, because no real
alert emails existed yet. When real ones arrive:

1. In Gmail open an alert -> **⋮ -> Download message** (saves an `.eml`).
2. Run it offline (no network, no database, nothing fetched):

   ```powershell
   .\.venv\Scripts\python.exe -m jobhunt parse-email path\to\alert.eml --platform linkedin
   ```

3. If a card is missed or a field is wrong, send me the redacted `.eml` or the command output and I will fix
   the exact line, and add it as a fixture.
4. Tighten the sender lists in `config.yaml` (`email.platforms.<name>.senders`) to the exact addresses you see.
   `"@domain.com"` matches the domain and its subdomains; a full address matches exactly.

## 7. Token lifetime: re-authorize weekly, or publish the app?

What Google's current documentation says (checked 2026-10-12):

- **Testing + External:** "Authorizations by a test user will expire seven days from the time of consent", and
  the refresh token expires with it, unless only name/email/profile scopes are requested. With
  `gmail.readonly` that means **`gmail-auth` every 7 days** (`jobhunt` then fails with a message telling you to).
- **In production:** the 7-day rule is stated only for the Testing status. A refresh token then lives until it is
  revoked, unused for six months, the password is changed (Gmail scopes), or the 100-token-per-client limit
  evicts it. A nightly run keeps it in use.
- **Verification:** `gmail.readonly` is a *restricted* scope, which Google requires to be verified (and security
  assessed) for apps that serve other users. An unverified app in production shows the "unverified app"
  warning and is capped at 100 new users in total. Neither limit matters for a single-user app.

**Recommendation: for personal use, move the app to production (unverified) once, and re-authorize once.**
It removes the weekly re-auth, you are the only user (the 100-user cap is irrelevant), and the unverified-app
screen is a click-through you already did in step 5.

1. **Google Auth Platform -> Audience -> Publish app** (or *OAuth consent screen -> Publish app*), confirm.
2. Delete `.secrets\token.json` and run `gmail-auth` again (tokens issued while in Testing keep their 7-day
   expiry).

Two honest caveats: (a) the pages I could read do not spell out whether Google will let a *restricted-scope,
unverified, production* app complete consent for its own developer; the click-through in step 5 is how this is
widely reported to work, but if Google blocks it, set the status back to **Testing** and re-run `gmail-auth`
weekly (it is one command, and a scheduled-task failure notice makes the need obvious); (b) the 6-month and
password-change rules still apply, so re-run `gmail-auth` if `ingest-email` ever reports an expired or revoked
token.

## 8. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Google rejected the refresh token` / `invalid_grant` | Run `gmail-auth` again (7-day Testing expiry, password change, or revoked access). |
| "Access blocked: jobhunt has not completed the Google verification process" | You are not in **Test users** (step 3.5), or you are signed in as another account. |
| `token belongs to X, config.yaml expects Y` | Authorized the wrong Google account; run `gmail-auth` again and pick Y. |
| `no token at ...` | Run `gmail-auth` first. |
| `credentials.json not found` | Step 4: save the downloaded client JSON as `.secrets\credentials.json`. |
| Nothing is picked up | Check the alert emails really come from an address matching `email.platforms.*.senders`; try `ingest-email --since-days 30`. |

## 9. Revoking access

<https://myaccount.google.com/permissions> -> *jobhunt* -> **Remove access**, and delete `.secrets\token.json`.
