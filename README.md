# gmail-thunderbird-analysis

Analyze your Gmail account from the command line — sender frequency, storage usage, message volume over time, and more.

## How it works

Gmail's web interface and API don't give you easy access to bulk analytics. This tool takes a different approach: it reads the local mail cache that Thunderbird maintains on your machine, which is stored in standard [mbox format](https://en.wikipedia.org/wiki/Mbox) and contains your full account history.

**You need to sync your Gmail to Thunderbird first.** Once Thunderbird has downloaded your mail, this script runs entirely offline against the local cache — no API keys, no OAuth, no rate limits.

## Setup

### 1. Install Thunderbird and sync your Gmail

1. Download and install [Thunderbird](https://www.thunderbird.net)
2. Add your Gmail account (File → New → Existing Mail Account)
3. In Thunderbird's account settings, subscribe to the **All Mail** folder under `[Gmail]` — this is the canonical folder containing every message in your account
4. Let Thunderbird fully sync. For a large account this can take hours and will use significant disk space

### 2. Clone this repo

```bash
git clone https://github.com/your-username/gmail-thunderbird-analysis.git
cd gmail-thunderbird-analysis
```

No dependencies beyond Python 3.8+. Everything uses the standard library.

## Usage

```bash
python3 analyze_inbox.py [--report REPORT] [--top N] [--year YEAR] [--mbox PATH]
                         [--generate-cache] [--older-than DURATION]
```

### First run

The first run will parse the raw mbox file and build a small cache under `~/.analyze_inbox/`. This is the slow step — with an inbox of around 17,000 messages it takes close to 7 minutes. Every subsequent run reads from the cache and completes in a few seconds.

```bash
python3 analyze_inbox.py --report senders
```

### Reports

| Report | Description |
|--------|-------------|
| `summary` | Message count, date range, top senders and subject keywords (default) |
| `senders` | Top senders and domains by message count and storage |
| `timeline` | Messages per month |
| `subjects` | Top subject line words |
| `largest` | Largest individual messages |
| `all` | All of the above |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--report` | `summary` | Which report to run |
| `--top N` | `20` | Number of results per table |
| `--year YEAR` | *(all years)* | Filter to a specific year |
| `--mbox PATH` | Auto-detected | Path to the mbox file |
| `--generate-cache` | off | Force rebuild the cache |
| `--older-than DURATION` | `24 hours` | Auto-regenerate cache if older than this |

### Examples

```bash
# Full sender analysis
python3 analyze_inbox.py --report senders

# Top 30 senders for 2024 only
python3 analyze_inbox.py --report senders --top 30 --year 2024

# Message volume by month
python3 analyze_inbox.py --report timeline

# Force a cache refresh
python3 analyze_inbox.py --generate-cache --report senders

# Auto-refresh if cache is more than a week old
python3 analyze_inbox.py --older-than "1 week" --report senders

# Analyze a specific folder instead of All Mail
python3 analyze_inbox.py --mbox ~/Library/Thunderbird/Profiles/*/ImapMail/imap.gmail.com/INBOX --report senders
```

## Cache

The first run builds `~/.analyze_inbox/<folder>.cache` — a small JSON-lines file with one entry per message containing only the metadata needed for reports (date, sender, subject, size). This is what makes re-runs fast.

The cache is automatically regenerated if:
- It doesn't exist yet
- It is older than the `--older-than` threshold (default: 24 hours)
- You pass `--generate-cache` explicitly

The cache file is tied to the specific mbox it was built from, so analyzing multiple folders creates separate cache files.

## Notes

- **All Mail vs. other folders**: Gmail's `[Gmail]/All Mail` folder contains every message exactly once. Other folders like `Important` and `Starred` are label views that duplicate messages from All Mail. For account-wide analysis, stick with All Mail.
- **Sent mail**: All Mail includes your sent messages. The sender on those will be your own address.
- **Message sizes**: Sizes reflect the on-disk mbox representation, which includes full headers and Base64-encoded attachments (~33% larger than the original binary).
