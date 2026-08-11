# HabotConnect — LSA Service Booking API

**Candidate:** Vansh Mehta
**Email:** mehtavansh6626@gmail.com
**Position:** Python Backend Developer — Hiring Project
**Submission date:** August 2026

A production-shaped Django REST Framework backend for HabotConnect's platform connecting parents with Learning Support Assistants (LSAs) for children with learning difficulties.

---

## Table of contents

1. [What this delivers](#1-what-this-delivers)
2. [Quick start](#2-quick-start)
3. [Architecture and design choices](#3-architecture-and-design-choices)
4. [Database schema](#4-database-schema)
5. [API specification](#5-api-specification)
6. [Query optimisation: the N+1 problem](#6-query-optimisation-the-n1-problem)
7. [Preventing double-bookings](#7-preventing-double-bookings)
8. [Third-party integration and the webhook](#8-third-party-integration-and-the-webhook)
9. [Testing](#9-testing)
10. [CI/CD](#10-cicd)
11. [Git workflow](#11-git-workflow)
12. [What I would do next](#12-what-i-would-do-next)

---

## 1. What this delivers

| Requirement | Where it lives | Status |
|---|---|---|
| Normalised, indexed schema (Parent, LSA, Booking, Payment) | `apps/bookings/models.py` | Done |
| Optimised LSA search, N+1 resolved | `apps/bookings/views.py`, `models.py` | Done |
| `POST /api/v1/bookings/` with overlap prevention | `services/booking_service.py` | Done |
| `POST /api/v1/payments/webhook/` driving state transitions | `services/webhook_service.py` | Done |
| Mock third-party integration via `requests` | `services/payment_gateway.py` | Done |
| Automated test suite (≥ 5 required) | `tests/` — **75 tests** | Done |
| GitHub Actions CI | `.github/workflows/ci.yml` | Done |
| Technical documentation | this file | Done |

**Test suite: 75 passing.** Requirement was 5.

```
$ pytest -q
...........................................................................
75 passed in 3.61s
```

---

## 2. Quick start

### Option A — zero infrastructure (SQLite, ~60 seconds)

The project falls back to SQLite when no PostgreSQL credentials are present, so the suite runs on a clean machine with nothing installed but Python.

```bash
git clone <your-repo-url>
cd habot-lsa-booking-api

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt

python manage.py migrate
python manage.py seed_data          # demo data - run this
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> — a browsable console that walks the whole flow: search for an assistant, book them, watch the double-booking get refused, fire a payment webhook. Every button calls the real API and shows you the request and response.

Prefer raw API docs? <http://127.0.0.1:8000/api/docs/> is the interactive Swagger UI.

Run the tests:

```bash
pytest -v
```

### Option B — PostgreSQL via Docker (matches CI)

```bash
cp .env.example .env
docker compose up --build
```

This starts PostgreSQL 16, applies migrations, seeds demo data, and serves the API on <http://localhost:8000>.

To run against Postgres without Docker, set the `POSTGRES_*` variables in `.env` (or a single `DATABASE_URL`) and the project picks them up automatically.

### Configuration

Every setting is environment-driven — see `.env.example` for the full list. Nothing secret is committed.

---

## 3. Architecture and design choices

### MVT vs MVC — and why Django

The brief asks for an explanation of MVC vs MVT. They describe the same separation of concerns; the disagreement is over what "controller" means.

| | Classic MVC (Flask, Rails, Spring) | Django MVT |
|---|---|---|
| Data + business rules | Model | Model |
| Presentation | View | Template |
| Request handling / orchestration | Controller | View |
| Who maps URL → handler | The developer's controller | The framework's URL resolver |

Django's argument is that the "controller" in a web framework is almost entirely boilerplate — parse the URL, dispatch, marshal the request — so the framework owns it. What Django calls a **View** is what MVC calls a **Controller**; what MVC calls a **View** Django calls a **Template**. The naming is different; the layering is not.

**In a REST API the template layer disappears entirely**, because the response is JSON, not HTML. DRF replaces it with **serializers**, which give something templates never did: bidirectional translation with validation on the way in.

So this project's effective layering is:

```
URL resolver  →  View        →  Serializer    →  Service        →  Model
(config/urls)    (thin:         (validation,     (business        (data +
                  parse and      shape of the     rules, txns,     invariants +
                  delegate)      contract)        integration)     constraints)
```

**Why Django/DRF over Flask** for this brief specifically:

- The assessment weights **ORM query efficiency, migrations, and relational modelling** heavily. Django's ORM gives `prefetch_related`, `select_for_update`, `CheckConstraint`, and `UniqueConstraint(condition=...)` as first-class tools. In Flask + SQLAlchemy each of those is hand-rolled.
- Migrations are generated and checked automatically; CI can fail a build for a *missing* migration, which is the most common way a Django deploy breaks.
- DRF gives consistent status codes, content negotiation, throttling, and an OpenAPI schema for free — all of which the brief lists as assessment criteria.

### Why a service layer

Views here are deliberately thin. All booking rules live in `apps/bookings/services/`. This matters because:

- The double-booking guarantee holds whether a booking is created by the API, a management command, an admin action, or a future background job. It is not an HTTP-layer concern.
- Business rules are unit-testable without spinning up a request cycle (see `test_service_layer_raises_conflict_independently_of_the_api`).
- Views stay readable — each one is under 30 lines.

### Poka-Yoke: rules that cannot be forgotten

The brief explicitly asks for mistake-proof design. The approach taken throughout is **make the invalid state unrepresentable, rather than documenting that it is invalid**:

| Risk | Mistake-proofing |
|---|---|
| Someone forgets to prefetch and reintroduces N+1 | A test asserts the query count is *constant* as rows grow; it fails on regression |
| Two concurrent requests double-book an LSA | Database `UniqueConstraint`, not just a Python check |
| A booking ends before it starts | `CheckConstraint` at the database level |
| A developer writes an illegal state transition | `ALLOWED_BOOKING_TRANSITIONS` table + `transition_to()` raises |
| A client dictates the price | `total_amount` is derived server-side and the write serializer does not accept it |
| A forged webhook confirms a booking | HMAC-SHA256 signature + replay window, verified before any state is read |
| A gateway replay double-applies an event | `last_event_id` unique index makes reprocessing a no-op |
| A model change ships without a migration | CI runs `makemigrations --check --dry-run` |
| Audit columns get forgotten on a new table | `TimeStampedModel` abstract base |

---

## 4. Database schema

```
┌────────────────┐             ┌──────────────────┐          ┌────────────────┐
│     Parent     │             │    LSAProfile    │          │     Skill      │
├────────────────┤             ├──────────────────┤          ├────────────────┤
│ id (UUID) PK   │             │ id (UUID) PK     │   M2M    │ id  PK         │
│ full_name      │             │ full_name        │◄────────►│ slug   UNIQUE  │
│ email  UNIQUE  │             │ email  UNIQUE    │          │ name   UNIQUE  │
│ phone_number   │             │ city             │          │ description    │
│ city           │             │ hourly_rate      │          └────────────────┘
│ child_name     │             │ years_experience │
│ child_age      │             │ rating           │
│ is_active      │             │ is_verified      │
└───────┬────────┘             │ accepting_bookngs│
        │                      └────────┬─────────┘
        │ 1                             │ 1
        │                               │
        │ N                             │ N
        └──────────┬────────────────────┘
                   ▼
          ┌──────────────────────┐            ┌────────────────────┐
          │       Booking        │    1   1   │      Payment       │
          ├──────────────────────┤◄──────────►├────────────────────┤
          │ id (UUID) PK         │            │ id (UUID) PK       │
          │ reference   UNIQUE   │            │ booking_id  FK 1:1 │
          │ parent_id   FK       │            │ gateway_ref UNIQUE │
          │ lsa_id      FK       │            │ amount             │
          │ scheduled_start      │            │ status             │
          │ scheduled_end        │            │ failure_reason     │
          │ status               │            │ processed_at       │
          │ total_amount         │            │ last_event_id  UQ  │
          │ session_mode         │            │ raw_payload JSON   │
          └──────────────────────┘            └────────────────────┘
```

### Relationships

| Relationship | Cardinality | On delete | Why |
|---|---|---|---|
| Parent → Booking | 1 : N | `PROTECT` | Deleting a parent with financial history must fail loudly, not cascade |
| LSAProfile → Booking | 1 : N | `PROTECT` | Same — a booking is a financial record |
| LSAProfile ↔ Skill | M : N | — | An LSA has many skills; a skill has many LSAs |
| Booking → Payment | 1 : 1 | `CASCADE` | A payment has no meaning without its booking |

### Normalisation

The schema is in **3NF**. The decision worth calling out is `Skill` as its own table:

```python
# Rejected — unindexable
skills = models.CharField(max_length=500)   # "dyslexia,adhd,speech"
# → WHERE skills LIKE '%dyslexia%'   ... full table scan, always

# Chosen — indexed join
skills = models.ManyToManyField(Skill)
# → WHERE skill.slug IN ('dyslexia-support')   ... index seek
```

A `LIKE '%...%'` predicate cannot use a B-tree index, so the comma-separated version degrades linearly with table size forever. It also cannot enforce that a skill name is spelled consistently, cannot be renamed atomically, and cannot carry a description.

### Indexes, and why each one exists

| Index | Columns | Query it serves |
|---|---|---|
| `lsa_availability_idx` | `is_active, is_verified, accepting_bookings` | Applied on **every** search request |
| `lsa_rating_rate_idx` | `-rating, hourly_rate` | Default ordering + rate ceiling filter |
| `lsa_city_exp_idx` | `city, years_of_experience` | Location + experience filters |
| `booking_overlap_idx` | `lsa, status, scheduled_start, scheduled_end` | **The overlap check** — column order matches the predicate exactly |
| `booking_parent_idx` | `parent, -scheduled_start` | "My bookings, newest first" |
| `booking_status_idx` | `status, scheduled_start` | Operational dashboards, reminder jobs |
| `payment_status_idx` | `status, -created_at` | Reconciliation of stuck payments |

Column *order* in `booking_overlap_idx` is deliberate: equality predicates (`lsa`, `status`) come before range predicates (`scheduled_start`, `scheduled_end`), which is what lets a B-tree narrow to a contiguous range instead of scanning.

### Constraints

```python
# Booking
CheckConstraint(scheduled_end > scheduled_start)       # no negative-length sessions
CheckConstraint(total_amount >= 0)                     # no negative money
UniqueConstraint(lsa, scheduled_start, scheduled_end,
                 condition=Q(status__in=BLOCKING))     # no duplicate active slot

# LSAProfile
CheckConstraint(0.00 <= rating <= 5.00)
CheckConstraint(hourly_rate >= 0)

# Payment
CheckConstraint(amount >= 0)
UNIQUE(last_event_id)                                  # webhook idempotency
```

Note the **partial** unique constraint on `Booking`. It applies only to blocking statuses, so cancelling a booking genuinely releases the slot for someone else — a plain unique index would have left the slot poisoned forever.

---

## 5. API specification

Base URL: `/api/v1/`
Interactive docs: `/api/docs/` (Swagger) · `/api/redoc/` (ReDoc) · `/api/schema/` (raw OpenAPI)

### `GET /api/v1/lsas/search/`

Search bookable LSAs.

| Parameter | Type | Description |
|---|---|---|
| `skills` | string | Comma-separated slugs: `dyslexia-support,adhd-coaching` |
| `match_all_skills` | bool | `true` = must hold every skill; default `false` = any |
| `city` | string | Case-insensitive exact match |
| `min_experience` | int | Minimum years |
| `max_hourly_rate` | decimal | Rate ceiling |
| `min_rating` | decimal | 0.00–5.00 |
| `available_from` / `available_to` | ISO-8601 | Exclude LSAs already booked in that window |
| `page` | int | Page number (20 per page) |

```bash
curl "http://localhost:8000/api/v1/lsas/search/?skills=dyslexia-support&min_rating=4.5&city=Bengaluru"
```

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": "8f14e45f-ceea-467a-9a1b-1b9c2f3d4e5a",
      "full_name": "Priya Nair",
      "email": "priya.nair@example.test",
      "city": "Bengaluru",
      "skills": [
        {"id": 1, "slug": "dyslexia-support", "name": "Dyslexia Support"},
        {"id": 2, "slug": "adhd-coaching",    "name": "ADHD Coaching"}
      ],
      "years_of_experience": 7,
      "hourly_rate": "1200.00",
      "rating": "4.80",
      "is_verified": true,
      "is_bookable": true
    }
  ]
}
```

**Status codes:** `200` always (an unmatched filter returns an empty list, not a 404).

---

### `POST /api/v1/bookings/`

Create a booking request.

```json
{
  "parent_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "lsa_id": "8f14e45f-ceea-467a-9a1b-1b9c2f3d4e5a",
  "scheduled_start": "2026-08-20T10:00:00Z",
  "scheduled_end":   "2026-08-20T11:00:00Z",
  "session_mode": "ONLINE",
  "notes": "Please focus on reading comprehension.",
  "initiate_payment": false
}
```

`201 Created`:

```json
{
  "reference": "HB-7F3A9C2D",
  "status": "PENDING_PAYMENT",
  "duration_minutes": 60,
  "total_amount": "1200.00",
  "currency": "INR",
  "parent": { "...": "..." },
  "lsa":    { "...": "..." },
  "payment": null
}
```

`409 Conflict` — overlapping session:

```json
{
  "error": {
    "code": "booking_conflict",
    "message": "This Learning Support Assistant already has a session booked in the requested time window.",
    "details": {
      "conflicting_booking_reference": "HB-1A2B3C4D",
      "conflicting_start": "2026-08-20T10:30:00+00:00",
      "conflicting_end":   "2026-08-20T11:30:00+00:00"
    }
  }
}
```

| Code | When |
|---|---|
| `201` | Created |
| `400` | Payload invalid — past date, bad duration, unverified LSA, unknown ID |
| `409` | Time slot overlaps an existing active booking |

**Validation applied:** parent exists and is active · LSA exists, is active, verified and accepting bookings · end strictly after start · duration between 30 and 240 minutes · start in the future · start within 180 days · no overlap.

Note the deliberate **400 vs 409** split. A malformed payload is the client's error (400). A well-formed payload that the current state of the world makes impossible is a conflict (409) — a client can meaningfully retry the latter with a different slot.

---

### `GET /api/v1/bookings/` · `GET /api/v1/bookings/{reference}/`

List (filterable by `status`, `parent_id`, `lsa_id`) or retrieve by human-readable reference.

---

### `POST /api/v1/payments/webhook/`

Consumes gateway events. **Requires a valid signature.**

Headers:

```
X-Habot-Signature: <hex HMAC-SHA256 of "{timestamp}.{raw_body}">
X-Habot-Timestamp: <unix seconds>
```

Body:

```json
{
  "id": "evt_a1b2c3",
  "type": "payment.succeeded",
  "data": {
    "booking_reference": "HB-7F3A9C2D",
    "gateway_reference": "pi_mock_00001",
    "amount": "1200.00",
    "currency": "INR",
    "method": "card"
  }
}
```

| Event | Effect on booking | Effect on payment |
|---|---|---|
| `payment.succeeded` | `PENDING_PAYMENT` → `CONFIRMED` | → `SUCCEEDED` |
| `payment.failed` | `PENDING_PAYMENT` → `FAILED` (slot released) | → `FAILED` |
| `payment.refunded` | `CONFIRMED` → `CANCELLED` | → `REFUNDED` |

| Code | Meaning | Gateway should |
|---|---|---|
| `200` | Applied, or already applied | Stop |
| `202` | Understood but unactionable (unknown booking, unsupported type) | Stop |
| `400` | Malformed body | Stop — retrying identical garbage will not help |
| `401` | Bad or missing signature | Stop |

The status codes are chosen around **what the gateway will do next**. Returning 500 for an unknown booking would make the gateway retry forever against an event we can never apply.

---

### `GET /health/`

Liveness + database readiness. Returns `503` if the database is unreachable.

---

### `GET /` — demo console

A single-page client over the API, so the booking flow can be demonstrated without reading raw JSON. It holds **no business logic** — every action issues an ordinary `fetch()` to the endpoints documented above and displays the response verbatim.

One development-only route supports it: `POST /demo/simulate-payment/`. A genuine webhook is signed by the gateway with a shared secret, which a browser cannot hold without leaking it. Rather than weaken the webhook for a demo, this route signs the event server-side and replays it through the real signature-verified endpoint — so the signature check, the idempotency guard and the state machine all still run. It returns `404` whenever `DEBUG` is off, so it cannot be reached in production.

---

## 6. Query optimisation: the N+1 problem

### The problem

`LSAProfileSerializer` nests skills:

```python
skills = SkillSerializer(many=True, read_only=True)
```

Serialising each LSA touches `lsa.skills`. Without a prefetch, Django lazily issues **one query per LSA**:

```sql
SELECT ... FROM lsa_profile WHERE is_active AND is_verified LIMIT 20;   -- 1
SELECT ... FROM skill JOIN ... WHERE lsaprofile_id = '...';             -- 2
SELECT ... FROM skill JOIN ... WHERE lsaprofile_id = '...';             -- 3
...                                                                     -- 21
```

**21 round trips for 20 results.** At 100 results it is 101. Each is a network hop, so latency grows linearly with page size — and it only shows up under production data volume, never in local development with three rows.

### The fix

```python
# apps/bookings/models.py
def with_related(self):
    return self.prefetch_related(
        models.Prefetch("skills", queryset=Skill.objects.only("id", "slug", "name"))
    )

# apps/bookings/views.py
def get_queryset(self):
    return LSAProfile.objects.available().with_related().order_by("-rating", "full_name")
```

Now:

```sql
SELECT ... FROM lsa_profile WHERE ... LIMIT 20;                          -- 1
SELECT ... FROM skill JOIN lsa_skills WHERE lsaprofile_id IN (...20...); -- 2
```

**Constant 2 queries** (3 counting pagination's `COUNT`), regardless of page size.

| Result set | Naive | Optimised |
|---|---|---|
| 20 | 21 queries | 3 |
| 100 | 101 queries | 3 |
| 500 | 501 queries | 3 |

### Why `prefetch_related`, not `select_related`

They solve different shapes and are not interchangeable:

- `select_related` — follows **forward FK / one-to-one** with a SQL `JOIN`, single query. Used on `Booking` for `parent`, `lsa`, `payment`.
- `prefetch_related` — handles **many-to-many and reverse FK** with a second query and an in-Python join. A `JOIN` cannot be used here because it would multiply the LSA rows by their skill count and force deduplication of a much larger result set.

### The availability filter

Excluding LSAs already booked in a window uses a correlated `NOT EXISTS`:

```python
def free_between(self, start, end):
    clashing = Booking.objects.filter(
        lsa_id=models.OuterRef("pk"),
        status__in=BLOCKING_BOOKING_STATUSES,
        scheduled_start__lt=end,
        scheduled_end__gt=start,
    )
    return self.exclude(models.Exists(clashing))
```

The database does the elimination; only surviving rows cross the wire. The alternative — fetching bookings and filtering in Python — would transfer every booking for every candidate LSA and then discard most of them.

### `only()` on the prefetch

`Skill.objects.only("id", "slug", "name")` skips `description` (a `TextField`) and the audit columns. The serializer never reads them, so shipping them is pure wasted bytes on a query that runs on every search.

### This is enforced, not just documented

```python
def test_search_query_count_is_constant_regardless_of_result_size(...):
    # 5 LSAs
    with CaptureQueriesContext(connection) as small:
        api_client.get(URL, {"page_size": 100})
    # 40 LSAs
    with CaptureQueriesContext(connection) as large:
        api_client.get(URL, {"page_size": 100})

    assert len(small.captured_queries) == len(large.captured_queries)
    assert len(large.captured_queries) <= 4
```

Delete `.with_related()` from the view and this test fails immediately with the offending SQL printed. The optimisation cannot silently regress.

To watch the SQL yourself, set `SQL_LOG_LEVEL=DEBUG` in `.env`.

---

## 7. Preventing double-bookings

### Detecting overlap correctly

Two intervals overlap when:

```
existing.start < new.end   AND   existing.end > new.start
```

Strict inequalities make the interval **half-open** — `[start, end)`. That means 09:00–10:00 and 10:00–11:00 do **not** clash, which is how people actually book back-to-back appointments. Using `<=` would reject a perfectly valid consecutive session.

Five overlap geometries are covered, each with its own parametrised test:

```
existing:        |=========|
identical:       |=========|      reject
starts inside:        |=========| reject
ends inside:   |=====|            reject
swallows:     |=============|     reject
inside:            |===|          reject
adjacent:                  |====| ALLOW
```

### Why a Python check alone is not enough

```
Request A                       Request B
─────────                       ─────────
SELECT ... overlap? → none
                                SELECT ... overlap? → none
INSERT booking ✓
                                INSERT booking ✓   ← double-booked
```

Both requests read before either wrote. The check passed for both. This is not a hypothetical — it is the default outcome under any real concurrency.

### Three layers

```python
# 1. Row lock — serialises concurrent attempts for THIS LSA only
locked_lsa = LSAProfile.objects.select_for_update().get(pk=lsa.pk)

# 2. Overlap check, now inside the lock's consistent snapshot
clash = Booking.objects.overlapping(locked_lsa.pk, start, end).first()
if clash:
    raise BookingConflictError(...)

# 3. Database constraint — the actual guarantee
try:
    booking.save()
except IntegrityError:
    raise BookingConflictError(...)
```

- **Layer 1** blocks only requests for the same LSA. Bookings for different LSAs never contend, so throughput is unaffected.
- **Layer 2** returns a clean, informative 409 in the common case.
- **Layer 3** is what makes the rule *unbreakable*. Even if a future refactor bypasses the service, or a read replica serves a stale snapshot, the database itself refuses the row.

Layer 3 alone would be correct but would produce an ugly `IntegrityError`. Layers 1–2 alone would be friendly but racy. Together: friendly *and* correct.

### Cancellation releases the slot

Because the unique constraint is conditional on `status__in=BLOCKING_BOOKING_STATUSES`, a cancelled or failed booking stops blocking the calendar — verified by `test_a_cancelled_booking_releases_its_slot`.

### Production note

On PostgreSQL, `ExclusionConstraint` with a `tstzrange` and the `btree_gist` extension would enforce *arbitrary* overlap at the database level, not just exact-duplicate slots — a strictly stronger guarantee. I have used the conditional `UniqueConstraint` here because it works identically on PostgreSQL and SQLite, keeping the reviewer's setup at zero infrastructure. In a Postgres-only production deployment, the exclusion constraint is the right call, and the migration is a small addition.

---

## 8. Third-party integration and the webhook

### The gateway client

All outbound HTTP is isolated in `services/payment_gateway.py`. Nothing else in the codebase imports `requests`.

**Exception handling** — every failure mode is caught and translated:

| Failure | Handling |
|---|---|
| `requests.Timeout` | Retried with exponential backoff, then `PaymentGatewayError` |
| `requests.ConnectionError` | Retried, then `PaymentGatewayError` |
| `429`, `5xx` | Retried (transient) |
| `4xx` | **Not** retried — the request is wrong; retrying burns rate limit |
| Non-JSON body | Logged with a truncated snippet, `PaymentGatewayError` |
| JSON missing required fields | `PaymentGatewayError` with context |

Backoff is `0.25s → 0.5s → 1s`. Timeouts are always explicit — a `requests` call with no timeout can hang a worker thread indefinitely.

**Idempotency:** every charge sends an `Idempotency-Key` derived from the booking UUID, so a retry after a network timeout re-uses the original charge rather than billing the parent twice.

**A gateway outage does not lose the booking.** If the charge cannot be opened, the failure is logged and the booking stays in `PENDING_PAYMENT` for a retry — verified by `test_a_gateway_outage_does_not_lose_the_booking`.

**Logging** at every boundary: outbound method/path/status/latency at INFO, retries at WARNING, unrecoverable failures at ERROR with full context.

### Webhook security

Three checks, all before any state is touched:

1. **HMAC-SHA256** over `{timestamp}.{raw_body}` using a shared secret. Without this, anyone who learns a booking reference can confirm bookings for free.
2. **Replay window** — timestamps older than 300 seconds are rejected. Binding the timestamp into the signed string is what makes this enforceable.
3. **`hmac.compare_digest`**, never `==`. A naive comparison short-circuits on the first differing byte, leaking the secret one byte at a time to an attacker who can measure response time.

The signature is verified against the **raw request bytes**. Re-serialising parsed JSON would change key order or whitespace and break the HMAC.

### Idempotency

Payment gateways guarantee *at-least-once* delivery — they will resend an event if the first `200` was slow. `Payment.last_event_id` carries a unique index; a replayed event returns the cached outcome and changes nothing.

### Late-payment safety

If a payment succeeds *after* the parent cancelled, the booking is **not** silently resurrected. `transition_to()` refuses the illegal transition, the event is logged at WARNING, and the response flags it for refund review. Money arriving late is an operations problem, not a reason to corrupt state.

---

## 9. Testing

**75 tests.** The brief asked for at least 5.

```bash
pytest -v                                    # all tests
pytest --cov=apps --cov-report=term-missing  # with coverage
pytest tests/test_booking_api.py -v          # one module
pytest -k "overlap or conflict" -v           # by keyword
```

| File | Tests | Focus |
|---|---|---|
| `test_models.py` | 13 | Constraints, state machine, derived values |
| `test_lsa_search.py` | 13 | Filtering, availability, **N+1 regression guard** |
| `test_booking_api.py` | 26 | Validation, **all five overlap geometries**, pricing |
| `test_payment_webhook.py` | 15 | Signature, transitions, replay, tampering |
| `test_payment_gateway.py` | 13 | Retries, timeouts, malformed responses |

Coverage across the three categories the brief names:

**Success** — valid booking returns 201 · search returns matching LSAs · webhook confirms a booking · gateway returns a payment intent · back-to-back sessions allowed · price derived correctly.

**Edge** — adjacent bookings do not clash · cancelled booking frees the slot · unknown skill returns empty not 404 · malformed date is ignored not fatal · duplicate webhook is a no-op · payment succeeding on a cancelled booking is flagged not applied · transient 500 retried then succeeds.

**Failure** — past date rejected · end before start rejected · duration out of bounds rejected · unverified LSA rejected · all five overlap geometries rejected · missing signature rejected · wrong secret rejected · stale timestamp rejected · tampered body rejected · gateway timeout raises cleanly · 4xx not retried.

**No test touches the network.** Every HTTP call is stubbed with `responses`, so CI is deterministic and works offline.

---

## 10. CI/CD

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Step | Why |
|---|---|---|
| **lint** | `ruff format --check` | Formatting is not a review conversation |
| | `ruff check` | Catches unused imports, shadowing, Django anti-patterns |
| **test** | `makemigrations --check --dry-run` | **Fails on a missing migration** — the most common Django deploy break |
| | `manage.py check --deploy` | Surfaces insecure production settings |
| | `migrate` | Migrations must actually apply cleanly |
| | `pytest --cov` | The suite, against **real PostgreSQL 16** |

Tests run against a PostgreSQL service container, not SQLite, on a matrix of **Python 3.11 and 3.12**. This is deliberate: the schema depends on database constraints, and those must be exercised against the engine that will run in production. `concurrency` cancels superseded runs so a rapid push sequence does not queue.

---

## 11. Git workflow

```
main            production-ready, protected, merges only via reviewed PR
└── develop     integration branch
    ├── feature/booking-api
    ├── feature/lsa-search-optimisation
    ├── feature/payment-webhook
    └── fix/overlap-edge-case
```

Conventional Commits throughout:

```
feat(bookings): prevent overlapping sessions with row-level locking
fix(search): prefetch skills to eliminate N+1 query
test(webhook): cover replay and signature-tampering cases
docs(readme): document query optimisation rationale
ci: run test suite against PostgreSQL 16
```

Branch protection on `main`: CI must pass, one approving review, no direct pushes.

---

## 12. What I would do next

Being explicit about scope boundaries, since this was a 4–6 hour brief:

| Gap | Approach |
|---|---|
| **No authentication** | Endpoints are `AllowAny` so a reviewer can exercise them with `curl`. Production needs JWT (`djangorestframework-simplejwt`) with parents scoped to their own bookings and LSAs to their own calendar |
| **Exclusion constraint** | On Postgres-only, `ExclusionConstraint` + `tstzrange` + `btree_gist` enforces arbitrary overlap at the database level — strictly stronger than the conditional unique index |
| **No timezone handling per user** | Everything is UTC. A real platform needs the parent's local timezone stored and rendered |
| **Synchronous gateway call** | Should move to Celery so a slow gateway never blocks the request thread |
| **No rate limiting per user** | Only anonymous throttling is configured |
| **Webhook retry queue** | Failed events should land in a dead-letter table with an operator replay path |
| **Observability** | Structured JSON logs and request-ID correlation for production tracing |

---

## Project structure

```
habot-lsa-booking-api/
├── config/                          Django project
│   ├── settings.py                  env-driven; Postgres with SQLite fallback
│   ├── urls.py                      root routing + OpenAPI docs
│   ├── wsgi.py  asgi.py
├── apps/
│   ├── common/
│   │   ├── models.py                TimeStampedModel, UUIDPrimaryKeyModel
│   │   ├── exceptions.py            domain errors + uniform error envelope
│   │   └── views.py                 health check
│   ├── demo/                        browsable console (thin client, no logic)
│   │   ├── views.py                 page + dev-only payment simulator
│   │   └── templates/demo/
│   │       └── console.html         single-file UI
│   └── bookings/
│       ├── models.py                Parent, Skill, LSAProfile, Booking, Payment
│       ├── serializers.py           request/response contract
│       ├── filters.py               search filters (all resolve to SQL)
│       ├── views.py                 thin views
│       ├── urls.py
│       ├── admin.py
│       ├── migrations/
│       ├── management/commands/
│       │   └── seed_data.py         realistic demo data
│       └── services/
│           ├── booking_service.py   overlap prevention, pricing, transactions
│           ├── payment_gateway.py   requests client + HMAC helpers
│           └── webhook_service.py   idempotent event processing
├── tests/                           75 tests
├── .github/workflows/ci.yml
├── docker-compose.yml  Dockerfile
├── requirements.txt  requirements-dev.txt
├── pytest.ini  pyproject.toml
├── .env.example  .gitignore
└── README.md
```

---

**Vansh Mehta** · mehtavansh6626@gmail.com
Submitted for the HabotConnect Python Backend Developer hiring project.
