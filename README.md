# TG Chat Guard — anti-spam moderator for Telegram (aiogram 3.x)

**English** | [Русский](README.ru.md)

![Python](https://img.shields.io/badge/python-3.12-blue)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Redis](https://img.shields.io/badge/Redis-7-DC382D)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)
![License](https://img.shields.io/badge/license-MIT-green)

A production-ready moderator bot for a book-lovers' Telegram chat: an extended
spam filter, a two-level whitelist and **false-positive protection** for book
requests ("Mastering Bitcoin", "books on trading", "Cryptography").

The bot is built for a Russian-speaking community, so its chat messages and
command replies are in Russian, while the spam dictionary covers both Russian
and English.

## Core invariant

> A stop word on its own is **never** a reason to punish.

A sanction is applied only when topical vocabulary is combined with at least one
"hard" spam factor:

| Factor | Example |
|---|---|
| external link | `http://…`, `scam.top`, `t.me/…` |
| invite to a private channel | `t.me/+abc`, `t.me/joinchat/…` |
| hidden link | text `Read the book` pointing to `https://scam.top/ref/7` |
| referral code | `?ref=`, `promo code`, `via my link` |
| call to action | `follow the link`, `DM me`, `start earning` |
| mention of an unrelated account | `@scam_channel` |
| mention of a third-party bot (higher weight) | `@llkaebot`, `@promo_b0t`, `t.me/leaks_bot` |
| contact details | phone number, WhatsApp, Viber |
| advertising in the profile name | name "CASINO VULKAN", username `@casino_promo` |
| link in the profile name | "Earn money t.me/+abcdef" |
| the same text from several accounts | a mailing from 3+ different members within an hour |

Links to trusted domains (`flibusta.is`, `litres.ru`, `fantlab.ru`, …) are not
counted as a factor, and "book intent" (`looking for`, `author`, `fb2`,
`recommend`, …) lowers the spam score — but it does not save spam that carries an
invite, a referral code or a direct call to action.

## Architecture

```
Dockerfile                           # multi-stage image, runs as appuser
docker-compose.yml                   # bot + postgres + redis
alembic.ini                          # migrations config (strictly ASCII!)
migrations/                          # schema revisions
bot/
├── config.py                        # pydantic-settings v2: tokens, thresholds, whitelist
├── main.py                          # dependency wiring, routers, polling
├── db/
│   ├── models.py                    # WhitelistEntry, ModerationEvent, MessageSample
│   ├── migrator.py                  # applies migrations on startup
│   └── session.py                   # async engine + session factory
├── services/
│   ├── text_cleaner.py              # normalization: homoglyphs, leet, zero-width, "c.r.y.p.t.o"
│   ├── stopwords.py                 # categorized dictionary + context markers
│   ├── antispam_engine.py           # smart detection: scoring, anti-FP, sanction choice
│   ├── whitelist_service.py         # whitelist: local cache → Redis → DB
│   ├── reputation_service.py        # CAS/LOLS blocklists: in-memory snapshot + cache
│   ├── event_service.py             # log of detections and their admin review
│   ├── sample_service.py            # labeled /spam and /ham examples
│   ├── report_service.py            # counting member reports
│   ├── duplicate_detector.py        # one text from several accounts
│   ├── corpus_check.py              # regression run over the labeled sample
│   ├── purge.py                     # batch deletion within Telegram limits
│   ├── deleted_registry.py          # memory of the bot's own deletions
│   ├── admin_cache.py               # TTL cache of chat admins
│   └── activity_tracker.py          # clean-message counter → "approved" status
├── middlewares/
│   ├── dependencies.py              # injects services into handlers
│   └── whitelist_middleware.py      # trusted members never reach moderation
├── handlers/
│   ├── moderation.py                # sanctions, safe deletion, event log
│   ├── admin_review.py              # card in the admin chat + rollback buttons
│   ├── admin_training.py            # /spam, /ham, /samples, /report
│   ├── cleanup.py                   # /clean and /purge — bulk chat cleanup
│   ├── flibusta_filter.py           # removes "junk" replies of the book bot
│   ├── join_guard.py                # checks newcomers against blocklists on join
│   ├── service_cleanup.py           # removes "joined the group", "left" and so on
│   └── admin_whitelist.py           # /whitelist_add, /whitelist_remove, /spamcheck
├── filters/admin.py                 # IsChatAdmin, IsSuperAdmin
├── tools/regress.py                 # CLI: run the sample without starting the bot
└── utils/
    ├── logging.py                   # JSON logs with structured fields
    ├── service_kinds.py             # groups of Telegram service messages
    └── telegram.py                  # safe delete/restrict/ban, entity parsing
```

Message processing flow:

```
Update → DependenciesMiddleware → admin routers (commands, review buttons)
                                → moderation router
                                     └─ WhitelistMiddleware (trust)
                                          │   static whitelist → chat admin →
                                          │   dynamic whitelist → approved member
                                          └─ AntiSpamEngine.evaluate()
                                               └─ sanction + event log + admin card
```

## Stop-word categories

`casino` · `crypto_scam` · `adult` · `job_scam` · `drugs` · `account_trade` ·
`finance_neutral`

The dictionary is bilingual: mailings often contain no Russian words at all —
"telegram accounts cheap store, get yours @bot". English rules live in separate
`*_en` rules so the logs show exactly what caught a message.

English in a readers' chat needs extra care: `financial freedom`,
`passive income`, `work from home`, `investment plan`, `side hustle` and
`stock market` are book titles and ordinary conversation, so they sit in the
neutral category and cannot trigger a punishment.

`finance_neutral` (crypto, bitcoin, trading, finance, earnings, cryptography,
passive income, financial freedom) is the vocabulary of legitimate book requests:
its weight is symbolic and it can never lead to a sanction.
`drugs` combined with a spam factor escalates straight to a ban.
`account_trade` covers selling accounts, phone numbers and fake engagement.

### P2P scam: two-part rules

"Куплю USDT при личной встрече" and "I'm from China, buying USDT above market
price" carry no link and no channel invite, so the older rules never saw them.
These schemes are identified by a combination rather than a word: the rule only
fires when the message contains both the asset (`usdt`, `bitcoin`, `crypto`)
and the scheme itself ("personal meeting", "above market price", "policy
restrictions", "no need to scan QR", "we pay first").

Such rules are marked `standalone`: a match is a spam factor on its own
(`SignalKind.SCAM_OFFER`) — an offer is not a topic of conversation. They are
deliberately not "aggressive", so the book-intent discount still protects
readers: "looking for Mastering Bitcoin, will buy in person" passes freely.

## Getting started

### Docker (recommended)

```bash
cp .env.example .env          # Windows: copy .env.example .env
# fill in BOT_TOKEN, POSTGRES_PASSWORD, REDIS_PASSWORD (letters and digits only)
docker compose up -d --build
docker compose logs -f bot
```

Three services start: `bot`, `postgres` (16) and `redis` (7). Inside compose,
`DATABASE_URL` and `REDIS_URL` are built from `POSTGRES_*` and `REDIS_PASSWORD` —
the local values in `.env` are ignored there. Migrations run when the bot starts.
Database and Redis ports are not published; the bot runs as the unprivileged
`appuser`, with a read-only filesystem and all capabilities dropped.

```bash
docker compose ps                  # status and healthchecks
docker compose down                # stop (data stays in volumes)
docker compose down -v             # stop and DELETE the database and Redis data
```

### Local

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements-dev.txt                  # runtime + pytest/ruff/mypy
copy .env.example .env                               # and fill in BOT_TOKEN
python main.py
```

The bot works without Redis (the cache is disabled and data is read from the DB)
and on SQLite (`sqlite+aiosqlite:///./moderation.db`). For production: PostgreSQL + Redis.

Bot permissions in the chat: **delete messages** and **ban users**.

## Database schema and migrations

The schema is managed by alembic. The bot applies migrations itself on startup,
so no separate deployment step is needed:

```
Схема БД готова  migrations=upgraded
```

To disable this (if deployment updates the schema): `RUN_MIGRATIONS_ON_STARTUP=false`,
then run `alembic upgrade head` before starting.

After changing models in `bot/db/models.py`:

```bash
alembic revision --autogenerate -m "add something"
alembic upgrade head
```

The startup migrator handles each database state separately:

| Database state | What the bot does |
|---|---|
| empty | applies all revisions |
| managed by alembic | applies the missing ones |
| tables exist, alembic does not know about them | marks it with the current revision (`stamp`) and logs a warning |
| some tables from an older version | **stops** with a clear error — a human decides |

The last case is deliberately not automated: the bot should not guess what to do
with someone else's data.

> Keep `alembic.ini` strictly ASCII: alembic reads it in the locale encoding, and
> Cyrillic in comments breaks startup on Windows (verified on this machine).

`tests/test_migrations.py::test_migrations_match_models` fails if a model was
changed without adding a revision.

## Whitelist

Two levels:

1. **Static** — `.env`, changed only by redeploying. It holds system bots,
   including the Flibusta book bot (`FLIBUSTA_BOT_ID` / `FLIBUSTA_USERNAME`),
   which is fully excluded from all checks.
2. **Dynamic** — DB + Redis cache, managed by chat admins on the fly.

Scope of an entry: the current chat or (flag `--global`, only for
`SUPER_ADMIN_IDS`) all chats at once.

### Admin commands

| Command | Purpose |
|---|---|
| `/whitelist_add` (as a reply) | add the author of the message |
| `/whitelist_add @username [reason]` | add by username |
| `/whitelist_add 123456789 [reason]` | add by id |
| `/whitelist_add @bot --global` | add in all chats (super admin) |
| `/whitelist_remove @username \| id` | remove from the list |
| `/whitelist_list` | show static and dynamic rules |
| `/spamcheck <text>` | run text through the engine and see the breakdown |
| `/antispam_status` | current thresholds and mode |
| `/spam` (as a reply) | delete, ban the author and save a spam example |
| `/ham` (as a reply) | lift the sanction, mark it a false positive, save an example |
| `/samples` | size of the collected sample |
| `/clean N` · `/purge N` | delete the last N messages |
| `/clean all` | deep cleanup (with confirmation) |
| `/regress` | run the sample through the current rules |

Regular members have a single command — `/report` as a reply to a message.
When `REPORT_THRESHOLD` different people (2 by default) have reported, the admin
chat receives an alert with a link to the message. A repeated report from the same
member is not counted, and the command itself is removed from the chat.

## Reviewing detections and check scope

### Moderation admin chat

Create a private chat, add the bot and put its id into `ADMIN_LOG_CHAT_ID`.
Every detection produces a card there: author, score, reason, triggered signals,
message text — and three buttons:

| Button | What it does |
|---|---|
| ✅ Not spam | lifts the mute/ban, returns the text to the chat, marks the event as a false positive |
| ✔️ Correct | confirms the decision and closes the card |
| ➕ Whitelist | adds the author to this chat's whitelist |

The buttons are available to admins of the source chat and to the bot's super
admins. All decisions are stored in `moderation_events` (`false_positive`,
`reviewed_by`, `reviewed_at`) — material for tuning rules:
`EventService.recent_false_positives()`.

Without this chat false positives stay invisible: the member just silently
leaves. That is why `NOTIFY_CHAT` is recommended to be off, with everything
reviewed here.

### Approved members

The counter grows **only on clean messages**; an anti-spam detection resets it.
After `APPROVED_AFTER_MESSAGES` (7 by default) a member is approved and not checked
at all — this is how `--first-messages-count` works in tg-spam, and it is the main
defence against false positives for long-standing members.
While a member has fewer than `NEW_MEMBER_MESSAGES` (5) messages, they count as a
newcomer and get +1 to the spam score.

During an attack, `PARANOID_MODE=true` checks everyone.
If Redis is unavailable, nobody is approved and everyone is checked: noisy, but safe.

## Author profile

Spammers often keep the text harmless and move the whole offer into the name:
"CASINO VULKAN", "Earn 24/7", `@promo_casino`. So the name and username are
checked with the same dictionary as the text.

The rules are the same as for text — a name alone is not punished:

* "CASINO VULKAN" + "hi" → 3.5 points, the message passes;
* "CASINO VULKAN" + any link → 7.5 points, deletion;
* "Cryptowitcher", "Bitcoin Ivanych", "Trader Peter" → **nothing**: neutral
  financial vocabulary in a name is ignored, these are ordinary readers;
* "Intimate leisure 18+" + `@vasya hi` → 5.5 points, the message passes. This
  boundary is intentional: replying to someone should not cost a member a message.

For messages sent on behalf of a channel, the channel title and username are used.
To check someone's profile manually: `/spamcheck` as a reply — the breakdown will
include a "Profile" line.

## Hidden advertising: leetspeak and bots

Spam networks disguise text and lead people into a bot:
`super h0t vide0s avaible on this bot - @llkaebot`. The words are "broken" with
digits, and all the actual content is hidden inside the bot.

* **Leetspeak.** The normalizer builds text variants with digits and symbols
  replaced by letters: `0→o`, `1→i/l/и`, `3→e`, `4→a`, `5→s`, `7→t`, `@→a`, `$→s` —
  separately for Latin and Cyrillic. `h0t vide0s` becomes `hot videos`,
  `в1де0` becomes `видео`. A digit inside a word (`h0t`) is treated as evasion;
  digits at word edges (`mp3`, `fb2`, `4k`, `1984`) are not.
* **Look-alike letters.** Greek, Coptic and "small capital" characters are
  visually identical to Cyrillic but are different letters to the dictionary:
  a human reads `Пσᴧучuть uнфσρмᴀцuю` as "Получить информацию" while the filter
  saw a string of unknown symbols. NFKD does not decompose them — they have no
  canonical decomposition — so `text_cleaner.py` carries a dedicated
  `_HOMOGLYPHS` table. A look-alike adjacent to a normal letter counts as
  evasion; a standalone Greek letter (`σ-algebra`, `α-particles`) does not.
* **Keycap digits.** `6️⃣5️⃣ тысяч` collapses to `65 тысяч`. The variation
  selector (VS16) is not treated as evasion: it appears in every other ordinary
  emoji such as `❤️`.
* **Private message links.** `t.me/m/<hash>` hides the username and previously
  did not match the invite pattern at all — only one character follows the
  slash. It is now an invite on par with `t.me/+…` and `joinchat`.
* **Bot mentions.** Telegram requires bot usernames to end in `bot`.
  `@…bot`, a disguised `@promo_b0t` and a `t.me/…bot` link produce a hard factor
  weighted `BOT_MENTION_WEIGHT` (4.5) — more than a regular mention (2.0) — and
  book intent does not soften it. Trusted bots (Flibusta, static whitelist) are
  not counted.
* **Dictionary.** Rules `adult_video_en` / `adult_video_ru` and calls to action
  such as `available on this bot`, `in our bot`, `переходи в бота`, `в нашем боте`.

A single bot mention does not cause deletion: in a book chat people recommend bots
all the time. Combined with a topic or a call to action it is deleted, and the
example above scores 14 points. To delete any third-party bot mention, set
`BOT_MENTION_WEIGHT=6`.

### Promo campaigns: behaviour over words

A spam network posts **the same bot** every 40 minutes, changing the text each time:
`videos bot= @llkaebot`, `check BEST vide0s … @llkaebot`, `bhai log … real videos h @llkaebot`.
A dictionary cannot keep up with that rotation, so three behavioural rules apply:

| Rule | How it works |
|---|---|
| promotion earns no trust | a message with a link, bot, contacts or referral code does not count toward "approved" status, even if it was let through. Otherwise an uncaught spammer would gain immunity after 7 messages |
| a newcomer promotes a bot | `NEWCOMER_PROMO_WEIGHT` (+2.0) — deleted from the very first message |
| the same bot again | `PROMO_REPEAT_WEIGHT` (+4.0) for each earlier mention by this author within `PROMO_REPEAT_WINDOW` (one day), up to ×3 |

A real spammer's feed of 11 messages: the first is deleted with a mute, from the
second on the author is banned (`tests/test_promo_campaign.py`).

The cost of the newcomer rule: a member with fewer than `NEW_MEMBER_MESSAGES`
messages who recommends a third-party bot gets the message deleted; the admin-chat
card allows a rollback. Approved members are not affected. Disable with
`NEWCOMER_PROMO_WEIGHT=0`.

## Closed loop: labeling → regression run

`/spam` and `/ham` do more than punish and roll back — they build a labeled sample.
`/regress` runs it through the current rules and shows what changed
(bot output is in Russian):

```
⚠️ Прогон выборки · 8 примеров за 2 мс

Спам пойман: 2/4 (50%)
Ложные срабатывания: 0/4 (0%)

🕳 Пропущенный спам:
• 2.0 · no_spam_factor — Продам аккаунты, недорого, пишите
• 3.0 · below_threshold — Здравствуйте! Ищу людей для сотрудничества, подробности в лс
```

The same without starting the bot — handier while editing the dictionary:

```bash
python -m bot.tools.regress
python -m bot.tools.regress --examples 20
```

The exit code is 1 if there are **false positives**, so the run can be used in CI.
Missed spam does not change the exit code: that is about recall, while trust in the
bot is destroyed precisely by false positives.

The run intentionally uses **text only**: no blocklists, duplicates or profile.
This measures the quality of the rules themselves, and two consecutive runs give
the same result.

## Bulk chat cleanup

`/clean 300` deletes the last 300 messages, `/clean all` does a deep cleanup
(1000 by default). Alias: `/purge`.

Three safeguards around an irreversible command:

1. **permissions are re-checked** via `get_chat_member` — the admin cache lives
   five minutes, which is not enough for deleting hundreds of messages. The owner
   may do anything; an administrator needs the "Delete messages" right;
2. **depth is capped** by `CLEAN_MAX_DEPTH`: a typo like `/clean 999999`
   does not wipe the whole history;
3. **button confirmation** starting from `CLEAN_CONFIRM_THRESHOLD` (200).
   Only the person who started it can press it.

The report and the command itself disappear after `CLEAN_NOTICE_TTL` seconds, so
the cleanup does not leave new clutter behind.

### What Telegram does not allow

| Limitation | How it is handled |
|---|---|
| `deleteMessages` accepts at most 100 ids | ids are split into batches of 100 with a `CLEAN_BATCH_PAUSE` pause between them |
| only messages **up to 48 hours old** can be deleted | three failed batches in a row mean the wall is reached, and cleanup stops |
| the API does not give bots a list of chat messages | ids are walked backwards from the command — there is no other way |
| the response does not say how many messages actually existed | the report honestly says "deleted **up to** N", and skipped ones are counted separately |

So the report looks like this ("chat cleaned — up to 200 deleted, 800 skipped
(older than 48 hours or already deleted), older messages start from here"):

```
🧹 Чат очищен — удалено до 200, 800 пропущено (старше 48 часов или уже удалены),
   дальше начинаются сообщения старше 48 часов.
```

## Book bot: immunity and cleanup of failed searches

The Flibusta bot is **untouchable**: its messages are not checked by the
anti-spam, it receives no sanctions and no spam points. The protection sits in two
places — `WhitelistMiddleware` never lets its messages into moderation, and the
handler itself starts with a `user_id == FLIBUSTA_BOT_ID` check.

Two kinds of its replies are still unwanted in the chat, and they are removed
(`FLIBUSTA_CLEANUP`):

| What | How it is detected |
|---|---|
| failed search | "не найдено книг", "ничего не найдено" ("no books found"), "По запросу …" with ❌ |
| reply into the void | the user's request was already deleted by the anti-spam |

The second case works like this: Telegram does not notify bots about deleted
messages, so the bot remembers its own deletions — `DeletedMessageRegistry` keeps
them for an hour. When the book bot replies to a vanished request, the reply is
removed too.

## Service message cleanup

"Joined the group", "left the group", "changed the title" — in an active chat there
is more of this noise than conversation. The bot removes it.

Configured by groups in `SERVICE_CLEANUP`:

| Group | What it removes |
|---|---|
| `join` | joined the group |
| `leave` | left the group |
| `title` | chat title changed |
| `photo` | chat photo or background changed |
| `pin` | "pinned a message" — **not** removed by default |
| `videochat` | video chat started, ended, scheduled |
| `forum` | forum topic creation and edits |
| `boost` | chat boost |
| `giveaway`, `gift` | giveaways and gifts |
| `created` | group created |
| `other` | remaining service noise |

Special values: `all` — everything, `none` — disable cleanup entirely.

Two rules that cannot be overridden by configuration:

* **payments are never deleted** (`successful_payment`, `refunded_payment`) —
  they are financial records;
* **messages about a group migrating to a supergroup also stay** — the link between
  the old and new chat depends on them.

With `DRY_RUN=true` service messages are not deleted, like everything else.
A typo in a group name does not crash the bot: an unknown name is skipped with a
warning in the log listing the available values.

## Mailing detector

The mailing hardest for a dictionary is one with neither links nor stop words:
"Hello! Looking for people to collaborate, details in private" sent from several
fresh accounts. It can only be seen through repetition.

What is counted is **different authors of the same text**, not the number of
repeats: a person who bumps their book request a second time in an evening loses
nothing.

The fingerprint is taken from the squashed form of the text — case, spaces,
punctuation, emoji and repeated characters are dropped, so padding a mailing with
random emoji does not save it.

The weight grows with the number of accounts and is capped:

| Different authors | Weight | What happens |
|---|---|---|
| 1–2 | 0 | nothing |
| 3 | 4.0 | not enough to delete yet, but enables other signals |
| 4 | 8.0 | deletion |
| 5 | 12.0 | deletion + mute |
| 9+ | 12.0 | cap |

Messages shorter than `DUPLICATE_MIN_LENGTH` (40 characters) are not checked:
"thanks" and "up" match for everyone. Book intent does not excuse a mailing — the
word "looking for" inside it changes nothing.

> The first accounts of a mailing do get their messages through: the threshold
> triggers on the third or fourth account. Remove those manually with `/spam` as a reply.

## External blocklists

Two independent public spammer registries, both keyless:

* **LOLS** (`api.lols.bot/account?id=`) — an online request with a 2 s timeout,
  cached in Redis. Queried only on an author's first message and on joining the chat.
* **CAS** (`api.cas.chat/export.csv`) — a full snapshot of the list. At the time of
  checking it held 1,270,682 ids, stored as a sorted `array("q")` in-process:
  **9.7 MB of memory** and binary search. Refreshed in the background every
  12 hours; the first download does not delay bot startup.

The CAS point lookup endpoint (`/check`) is intentionally not used: it fails to
find even ids present in its own export.

A blocklist hit is **a signal weighted `REPUTATION_WEIGHT` (5.0), not a verdict**.
A blocklisted author who writes "hi" gets 5.0 with a deletion threshold of 6.0 and
passes. But a "casino bonus" message without a single link, which normally passes
under the "no spam factor — no punishment" rule, scores 9.0 with a blocklist hit
and gets muted.

On joining the chat (`chat_member`) a newcomer is checked against the registries
before their first message. By default the bot does not ban but notifies the admin
chat: `REPUTATION_AUTOBAN=true` enables auto-ban if you are ready to trust the databases.

## Sensitivity tuning

| Variable | Default | Meaning |
|---|---|---|
| `REQUIRE_SPAM_FACTOR` | `true` | the main anti-false-positive safeguard |
| `DELETE_THRESHOLD` | `6.0` | deletion threshold |
| `MUTE_THRESHOLD` | `9.0` | mute threshold |
| `BAN_THRESHOLD` | `13.0` | ban threshold |
| `MUTE_DURATION` | `3600` | mute duration, seconds |
| `SAFE_DOMAINS` | book resources | link exceptions |
| `DRY_RUN` | `false` | observation mode: score and log, delete nothing |
| `ADMIN_LOG_CHAT_ID` | — | private chat for reviewing detections |
| `APPROVED_AFTER_MESSAGES` | `7` | clean messages after which a member is no longer checked |
| `NEW_MEMBER_MESSAGES` | `5` | messages below which a member counts as a newcomer |
| `PARANOID_MODE` | `false` | check everyone, including approved members |
| `REPUTATION_ENABLED` | `true` | check authors against CAS and LOLS |
| `REPUTATION_WEIGHT` | `5.0` | weight of a blocklist hit |
| `REPUTATION_AUTOBAN` | `false` | ban immediately on a blocklist hit |
| `REPORT_THRESHOLD` | `2` | how many reports summon admins |
| `DUPLICATE_THRESHOLD` | `3` | how many different authors of one text make a mailing |
| `DUPLICATE_WINDOW` | `3600` | duplicate observation window, seconds |
| `DUPLICATE_MIN_LENGTH` | `40` | shorter messages are not checked |
| `BOT_MENTION_WEIGHT` | `4.5` | weight of a third-party bot mention (`@..._bot`, `t.me/...bot`); `>= DELETE_THRESHOLD` deletes any |
| `NEWCOMER_PROMO_WEIGHT` | `2.0` | extra weight when a newcomer promotes a bot; `0` disables |
| `PROMO_REPEAT_WEIGHT` | `4.0` | extra weight per repeat of the same bot by the same author (up to ×3) |
| `PROMO_REPEAT_WINDOW` | `86400` | how long bot promotions are remembered, seconds |
| `SERVICE_CLEANUP` | all groups except `pin` | which service messages to remove |
| `CLEAN_DEFAULT_DEPTH` | `1000` | depth of `/clean all` |
| `CLEAN_MAX_DEPTH` | `5000` | cleanup depth cap |
| `CLEAN_CONFIRM_THRESHOLD` | `200` | depth from which confirmation is required |
| `FLIBUSTA_CLEANUP` | `true` | remove failed searches of the book bot |

New rules are added in `bot/services/stopwords.py` via `KeywordRule.build(...)` —
the engine does not need to change. Try changes with `/spamcheck` and in
`DRY_RUN=true` mode.

## Resilience

* Telegram API errors (`TelegramBadRequest`, `TelegramForbiddenError`,
  `TelegramRetryAfter`) are handled — moderation does not fail because of a deleted
  or too-old message.
* Redis unavailability degrades to reading from the DB; DB unavailability degrades
  to an empty whitelist with an error in the log; the bot keeps working.
* All moderation events are written to `moderation_events` and a structured JSON log.

## Tests

```bash
pytest -q                                  # full suite
pytest -q --cov=bot --cov-report=term-missing   # with coverage report
ruff check .                               # linter (settings in pyproject.toml)
python tests/test_antispam_engine.py       # any file also runs without pytest
python -m bot.tools.regress                # run the labeled sample
```

291 tests, `bot` package coverage 87%. Covered: text normalization, anti-false-positive
scenarios for book requests, spam detection, loading settings from `.env`, the
whitelist, blocklists, profile checks and the mailing detector (including
degradation on Redis and network failures), the trust middleware, approved-member
scope, the event log, false-positive rollback, manual labeling and reports,
newcomer checks on join, schema migrations, log formatters, safe Bot API wrappers
(including Telegram error branches), and an end-to-end run of updates through a
real dispatcher on a mocked Bot API.
`tests/test_regressions.py` — one test per bug found,
`tests/test_english_spam.py` — an English mailing that slipped through in the chat,
`tests/test_bot_adult_spam.py` — leetspeak and 18+ advertising via bots,
`tests/test_promo_campaign.py` — a real promo campaign replayed through the handler.

> Tests are isolated from the local `.env` and environment variables
> (`tests/conftest.py` + `_env_file=None`): lowering a threshold locally does not
> change the run.

Roadmap: `docs/RECOMMENDATIONS.md` (in Russian).

## License

[MIT](LICENSE)
