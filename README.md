# Morning Desk

One website with two tabs, built for a 6am to 2pm Toronto shift.

- **Scanner:** federal contracts and grants, DoD contract list, press releases, SEC 8-Ks and unusual volume, scored bullish or bearish, with a plain-English setup label on each name.
- **Live tape:** real-time buying and selling pressure for the symbols you pick. It runs inside the page, so there is nothing to install and any computer or phone can use it.

It is free, has no ads, and the only account you need is GitHub (plus Alpaca for the live tape, and Twilio if you want texts).

## Go live (about 15 minutes, all in a web browser)

**1. Make a GitHub repo.** Sign in at github.com, click New repository, name it `morning-desk`, set it to **Public** (free scheduled runs and free hosting need this; your keys stay private, see "What is public" below), tick "Add a README", create it.

**2. Upload the files.** On the repo page click Add file, Upload files, and drag in everything from the unzipped folder (including the `.github` and `site` folders). If your browser skips the hidden `.github` folder, click Add file, Create new file, type `.github/workflows/scanner.yml` as the name, and paste in the contents of `scanner.yml`. Then click Commit.

**3. Add your secrets.** Repo Settings, Secrets and variables, Actions, New repository secret. Add:

| Name | What to put |
|---|---|
| `SEC_USER_AGENT` | `morning-desk yourname` (any short text) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | From twilio.com (for texts) |
| `TWILIO_FROM` | Your Twilio number, like `+16475550100` |
| `SMS_TO` | Your cell, like `+14165550123` |
| `NTFY_TOPIC` | Optional. Free phone push: install the ntfy app and pick a long random topic name |

You can skip Twilio and use only ntfy (free) at first.

**4. Turn on the website.** Repo Settings, Pages, under Source choose **GitHub Actions**.

**5. Run it once.** Actions tab, click `scanner`, Run workflow. The first run only records what already exists, so no alerts. After a few minutes your site is at `https://YOUR-USERNAME.github.io/morning-desk/`. Bookmark it on your phone and your work computer.

From then on it runs by itself every 15 minutes between 6am and 8pm Toronto time on weekdays and refreshes the site each time.

## Your day

| Time (Toronto) | What happens |
|---|---|
| Overnight and after 2:15pm | Alerts are held, not sent. Only a score of 8 or more pushes to ntfy or Telegram, never a text. |
| 8:40 to 9:25am | You get the game plan text: what queued overnight, top bullish names, names to avoid. |
| 6am to 2:15pm | Texts at a score of 5 or more, push at 3 or more. |
| About 2pm | Wrap-up text with where things stand. |

Test the channels any time with `python scanner.py --test` (needs Python on a computer), or just wait for the first real alert.

## Live tape

Open the **Live tape** tab. It starts in a demo so you can see it work. To go live:

1. Make a free account at alpaca.com, open the paper-trading dashboard and generate an API key and secret. (A paper key is enough for market data.)
2. Paste them in, add your symbols or press "Use scanner's top names", press Connect. They are saved only in that browser, so you enter them once per device.

What to know:
- The free feed is real-time but covers **one exchange (IEX)** and 30 symbols, so volumes are a sample of the whole market. Full-market real-time is Alpaca's paid plan.
- Only one connection per Alpaca account at a time. If you see an error about a connection limit, close other tabs using the same key.
- Buy and sell are **inferred** from trade price against the bid and ask. No feed labels them.
- Alerts (sound, notification, ntfy) only work while the page is open.
- Your TradingView chart stays your chart. Its 15-minute delay is a licensing rule; the fix is buying real-time exchange data inside TradingView. The tape tab gives you real-time pressure alongside it.

`tape.py` in the folder is an optional older version that runs on an always-on computer and can text you on surges. You do not need it.

## What is public
The repo and website are public, so anyone with the link could see the scanner results (all taken from public sources). Secrets (Twilio, ntfy topic) are stored in GitHub Secrets and never appear in the repo. Alpaca keys never leave your browser. If you want it private, GitHub's paid plan can host private Pages.

## Tuning
Environment variables (add under `env:` in the workflow): `MAX_MCAP`, `MIN_MCAP`, `MIN_PRICE`, `MIN_AWARD`, `SMS_MIN_SCORE`, `PUSH_MIN_SCORE`, `URGENT_MIN_SCORE`, `WORK_WINDOW_ET` (default `05:50-14:15`).

## Known limits
- The live data endpoints (war.gov feed, Nasdaq screener, USAspending, PR Newswire, EDGAR, Yahoo) could not be reached from where this was built. Parsers and scoring pass tests on sample data, but the first live run may need small fixes. Check the Actions log's "source health" line, and the Sources row at the bottom of the Scanner tab.
- The Nasdaq screener sometimes blocks cloud servers. Fallback: list tickers in `watchlist.txt`.
- The scanner is minutes behind, not instant. The data is public and many people watch it, so a hit may already be priced in. Company name matching is fuzzy: check the company and read the source before acting.
- GitHub can start scheduled runs a few minutes late.
- Not financial advice. Only public data is used.
