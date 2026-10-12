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

The `render.yaml` Blueprint describes a Render web service and persistent PostgreSQL database. Its selected plans are paid; review Render's current pricing before creating the Blueprint. Push the project to GitHub, then create a Render Blueprint from that repository. After the first deployment, create the admin account on the Render URL.

For native test wrappers, set the GitHub repository variable `FIXTRACK_PUBLIC_URL` to the deployed HTTPS origin (for example, `https://fixtrack-api.onrender.com`). Run **Actions → Mobile test builds → Run workflow**. The workflow uploads an Android debug APK and an iOS Simulator `.app` artifact. These Capacitor shells load the hosted website, so the web URL must be available.

The Android debug APK can be sideloaded for testing. The iOS simulator build requires a Mac/Xcode simulator; installing on a physical iPhone requires Apple Developer signing and TestFlight configuration. These are test builds, not store releases.

On first launch, create the administrator account in the browser. Choose a unique password of at least 12 characters. There are no default credentials. The admin can create technician accounts and verify customer accounts from **Accounts**; customer signups cannot grant themselves staff or admin access.

## Data and access

Local accounts, repairs, quotations, inventory, and repair history are stored in `instance/fixtrack.sqlite3`, not in browser storage. With `DATABASE_URL`, the same server uses PostgreSQL. Passwords are stored as Werkzeug password hashes. The server issues signed, HTTP-only session cookies and checks roles on API requests.

New repairs must include the customer's registered email so that only that customer account can view the repair and approve or decline its quotation. Customer signups remain pending until an administrator manually verifies the email with the customer. Administrators manage quotations and inventory; technicians can update repair work and record used parts; customers can view their own repairs and respond to quotations.

Signed-in users can change their own password. Administrators can issue a temporary password to another account; that user must replace it before continuing. If a user cannot sign in, they must contact their administrator. No password-reset or verification emails are sent.

No fictional sample records are inserted. Create users and records on the running website.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Deployment note

The built-in Flask server is for local development only. Before exposing FixTrack on a network or the internet, deploy behind a production WSGI server with HTTPS, set `FIXTRACK_COOKIE_SECURE=1`, configure backups and operational monitoring, and review the deployment's security requirements. Automated password recovery, email delivery, and invitation links are not implemented.
