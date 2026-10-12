# Deploy FixTrack for free using GitHub, Render and Neon

Repository: https://github.com/chahilapaulus1111-blip/FIXTRACK_PUBLIC_URL-

GitHub stores the code, Render serves the Flask API and website together, and Neon stores PostgreSQL data. GitHub Pages cannot run this Flask backend. The repository's render.yaml selects a free web service and does not create a paid Render database.

## 1. Push the project to GitHub

The Git repository is the FixTrack folder itself: server.py, requirements.txt, render.yaml and .python-version should be at the GitHub repository root. Do not set a Render root directory to FixTrack for this layout.

From this folder:

    git add render.yaml server.py app.js index.html service-worker.js .gitignore README.md DEPLOYMENT.md tests/test_server.py .github/workflows/backend-tests.yml
    git commit -m "Configure free Render hosting with Neon PostgreSQL"
    git push origin main

Do not upload instance/, secret.key, SQLite files, .env files, virtual environments or credentials.

## 2. Create a free Neon database

1. Sign up at https://console.neon.tech using your GitHub account if preferred.
2. Create a project named fixtrack on the Free plan.
3. Choose a database region close to your Render service. This Blueprint uses Oregon; choose a corresponding US West region in Neon when offered.
4. In Connect, select the database/role and enable connection pooling.
5. Copy the PostgreSQL connection URI, including sslmode=require and any other Neon-supplied parameters.

Keep this URI private. It contains the database password. Paste it only into Render's DATABASE_URL environment variable, never into source files, GitHub repository variables, screenshots or chat.

The app creates its tables on startup. A new Neon database starts empty; local SQLite data is not migrated automatically.

## 3. Deploy from GitHub to Render

### New service using the Blueprint

1. At https://dashboard.render.com, choose New > Blueprint.
2. Connect GitHub and select chahilapaulus1111-blip/FIXTRACK_PUBLIC_URL- on main.
3. Use render.yaml at the repository root.
4. When prompted for DATABASE_URL, paste the Neon connection URI.
5. Verify the only proposed resource is the Free fixtrack-api web service, then deploy.

Render generates SECRET_KEY and FIXTRACK_SETUP_TOKEN. The app refuses to start without a PostgreSQL URL when FIXTRACK_REQUIRE_POSTGRES=1, so it cannot silently save hosted records to a disposable SQLite file.

### Existing Render web service or manual setup

Use the existing service if it already hosts this app. Set its instance type to Free and update these settings:

| Setting | Value |
| --- | --- |
| Repository | chahilapaulus1111-blip/FIXTRACK_PUBLIC_URL- |
| Branch | main |
| Runtime | Python |
| Root directory | Leave blank |
| Build command | pip install -r requirements.txt |
| Start command | gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 60 |
| Health check | /healthz |
| DATABASE_URL | Private Neon PostgreSQL URI |
| SECRET_KEY | Stable random secret; preserve it on future deployments |
| FIXTRACK_SETUP_TOKEN | Private random setup token |
| FIXTRACK_REQUIRE_POSTGRES | 1 |
| FIXTRACK_COOKIE_SECURE | 1 |

For manual setup, generate each random secret locally with Python's secrets.token_urlsafe(32) and paste it into Render's Environment page. Do not commit or send the values in chat. Save settings and redeploy.

Changing DATABASE_URL switches databases; it does not copy existing records. If an existing service already contains data, back it up and plan migration before changing its URL. Keep existing databases until their data is accounted for.

## 4. Create the administrator

1. Wait for Render to report a successful deployment and open the exact HTTPS URL it returns.
2. In Render's service Environment page, reveal/copy FIXTRACK_SETUP_TOKEN.
3. Enter that token in FixTrack's first-admin setup form with your name, email and a unique password of at least 12 characters.
4. Once the admin exists, further setup attempts are refused. Keep the token private.
5. Create technician accounts from Accounts; customer signups require admin verification.

The setup token appears only during first-admin setup when configured. Local development without a token retains the existing setup flow.

## 5. Verify deployment

- GET /healthz returns status ok. This checks the web process without repeatedly querying the database.
- Sign in, create a repair and quotation, then sign in as its verified customer and respond.
- Sign in as the assigned technician and confirm repair/parts actions work.
- Redeploy and verify users and records remain in Neon.
- If using the mobile workflow, set the GitHub repository variable FIXTRACK_PUBLIC_URL to the real HTTPS origin returned by Render. Never put DATABASE_URL or secrets there.

GitHub Actions runs the backend workflow tests on pushes and pull requests. A healthy process alone does not prove every database operation works; perform the signed-in workflow checks above after deployment.

## Free plan limits

Render free web services sleep after 15 minutes of inactivity and may take about a minute to wake. Render's free PostgreSQL databases expire after 30 days, so this configuration uses Neon instead. Neon Free has usage/storage limits and may suspend compute when its allowance is exhausted. Review the current dashboards before choosing paid upgrades.

Sources:
- https://render.com/docs/free
- https://render.com/docs/blueprint-spec
- https://neon.com/pricing
