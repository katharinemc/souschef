# Sous Chef — Weekly Meal Planning Agent

A household meal planning tool that reads Google Calendar, builds a weekly dinner and lunch plan, generates a categorised grocery list, and walks through the plan interactively in the terminal. The planner is driven by Claude (Anthropic API) and falls back to a deterministic algorithm when the API is unavailable.

---

## How it works

Every week (typically Thursday), you run `python main.py plan`. The tool:

1. Reads the upcoming Mon–Sun from Google Calendar to find no-cook nights, fasting days, and open Saturday slots
2. Asks Claude to assign dinners for each night, respecting household rules (see below)
3. Prints the plan and grocery list to the terminal
4. Enters a reply loop — you type feedback in plain English to swap meals, mark a night as eating out, rate an experiment, etc.
5. Re-plans and reprints after each change until you type `done`

---

## Setup

### 1. Python dependencies

```bash
pip install anthropic pyyaml google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

### 2. Anthropic API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Add this to your shell profile (`~/.zshrc` or `~/.bashrc`) to persist it.

### 3. Google Calendar OAuth

The tool reads your calendar to find busy nights. You need a Google Cloud project with the Calendar API enabled and an OAuth credentials file.

**One-time setup:**

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. Enable **Google Calendar API** under "APIs & Services"
4. Create **OAuth 2.0 credentials** (Desktop app type) and download the JSON file
5. Save it as `credentials.json` in the project directory (or set `credentials_path` in config)
6. Under "OAuth consent screen", add your Gmail address as a test user

On first run, a browser window will open asking you to authorise the app. After that, a `token.json` file is saved locally and you won't be prompted again for ~1 year.

### 4. Configuration file

Copy the template and fill in your values:

```bash
cp config.yaml.template config.yaml
```

Key settings:

```yaml
credentials_path: credentials.json
token_path: token.json

email:
  to_address: your@email.com

calendar:
  calendar_id: "McLeod Family G-Cal"   # exact calendar name or ID
  timezone: America/New_York

planner:
  recipe_dir: recipes_yaml
  lunch_file: lunches.yaml
  db_path: meal_planner.db

anthropic:
  model: claude-sonnet-4-20250514      # or set ANTHROPIC_API_KEY env var
```

---

## Running the planner

### Generate this week's plan

```bash
python main.py plan
```

The plan prints to the terminal. You then interact in the reply loop until you type `done`.

**Options:**

| Flag | Effect |
|------|--------|
| `--dry-run` | Print the plan without saving anything to the database |
| `--legacy` | Use the deterministic rule-based planner instead of Claude |
| `--verbose` | Show debug logs (calendar reads, tool calls, etc.) |
| `--config PATH` | Use a different config file (default: `config.yaml`) |

```bash
# Preview without saving — good for testing setup
python main.py plan --dry-run

# Use the rule-based planner (no API key required)
python main.py plan --legacy --dry-run
```

### The reply loop

After the plan prints, you can type feedback in plain English:

```
Respond to make changes, or type 'done' to approve:
> Swap Tuesday for something with chicken
> Mark Saturday as dinner at Sarah's
> Waffles for dinner on Sunday
> Rate the merguez 4 stars
> done
```

Supported actions:

| What you type | What happens |
|---------------|--------------|
| `Swap [day] for [constraint]` | Re-plans that day with a matching recipe |
| `[day] is no cook` / `leftovers on [day]` | Marks the day as no-cook |
| `Make [day] a cook night` | Turns a no-cook day into a cook night |
| `Skip the experiment this week` | Removes the Saturday experiment |
| `Mark [day] as [description]` | Marks eating out (no grocery items added) |
| `[description] on [day]` | Marks a cook night not in the recipe library |
| `Rate [recipe] [N] stars` | Records a star rating for an experiment recipe |
| `Add [recipe] to rotation` | Promotes an experiment recipe to `onRotation` |
| `done` / `looks good` / `okay thanks` | Approves the plan and exits |

---

## Household rules the planner follows

These are enforced automatically and cannot be overridden:

- **Wednesday and Friday are always meatless** (vegetarian, pescatarian, or vegan)
- **Friday is always pizza** — homemade on the first Friday of the month, takeout all other Fridays
- **Experiment recipes only on open Saturdays** — Saturdays with no qualifying KRM or Family calendar events longer than 2 hours
- **Saturday vs Sunday:** cook one or the other, never both; Saturday takes priority
- **Calendar events prefixed `KRM` or `Family` after 3pm** = no-cook night
- **Events prefixed `SGM`** are ignored entirely
- **Fasting days** (a 30-minute event titled "fasting" at 5am) are always meatless

---

## Recipe library

Recipes live as YAML files in `recipes_yaml/`. Each file is named by recipe ID (slugified name).

### Recipe format

```yaml
id: hamburger-steaks-with-onion-gravy
name: Hamburger Steaks with Onion Gravy
tags:
  - onRotation
servings: '4'
prep_time: null
cook_time: 40 minutes
source: https://...
ingredients:
  - name: 85 percent lean ground beef
    quantity: 1 1/2
    unit: pounds
  - name: onion halved and sliced thin
    quantity: '1'
instructions: ...
notes: null
```

### Tags

| Tag | Meaning |
|-----|---------|
| `onRotation` | Recipe is in active weekly rotation |
| `experiment` | New recipe to try — scheduled on open Saturdays only |
| `vegetarian` | Vegetarian (qualifies for Wed/Fri) |
| `pescatarian` | Pescatarian (qualifies for Wed/Fri) |
| `vegan` | Vegan (qualifies for Wed/Fri) |
| `pizza` | Friday pizza recipe |
| `easy` | Suitable for a Sunday when the week is already busy |

### Adding recipes from Paprika

Export from Paprika as a `.paprikarecipes` file (bulk export), then:

```bash
python main.py ingest --input MyExport.paprikarecipes
```

This converts the export to YAML files in `recipes_yaml/`. Use `--list` to preview tag mappings before writing, and `--force` to overwrite existing files.

---

## Rating experiment recipes

After cooking an experiment on Saturday, record how it went:

```bash
python main.py rate --recipe merguez --stars 4
```

To also promote it to the regular rotation:

```bash
python main.py rate --recipe merguez --stars 5 --promote
```

`--promote` adds the `onRotation` tag to the recipe's YAML file so it appears in future plans.

You can also rate from inside the reply loop: `Rate the merguez 4 stars`.

---

## Other commands

### Check database and config

```bash
python main.py status
```

Shows the number of tracked recipes, recent plans, and config summary. Useful for verifying setup.

### Preview the plan

```bash
python main.py preview
```

Runs the full pipeline (calendar read, planning, grocery build) and prints the formatted output. Does not enter the reply loop or save anything.

### Poll for email replies

```bash
python main.py reply
```

Polls the configured inbox for replies to a sent plan and triggers re-plans. Used in Phase 3 (email delivery, not yet enabled).

---

## Lunches

The weekly lunch is a single recipe used for the whole week (batch-cooked on the weekend). Define options in `lunches.yaml`:

```yaml
- id: quinoa-bowl
  label: Quinoa and roasted vegetable bowl
  prep_notes: Cook quinoa and roast vegetables Sunday afternoon
```

The planner selects from this list. If the file is missing or empty, no lunch is assigned.

---

## Database

Plans and rotation history are stored in `meal_planner.db` (SQLite). The database is created automatically on first run. Key tables:

| Table | Contents |
|-------|----------|
| `planned_meals` | Every assigned dinner slot, by week |
| `tracked_recipes` | `last_planned` date for each recipe ID |
| `experiment_ratings` | Star ratings recorded via `rate` command or reply loop |
| `meal_notes` | Out and cook notes assigned during the reply loop |

To start fresh (e.g. for testing), delete or rename `meal_planner.db`.

---

## Files not to commit

Add these to `.gitignore` if not already there:

```
credentials.json
token.json
meal_planner.db
config.yaml
```

`config.yaml.template` is the version-controlled template. `config.yaml` contains your actual credentials and calendar IDs.
