# Deploying to Railway

> What the deploy needs, and the two things that are easy to get wrong.
> Written 2026-08-22, alongside W2.

---

## The two things that are easy to get wrong

### 1. There is no `MPESA_CALLBACK_URL`

The M-Pesa callback URL is **derived, not configured**:

```
APP_BASE_URL  +  /payments/mpesa/callback
```

`CALLBACK_PATH` is fixed in `app/api/payments.py` because it is registered with
Safaricom, and a URL that silently changed would strand every in-flight payment.

So set **`APP_BASE_URL`** to the public Railway URL, with **no trailing slash**,
and the callback follows. Setting an `MPESA_CALLBACK_URL` variable does nothing
— `config.py` is the only file that reads the environment, and it does not
define one.

`APP_BASE_URL` also builds `og:image`, so getting it wrong costs the WhatsApp
link preview as well as the payments.

### 2. `TEST_DATABASE_URL` must never equal `DATABASE_URL`

The suite refuses to run if they match, because it drops and recreates tables.
On Railway you set only `DATABASE_URL`. Leave `TEST_DATABASE_URL` unset there.

---

## What the build does

`railway.json` at the repo root drives it:

| Step | Command |
|---|---|
| Build | Nixpacks default — `pip install -r requirements.txt` |
| Start | `cd backend && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Healthcheck | `/health` |

### Why there is a `requirements.txt` at the repo root

**Nixpacks decides which toolchain to install by looking for markers at the
repository root**, and the application lives in `backend/`. With nothing at the
root it builds a generic image containing no Python at all, and the first build
step dies with:

```
/bin/bash: line 1: pip: command not found
exit code: 127
```

That is not a dependency problem — it is Nixpacks never having installed Python.

So the root holds two markers:

- **`requirements.txt`** — one line, `-r backend/requirements.txt`. A pointer,
  not a second list. Versions stay pinned in exactly one file.
- **`.python-version`** — `3.11`, matching `pyproject.toml`.

`railway.json` deliberately sets **no `buildCommand`**. Nixpacks' own Python
install step is what proves the toolchain is present; overriding it was how the
missing interpreter got hidden in the first place.

> **The alternative, if you prefer it:** set the service's **Root Directory** to
> `backend` in the Railway dashboard. Nixpacks then sees `backend/` as the root,
> detects Python from `backend/requirements.txt`, and the two root markers become
> unnecessary — but the start command must drop its `cd backend &&`, and the
> setting lives in the dashboard rather than in the repo. Keeping it in
> `railway.json` means the deploy is reproducible from the code alone.

**Migrations run on every boot, before the server starts.** Alembic is a no-op
when the database is already at head, so this is safe to repeat — and it means a
deploy can never serve a schema the code does not expect.

`/health` is liveness only and does not touch the database; `/health/ready`
does. The healthcheck uses the first deliberately, so a brief database blip does
not cause Railway to kill a healthy container.

---

## Variables to set on Railway

Set these in the service's **Variables** tab. **Never commit them** — `.env` is
gitignored and must stay that way.

### Required

| Variable | Value |
|---|---|
| `APP_ENV` | `prod` |
| `APP_BASE_URL` | `https://<your-app>.up.railway.app` — no trailing slash |
| `SECRET_KEY` | A long random string. **See the warning below.** |
| `DATABASE_URL` | Paste whole from the Postgres service |

### M-Pesa (W2)

| Variable | Value |
|---|---|
| `DARAJA_ENVIRONMENT` | `sandbox` until a real shortcode is live |

> The `DARAJA_*` key/secret/shortcode/passkey variables are for **our own**
> sandbox testing. In production the credentials used for an STK push are the
> **seller's**, stored encrypted on their `PaymentMethod` — we are never in the
> money path. See `docs/PRODUCTION_PLAN.md` §3.

### WhatsApp (W3, not yet built)

`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN`,
`WHATSAPP_APP_SECRET`. Nothing reads these yet.

---

## ⚠️ `SECRET_KEY` is now load-bearing for money

It was only session tokens before. It now also derives the Fernet key that
encrypts sellers' Daraja credentials — see `app/secrets_vault.py`.

**Changing it invalidates every stored credential**, and each affected seller
must re-enter theirs. Key rotation is not implemented; it is called out in that
module rather than quietly omitted. Set it once, back it up, and leave it alone.

A seller on Pochi la Biashara is unaffected: they hold no credentials, because
Daraja cannot push to Pochi at all.

---

## After the first deploy

1. Confirm `https://<app>/health` returns 200.
2. Confirm `https://<app>/health/ready` returns 200 — that proves the database
   connected and migrations ran.
3. Put `APP_BASE_URL` in, redeploy, and check a shop page's `og:image` renders
   an absolute URL.
4. Register the callback with Safaricom as
   `https://<app>/payments/mpesa/callback`.

**Safaricom cannot reach `localhost`.** That is the whole reason a deploy has to
come before the first sandbox STK round-trip; a tunnel works too, but the URL
changes each time and has to be re-registered.

---

## Not deployed yet

The **worker** (`python -m app.worker`) drains the background job queue. It is
a second Railway service pointed at the same repo and database, with the start
command `cd backend && python -m app.worker`. It is **not needed for W1 or W2** —
nothing in the storefront, checkout or payment path is queued — and the jobs it
serves belong to the parked analytics work. Add it when W3 needs media
downloads.
