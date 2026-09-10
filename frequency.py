"""How many letters a person has actually received in the last 7 days - counting EVERY sender.

WHY THIS EXISTS, and it is not a refinement. The engine's frequency rule (MAX_EMAILS_PER_WEEK, per
PERSON rather than per address) counted only what the ENGINE sent. On 2026-09-10 campaigns 169 and
170 went out to 8 494 people from the Brevo interface, outside the engine entirely, and the engine
could see none of them - Brevo credits fell from 55 769 to 47 227 and that was the only trace. So
the guarantee was already false before the first real send: some of those people would have taken a
third letter in a week and nothing here would have known.

The rule this repeats, learned in another node and paid for twice: when somebody says "too much
mail", you count EVERY sender, not the one you built.

LIVE BREVO, NOT THE MIRROR, and the reason is specific rather than superstitious. The BigQuery
mirror of Brevo has been incomplete in this house before - one read showed 2 campaigns where the
live API had 19 - and an incomplete source in a frequency check does not fail loudly, it quietly
under-counts and lets the letter go. A source that can be wrong in the permissive direction is not
usable for a guarantee.

THE TWO SOURCES, and each one names what it covers:

  campaign  - `statistics.messagesSent` on the contact, read live per address. Verified against the
              live API on 2026-09-10: entries are {campaignId, eventTime}, newest first, and they
              appear no matter WHO created the campaign - which is the entire point. Filtering is
              done here rather than through the endpoint's startDate/endDate so the code depends on
              one documented field instead of two.
  transac   - the transactional list for the same window, fetched ONCE for everybody rather than
              per address, because it is one endpoint over a time range and per-person calls would
              multiply cost for the same answer. Transactional messages do NOT appear in the
              contact's messagesSent, so without this the count would silently miss a whole path.

AN UNREAD NUMBER IS NOT A ZERO. A contact Brevo has never heard of received nothing, and that IS a
zero - it is an answer. Anything else - a timeout, a 500, a rate limit, a page we could not reach -
raises, and the caller turns it into a refusal with a record. The tempting shape is to count what
we could reach and carry on; that produces a number that looks like a measurement and was never
measured, which is worse than a blank.
"""
import datetime as dt
import logging
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import campaign as C

log = logging.getLogger("frequency")

WINDOW_DAYS = int(os.environ.get("FREQ_WINDOW_DAYS", "7"))
WORKERS = int(os.environ.get("FREQ_WORKERS", "8"))
# A ceiling, not a sample size. Above it the scan REFUSES rather than measuring part of the day and
# reporting it as the day - see the module docstring.
MAX_ADDRESSES = int(os.environ.get("FREQ_MAX_ADDRESSES", "12000"))
TRANSAC_PAGE = 500
TRANSAC_MAX_PAGES = int(os.environ.get("FREQ_TRANSAC_MAX_PAGES", "40"))


class FrequencySourceUnavailable(RuntimeError):
    """A source could not be READ. Never downgrade this to a zero; it is a refusal."""


def _parse(ts: str):
    """Brevo returns an ISO timestamp with an offset. Keep the offset; never compare naive."""
    try:
        return dt.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def campaign_sends(email: str, since: dt.datetime):
    """Campaign sends to ONE address inside the window. A contact Brevo never saw returns []."""
    try:
        c = C._call("GET", f"/contacts/{urllib.parse.quote(email)}")  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        if "404" in repr(e):
            # Brevo has never seen this address, so it has sent nothing to it. That is a measured
            # zero, not an unknown, and it is the ONLY error that may become one.
            return []
        raise FrequencySourceUnavailable(
            f"cannot read the contact record behind one of this day's addresses: {e!r}") from e
    out = []
    for ev in (c.get("statistics", {}) or {}).get("messagesSent", []) or []:
        when = _parse(ev.get("eventTime"))
        if when and when >= since:
            out.append((ev.get("campaignId"), ev.get("eventTime")))
    return out


def transactional_recipients(since: dt.datetime, until: dt.datetime):
    """Every address a transactional message reached in the window. One pass, not per person.

    Returns a dict address -> count. Raises if the list cannot be read to the END: a partial page
    walk is a partial answer, and a partial answer in a frequency check is an under-count.
    """
    got, offset, total = {}, 0, None
    for _ in range(TRANSAC_MAX_PAGES):
        q = (f"/smtp/emails?startDate={since:%Y-%m-%d}&endDate={until:%Y-%m-%d}"
             f"&limit={TRANSAC_PAGE}&offset={offset}")
        try:
            page = C._call("GET", q)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            raise FrequencySourceUnavailable(
                f"the transactional list could not be read at offset {offset}: {e!r}. This is a "
                f"whole sending path, so continuing without it would under-count on purpose.") from e
        rows = page.get("transactionalEmails") or []
        if total is None:
            total = int(page.get("count") or 0)
        for row in rows:
            addr = (row.get("email") or "").strip().lower()
            when = _parse(row.get("date"))
            if addr and (when is None or when >= since):
                got[addr] = got.get(addr, 0) + 1
        offset += len(rows)
        if not rows or (total is not None and offset >= total):
            break
    else:
        raise FrequencySourceUnavailable(
            f"the transactional list has more than {TRANSAC_MAX_PAGES * TRANSAC_PAGE} messages in "
            f"the window and was not walked to the end; refusing rather than reporting the part "
            f"that was read as if it were the whole.")
    log.info("FREQ_TRANSAC window messages=%s distinct_addresses=%s", total, len(got))
    return got


def scan(people, now: dt.datetime = None, include_transactional: bool = True) -> dict:
    """people: iterable of (master_key, email). Returns the whole measurement, or raises.

    The grain is the PERSON, which is why the counts are summed across every address that person
    owns: two addresses that each received a letter is two letters for one human being, and the
    rule was written about human beings.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=WINDOW_DAYS)
    pairs = [(str(mk), (em or "").strip().lower()) for mk, em in people if em]
    addresses = sorted({em for _, em in pairs})
    if len(addresses) > MAX_ADDRESSES:
        raise FrequencySourceUnavailable(
            f"{len(addresses)} addresses is above FREQ_MAX_ADDRESSES={MAX_ADDRESSES}. Refusing "
            f"rather than measuring some of the day and reporting it as the day.")

    transac = transactional_recipients(since, now) if include_transactional else {}
    per_address = {}

    def one(addr):
        return addr, len(campaign_sends(addr, since))

    if addresses:
        with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
            for addr, n in pool.map(one, addresses):
                per_address[addr] = n + transac.get(addr, 0)

    per_person = {}
    for mk, em in pairs:
        per_person[mk] = per_person.get(mk, 0) + per_address.get(em, 0)

    limit = int(os.environ.get("MAX_EMAILS_PER_WEEK", "2"))
    with_prior = sum(1 for n in per_person.values() if n >= 1)
    would_exceed = sum(1 for n in per_person.values() if n + 1 > limit)
    source = (f"brevo-live campaigns+transactional, window {WINDOW_DAYS}d, "
              f"{len(addresses)} address(es), {len(per_person)} person(s), limit {limit}"
              if include_transactional else
              f"brevo-live campaigns ONLY (transactional not included), window {WINDOW_DAYS}d")
    log.info("FREQ_SCAN people=%s with_prior=%s would_exceed=%s",
             len(per_person), with_prior, would_exceed)
    return {"people": len(per_person), "addresses": len(addresses),
            "with_prior": with_prior, "would_exceed": would_exceed,
            "per_person": per_person, "source": source,
            "window_start": since.isoformat(), "limit": limit}
