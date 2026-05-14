# GPT Rollout Health Dashboard — Local Installation

Single-laptop setup. Two terminal commands + one browser tab. No Netlify, no
cloud, no team infrastructure.

## Prerequisites (one-time)

```bash
# 1. macOS Command Line Tools (only if `git` / `curl` aren't already there):
xcode-select --install

# 2. Python 3.11 or newer (3.13 is what's tested):
python3 --version          # if < 3.11, install via Homebrew:
brew install python@3.13

# 3. AWS CLI v2 (needed by the bridge for IAM auth + CloudWatch):
brew install awscli
aws --version              # expect: aws-cli/2.x

# 4. Configure AWS credentials (skip if `aws sts get-caller-identity` already works):
aws configure              # or `aws sso login` if your org uses SSO
```

For Pinot fetches you'll also need **Cisco AnyConnect** connected when the
bridge runs (it inherits your laptop's network routes).

## Step 1 — Clone the repo

```bash
git clone <repo-url> lambda_performance_calculator
cd lambda_performance_calculator
```

## Step 2 — Python virtualenv + dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Installs: `pandas`, `matplotlib`, `boto3`, `pymysql`. Nothing else.

## Step 3 — Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` to look like this (fill in `RDS_DATABASE`):

```dotenv
AWS_REGION=us-west-1

# MySQL (RDS read replica) — used by the MySQL fetch tab
RDS_HOST=encrypted-db-replica-reports.cth9abmhwqni.us-west-2.rds.amazonaws.com
RDS_PORT=3306
RDS_USER=<your IAM-enabled MySQL user>
RDS_REGION=us-west-2
RDS_DATABASE=<your db name — the one Sequel Ace selects after connecting>

# Bridge
BRIDGE_PORT=8765

# Pinot (StarTree) — used by the Pinot fetch tab
PINOT_BASE_URL=https://pinot.05sqy3.cp.s7e.startree.cloud
# PINOT_AUTH_TOKEN=    # optional; you can paste the JWT in the browser instead
```

## Step 4 — Start the bridge

```bash
# In one terminal, with the venv activated:
python3 scripts/local_data_bridge.py
```

Expected output:

```text
Local data bridge listening on http://127.0.0.1:8765
Bridge token: <32-char hex>
  Paste it into the dashboard's 'Bridge token' field.
Datasets: mysql.adoption, lambda.cloudwatch, pinot.latency
```

**Keep this terminal open.** A fresh token is generated on every restart.

> If you'll use the **Pinot** tab: connect Cisco AnyConnect now. The RDS and
> Pinot endpoints are not reachable from off-VPN.

## Step 5 — Open the dashboard

```bash
# In a second terminal (or just via Finder):
open dashboards/gpt_rollout_dashboard.html
```

The page opens in your default browser.

## Step 6 — Paste tokens (per bridge restart)

In the **Bridge connection** bar at the top:

1. **Bridge URL** — already `http://127.0.0.1:8765`, leave as-is.
2. **Bridge token** — paste the 32-char hex from your terminal.

The status pill turns green: `Bridge v1.0 • 3 datasets`. Both values persist
in browser `localStorage` until you click Reset.

## Step 7 — Fetch and generate

For each section you want in the report:

- **MySQL adoption CSV** → *Fetch from RDS* tab → click **Fetch adoption metrics from RDS**.
- **Lambda CloudWatch CSV** → *Fetch from bridge* tab → click **Fetch lambda metrics from CloudWatch**. Takes 30-90s.
- **Pinot latency CSV** → *Fetch from bridge* tab → paste your **Pinot bearer token** (the JWT from your `curl --header 'authorization: Bearer …'` line; rotates every ~24h) → click **Fetch latency metrics from Pinot**.

Click **Generate dashboard**. Use the download buttons (PNG / HTML / CSV) as
needed.

---

## Daily cheatsheet

After the one-time setup, your daily flow is just:

```bash
cd lambda_performance_calculator
source venv/bin/activate
python3 scripts/local_data_bridge.py        # copy the new bridge token
```

```bash
open dashboards/gpt_rollout_dashboard.html   # paste new bridge token, fetch, generate
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: python3` | `brew install python@3.13` and reopen the terminal. |
| `ModuleNotFoundError: No module named 'boto3'` (or `pymysql`) | You forgot to `source venv/bin/activate` before running the bridge. |
| Red "Bridge not reachable" pill | Bridge isn't running, or `BRIDGE_PORT` in `.env` doesn't match what's pasted in the Bridge URL field. Restart the bridge and re-paste the token. |
| 401 "Missing or invalid X-Bridge-Token" | Bridge token changes on every launch. Copy the latest one from the terminal and paste it into the Bridge connection bar. |
| MySQL fetch fails with `ExpiredToken` / `InvalidClientTokenId` | `aws sso login` (or your usual login command) **in the same terminal that runs the bridge**, then restart the bridge. |
| MySQL fetch returns empty rows | The window is correct but traffic was idle. Bump **Last N days** to `7` or wider. |
| Pinot fetch fails with `Could not reach Pinot at https://…` | Cisco AnyConnect VPN isn't connected. |
| Pinot fetch returns 401 | Pinot JWT expired (~24h lifetime). Mint a fresh one and paste it into the **Pinot bearer token** field. |
| Lambda fetch shows "ran the collector but it failed" | Open the bridge terminal and read the tail of the stderr — almost always an AWS-credential or missing-Lambda issue, surfaced verbatim. |
| `Operation not permitted` reading artifacts | macOS sandbox quirk — grant Terminal "Full Disk Access" in System Settings → Privacy & Security. |

---

## What gets installed and where

| Thing | Location |
|---|---|
| Python venv | `./venv/` (gitignored) |
| Bridge runtime deps | `pandas`, `matplotlib`, `boto3`, `pymysql` (per `requirements.txt`) |
| Bridge token | Regenerated per launch, printed to stderr, never written to disk |
| Pinot bearer token | Browser `localStorage` only (key: `gptRollout.pinot.token`) |
| Bridge URL + token in browser | Browser `localStorage` (`gptRollout.bridge.*`) |
| AWS RDS CA bundle | Auto-downloaded once to `~/.aws/rds/global-bundle.pem` |
| Generated CloudWatch artifacts | `./artifacts/cloudwatch-review/<run-id>/` (gitignored) |
| Generated reports / CSVs | `./reports/` (gitignored) |

Nothing leaves the laptop. Browser only ever talks to `http://127.0.0.1:<port>`;
the bridge is the only thing that reaches out to AWS and Pinot.
