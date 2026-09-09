"""The campaign layer: creates Brevo campaigns as DRAFTS. The send path refuses.

WHAT RAIVIS DECIDED, 2026-09-09, in his words: "lai lietot to pasu ir ok" - this layer may read
the EXISTING BREVO_API_KEY secret; no second copy of the key is made anywhere. And in the same
breath, the condition that shapes every line below: "visam japaliek dry run kamer nav viss lidz
galam gatavs, buvet mes varam bet sutit nevaram."

WHY THE REFUSAL IS IN CODE AND NOT IN CONFIGURATION. Verified in Brevo's own documentation before
this was designed, because assuming it would have been the fourth time in ten days this node
trusted a document about a system: Brevo has NO restricted API key - its help centre states that
API keys "give full access to your Brevo account" - and on the OAuth side campaigns are ONE write
scope, `campaigns.email:write` = "Create, modify, send and delete email campaigns". So the
capability to send cannot be made ABSENT. It can only be refused. A key that can create a draft
can send it, and no amount of IAM changes that.

THREE LAYERS, and they are deliberately not the same kind of thing:

  1. ALLOWED_LIST_IDS, a hard allowlist IN CODE. Today it holds one list whose entire membership
     is Raivis' own address. A completely broken build can therefore mail nobody else. This is not
     an environment variable, because an environment variable is something a future person widens
     at 23:00 without noticing what it was protecting.
  2. SendRefused at the call site, unconditionally, unless an un-revoked approval row exists for
     that send_date. Not a mode, not a flag - the function raises before any HTTP happens.
  3. SendRefused is NOT a subclass of any error a per-item handler catches. That shape was earned
     this morning: step6_send wraps each message in `except Exception`, which is correct for "Brevo
     rejected this address" and destructive for "this must not happen", where one structural
     refusal becomes 6 156 quiet failures and a run that still exits 0.
"""
import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("campaign")

BREVO_BASE = "https://api.brevo.com/v3"
SENDER_ID = 2               # Tiktik.lv <info@tiktik.lv>, verified active live 2026-09-09
SUPPRESSION_LIST_ID = 4     # tiktik_suppression: 3 473 blacklisted + 201 subscribers, read live

# THE HARD ALLOWLIST. List 62 read live on 2026-09-09: exactly one contact, raivis@alenda.lv.
# Widening this is a code change with a commit message, which is the entire point.
ALLOWED_LIST_IDS = frozenset({62})


class CredentialUnavailable(RuntimeError):
    """The Brevo key cannot be read. Fail closed and say so; never continue without it."""


class ListNotAllowed(RuntimeError):
    """A campaign was pointed at a list that is not on the hard allowlist."""


class EmptyAudience(RuntimeError):
    """The draft would reach nobody once the suppression list is applied."""


class SendRefused(RuntimeError):
    """A send was attempted without an approval row. Deliberately outside BrevoError."""


class TemplateUsesDiscount(RuntimeError):
    """The template still renders a discount attribute. Raivis struck those on 2026-09-07."""


class TemplateInactive(RuntimeError):
    """Brevo will not build a campaign from an inactive template.

    Brevo answers this with HTTP 405 method_not_allowed, which is a wrong status for a semantic
    refusal and sends the reader looking for a bad URL or a bad verb. Measured on 2026-09-09
    against template 20. Checked here first so the failure says what is actually wrong.
    """


# Raivis retired discount codes on 2026-09-07: offers live in the customer's own replenisher link
# with a deadline and a margin, never as a percentage. Eleven educational templates still carried
# XSELL_CODE / XSELL_PCT with 10 % hardcoded when Marketing checked on 09.09.
#
# THE REASON THIS IS ENFORCED HERE AND NOT BY THE PERSON EDITING THE TEMPLATES: a rule kept by
# somebody remembering to strip eleven templates is a rule that comes back the first time a
# twelfth is copied from an old one. A rule kept by the code that builds the campaign does not.
DISCOUNT_ATTRIBUTES = ("XSELL_CODE", "XSELL_PCT", "NEXT_DISCOUNT_CODE", "NEXT_DISCOUNT_PCT")


_key_cache = None


def api_key() -> str:
    """Read BREVO_API_KEY from Secret Manager at call time.

    A plain API key has to be in the process to go into a header - Raivis asked why it cannot be
    used without being read, and it cannot. Secret Manager decides WHO may fetch it, never what
    happens afterwards, which is why the refusal above is where the safety actually lives.
    """
    global _key_cache
    if _key_cache:
        return _key_cache
    inline = os.environ.get("BREVO_API_KEY")
    if inline:
        _key_cache = inline
        return _key_cache
    secret = os.environ.get("BREVO_SECRET_NAME",
                            "projects/jaunais-za-aizv04022026/secrets/BREVO_API_KEY/versions/latest")
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        _key_cache = client.access_secret_version(
            request={"name": secret}).payload.data.decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        raise CredentialUnavailable(
            f"cannot read {secret}: {e!r}. As of 2026-09-09 the per-secret IAM policy is EMPTY and "
            f"access comes from project-level roles, so this service account needs "
            f"roles/secretmanager.secretAccessor bound ON THE SECRET. Refusing to continue rather "
            f"than falling back to anything.") from e
    if not _key_cache:
        raise CredentialUnavailable("the secret is readable but empty")
    return _key_cache


def _call(method: str, path: str, payload=None, timeout: int = 30):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BREVO_BASE}{path}", data=body, method=method)
    req.add_header("api-key", api_key())
    req.add_header("accept", "application/json")
    if body:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def list_contacts(list_id: int, limit: int = 500):
    return _call("GET", f"/contacts/lists/{list_id}/contacts?limit={limit}").get("contacts", [])


def effective_audience(list_id: int) -> int:
    """How many people the campaign would actually reach once exclusionListIds=[4] is applied.

    THE TRAP THIS EXISTS FOR, found live on 2026-09-09: raivis@alenda.lv is a member of list 4,
    the suppression list. A draft aimed at the test list with the mandated exclusionListIds=[4]
    therefore reaches NOBODY - and Brevo reports that as a perfectly healthy campaign. A test that
    silently proves nothing is worse than no test.

    Only ever run against allowlisted lists, which are small by construction. The real variants'
    audience is counted in BigQuery from mkt_control.variant_list_plan, where suppression was
    already applied by the assignment; counting 8 000 contacts through this API would be a second
    answer to a question BigQuery already answers.
    """
    return sum(1 for c in list_contacts(list_id)
               if SUPPRESSION_LIST_ID not in (c.get("listIds") or []))


def template(template_id: int):
    return _call("GET", f"/smtp/templates/{template_id}")


def template_is_active(template_id: int) -> bool:
    """Read Brevo directly, not mkt_control.brevo_template_status.

    The mirror is refreshed at 06:30 and was 8 h old when this was written; a campaign is built
    NOW. The mirror is the right source for planning a week and the wrong source for the moment a
    campaign is created, and this is the moment.
    """
    return bool(template(template_id).get("isActive"))


def template_discount_attributes(template_id: int):
    """Which retired discount attributes this template still renders. Empty is the good answer."""
    t = template(template_id)
    blob = (t.get("htmlContent") or "") + " " + (t.get("subject") or "")
    return [a for a in DISCOUNT_ATTRIBUTES if a in blob]


def contact_attributes(email: str) -> dict:
    """The contact's real attribute values, for substituting into links before checking them."""
    import urllib.parse
    return _call("GET", f"/contacts/{urllib.parse.quote(email)}").get("attributes", {}) or {}


def campaign(campaign_id: int, statistics: str = "campaignStats"):
    return _call("GET", f"/emailCampaigns/{campaign_id}?statistics={statistics}")


def campaign_stats(campaign_id: int, list_ids):
    """Delivered/sent for OUR lists, read from campaignStats and never from globalStats.

    campaignStats is an ARRAY keyed by listId, so a campaign with two lists needs summing and a
    missing list id is a real error rather than a zero - that distinction is the whole reason for
    reading per list.

    Marketing's caveat, checked live today rather than inherited: globalStats is NOT permanently
    broken. Campaign 164 returned 8 042 sent / 7 961 delivered through globalStats today, against
    8 038 / 7 960 in campaignStats. The pavediens' "globalStats returns zeros" was true in the
    minutes after that send and is not true now. The rule stands anyway, for a different and better
    reason: globalStats answers for the whole campaign, campaignStats answers per list, and it is
    the per-list number that maps to an audience we chose.
    """
    data = campaign(campaign_id, statistics="campaignStats")
    rows = {r["listId"]: r for r in data.get("statistics", {}).get("campaignStats", [])}
    missing = [i for i in list_ids if i not in rows]
    if missing:
        raise RuntimeError(
            f"campaign {campaign_id} has no campaignStats row for list(s) {missing}; the campaign "
            f"was sent to {sorted(rows)}. A zero here would be a lie, not a measurement.")
    return {
        "sent": sum(rows[i]["sent"] for i in list_ids),
        "delivered": sum(rows[i]["delivered"] for i in list_ids),
        "unique_views": sum(rows[i].get("uniqueViews", 0) for i in list_ids),
        "unique_clicks": sum(rows[i].get("uniqueClicks", 0) for i in list_ids),
        "unsubscriptions": sum(rows[i].get("unsubscriptions", 0) for i in list_ids),
    }


def credit_headroom() -> int:
    acc = _call("GET", "/account")
    for p in acc.get("plan", []):
        if p.get("type") != "sms" and "credits" in p:
            return int(p.get("credits") or 0)
    return 0


def create_draft(name: str, subject: str, list_id: int, template_id: int, utm_campaign: str,
                 reply_to: str = "info@tiktik.lv") -> int:  # noqa: PLR0913
    """Create the campaign as a DRAFT. Never scheduled, never sent, from here.

    The draft has to exist BEFORE the approval e-mail, not after: the node's hard rule is that no
    campaign is scheduled until every link in the RENDERED letter has answered 200, and there is
    nothing to render until a draft exists. Raivis found four broken links exactly this way on
    24.08.

    utmCampaign is set to the same slug the links carry. Campaign 164 shows why that is worth
    stating: its campaign-level UTM reads '2026 09 cimdi un ada', with spaces, while its links
    carry '2026-09-cimdi-un-ada'. Two values for one campaign is two rows in any report that joins
    on it.
    """
    if list_id not in ALLOWED_LIST_IDS:
        raise ListNotAllowed(
            f"list {list_id} is not in the hard allowlist {sorted(ALLOWED_LIST_IDS)}. Until Raivis' "
            f"first approval, this layer may address only a list whose entire membership is his own "
            f"address. Widening it is a code change, on purpose.")
    if not template_is_active(template_id):
        raise TemplateInactive(
            f"template {template_id} is inactive in Brevo, so no campaign can be built from it. "
            f"Brevo reports this as HTTP 405 method_not_allowed, which is why this is checked "
            f"before the call rather than read out of the error. Activating a template is "
            f"template work and belongs to Marketing, not here.")
    still_discounting = template_discount_attributes(template_id)
    if still_discounting:
        raise TemplateUsesDiscount(
            f"template {template_id} still renders {', '.join(still_discounting)}. Raivis retired "
            f"discount codes on 2026-09-07; a campaign built from this template would print an "
            f"offer he has withdrawn. Strip the attribute from the template, not this check.")
    reachable = effective_audience(list_id)
    if reachable == 0:
        raise EmptyAudience(
            f"list {list_id} would reach nobody once exclusionListIds=[{SUPPRESSION_LIST_ID}] is "
            f"applied - every member of it is also in the suppression list. Creating this draft "
            f"would produce a campaign that looks healthy and proves nothing.")
    payload = {
        "name": name,
        "subject": subject,
        "sender": {"id": SENDER_ID},
        "replyTo": reply_to,
        "templateId": template_id,
        "recipients": {"listIds": [list_id], "exclusionListIds": [SUPPRESSION_LIST_ID]},
        "inlineImageActivation": False,
    }
    # utmCampaign IS DELIBERATELY NOT SENT, and this reverses what I wrote this morning about
    # campaign 164. Brevo's own field documentation says utmCampaign accepts "only alphanumeric
    # characters and spaces" - so `2026-w37-papildinam` CANNOT be stored there at all. Campaign
    # 164's `2026 09 cimdi un ada` was not sloppiness; it was the only shape the field accepts.
    #
    # Setting a spaces-variant would create a SECOND utm_campaign value for one campaign, which is
    # the defect this whole UTM contract exists to prevent. So the slug lives where we control it
    # completely - in the links - and check_links asserts every href carries it. The campaign name
    # carries the slug for a human reading the Brevo UI.
    created = _call("POST", "/emailCampaigns", payload)
    log.info("DRAFT_CREATED id=%s list=%s reachable=%s utm=%s",
             created.get("id"), list_id, reachable, utm_campaign)
    return int(created["id"])


_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)


_PLACEHOLDER = re.compile(r"\{\{\s*(?:contact\.)?([A-Z0-9_]+)\s*\}\}")


def substitute(text: str, attributes: dict) -> str:
    """Replace {{ contact.X }} with the contact's real value. Unknown names are left alone.

    Left alone rather than blanked on purpose: a placeholder nobody can fill has to stay visible as
    a placeholder, or the link check passes on a URL that will be broken for every real reader.
    """
    return _PLACEHOLDER.sub(
        lambda m: str(attributes.get(m.group(1), m.group(0))), text or "")


def check_links(campaign_id: int, as_contact: str = None, expect_utm: str = None):
    """Every link in the campaign's HTML must answer 200 before the campaign may be scheduled.

    Two classes, and conflating them is how a broken link survives a green check. A STATIC link is
    checked here. A DYNAMIC one - anything still carrying a Brevo placeholder - cannot be resolved
    without a real contact's attributes, so it is reported as unresolved and BLOCKS; resolving it
    needs a test send to the allowlisted list, which is the second stage.
    """
    html = campaign(campaign_id).get("htmlContent") or ""
    attrs = contact_attributes(as_contact) if as_contact else {}
    static, dynamic = [], []
    for href in set(_HREF.findall(html)):
        resolved = substitute(href, attrs)
        # A link is only checkable once its placeholders carry real values. This is why the check
        # is run AS a contact: the 24.08 breakage lived inside the substituted part of the URL and
        # is invisible in the template's own HTML.
        (dynamic if ("{{" in resolved or "{%" in resolved) else static).append(resolved)
    ok, failed = [], []
    for url in sorted(static):
        if not url.lower().startswith("http"):
            continue
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("user-agent", "tiktik-campaign-linkcheck/1.0")
            with urllib.request.urlopen(req, timeout=20) as r:
                (ok if r.status == 200 else failed).append((url, r.status))
        except Exception as e:  # noqa: BLE001
            failed.append((url, repr(e)))
    # Because the campaign-level utmCampaign field cannot hold our slug shape (see create_draft),
    # the links are the ONLY place the slug exists. A link without it is a click nobody can
    # attribute afterwards, which is the same hole the June send left behind.
    missing_utm = []
    if expect_utm:
        missing_utm = sorted(u for u, _ in ok if f"utm_campaign={expect_utm}" not in u)
    return {"ok": ok, "failed": failed, "dynamic_unresolved": sorted(dynamic),
            "missing_utm": missing_utm}


def send_now(campaign_id: int, send_date: str, approval_lookup):
    """Send a campaign. Raises unless Raivis has approved that sending day.

    `approval_lookup` is injected rather than imported so this module holds no BigQuery client and
    the refusal can be exercised in a test without a warehouse. It must return the approval row or
    None.

    There is no flag that turns this off. SEND_MODE, DRY_RUN and an absent key are all things a
    person can change in thirty seconds under pressure; a raise at the call site is not.
    """
    row = approval_lookup(send_date)
    if not row:
        raise SendRefused(
            f"no approval row for send_date={send_date}. Nothing sends without Raivis pressing the "
            f"day-batch button, and silence is not consent.")
    if row.get("revoked_at"):
        raise SendRefused(
            f"the approval for {send_date} was revoked at {row['revoked_at']}: "
            f"{row.get('revoked_reason')}")
    raise SendRefused(
        f"the send path is not built. Raivis' condition of 2026-09-09 stands - 'visam japaliek dry "
        f"run kamer nav viss lidz galam gatavs' - and enabling it is his call, not a code change "
        f"anyone here may make. campaign_id={campaign_id} send_date={send_date}")
