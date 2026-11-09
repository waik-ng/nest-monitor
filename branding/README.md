# Branding pages for Google OAuth — Nest Monitor

These two files are the homepage + privacy policy Google asks for on the
Branding page before it lets you publish the app to production (which is
what stops the 7-day refresh-token expiry).

- `index.html` — homepage: describes what Nest Monitor does, links to the
  privacy policy.
- `privacy.html` — privacy policy: what thermostat data is read, where it
  is stored (locally on your Pi), and that it is not shared/sold.

## Publish them (5 minutes, free)

Yes — create a **new public repository** on GitHub for these two files.
A separate repo keeps the URLs clean and is the simplest way to get a
public homepage Google will accept.

1. On GitHub, create a new repository named `nest-monitor`, set to
   **Public**, with no README / .gitignore / license added (or add them,
   it doesn't matter — you will upload these files after).
2. Upload `index.html` and `privacy.html` to the **root** of that repo
   (Add file > Upload files, or `git push` them).
3. In the repo, go to **Settings > Pages**:
   - Source: **Deploy from a branch**
   - Branch: **main**, folder: **/(root)**
   - Save. Wait ~1 minute.
4. Your URLs will be:
   - Homepage: `https://<your-github-username>.github.io/nest-monitor/`
   - Privacy: `https://<your-github-username>.github.io/nest-monitor/privacy.html`
   - Replace `<your-github-username>` with your actual GitHub username.

## Fill in Google Cloud Branding

In Google Cloud Console > APIs & Services > OAuth consent screen > Branding:

- App name: `Nest Monitor`
- User support email: your Gmail (from the dropdown)
- App logo: leave blank (uploading one triggers brand verification)
- Authorized domains: add `<your-github-username>.github.io` first
- Application home page: `https://<your-github-username>.github.io/nest-monitor/`
- Application privacy policy link: `https://<your-github-username>.github.io/nest-monitor/privacy.html`
- Terms of service: leave blank
- Developer contact information: your Gmail

Click **Save**, then go to **Audience** > **Publish app** > confirm.
Status should change from Testing to **In production**.

## After publishing — important

Publishing does NOT fix a refresh token minted while the app was in
Testing — that token keeps its 7-day expiry. Re-run this to get a fresh
token, then update `SDM_REFRESH_TOKEN` in the Pi's `.env`:

```bash
python3 get_refresh_token.py --project-id <UUID> \
  --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>
```

You do not need to submit for Google verification — this is personal use
only, under 100 users. You will see an "unverified app" warning at
sign-in; click Advanced > Go.
