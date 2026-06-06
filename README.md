# Job Application Script

A CLI tool that searches multiple job boards, shows you the results in a formatted table, and opens applications in your browser — with everything logged to a CSV for reference.

## How it works

1. Searches job listings across up to three sources based on your filters
2. Always displays the full results table first before doing anything
3. Prompts you to pick a mode (or pass a flag upfront):
   - **Review** — step through each listing one by one, press `y` to open or `n` to skip
   - **Auto** — confirm once, then all listings open in your browser in sequence
   - **List-only** — log everything without opening anything
4. Every listing is written to a CSV log regardless of what you do with it

Applications open in your default browser, where you're already logged into LinkedIn, Indeed, etc. This avoids bot detection and account bans while still letting you apply quickly.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

The script works out of the box without any API keys — it will search [Remotive](https://remotive.com) (remote jobs) and [The Muse](https://www.themuse.com) (general tech jobs). For broader coverage including non-remote and location-specific listings, add a free Adzuna key to your `.env`:

```
ADZUNA_APP_ID=your_id
ADZUNA_APP_KEY=your_key
```

Register for a free Adzuna key at [developer.adzuna.com](https://developer.adzuna.com/).

## Usage

```
python apply.py -k KEYWORDS [options]
```

### Arguments

| Flag | Short | Description | Default |
|---|---|---|---|
| `--keywords` | `-k` | Job title / skills to search for | *(required)* |
| `--location` | `-l` | City, state, or country | *(any)* |
| `--remote` | `-r` | Include remote listings | off |
| `--job-type` | `-t` | `full-time`, `part-time`, or `contract` | `full-time` |
| `--max-age-days` | `-d` | Only show listings posted within N days | 30 |
| `--count` | `-n` | Max number of listings to fetch | 20 |
| `--min-salary` | `-s` | Minimum salary (e.g. `120000` or `120k`) | *(any)* |
| `--auto` | `-a` | Open all listings in browser after the table | off |
| `--review` | | Step through listings one by one | off |
| `--list-only` | | Log listings without opening any | off |
| `--log` | | Path to CSV log file | `applications.csv` |
| `--delay` | | Seconds between browser opens in auto mode | 2 |

If none of `--auto`, `--review`, or `--list-only` are passed, the script will ask you to choose after showing the table.

### Salary filter behavior

`--min-salary` excludes listings where the salary is explicitly known and tops out below your floor. Listings with no salary information are always kept — most postings omit it, and you'd rather see them than miss them.

## Examples

Search for remote software engineer roles in the US posted in the last week:
```bash
python apply.py -k "software engineer" --remote --location US --max-age-days 7
```

Pull 15 listings and review each one before applying:
```bash
python apply.py -k "backend engineer python" -l "San Francisco, CA" -n 15 --review
```

Auto-open all contract roles with a minimum $150k salary:
```bash
python apply.py -k "staff engineer" -t contract --min-salary 150k --auto
```

Just log listings without opening anything (useful for scouting):
```bash
python apply.py -k "SDE II" --remote --max-age-days 3 --list-only
```

## Log file

Every listing is appended to `applications.csv` (or whatever path you pass to `--log`) with the following columns:

| Column | Description |
|---|---|
| `id` | Sequential index for this run |
| `title` | Job title |
| `company` | Company name |
| `location` | Listed location or candidate region |
| `remote` | `yes` / `no` |
| `job_type` | Employment type |
| `salary_min` | Parsed minimum salary (if available) |
| `salary_max` | Parsed maximum salary (if available) |
| `url` | Direct link to the listing |
| `source` | Which API returned this listing |
| `posted_date` | ISO 8601 timestamp of when the job was posted |
| `status` | `applied`, `skipped`, or `listed` |
| `applied_at` | Timestamp of when you opened it |
| `notes` | Empty column for your own notes |

## Job sources

| Source | Coverage | Key required |
|---|---|---|
| [Remotive](https://remotive.com) | Remote tech jobs worldwide | No |
| [The Muse](https://www.themuse.com) | General tech/startup jobs | No (optional for higher rate limits) |
| [Adzuna](https://www.adzuna.com) | Broad US job market | Yes (free) |

Adzuna is the only source that supports server-side salary filtering via `--min-salary`. The other sources are filtered client-side based on whatever salary data they include in their responses.
