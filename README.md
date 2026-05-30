# FareWatch

A flight fare monitoring tool. Set up watches on specific routes and get email alerts when prices drop to your target.

---

## How to run locally

### 1. Create and activate a virtual environment

A virtual environment keeps this project's dependencies isolated from the rest of your computer.

```bash
cd farewatch
python3 -m venv venv
source venv/bin/activate
```

You'll know it's active when you see `(venv)` at the start of your terminal prompt.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up your `.env` file

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Then open `.env` and fill in each variable (see below).

### 4. Set up the database tables in Supabase

Go to your [Supabase SQL editor](https://supabase.com/dashboard/project/qelanerqtfzqsgyddfmw/sql/new) and run this SQL:

```sql
-- Watches table
create table watches (
  id uuid primary key default gen_random_uuid(),
  origin text not null,
  destination text not null,
  date_from date not null,
  date_to date not null,
  passengers integer not null default 1,
  target_price numeric(10, 2) not null,
  client_name text not null,
  client_email text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

-- Price history table
create table price_history (
  id uuid primary key default gen_random_uuid(),
  watch_id uuid not null references watches(id) on delete cascade,
  price numeric(10, 2) not null,
  currency text not null default 'USD',
  checked_at timestamptz not null default now()
);
```

### 5. Start the app

```bash
python app.py
```

Then open your browser and go to: **http://localhost:5000**

Log in with the password you set in `APP_PASSWORD`.

---

## Environment variables

| Variable | What it does |
|---|---|
| `SUPABASE_URL` | The base URL of your Supabase project, e.g. `https://abcdef.supabase.co` |
| `SUPABASE_ANON_KEY` | The public/anon API key from Supabase — found in Project Settings → API |
| `APP_PASSWORD` | The password you'll use to log into FareWatch — choose anything you like |
| `FLASK_SECRET_KEY` | A long random string used to secure login sessions — change this before deploying |

---

## Project structure

```
farewatch/
├── app.py               # Main Flask application
├── requirements.txt     # Python dependencies
├── .env                 # Your secrets (never commit this)
├── .env.example         # Template showing which variables are needed
└── templates/
    ├── base.html        # Shared layout (nav, flash messages)
    ├── login.html       # Password login page
    ├── index.html       # List of active watches
    └── add_watch.html   # Form to add a new watch
```

---

## Deploying to Render (later)

When you're ready to deploy, set all four environment variables in Render's dashboard under **Environment**. Do not upload your `.env` file — Render reads them directly from its settings.
