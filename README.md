# Weekly Activity Report — Setup & Usage Guide

## What this tool does

Every Monday morning (or whenever you run it), this tool:

1. Reads your **Google Calendar** for the past week's meetings
2. Pulls **Gong and Zoom call summaries** for those meetings from Snowhouse
3. Fetches **Salesforce opportunity and use case data** linked to your meetings
4. Produces a single **Markdown report** grouped by customer, showing meeting notes, summaries, next steps, and deal context

Reports are saved as text files in the `output/` folder and can be opened in
any text editor, Obsidian, Typora, VS Code, or similar app.

---

## Before you start — what you need

You will need the following before setting up this tool. Collect these items
first, then follow the steps below.

| What | Where to get it |
|---|---|
| Python 3.11 or newer | https://www.python.org/downloads/ |
| Google Cloud SDK (`gcloud`) | https://cloud.google.com/sdk/docs/install-sdk (or let `setup.sh` install it — see Step 3) |
| Your Snowflake account details | See Step 2 below |

---

## Step 1 — Check Python is installed

Open the **Terminal** app on your Mac. You can find it by pressing
`Command + Space` and typing "Terminal".

In Terminal, type the following and press Enter:

```
python3 --version
```

You should see something like `Python 3.12.4`. If the version number starts
with `3.11` or higher, you are good.

If you see `command not found` or a version lower than 3.11:
1. Go to https://www.python.org/downloads/
2. Click **Download Python 3.x.x** (the big yellow button)
3. Open the downloaded file and follow the installer
4. Close Terminal, open a new Terminal window, and repeat the check above

---

## Step 2 — Configure your Snowflake credentials

This tool connects to Snowhouse (Snowflake's internal data warehouse) to pull
your Gong call summaries and Salesforce data.

**2a.** Copy the credentials template:

```
cp .secrets.example .secrets/snowhouse.env
```

**2b.** Open the file in a text editor:

```
open -a TextEdit .secrets/snowhouse.env
```

**2c.** Fill in your values. Here is what each line means:

```
SNOWHOUSE_SNOWFLAKE_ACCOUNT=SFCOGSOPS-SNOWHOUSE_AWS_US_WEST_2
```
> The Snowhouse account identifier. **Do not change this** — it is the same
> for all Snowflake employees using Snowhouse.

---

```
SNOWHOUSE_SNOWFLAKE_USER=your.name@snowflake.com
```
> **Change this** to your Snowflake work email address.
> Example: `john.smith@snowflake.com`

---

```
SNOWHOUSE_SNOWFLAKE_WAREHOUSE=SE_WH
```
> The compute warehouse to run queries on. `SE_WH` is the standard SE
> warehouse. Change this only if you have been told to use a different one.

---

```
SNOWHOUSE_SNOWFLAKE_DATABASE=
SNOWHOUSE_SNOWFLAKE_SCHEMA=
```
> Leave these **blank**. The SQL queries in this tool use fully-qualified table
> names (e.g. `SALES.SE_REPORTING.DIM_SE_ACTIVITY`) and do not depend on a
> default database or schema.

---

```
SNOWHOUSE_SNOWFLAKE_ROLE=
```
> Leave this **blank** to use your default Snowhouse role. Only fill this in
> if you have been advised to use a specific role (e.g. `SE_ANALYST`).

---

```
# SNOWFLAKE_SE_NAME=John Doe
```
> Optional. Your full name as it appears in **Gong and Salesforce** (used to
> filter your calls from the data). If you leave this commented out, the tool
> automatically converts your email to a name:
> `john.smith@snowflake.com` → `John Smith`
>
> **Only uncomment and set this** if your display name in Gong is different
> from what would be derived from your email. For example, if your email is
> `jsmith@snowflake.com` but you appear in Gong as `John Smith`, set:
> `SNOWFLAKE_SE_NAME=John Smith`
>
> To uncomment a line, remove the `#` at the start.

---

```
# SNOWHOUSE_SNOWFLAKE_PASSWORD=
# SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR=snowflake
```
> Leave these **commented out** for normal use. When running interactively,
> the tool will open your browser for Okta SSO login automatically.
>
> Only uncomment these if you want the report to run automatically on a
> schedule without any browser popups (see the Scheduling section below).

---

## Step 3 — Set up Google Calendar access

This is handled automatically when you run `bash setup.sh` in Step 4.

> **You do not need a Google Cloud account, project, or any credentials from
> the Google Cloud Console.** This tool uses your personal Google login
> (the same `@snowflake.com` account you use for Gmail and Google Calendar)
> via the `gcloud` command-line tool. No project setup, no API keys, no
> billing — just a one-time browser sign-in.

During setup you will be asked two questions:

**If Google Cloud SDK is not installed:**
> "Would you like to download and install it now? [Y/n]"

Type `Y` and press Enter. The installer will download (~100 MB) and set it up
for you automatically — no manual steps required.

**To authorise your Google Calendar:**
> "Ready to open the browser? [Y/n]"

Type `Y` and press Enter. A browser window will open — sign in with your
**Snowflake Google account** (`@snowflake.com`) and click **Allow**.

Both of these only happen once. On every subsequent run, setup skips these
steps automatically.

---

## Step 4 — Run setup

In Terminal, navigate to the `time tracking` folder. If you saved it in your
Documents folder, type:

```
cd ~/Documents/time\ tracking
```

Then run:

```
bash setup.sh
```

This will:
- Check your Python version ✓
- Create a Python virtual environment (isolated workspace for this tool) ✓
- Install all required packages using pip ✓

**You only need to run this once.** It is safe to run again at any time if
something goes wrong — it skips steps that are already done.

---

## Step 5 — Run the report

```
bash run.sh
```

**The first time only:** A browser window will open asking you to sign in to
your Google account and grant access to your calendar. Follow the prompts,
then return to Terminal.

The tool will then:
1. Connect to Snowhouse (your browser may open for Okta SSO)
2. Fetch last week's meetings from your Google Calendar
3. Pull Gong/Zoom summaries and Salesforce data from Snowhouse
4. Write a report to the `output/` folder

The report file will be named `week_YYYY-MM-DD.md` (e.g. `week_2025-01-06.md`)
where the date is the Monday of the week that was reported on.

---

## Regular weekly usage

Once setup is complete, the only command you need every week is:

```
bash run.sh
```

Run it from the `time tracking` folder. The tool automatically determines
"last week" (Monday through Sunday) and pulls data for that window.

### Reporting on a specific week

If you need a report for a week other than the most recent one, pass the
**Monday date** of that week using `--week`:

```
bash run.sh --week 2025-03-10
```

The date must be the **Monday** of the week you want, in `YYYY-MM-DD` format.

### Viewing your report

Open the output file in any text editor or Markdown viewer:

```
open output/week_2025-01-06.md
```

For the best experience, use an app that renders Markdown formatting:
- **Obsidian** (free) — https://obsidian.md
- **Typora** — https://typora.io
- **VS Code** — open the file, then press `Cmd+Shift+V` for a preview

---

## Scheduling automatic Monday morning reports (optional)

To have the report run automatically every Monday at 8am without any manual
steps:

**1.** Create the scheduler file. In Terminal:

```
nano ~/Library/LaunchAgents/com.weekly-report.plist
```

**2.** Paste the following, replacing `/path/to/time tracking` with the actual
path where you saved this folder (e.g. `/Users/yourname/Documents/time tracking`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.weekly-report</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/path/to/time tracking/run.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>8</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/path/to/time tracking/output/run.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/time tracking/output/run.log</string>
</dict>
</plist>
```

**3.** Press `Ctrl+X`, then `Y`, then `Enter` to save.

**4.** Activate the schedule:

```
launchctl load ~/Library/LaunchAgents/com.weekly-report.plist
```

**Important:** For automated (unattended) runs, the Snowflake browser popup
cannot open. You must store your password in `.secrets/snowhouse.env`:

```
SNOWHOUSE_SNOWFLAKE_PASSWORD=your-snowflake-password
```

The Google Calendar token is cached after your first manual run and refreshes
automatically — no browser popup needed for GCal after that.

---

## Configuration reference

All settings live in `.secrets/snowhouse.env`. Here is the complete reference:

| Variable | Required | Description |
|---|---|---|
| `SNOWHOUSE_SNOWFLAKE_ACCOUNT` | Yes | Snowhouse account ID — `SFCOGSOPS-SNOWHOUSE_AWS_US_WEST_2` for all Snowflake employees |
| `SNOWHOUSE_SNOWFLAKE_USER` | Yes | Your Snowflake work email — e.g. `john.smith@snowflake.com` |
| `SNOWHOUSE_SNOWFLAKE_WAREHOUSE` | Yes | Compute warehouse — `SE_WH` for SEs |
| `SNOWHOUSE_SNOWFLAKE_DATABASE` | No | Leave blank — queries use fully-qualified names |
| `SNOWHOUSE_SNOWFLAKE_SCHEMA` | No | Leave blank — queries use fully-qualified names |
| `SNOWHOUSE_SNOWFLAKE_ROLE` | No | Leave blank to use your default role |
| `SNOWFLAKE_SE_NAME` | No | Your display name in Gong/Salesforce. Auto-derived from email if not set |
| `SNOWHOUSE_SNOWFLAKE_PASSWORD` | No | Only needed for scheduled (unattended) runs |
| `SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR` | No | Only needed for scheduled runs — set to `snowflake` when using a password |
| `INTERNAL_DOMAINS` | No | Comma-separated email domains treated as internal (default: `snowflake.com`) |

---

## Folder structure

```
time tracking/
├── run.sh                  ← Run this every week to generate your report
├── setup.sh                ← Run this once during setup
├── weekly_report.py        ← Main report logic (no need to edit)
├── snowhouse_week.sql      ← Snowhouse data query (no need to edit)
├── requirements.txt        ← Python package list (used by setup.sh)
├── .secrets.example        ← Credentials template — copy this to get started
├── .secrets/               ← Your private credentials (you create this in Step 2)
│   └── snowhouse.env           ← Snowflake connection details (required)
├── .venv/                  ← Python environment (created by setup.sh, do not edit)
├── lib/                    ← Bundled code libraries (do not edit)
└── output/                 ← Your generated reports appear here
```

---

## Troubleshooting

**"Python 3.11 or newer was not found"**
→ Install Python 3 from https://www.python.org/downloads/, then open a fresh
Terminal window and run `bash setup.sh` again.

**"Snowflake credentials not found"**
→ The file `.secrets/snowhouse.env` does not exist. Run:
`cp .secrets.example .secrets/snowhouse.env`
Then open it and fill in your details.

**"I don't have access to create credentials / projects in Google Cloud Console"**
→ You don't need to. This tool never asks you to create a Google Cloud project
or visit the Google Cloud Console. It uses **Application Default Credentials**
— a simple personal login via the `gcloud` command-line tool that is already
bundled with the Google Cloud SDK. Just run `bash setup.sh` and follow the
browser prompt to sign in with your `@snowflake.com` Google account. That's
all that is required.

**"Google Calendar credentials not found"**
→ The tool uses Application Default Credentials. Run:
`gcloud auth application-default login`
and sign in with your Snowflake Google account.

**Report runs but shows no Gong summaries**
→ Check that `SNOWFLAKE_SE_NAME` in your `.secrets/snowhouse.env` matches
exactly how your name appears in Gong (check a Gong call recording to confirm).
For example, if Gong shows "Jonathan Smith" but your email is `jon.smith@...`,
add: `SNOWFLAKE_SE_NAME=Jonathan Smith`

**"externalbrowser" / Okta error on Snowflake login**
→ Make sure you are signed in to your Snowflake Okta account in your default
browser *before* running the report. If you are signed in as a personal Google
account in that browser, sign out first.

**Report shows meetings but wrong customers**
→ Customer attribution is inferred from meeting titles and Gong data. If a
specific customer is consistently wrong, check that the meeting titles in your
calendar include the customer name.

**The report is empty or only shows a few meetings**
→ The tool only reports on meetings that have at least one attendee in your
calendar. Blocked time, focus time, and OOO events are excluded automatically.
