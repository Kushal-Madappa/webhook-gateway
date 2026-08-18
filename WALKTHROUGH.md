# Walkthrough — one event, end to end

This document traces a single webhook through the system and then answers the six
questions an interviewer would ask about *why* it's built this way. It assumes no
prior knowledge of the codebase.

---

## The life of one event

**1. A provider sends it.**
Stripe (say) POSTs to `/v1/webhooks/acme` with a JSON body and a header
`X-Signature: sha256=<hmac>`. The hmac was computed by the provider over the raw
body bytes using a secret we both know.

**2. The API authenticates it.**
The ingest handler reads the **raw bytes** of the body (not a re-parsed dict —
re-serializing would change the bytes and break the hmac), looks up the `acme`
source to get its `signing_secret`, and recomputes the hmac. It compares the two
with `hmac.compare_digest`. If they differ → `401`. If the source doesn't exist
→ `404`. (`app/routers/webhooks.py`, `app/security.py`)

**3. The API records it, idempotently.**
It pulls three fields out of the payload — `provider_event_id` (the dedupe key),
`resource_key` (what the event is *about*, e.g. `order:42`), and `status_ordinal`
(a monotonic version number for that resource) — and runs:

```sql
INSERT INTO events (...) VALUES (...)
ON CONFLICT (source_id, provider_event_id) DO NOTHING
RETURNING id;
```

If a row comes back, it's new → `accepted`. If not, an identical event already
exists → `duplicate` (we look up and return the existing id). Either way the
transaction is **committed**. (`app/ingest.py`)

**4. The API acknowledges it — only now.**
Because the commit already happened, the `200 {"status":"accepted","event_id":…}`
is an honest promise: the event is durably in Postgres. The provider can stop
retrying.

**5. A worker claims it.**
Each worker loops, running one transaction per event:

```sql
SELECT * FROM events
WHERE status IN ('pending','failed') AND next_attempt_at <= now()
ORDER BY next_attempt_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

`FOR UPDATE` locks the row; `SKIP LOCKED` makes other workers skip it and grab a
different one. No two workers ever hold the same event. (`app/worker.py`)

**6. The worker enforces ordering.**
Still inside the same transaction, it takes a per-resource advisory lock
(`pg_advisory_xact_lock(hashtext(resource_key))`) and asks: *has a higher
`status_ordinal` for this `resource_key` already been delivered?* If yes, this
event is a stale/out-of-order update — mark it `superseded` and stop. It must
never reach the downstream.

**7. The worker delivers it.**
If the guard passes, it POSTs the payload to the source's `downstream_url` via
httpx, **while still holding the row lock**. On a 2xx → `delivered`. On failure →
`attempts += 1` and either schedule a retry (`failed`, with a jittered backoff in
`next_attempt_at`) or, once `attempts` hits `max_attempts`, park it as `dead`.
The transaction commits, releasing both locks.

**8. If the worker crashes mid-delivery,** its transaction rolls back and the row
reverts to `pending`/`failed` automatically — so another worker picks it up.
Nothing is stuck, nothing is lost. (This is why there's no separate "reaper".)

**9. An operator inspects and replays.**
`GET /v1/events?status=dead` lists the dead-letter queue. After fixing the
downstream, `POST /v1/events/{id}/replay` flips a `dead` event back to `pending`
with a fresh attempt budget, and the worker tries again.

---

## Interviewer questions

### Why a unique constraint instead of checking whether the event exists first?

Because a check-then-insert has a race. Two identical requests (a provider that
retries, or a double-click) can run concurrently:

```
Request A: SELECT ... → not found
Request B: SELECT ... → not found      # both saw "not found"
Request A: INSERT                       # ok
Request B: INSERT                       # duplicate row!
```

The gap between the read and the write is where correctness dies. A
`UNIQUE (source_id, provider_event_id)` constraint plus `INSERT … ON CONFLICT DO
NOTHING` moves the decision **into the database**, which evaluates it atomically:
the first insert wins, every concurrent duplicate becomes a no-op. Our test fires
12 identical inserts through a thread barrier and asserts exactly one row — a
check-then-insert fails that test.

### Why `hmac.compare_digest` instead of `==`?

`==` on strings/bytes short-circuits at the first differing byte, so it returns
*faster* when fewer leading bytes match. That timing difference is measurable
over many requests, and it leaks how much of a guessed signature is correct —
letting an attacker recover a valid signature one byte at a time (a timing
attack). `hmac.compare_digest` compares in time that doesn't depend on where the
first mismatch is, removing the signal. We use it for the webhook signature *and*
the admin token — both are secrets.

### Why `FOR UPDATE SKIP LOCKED`, and what breaks without it?

It turns the `events` table into a safe concurrent queue. `FOR UPDATE` locks the
claimed row; `SKIP LOCKED` tells other workers "don't wait for it, take the next
one." So N workers partition the backlog into disjoint sets.

Without it:
- **Plain `SELECT`** (no lock): two workers read the same `pending` row and both
  deliver it → the downstream is hit twice (double-processing).
- **`FOR UPDATE` without `SKIP LOCKED`**: the second worker *blocks* on the first
  worker's locked row instead of doing useful work → throughput collapses to
  serial, and a slow delivery stalls everyone behind it.

`SKIP LOCKED` is exactly the "give me work nobody else is already doing" primitive
a queue needs. Our concurrency test drains 40 events with 3 workers and sees them
split the load with zero double-deliveries.

### Why add jitter to the backoff?

Plain exponential backoff synchronizes failures. If a downstream goes down, many
events fail at nearly the same instant and are all rescheduled to the *same*
backed-off time (`now + 2^n`). When that time arrives they retry in a
synchronized burst — a thundering herd that knocks the downstream over again the
moment it recovers. Full jitter picks a random delay in `[0, cap]` for each event,
spreading the retries out so the recovering service sees a smooth trickle instead
of a spike.

### Why return 200 only after committing the row?

Because a `200` tells the provider "I've got it, stop retrying." If we returned
`200` *before* the row was durably committed and then the process crashed in that
window, the event would be gone — and the provider, having seen success, would
never resend it. That's a silently lost webhook. Committing first (persist-then-
ACK) makes the `200` mean what the provider thinks it means: the event is safe in
Postgres and will be delivered. The ordering is the entire difference between
at-least-once and "sometimes-zero" delivery.

### How does the monotonic status guard prevent an out-of-order "delivered"-before-"shipped" bug?

Imagine an order emits two webhooks: `confirmed` (`status_ordinal=1`) then
`shipped` (`status_ordinal=2`), same `resource_key`. Networks reorder things, and
retries can resurface an old event *after* a newer one already went out. If the
stale `confirmed` reaches the downstream **after** `shipped`, the downstream's
state regresses from shipped back to confirmed — a real, ugly bug.

The guard prevents it: before delivering, the worker checks the highest
`status_ordinal` already **delivered** for that `resource_key`. If the event it's
holding has a *lower* ordinal, it's stale — mark it `superseded`, don't send it.
So once `shipped` (2) is delivered, a late `confirmed` (1) is dropped; the
downstream never sees the regression. The per-resource advisory lock makes this
correct even when two workers process ordinal 1 and ordinal 2 of the same
resource at the same instant: they serialize on the lock, and whichever delivers
the higher ordinal first causes the lower one to be superseded. In-order updates
(1 then 2) are untouched and both deliver — verified by a dedicated test.
