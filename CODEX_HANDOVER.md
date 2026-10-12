# FixTrack handover

## Context

FixTrack is a Namibian repair-workshop website for device intake, repair progress, quotations, customer decisions, and spare-part inventory. The owner is developing this as a third-year NQF Level 6 IT project. Confirm assessment requirements and required stack before claiming this implementation meets them.

## Current implementation

- Frontend: plain HTML, CSS, and JavaScript; responsive desktop/mobile web plus an installable Android PWA (`manifest.webmanifest`, `service-worker.js`, SVG icon).
- Deployment/builds: `render.yaml` provisions Flask and PostgreSQL. `mobile/` contains a Capacitor web wrapper; `.github/workflows/mobile-builds.yml` builds an Android debug APK and iOS Simulator app using the `FIXTRACK_PUBLIC_URL` repository variable.
- Offline boundary: the service worker caches only the static app shell; never cache `/api/` responses, sessions, or repair/customer data. The UI may open offline, but all account and workshop work requires the backend connection.
- Backend: Python 3.12+ with Flask; install from `requirements.txt`. SQLite is used locally; setting `DATABASE_URL` switches to PostgreSQL via Psycopg.
- Database: local SQLite at `instance/fixtrack.sqlite3`, or PostgreSQL when deployed. No mock jobs or parts are seeded. Users/sessions are relational rows; workshop collections are stored in a versioned JSON workspace row.
- Startup: run `python server.py`; on first launch use the browser to create the first admin account. No default credentials. `python server.py create-admin` is an alternative bootstrap command.
- Auth: Werkzeug password hashes, random bearer tokens stored hashed in SQLite, signed HTTP-only session cookies, CSRF Origin checks, and API-level role checks.
- Passwords: signed-in users can change their password after confirming the current one. Admins can issue temporary passwords to other accounts; target sessions are revoked and the user must replace the temporary password before using the workspace. There is no email-based recovery flow; users who cannot sign in must contact an administrator.
- Roles: public signup requests customer access and remains pending until an administrator manually verifies the email with the customer. Admins create technician accounts and manage quotation/inventory. Technicians update repairs and consume stock. Customers see only repairs matching their registered email and can decide their quotations.
- Tests: `python -m unittest discover -s tests -v` exercises bootstrap, auth, password change/reset and session revocation, authorization, customer verification, quotations, repeated stock use, and persistence.

## Important limitations

The local Flask development server is not for internet exposure. Capacitor mobile shells load the hosted website; the iOS workflow creates a Simulator app, not a signed iPhone IPA. Physical iPhone testing needs Apple signing/TestFlight setup. Customer email verification is manual; automated password recovery emails, invitation links, multi-workshop tenancy, and production operational configuration are not implemented. The workspace JSON model should be normalized into domain tables with migrations if the project grows. Validate all workflow rules against the course brief, and do not claim this is production-ready.

## Next work

Review the actual course brief and required stack. Consider normalized repair/customer/quotation/part/stock-history tables, database migrations, deployment behind a production WSGI server with HTTPS, backups, automated customer email verification, and additional concurrency/security tests.
