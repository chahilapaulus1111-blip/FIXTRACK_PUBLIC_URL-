# FixTrack Namibia

FixTrack is a responsive repair-workshop website backed by a Python Flask API. Local development uses SQLite; production can use PostgreSQL through `DATABASE_URL`.

## Run locally

Use Python 3.12 or newer. From this folder, create and activate a virtual environment, install dependencies, and start the server:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python server.py
```

If the Windows `py` launcher is unavailable, use the installed Python executable to create the virtual environment. Open http://127.0.0.1:8000. The server is bound to this computer only by default.

## Android and web access

FixTrack is a website first: users can open its URL in a browser without installing an app. On a supported Android browser over HTTPS, use **Install app** (or the browser menu) to add the same website to the home screen as a PWA. It is not a Play Store APK or a separate native Android project. The cached app shell can open offline, but sign-in, repairs, and other server data require a connection to the backend.

The current local server is bound to this computer and is not reachable from other phones or users. Publish the backend at an HTTPS address before sharing the website or installing it on other devices.

## Render and mobile test builds

The `render.yaml` Blueprint selects a **free Render web service** with an external **Neon PostgreSQL database**. Push the project to GitHub, create a free Neon project, then connect the repository in Render and provide the private Neon URI as `DATABASE_URL`. Render generates the session secret and an administrator setup token. See [DEPLOYMENT.md](DEPLOYMENT.md) for the complete setup and existing-service instructions.

For native test wrappers, set the GitHub repository variable `FIXTRACK_PUBLIC_URL` to the deployed HTTPS origin (for example, `https://fixtrack-api.onrender.com`). Run **Actions → Mobile test builds → Run workflow**. The workflow uploads an Android debug APK and an iOS Simulator `.app` artifact. These Capacitor shells load the hosted website, so the web URL must be available.

The Android debug APK can be sideloaded for testing. The iOS simulator build requires a Mac/Xcode simulator; installing on a physical iPhone requires Apple Developer signing and TestFlight configuration. These are test builds, not store releases.

On first launch, create the administrator account in the browser. Hosted setup also requires the private `FIXTRACK_SETUP_TOKEN` from Render's Environment page. Choose a unique password of at least 12 characters. There are no default credentials. The admin can create technician accounts and verify customer accounts from **Accounts**; customer signups cannot grant themselves staff or admin access.

## Data and access

Local accounts, repairs, quotations, inventory, and repair history are stored in `instance/fixtrack.sqlite3`, not in browser storage. With `DATABASE_URL`, the same server uses PostgreSQL. Passwords are stored as Werkzeug password hashes. The server issues signed, HTTP-only session cookies and checks roles on API requests.

New repairs must include the customer's registered email so that only that customer account can view the repair and approve or decline its quotation. Customer signups remain pending until an administrator manually verifies the email with the customer. Administrators manage quotations and inventory; technicians can update repair work and record used parts; customers can view their own repairs and respond to quotations.

Signed-in users can change their own password. Administrators can issue a temporary password to another account; that user must replace it before continuing. If a user cannot sign in, they must contact their administrator. No password-reset or verification emails are sent.

While signed in, the visible app checks for updates every five seconds and when the tab regains focus. Refreshing pauses while a dialog is open, a field is focused, or a save is in progress, so unfinished edits are preserved. Customers see their linked repairs and pending quotation decisions on their home screen.

No fictional sample records are inserted. Create users and records on the running website.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The browser regression check uses an isolated SQLite database and two separate sessions. With Node.js and Chrome installed, install Playwright outside the application folder and provide its module path:

```powershell
npm install --prefix ../.tools/browser-check playwright
$env:PLAYWRIGHT_MODULE = (Resolve-Path ../.tools/browser-check/node_modules/playwright).Path
$env:CHROME = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
# Set PYTHON to the Python executable path if python is not on PATH.
node tests/browser_sync.cjs
```

It checks automatic quotations, customer decisions, account registrations, preserved search and form edits, delayed refresh responses, and logout cleanup. Port 5087 must be available.

## Deployment note

The built-in Flask server is for local development only. Before exposing FixTrack on a network or the internet, deploy behind a production WSGI server with HTTPS, set `FIXTRACK_COOKIE_SECURE=1`, configure backups and operational monitoring, and review the deployment's security requirements. Automated password recovery, email delivery, and invitation links are not implemented.
