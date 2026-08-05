# InfoCar exam checker and auto-booker

Polish version: [README.pl.md](README.pl.md)

A Python bot that watches [info-kierowca.pl](https://info-kierowca.pl) for practical driving exam slots, sends you a Telegram message the moment one appears, and can book it for you automatically.

It is a free analogue to zlap-termin, built with Python.

## The one command

```bash
pip install requests playwright
playwright install chromium

python infocar_bot.py --org-id 43 --days 14 --auto-book
```

Swap 43 for your own WORD ID before running this, since 43 is PORD Gdańsk and `--auto-book` will genuinely reserve a slot there. Run `--list-words` to find yours, and leave `--auto-book` off the first time if you want to watch the bot work before it commits you to anything.

The first run opens a browser window so you can log in through login.gov.pl or the eDO App. After that the session is stored in `./browser_data` and the bot goes straight into its monitoring loop. Later runs skip the window entirely.

If you would rather log in ahead of time, run `python infocar_bot.py --login` on its own.

## How it stays logged in

This is the part that took the longest to get right, so it is worth knowing.

The site issues a JWT with a 900 second lifetime. The bot calls `/bknd/auth/api/v1/jwt/refresh` at the top of every check cycle and never sleeps longer than 600 seconds, so the token is always renewed before it lapses. Cookies live in the requests session jar rather than a fixed header, because the server rotates `__Secure-PUDOJT` on each refresh.

No browser stays open while the bot runs. The site's own frontend fires `jwt/logout` after 600 seconds without real mouse or keyboard activity, and that call kills the session on the server side, not just in the tab.

Which leads to the one rule worth writing on a sticky note: do not leave info-kierowca.pl open in your own browser while the bot is running. That idle tab will log the bot out after about ten minutes.

## The one hour ceiling

Refreshing does not buy you forever. The server caps a session at 3600 seconds from login and then answers `jwt/refresh` with HTTP 500, whatever the client does. Nothing works around it: the 500 outlives retries and process restarts, and the token carries no claim that would let the bot see it coming. Only a fresh login helps, and that needs you and your phone.

So the bot treats a failed refresh as a question rather than a verdict. It probes the API, and if the session still answers it carries on using the token it already holds until that token really expires, which is worth another ten minutes or so of monitoring. Five minutes before the cap you get a Telegram message, so you can hand over a fresh session instead of discovering a dead bot an hour later. Do it in this order: stop the bot, run `--login`, start it again. A running bot rewrites `auth_token` on every successful refresh, so logging in beside it just gets your new token overwritten by the old session's. Login time is stamped into `config.json` as `session_started_at`, which is how the warning survives a restart.

A single dropped connection no longer ends the run either. The refresh retries three times with a short backoff, and only a definitive 401 or 404 from the gateway counts as a rejection worth giving up on.

## What a check looks like

Every 4 to 6 minutes (randomized, and never more than 10 minutes apart) the bot refreshes the session, then pages through your search window in 7 day chunks, pausing 5 to 15 seconds between requests. Slots are deduplicated by date and exam ID, sorted, and the earliest one wins.

With `--auto-book` it creates a reservation, hits the confirmation stream, then polls the reservation state up to four times. Only `PlaceReserved` or `Accepted` counts as booked. Anything else gets reported to Telegram as a failure telling you to book manually, because a reservation stuck in `Created` is not yours yet.

Without `--auto-book` you just get the alert and do the clicking yourself.

## Commands

```bash
python infocar_bot.py --login                          # log in once, save the session
python infocar_bot.py --test-telegram                   # send a test message
python infocar_bot.py --list-words                      # list WORD centers and their IDs
python infocar_bot.py --check-once                      # one check, then exit
python infocar_bot.py --org-id 43 --days 14             # monitor, notify only
python infocar_bot.py --org-id 43 --days 14 --auto-book # monitor and book
python infocar_bot.py --all-words --days 14             # monitor every known WORD center
```

`--list-words` reads `word_centers.json`. If that file is missing the list comes up empty, but any integer organization ID still works. 43 is PORD Gdańsk.

`--all-words` (`-a`) checks every center in `word_centers.json` (91 as of writing) instead of `--org-id`. It requires `word_centers.json` to be present and overrides `--org-id` and `organization_id(s)` in `config.json` when set. Expect a much slower loop, since every cycle now pages through all of those centers instead of one.

`easy_word_centers.json` is a hand-picked shortlist of WORD centers with historically higher practical exam pass rates, for picking an `--org-id` by your odds of passing rather than just proximity. It's a reference file only, nothing in the bot reads it automatically.

## Configuration

Copy `config.example.json` to `config.json` and fill it in:

| Key | What it does |
| --- | --- |
| `auth_token` | Session cookie string. The bot writes this itself after login, so leave it alone. |
| `pkk` | Your PKK profile number. Omit it and the bot fetches it from your account. |
| `organization_id` | WORD center ID, for example 43. |
| `organization_ids` | Several WORD center IDs to check every cycle, for example `[43, 42, 73]`. Wins over `organization_id` if both are set. `--org-id 43,42,73` does the same from the command line. |
| `max_days` | How many days ahead to search. |
| `auto_book` | `true` to book automatically, `false` to only send alerts. |
| `min_interval` / `max_interval` | Sleep range between checks, in seconds. Defaults are 240 and 360, and anything above 600 is clamped down to protect the JWT. |
| `telegram_bot_token` | From @BotFather. |
| `telegram_chat_id` | Your chat ID. |

Telegram is optional. Without a token and chat ID the bot logs what it would have sent and carries on.
