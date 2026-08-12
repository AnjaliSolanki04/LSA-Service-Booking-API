# LSA Service Booking API — Walkthrough for the Author

A plain-language explanation of what this project is, how it is put together, and
how a request travels through it. Written assuming you know basic Python and
nothing about Django.

---

## Table of contents

1. [What the project actually does](#1-what-the-project-actually-does)
2. [Vocabulary you need before the panel](#2-vocabulary-you-need-before-the-panel)
3. [MVC vs MVT — the question they will ask](#3-mvc-vs-mvt--the-question-they-will-ask)
4. [The folder structure, file by file](#4-the-folder-structure-file-by-file)
5. [The database, explained as a picture](#5-the-database-explained-as-a-picture)
6. [Flow 1 — searching for an assistant](#6-flow-1--searching-for-an-assistant)
7. [Flow 2 — creating a booking](#7-flow-2--creating-a-booking)
8. [Flow 3 — the payment webhook](#8-flow-3--the-payment-webhook)
9. [The three hard problems](#9-the-three-hard-problems)
10. [Testing and CI](#10-testing-and-ci)
11. [Questions the panel will ask, and answers](#11-questions-the-panel-will-ask-and-answers)

---

## 1. What the project actually does

HabotConnect connects **parents** of children with learning difficulties to
**Learning Support Assistants (LSAs)** — tutors with specific specialisations
like dyslexia support or ADHD coaching.

This project is the **backend** for that. Backend means: no screens, no buttons.
It is a program that sits on a server, waits for other programs to send it
messages over the internet, and sends structured answers back. A mobile app or a
website would be the thing with buttons; this is the thing the buttons talk to.

It does four jobs:

1. **Find me an assistant.** Given "I need dyslexia support in Bengaluru, under
   ₹900/hour, free next Tuesday at 4pm", return the matching assistants.
2. **Book one.** Reserve a time slot, and *guarantee* nobody else gets the same
   slot with the same assistant.
3. **Take the money.** Ask an external payment company to open a charge.
4. **React to the money.** When the payment company later says "that charge
   succeeded", flip the booking from *pending* to *confirmed* automatically.

Job 2 and job 4 are where all the difficulty is, and they are what the hiring
brief is really testing.

---

## 2. Vocabulary you need before the panel

You will be asked to use these words. Here is what each one means in this
project, with the file where you can point to it.

**API** — a way for one program to talk to another. Not a person clicking; a
program sending a message.

**REST / RESTful** — a style of API where you address *things* by URL and use
HTTP verbs to say what you want done to them. `GET /api/v1/bookings/` means "give
me bookings". `POST /api/v1/bookings/` means "create a booking". Same noun,
different verb, different meaning.

**Endpoint** — one specific URL + verb combination the API responds to. This
project has five. They are listed in `apps/bookings/urls.py`.

**JSON** — the text format both sides speak. Looks like a Python dict.

**Status code** — a three-digit number in every HTTP response saying how it went.
You must know these five, because the project deliberately uses each one:

| Code | Meaning | Where this project returns it |
|---|---|---|
| 200 | OK | Webhook applied successfully |
| 201 | Created | Booking created |
| 400 | Bad Request — *your payload is wrong* | Session in the past, end before start |
| 401 | Unauthorised — *I don't believe you are who you say* | Webhook signature invalid |
| 409 | Conflict — *your payload is fine, but the world says no* | Slot already booked |

The 400-vs-409 distinction is a deliberate design choice and a good thing to
volunteer in the presentation. A double-booking request is *perfectly well
formed* — every field is valid. What makes it fail is the current state of the
database. That is what 409 exists for.

**ORM (Object-Relational Mapper)** — lets you write Python instead of SQL.
`Booking.objects.filter(status="CONFIRMED")` becomes
`SELECT * FROM booking WHERE status = 'CONFIRMED'`. Django's ORM is what
`models.py` is built on.

**Model** — a Python class that describes one database table. `class Parent` in
`apps/bookings/models.py` describes the `parent` table. One class = one table,
one class attribute = one column.

**Migration** — Django compares your model classes to the database and generates
a script that changes the database to match. `apps/bookings/migrations/0001_initial.py`
is that script. You run `python manage.py migrate` to apply it. This is how the
schema gets created without you writing `CREATE TABLE` by hand.

**Serializer** — a translator between JSON (what arrives over the wire) and
Python objects (what the code works with), which also *validates* on the way in.
`apps/bookings/serializers.py`.

**Queryset** — an unevaluated database query you can keep adding to.
`LSAProfile.objects.available().with_skills(["dyslexia-support"])` has not
touched the database yet; it hits the database only when you iterate over it.
This laziness is why filters can be chained.

**Webhook** — a reversed API call. Normally *you* call the payment company. A
webhook is the payment company calling *you*, later, to report something. You
give them a URL; they POST to it when an event happens.

**HMAC** — a cryptographic stamp. You and the payment company share a secret
string. They mix the secret with the message body to produce a hash, and send
that hash in a header. You recompute it with the same secret. If your answer
matches theirs, the message really came from them and was not altered. Without
this, anyone who knows your webhook URL could POST "payment succeeded" and get a
free session. See `compute_webhook_signature` in
`apps/bookings/services/payment_gateway.py`.

**Idempotent** — doing it twice has the same effect as doing it once. Critical
for webhooks, because payment companies send the same event repeatedly if they
are unsure you received it.

**Race condition** — two requests arriving at the same instant, each reading the
database before the other writes, so both think the slot is free and both book
it. Section 9 covers how this project prevents that.

**N+1 query problem** — see section 9. The single most likely technical question
you will be asked.

---

## 3. MVC vs MVT — the question they will ask

The brief names this explicitly, so expect it.

**MVC (Model–View–Controller)** is the classic pattern:

- **Model** — the data and the rules about the data.
- **View** — what the user sees.
- **Controller** — receives the request, decides what to do, picks a view.

**MVT (Model–View–Template)** is Django's naming of the *same idea* with two
labels swapped:

- **Model** — same thing. `models.py`.
- **View** — Django's "view" is the **controller**, not the screen. It is a
  Python function that receives a request and returns a response. `views.py`.
- **Template** — the HTML file. This is what MVC calls the view.

So the honest one-line answer is: **MVT is MVC with different labels, where
Django's URL router plays the part of the controller's dispatcher and Django's
"view" plays the part of MVC's controller.**

The sharper follow-up answer, which is what actually earns marks:

> In a pure REST API there is no template at all — the response is JSON, not
> HTML. So the "T" is effectively unused. What replaces it is the **serializer**,
> which renders Python objects into the response format the same way a template
> renders them into HTML.

### Where this project deliberately departs from textbook MVT

Textbook Django puts business logic in the view or in the model. This project
adds a fourth layer:

```
Request
  │
  ▼
urls.py          Which function handles this URL?
  │
  ▼
views.py         Parse the request. Delegate. Serialise the answer.   ← thin
  │
  ▼
serializers.py   Is this payload shaped correctly and legal?
  │
  ▼
services/        WHAT SHOULD HAPPEN. All business rules live here.    ← the brain
  │
  ▼
models.py        How is it stored, and what does the database refuse?
  │
  ▼
Database
```

Why bother? Three reasons you can state:

1. **Testability.** `create_booking()` is a plain Python function. Tests can call
   it directly without pretending to be a web browser. See
   `test_service_layer_raises_conflict_independently_of_the_api`.
2. **Reuse.** The same rule applies whether a booking comes from the API, from a
   management command, or from a future admin action or background job.
3. **One source of truth.** If the overlap rule lived in the view, and later
   someone added a second way to create bookings, they would have to remember to
   copy the rule. Nobody remembers. Putting it in a service means they cannot
   miss it.

That third point is the **Poka-Yoke** principle the brief mentions — Japanese
manufacturing term for "mistake-proofing". Design the system so the wrong thing
is *impossible*, rather than merely documented as forbidden.

---

## 4. The folder structure, file by file

```
LSA-Service-Booking-API/
│
├── manage.py                    Django's command-line entry point.
│                                Every command starts here:
│                                  python manage.py runserver
│                                  python manage.py migrate
│                                  python manage.py seed_data
│
├── config/                      The PROJECT (settings and top-level wiring)
│   ├── settings.py              Every configurable value. Reads from environment
│   │                            variables so nothing secret is hard-coded.
│   │                            Chooses PostgreSQL if credentials exist, SQLite
│   │                            otherwise — so a reviewer can run it instantly.
│   ├── urls.py                  Top-level URL map. Sends /api/v1/* to the
│   │                            bookings app, /admin/ to Django admin,
│   │                            /api/docs/ to the auto-generated API docs.
│   ├── wsgi.py / asgi.py        How a production web server starts the app.
│   │                            You will never edit these.
│   └── __init__.py
│
├── apps/                        The APPS (the actual features)
│   │
│   ├── common/                  Shared building blocks, no features of its own
│   │   ├── models.py            Two abstract base classes:
│   │   │                          TimeStampedModel   → adds created_at/updated_at
│   │   │                          UUIDPrimaryKeyModel → uses UUID ids, not 1,2,3
│   │   │                        "Abstract" = no table of its own; other models
│   │   │                        inherit from it to get those columns for free.
│   │   ├── exceptions.py        Custom error classes (BookingConflictError,
│   │   │                        PaymentGatewayError, ...) plus one function that
│   │   │                        wraps EVERY error the API returns in the same
│   │   │                        JSON shape, so clients never have to guess.
│   │   └── views.py             GET /health/ — a liveness probe that pings the
│   │                            database. Used by CI and by container platforms.
│   │
│   ├── bookings/                THE MAIN APP — everything important is here
│   │   ├── models.py            The 5 tables. ~450 lines, the heart of the project.
│   │   ├── migrations/
│   │   │   └── 0001_initial.py  Auto-generated schema creation script.
│   │   ├── serializers.py       JSON ⇄ Python translation + payload validation.
│   │   ├── filters.py           Turns ?skills=dyslexia&city=Pune into SQL WHERE
│   │   │                        clauses. Nothing is filtered in Python.
│   │   ├── views.py             The five endpoint handlers. Deliberately thin.
│   │   ├── urls.py              URL → view mapping for this app.
│   │   ├── admin.py             Registers the models with Django's built-in
│   │   │                        admin UI. Free CRUD screens — good for the demo.
│   │   ├── services/            THE BUSINESS LOGIC
│   │   │   ├── booking_service.py   create_booking(): overlap prevention,
│   │   │   │                        pricing, transaction management.
│   │   │   ├── payment_gateway.py   The `requests` client that calls the external
│   │   │   │                        payment company. Retries, timeouts, HMAC
│   │   │   │                        signing helpers.
│   │   │   └── webhook_service.py   Applies an incoming payment event to the
│   │   │                            booking. Idempotent.
│   │   └── management/commands/
│   │       └── seed_data.py     `python manage.py seed_data --lsas 40`
│   │                            Fills the database with realistic demo data.
│   │
│   └── demo/                    A browsable HTML console (NOT part of the brief)
│       ├── views.py             Renders one page; also a dev-only endpoint that
│       │                        signs a fake payment event and replays it
│       │                        through the real webhook.
│       └── templates/demo/console.html
│                                Single-file UI. Lets you demonstrate the whole
│                                booking flow live to the panel without curl.
│
├── tests/                       75 tests
│   ├── conftest.py              Shared fixtures — reusable test data (a parent,
│   │                            an LSA, a booking, a signed webhook helper).
│   ├── test_models.py           Database constraints and state transitions.
│   ├── test_lsa_search.py       Search filters + the query-count assertions.
│   ├── test_booking_api.py      Booking creation, validation, conflicts.
│   ├── test_payment_gateway.py  Timeouts, retries, malformed responses.
│   └── test_payment_webhook.py  Signature, replay, tampering, state changes.
│
├── .github/workflows/ci.yml     Runs lint + the full suite against real
│                                PostgreSQL 16 on every push.
├── Dockerfile, docker-compose.yml
├── requirements.txt             Runtime dependencies, pinned to exact versions.
├── requirements-dev.txt         Adds pytest, coverage, ruff.
├── pytest.ini                   Test configuration.
├── .env.example                 Template for the secrets file. The real .env is
│                                gitignored and never committed.
└── README.md                    The technical documentation the brief asks for.
```

### Django's "project vs app" distinction

This confuses everyone at first. `config/` is the **project** — global settings,
one per repository. `apps/bookings/` is an **app** — a self-contained feature
that could in principle be lifted into another Django project. A project
contains many apps. `INSTALLED_APPS` in `settings.py` is the list of which ones
are switched on.

---

## 5. The database, explained as a picture

```
        ┌──────────────┐                        ┌──────────────┐
        │    Parent    │                        │    Skill     │
        │──────────────│                        │──────────────│
        │ id (UUID) PK │                        │ id       PK  │
        │ full_name    │                        │ slug  UNIQUE │
        │ email UNIQUE │                        │ name  UNIQUE │
        │ phone_number │                        └──────┬───────┘
        │ city         │                               │
        │ child_name   │                               │ many-to-many
        │ child_age    │                               │
        │ is_active    │                        ┌──────┴───────┐
        └──────┬───────┘                        │  LSAProfile  │
               │                                │──────────────│
               │                                │ id (UUID) PK │
               │  one parent,                   │ full_name    │
               │  many bookings                 │ email UNIQUE │
               │                                │ city         │
               │                                │ skills  M2M ─┘
               │                                │ years_of_exp │
               │                                │ hourly_rate  │
               │                                │ rating       │
               │                                │ is_active    │
               │                                │ is_verified  │
               │                                │ accepting_.. │
               │                                └──────┬───────┘
               │                                       │  one LSA,
               │        ┌──────────────────┐           │  many bookings
               └───────▶│     Booking      │◀──────────┘
                        │──────────────────│
                        │ id (UUID)     PK │
                        │ reference UNIQUE │   "HB-7F3A9C2D"
                        │ parent_id     FK │
                        │ lsa_id        FK │
                        │ scheduled_start  │
                        │ scheduled_end    │
                        │ session_mode     │   ONLINE | IN_PERSON
                        │ status           │   PENDING_PAYMENT | CONFIRMED |
                        │ total_amount     │   COMPLETED | CANCELLED | FAILED
                        │ currency         │
                        └────────┬─────────┘
                                 │  one booking,
                                 │  at most one payment
                        ┌────────▼─────────┐
                        │     Payment      │
                        │──────────────────│
                        │ id (UUID)     PK │
                        │ booking_id  1:1  │
                        │ gateway_ref UNIQ │
                        │ amount           │
                        │ status           │   INITIATED | SUCCEEDED |
                        │ failure_reason   │   FAILED | REFUNDED
                        │ last_event_id U  │   ← the idempotency key
                        │ raw_payload JSON │
                        └──────────────────┘
```

### Relationship types, in words

- **Parent → Booking is one-to-many.** One parent books many sessions; each
  booking belongs to exactly one parent. Implemented as a ForeignKey on Booking.
- **LSAProfile → Booking is one-to-many.** Same shape.
- **LSAProfile ↔ Skill is many-to-many.** One assistant has several skills; one
  skill is held by several assistants. Django creates a hidden join table for
  this automatically.
- **Booking → Payment is one-to-one.** A booking has at most one payment record.

### Why `Skill` is its own table (a designed decision, not an accident)

The lazy alternative is a text column on LSAProfile: `skills = "dyslexia,adhd"`.
Then searching means `WHERE skills LIKE '%dyslexia%'`.

That query **can never use an index**. A `LIKE` with a leading wildcard forces
the database to read every single row and check each one. With 50 assistants
nobody notices. With 50,000 the endpoint takes seconds.

With a separate table, the same search becomes
`WHERE skill.slug IN ('dyslexia-support')` — an index seek, effectively instant
regardless of table size. This is textbook **normalisation**, and it is the
single clearest example in the project of a schema decision driven by the query
that will run against it.

### Why UUIDs instead of 1, 2, 3

Two reasons. Sequential integers leak information — if your booking is `#847`,
a competitor knows you have had 847 bookings. And they are guessable, so anyone
could try `/api/v1/bookings/848/` and read someone else's data. UUIDs are neither
countable nor guessable.

### Constraints — rules the database itself enforces

Validation in Python is *advisory*. Anyone who writes to the database another way
(a script, a migration, the admin, a future bug) bypasses it. A database
constraint cannot be bypassed by anything. This project declares seven:

| Constraint | Rule |
|---|---|
| `booking_end_after_start` | A session cannot end before it starts |
| `booking_total_amount_non_negative` | No negative prices |
| `uniq_active_booking_per_lsa_slot` | Same LSA + same start + same end cannot exist twice while active |
| `lsa_rating_between_zero_and_five` | Ratings stay in range |
| `lsa_hourly_rate_non_negative` | No negative rates |
| `payment_amount_non_negative` | No negative payments |
| `parent_child_age_within_range` | Child age ≤ 25 |

### Indexes — and why each one exists

An index is a lookup structure that lets the database jump straight to matching
rows instead of scanning every row. They cost disk space and slow down writes
slightly, so you add them for queries you *actually run*, not speculatively.
Every index here maps to a real query:

| Index | Serves |
|---|---|
| `lsa_availability_idx` on (is_active, is_verified, accepting_bookings) | The availability filter that *every* search applies |
| `lsa_rating_rate_idx` on (-rating, hourly_rate) | Default ordering + the "cheap and highly rated" filter |
| `lsa_city_exp_idx` on (city, years_of_experience) | Location + experience filtering |
| `booking_overlap_idx` on (lsa, status, scheduled_start, scheduled_end) | The overlap check — exactly these four columns, in this order |
| `booking_parent_idx` on (parent, -scheduled_start) | "My bookings, newest first" |
| `booking_status_idx` on (status, scheduled_start) | Operational queries by state |
| `payment_status_idx` on (status, -created_at) | Reconciliation queries |

Note that `booking_overlap_idx` lists its columns *in the same order the overlap
query filters on them*. That ordering is not cosmetic — a composite index can
only be used left-to-right, so getting the order wrong makes it useless for that
query. Being able to say that sentence is worth a lot in the interview.

---

## 6. Flow 1 — searching for an assistant

**Request:**

```
GET /api/v1/lsas/search/?skills=dyslexia-support,adhd-coaching
                        &city=Bengaluru
                        &max_hourly_rate=900
                        &available_from=2026-08-20T10:00:00Z
                        &available_to=2026-08-20T11:00:00Z
```

**What happens, step by step:**

1. `config/urls.py` sees `/api/v1/` and hands off to `apps/bookings/urls.py`.
2. That maps `lsas/search/` to `LSASearchView`.
3. `get_queryset()` runs and builds the query in three stages:

   **Stage 1 — only bookable assistants.**
   `LSAProfile.objects.available()` adds
   `WHERE is_active AND is_verified AND accepting_bookings`. Served by
   `lsa_availability_idx`.

   **Stage 2 — the calendar filter.** If `available_from` and `available_to` were
   supplied, `.free_between(start, end)` excludes anyone with a clashing booking.
   It does this with a single correlated `NOT EXISTS` subquery — the database
   does the elimination and only surviving rows cross the network. The naive
   alternative (fetch all bookings into Python, loop, filter) transfers thousands
   of rows to throw most of them away.

   **Stage 3 — the N+1 fix.** `.with_related()` attaches a `Prefetch` so all
   skills for the whole page load in one extra query. Section 9 explains why.

4. `LSAProfileFilter` (in `filters.py`) applies the query-string filters. Each one
   becomes a SQL `WHERE` clause. Nothing is filtered in Python.
5. DRF paginates to 20 results.
6. `LSAProfileSerializer` converts each object to a JSON dict.
7. The response goes back.

**Total database queries: 3, always.** One COUNT for pagination, one for the
page of assistants, one for all their skills. Whether the page has 1 assistant or
20, it is still 3. That constancy is asserted by a test, not just claimed.

---

## 7. Flow 2 — creating a booking

**Request:**

```json
POST /api/v1/bookings/
{
  "parent_id": "3f2a…", "lsa_id": "9c1b…",
  "scheduled_start": "2026-08-20T10:00:00Z",
  "scheduled_end":   "2026-08-20T11:00:00Z",
  "session_mode": "ONLINE",
  "initiate_payment": true
}
```

**Step by step:**

1. **`BookingCreateSerializer` validates the payload.** Note this is a plain
   `Serializer`, not a `ModelSerializer` — deliberately. The request body is *not*
   a mirror of the table. There is no `status` field and no `total_amount` field,
   because if a client could send `total_amount` it could book a ₹5,000 session
   for ₹0. Both are derived server-side. This is a security decision worth
   stating out loud.

   It checks:
   - Does the parent exist, and is the account active?
   - Does the LSA exist, and is it active, verified, and accepting bookings?
   - Is end after start?
   - Is the duration between 30 and 240 minutes?
   - Is the start in the future, and within 180 days?

   Any failure → **400** with a field-by-field error map.

   Small optimisation: `validate_parent_id` stashes the fetched row on
   `self.context`, so the service layer does not query for it a second time.

2. **The view calls `create_booking()`** in `booking_service.py`. The whole
   function is wrapped in `@transaction.atomic` — everything inside either
   commits together or rolls back together.

3. **Row lock.** `LSAProfile.objects.select_for_update().get(pk=...)` issues
   `SELECT ... FOR UPDATE`, which locks *that one assistant's row* until the
   transaction ends. A second simultaneous request for the same assistant waits
   here. Requests for *different* assistants are unaffected, so throughput does
   not suffer.

4. **Overlap check, now inside the lock.** Two time ranges overlap when
   `existing.start < new.end AND existing.end > new.start`. Draw two lines on
   paper and you will see there is no other case.

   The intervals are **half-open**: a session ending at 10:00 and one starting at
   10:00 do *not* clash, which is how people actually book back-to-back
   appointments. There is a test for exactly this.

   Only PENDING_PAYMENT, CONFIRMED and COMPLETED block a slot. Cancelling or
   failing a booking releases it automatically — no cleanup job needed.

   If a clash is found → **409**, and the response names the blocking booking's
   reference so the client can explain the problem to the user.

5. **Price the session.** `hourly_rate × hours`, rounded with `ROUND_HALF_UP`.
   Uses `Decimal`, never `float`. Floats cannot represent 0.1 exactly and money
   arithmetic drifts; `Decimal` is exact. Another good detail to volunteer.

6. **Save.** If the database's unique constraint fires anyway, the `IntegrityError`
   is caught and translated into the *same* 409. The caller cannot tell which
   layer caught it, which is the point.

7. **Optionally open a payment.** If `initiate_payment` was true,
   `PaymentGatewayClient` calls the external service. Crucially, if the gateway is
   down the exception is caught, logged, and the booking **survives** in
   `PENDING_PAYMENT`. A third party's outage must not destroy the customer's
   booking. There is a test named exactly that:
   `test_a_gateway_outage_does_not_lose_the_booking`.

8. **201 Created**, with the full booking including its `HB-XXXXXXXX` reference.

---

## 8. Flow 3 — the payment webhook

This is the part most candidates get wrong, so it is where you can differentiate.

The payment company processes the card **asynchronously** — possibly minutes
later. It then POSTs an event to a URL you gave it. That call arrives from the
open internet, unauthenticated by default.

**Request:**

```
POST /api/v1/payments/webhook/
X-Habot-Signature: 4f3a9c2d…
X-Habot-Timestamp: 1786531200

{"id": "evt_123", "type": "payment.succeeded",
 "data": {"booking_reference": "HB-7F3A9C2D", "amount": "850.00"}}
```

**The four defences, in order:**

**1. Signature verification.** Recompute `HMAC-SHA256(secret, "timestamp.body")`
and compare. Two subtleties worth mentioning:

- It signs the **raw bytes** of the body, not re-serialised JSON. Re-serialising
  would change key order or whitespace and break the hash.
- It compares with `hmac.compare_digest`, not `==`. A normal `==` returns as soon
  as it finds a differing byte, so an attacker can measure response times to
  discover the secret one character at a time. `compare_digest` always takes the
  same time. This is a **timing attack**, and knowing the term is worth a lot.

Fails → **401**.

**2. Replay window.** The timestamp is baked into the signed string, and events
older than 300 seconds are rejected. Without this, a captured valid request could
be replayed forever.

**3. Idempotency.** `Payment.last_event_id` is a unique column. If an event id has
already been recorded, the handler returns the previous outcome and changes
nothing. Payment gateways deliver *at least once* — if your 200 was slow, they
resend. Without this, a duplicate "refunded" event could cancel a booking twice.

**4. Row lock.** `select_for_update()` on the booking, so two simultaneous
deliveries cannot interleave.

**Then the state machine runs.** Legal transitions are declared as data, once, in
`ALLOWED_BOOKING_TRANSITIONS`:

```
PENDING_PAYMENT ──payment.succeeded──▶ CONFIRMED ──▶ COMPLETED
       │                                   │
       ├──payment.failed────▶ FAILED       └──▶ CANCELLED
       └──parent cancels───▶ CANCELLED

FAILED ──▶ PENDING_PAYMENT   (retry allowed)
CANCELLED, COMPLETED         (terminal — nothing follows)
```

`transition_to()` refuses anything not on that list. So a webhook arriving late
for a booking the parent already cancelled **cannot resurrect it**. Instead it is
logged loudly and flagged for a manual refund review — the money arrived, so
silently ignoring it would be worse than failing.

**Status codes, chosen so the gateway retries the right things:**

| Code | When | Effect on the gateway |
|---|---|---|
| 200 | Applied, or already applied | Stops retrying |
| 202 | Understood but unactionable (unknown booking, event type we don't handle) | Stops retrying — we will never be able to apply it |
| 400 | Body is not valid JSON | Retrying identical garbage would not help |
| 401 | Bad signature | Rejected outright |

Never return 5xx from a webhook you cannot process. The gateway will retry
forever.

---

## 9. The three hard problems

### Problem 1 — the N+1 query problem

**What it is.** Your view fetches 20 assistants: 1 query. Then the serializer
renders each one, and each one touches `lsa.skills`, which Django has not loaded
— so it fires a query. Per assistant. That is 1 + 20 = **21 queries**. It grows
linearly with the page size, and nobody notices in development with 3 rows.

**The fix.** `prefetch_related` on the queryset:

```python
def with_related(self):
    return self.prefetch_related(
        models.Prefetch("skills", queryset=Skill.objects.only("id", "slug", "name"))
    )
```

Django now runs **one** extra query — `WHERE skill_id IN (…)` for all 20 at once —
and stitches the results onto the objects in Python. 21 queries become 2.

**Why `prefetch_related` and not `select_related`.** They are not
interchangeable, and this is the standard follow-up question:

| | `select_related` | `prefetch_related` |
|---|---|---|
| Mechanism | SQL `JOIN`, one query | Two queries, joined in Python |
| Works for | ForeignKey, OneToOne (one related row) | ManyToMany, reverse FK (many rows) |
| Used here for | `booking.parent`, `booking.lsa`, `booking.payment` | `lsa.skills` |

Skills are many-to-many, so a JOIN would duplicate the assistant row once per
skill — an assistant with 4 skills appears 4 times and you have to de-duplicate.
`prefetch_related` avoids that entirely.

**`.only("id", "slug", "name")`** on the prefetch is a further refinement: it
tells the database not to bother sending the `description` text column, which the
serializer never uses.

**The part that actually matters.** This is *enforced*, not just documented:

```python
def test_search_query_count_is_constant_regardless_of_result_size(...):
    with assertNumQueries(3):
        api_client.get("/api/v1/lsas/search/")   # 5 assistants
    with assertNumQueries(3):
        api_client.get("/api/v1/lsas/search/")   # 25 assistants
```

If someone later deletes `.with_related()`, the build goes red. The optimisation
cannot silently regress. Point at this test in the presentation — it is the
difference between "I know about N+1" and "I engineered against it."

### Problem 2 — double-booking under concurrency

**Why the obvious solution is wrong.** This looks correct and is not:

```python
if not Booking.objects.filter(lsa=lsa, ...overlap...).exists():
    Booking.objects.create(...)          # ← two requests can both reach here
```

Two requests arriving microseconds apart both run the check against a database
where the other has not written yet. Both see "free". Both insert. Two parents,
one assistant, one time slot. The bug appears only under load, which means it
appears in production and not in your testing.

**Three layers, cheapest first:**

```python
# 1. Row lock — serialises concurrent attempts for THIS assistant only
locked_lsa = LSAProfile.objects.select_for_update().get(pk=lsa.pk)

# 2. Overlap check, now inside a consistent snapshot
clash = Booking.objects.overlapping(locked_lsa.pk, start, end).first()
if clash:
    raise BookingConflictError(...)

# 3. Database constraint — the actual guarantee
models.UniqueConstraint(
    fields=["lsa", "scheduled_start", "scheduled_end"],
    condition=Q(status__in=BLOCKING_BOOKING_STATUSES),
    name="uniq_active_booking_per_lsa_slot",
)
```

**Be precise about what each layer buys you.** Layer 3 is the only thing that
makes the rule *unbreakable* — it holds even against a different code path, a
raw SQL script, or a future refactor that forgets the lock. Layers 1 and 2 exist
so the common case returns a clean, informative 409 instead of an exception
trace. That distinction — "which layer is the guarantee and which is the user
experience" — is a senior-sounding answer.

**Known limitation, and say it before they ask.** The unique constraint catches
*identical* slots, not *partially overlapping* ones (10:00–11:00 vs 10:30–11:30).
The lock plus the overlap query handles those. The complete database-level
solution is PostgreSQL's `ExclusionConstraint` with `tstzrange` and the
`btree_gist` extension, which enforces arbitrary overlap in the database itself.
It is listed in README §12 as the next step. Naming your own gap is more
impressive than having it found.

### Problem 3 — an unreliable third party

Everything touching the network is isolated in `PaymentGatewayClient`, so no
other file imports `requests`. That gives one place to configure timeouts, one
place to translate transport failures into domain errors, and one seam to mock in
tests.

**Every failure mode is handled and tested:**

| Failure | Response |
|---|---|
| Timeout | Retry with exponential backoff (0.25s, 0.5s, 1s), then `PaymentGatewayError` |
| Connection refused | Same |
| 429 / 500 / 502 / 503 / 504 | Retryable — retry |
| 400–499 | **Not** retried. It is our mistake; retrying burns their rate limit |
| Non-JSON body | Caught, logged with a truncated preview, domain error raised |
| JSON missing the `id` field | Caught, domain error raised |

Two more details worth mentioning:

- **An idempotency key** is sent on every charge request, so a retry after a
  network timeout re-uses the original charge instead of billing the parent twice.
- **No test touches the network.** The `responses` library intercepts HTTP, so the
  suite is fast, deterministic, and works offline — which is why it runs in CI.

---

## 10. Testing and CI

**75 tests**, and the naming is deliberately descriptive so a failure reads like
a sentence: `test_back_to_back_sessions_are_allowed`,
`test_tampering_with_the_body_after_signing_is_detected`.

| File | Tests | Covers |
|---|---|---|
| `test_models.py` | 13 | Constraints, state transitions, derived properties |
| `test_lsa_search.py` | 11 | Filters, availability, **query counts**, pagination |
| `test_booking_api.py` | 19 | Creation, every validation rule, conflicts |
| `test_payment_gateway.py` | 12 | Timeouts, retries, malformed responses, outages |
| `test_payment_webhook.py` | 14 | Signature, replay, tampering, idempotency, states |

The brief asks for success, edge, and failure cases. Examples to cite:

- **Success** — a valid booking returns 201 with a reference.
- **Edge** — back-to-back sessions at exactly 10:00 are *allowed*; a cancelled
  booking frees its exact slot for re-use.
- **Failure** — overlapping request returns 409; tampered webhook returns 401.

**`conftest.py`** holds shared fixtures. A fixture is a reusable piece of test
setup — `parent`, `lsa`, `booking`, `signed_webhook`. Any test that names one as
an argument gets a fresh copy, and pytest rolls the database back afterwards.

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request:

1. **Lint** — `ruff format --check` and `ruff check`.
2. **Test matrix** — Python 3.11 and 3.12, in parallel.
3. Against **real PostgreSQL 16**, not SQLite. This matters: the schema depends
   on database constraints, and those must be exercised against the engine you
   will actually deploy on.
4. **`makemigrations --check --dry-run`** — fails the build if a model was changed
   without generating a migration. That is the single most common way a Django
   deploy breaks, and this catches it before merge.
5. **`manage.py check --deploy`** — Django's production security checklist.
6. **Coverage** reported and uploaded as an artifact.

Step 4 is the best one to highlight — it is Poka-Yoke applied to CI.

---

## 11. Questions the panel will ask, and answers

**"Explain MVC vs MVT."**
Same pattern, different labels. Django's "view" is MVC's controller; Django's
"template" is MVC's view; the URL router does the dispatching. In a JSON API the
template layer is unused — serializers take its place, rendering objects into the
response format. I added a service layer below the views so business rules are
testable without HTTP and reusable outside the request cycle.

**"What is the N+1 problem and how did you solve it?"**
One query for the list, then one per row for its related data. 20 assistants
becomes 21 queries and grows linearly. I used `prefetch_related` with an explicit
`Prefetch` and `.only()`, which makes it a constant 3 queries. And I asserted the
query count in a test, so removing the optimisation turns the build red.

**"Why `prefetch_related` rather than `select_related`?"**
`select_related` is a SQL JOIN and only works for single-valued relations —
ForeignKey and OneToOne. Skills are many-to-many, so a JOIN would duplicate each
assistant row once per skill. `prefetch_related` issues a second query and joins
in Python, which avoids the fan-out. I use `select_related` for
`booking.parent`, `booking.lsa` and `booking.payment`, where it is the right tool.

**"How do you prevent double-booking?"**
Three layers. `select_for_update` locks the assistant's row so concurrent
attempts for that assistant serialise. The overlap check then runs inside that
consistent snapshot. And a conditional unique constraint in the database is the
actual guarantee — it holds even if the lock is bypassed by a different code
path. The `IntegrityError` is translated back into the same 409, so the caller
cannot tell which layer caught it.

**"Why 409 and not 400?"**
400 means the payload is malformed. A double-booking request is perfectly well
formed — every field is valid. What makes it fail is the current state of the
resource, which is precisely what 409 Conflict is defined for. Clients can act on
that distinction: a 400 means fix your input, a 409 means try a different slot.

**"How is the webhook secured?"**
HMAC-SHA256 over the raw request bytes with a shared secret, compared using
`compare_digest` so the comparison does not leak the secret by timing. The
timestamp is bound into the signed string and events older than 300 seconds are
rejected, which closes the replay window. Delivery is at-least-once, so
processing is idempotent via a unique `last_event_id` column — a replay returns
the original outcome and changes nothing.

**"Which database, and why does it fall back to SQLite?"**
PostgreSQL in CI and in the Docker Compose setup. The settings fall back to
SQLite when no credentials are present so a reviewer can clone and run the suite
in about a minute with zero infrastructure. That is a developer-experience
choice, not the production configuration.

**"What would you do differently with more time?"**
Authentication is the biggest gap — endpoints are `AllowAny` so they can be
exercised with curl; production needs JWT with parents scoped to their own
bookings. Then PostgreSQL's `ExclusionConstraint` for true range-overlap
enforcement in the database, moving the gateway call to Celery so a slow third
party never blocks a request thread, and per-user rate limiting. They are all
written up in README §12.

**"What are you least happy with?"**
Have a real answer ready. A good one: the unique constraint catches identical
slots but not partial overlaps, so the lock is currently doing more work than the
schema is. An `ExclusionConstraint` would move that guarantee down into the
database where it belongs.
