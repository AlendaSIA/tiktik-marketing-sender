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
import html as _html
import json
import logging
import os
import re
import urllib.error
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


class TemplateUsesUnapprovedAttribute(RuntimeError):
    """The template renders an attribute outside the approved set."""


class TemplateUsesDiscount(RuntimeError):
    """Kept for the annotation only; it is no longer a gate. See DISCOUNT_ATTRIBUTES."""


class TemplateUsesParams(RuntimeError):
    """The template renders Liquid `params.*`, which are ALWAYS empty in a campaign.

    params fill only through the transactional API. In a campaign every one of them resolves to
    nothing, so the template renders a blank where the value should be. Template 20 is the case
    that taught this: its headline reads "Speciala atlaide - {{ params.xsell_group }}
    -{{ params.xsell_pct }}% ar kodu {{ params.xsell_code }}", and that headline is NOT inside a
    conditional. Sent as a campaign it advertises a discount with no discount in it.

    A stronger guard than the attribute allowlist, and a different question: the allowlist asks
    whether a field is allowed, this asks whether it can ever have a value here at all.
    """


class TemplateInactive(RuntimeError):
    """Brevo will not build a campaign from an inactive template.

    Brevo answers this with HTTP 405 method_not_allowed, which is a wrong status for a semantic
    refusal and sends the reader looking for a bad URL or a bad verb. Measured on 2026-09-09
    against template 20. Checked here first so the failure says what is actually wrong.
    """


# THE GATE IS AN ALLOWLIST, NOT A DENYLIST, and the inversion was paid for on 2026-09-09.
#
# The guard shipped that morning refused four NAMED discount attributes. The same evening the sync
# node removed the discount fields, found fifteen where the order named six, and found that the
# field which had ACTUALLY been delivering codes for three months was GROUP_CODE - which appeared
# on nobody's list of discount fields. Campaign 87 went out on 18.06, and code VRTKN's first use in
# the entire order history is 18.06.
#
# Their sentence, and it is about this function: "a list of names only ever catches the names
# somebody thought of." So a template may render only what is in
# mkt_control.template_attribute_allowlist, and the next discount-shaped field nobody has imagined
# is refused by default instead of admitted by default.
#
# The approved set is INJECTED rather than read here: this module keeps no BigQuery client, the
# refusal stays testable without a warehouse, and - the part that matters - forgetting to pass it
# is a TypeError rather than a silent pass.
#
# DISCOUNT_ATTRIBUTES IS THE SECOND LAYER, subordinate and never the primary one. It cannot catch a
# field nobody imagined - that is what the params refusal and the allowlist are for - but it CAN
# catch a mistake in the allowlist itself, which is the one failure those two cannot see. If someone
# approves XSELL_PCT by hand, this still refuses. Order matters: params first, allowlist second,
# this last, and it is never asked to do the job of either.
DISCOUNT_ATTRIBUTES = ("XSELL_CODE", "XSELL_PCT", "NEXT_DISCOUNT_CODE", "NEXT_DISCOUNT_PCT",
                       "GROUP_CODE", "GROUP2_CODE")

# A DISCOUNT CAN HIDE AS A PRICE. Measured by the sync node on 2026-09-09: customer_xsell computed
# XSELL_FINAL_PRICE = price x 0,9 - the same ten percent, with no floor, and no percent sign
# anywhere to catch it by. The new model's whole shape is a personal price INSTEAD of a percentage,
# so a percentage written as a price is indistinguishable from a legitimate one except by the
# floor. This does not judge the number - it makes the PAIR visible, so whoever decides the
# allowlist can see that a template renders both sides of a discount.
_PRICE_STANDARD = ("_STD", "_STD_PRICE")
_PRICE_FINAL = ("_FIN", "_FINAL", "_PRICE", "_FINAL_PRICE", "_AKC")

# BREVO'S OWN MERGE TAGS - an ALLOWLIST, and the direction is the OPPOSITE of the attribute gate
# above, which is why it is safe to have one here at all.
#
# The attribute gate asks "is this field allowed to appear", where an allowlist is strict and a
# denylist is a hole. This asks "is this a tag whose value we could never supply and Brevo always
# will" - and there the strict form is naming them one by one. Anything not named is treated as our
# own unfilled placeholder and still blocks.
#
# WHAT DECIDED IT, 2026-09-10: template 35's only href is `{{ unsubscribe }}`, and check_links
# refused it as an unresolved placeholder. Marketing settled the form from the only live evidence
# there is - campaign 164 went to 8 042 people on 03.09 carrying NO `{{ }}` token at all and
# `[DEFAULT_FOOTER]`, so the unsubscribe link comes from Brevo's campaign footer. The body token is
# surplus and will leave the templates when they are redrawn. Until then, refusing it under the
# name PlaceholdersUnresolved would fire on every body-token template forever, and a guard that is
# always on is not a guard - it is a thing people learn to click past.
#
# A template left with no checkable link at all is STILL refused, but under its own name,
# no_real_links. Two different reasons must never share one word.
#
# WHOLE-HREF MATCH ONLY. `{{ unsubscribe }}` as the entire href is Brevo's link. The same token
# inside a longer URL is a URL WE built around a value we cannot see, so it stays unresolved and
# blocks. Narrow on purpose.
BREVO_SYSTEM_TOKENS = frozenset({
    "unsubscribe", "mirror", "update_profile", "view_in_browser", "subscribe",
})

_WHOLE_TOKEN = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


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
            f"cannot read {secret}: {e!r}. The per-secret IAM policy is what grants this - measured "
            f"2026-09-10, BREVO_API_KEY carries roles/secretmanager.secretAccessor for "
            f"tiktik-campaign-sa ON THE SECRET, and this identity holds no project-level secret "
            f"role at all. Refusing to continue rather than falling back to anything.") from e
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


# THE CLOSING BRACES ARE DELIBERATELY NOT MATCHED, and that cost a scan to learn.
#
# The first version required `}}` immediately after the attribute name. Live template 35 renders
#   {{ contact.SVEICIENS | default : "Sveiki" }}
#   {{ contact.XSELL_GROUP | default : "papira preces" | lower }}
# so every reference carrying a Liquid filter was invisible, and the scan reported welcome_1 as
# rendering ZERO attributes. A welcome letter that personalises nothing is not plausible, which is
# the only reason anyone looked - the "implausibly low count is worth a human's eye" note in
# template_attributes did the work it was written for, on its first real run.
#
# It is the GROUP_CODE lesson a second time in one day, in my own code: a check that only sees the
# shape somebody thought of. So the name is captured from the opening `{{ contact.` and everything
# after it is somebody else's business.
_ATTR_REFS = (
    re.compile(r"\{\{\s*contact\.([A-Za-z0-9_]+)"),
    re.compile(r"%([A-Z][A-Z0-9_]{1,})%"),          # legacy Brevo personalisation
)

# A SECOND NAMESPACE, and missing it is how two honest scans disagreed on 2026-09-09.
#
# Data & analytics counted 14 templates carrying XSELL_CODE / XSELL_PCT. This layer's scan found
# none. Both were right about what they measured: the references are `params.xsell_code` and
# `params.xsell_pct` - LOWERCASE, and in the `params` namespace, not `contact`. Reading the raw
# HTML of template 20 settled it in one look, which is exactly why the instruction was to read the
# HTML rather than to trust either parser.
#
# It also rules out the third and most alarming possibility that was on the table - that deleting
# the Brevo attributes had silently changed what a template resolves to without changing its HTML.
# It did not, and could not have: params are not contact attributes.
#
# Both `{{ params.x }}` and `{% if params.x %}` are caught, and the dotted path is kept whole
# (`products.0.final`), because the discount lives in the tail of it.
_PARAM_REF = re.compile(r"params\.([A-Za-z0-9_.]+)")


def template_attributes(template_id: int):
    """Every contact attribute this template renders. Read from Brevo, not from a list.

    KNOWN LIMIT, stated rather than hidden: this sees the two personalisation syntaxes these
    templates actually use. An attribute referenced some third way would not be seen, so
    template_attribute_usage records the whole set it DID find and the count - a template whose
    count looks implausibly low is worth a human's eye.
    """
    t = template(template_id)
    return attributes_in((t.get("htmlContent") or "") + " " + (t.get("subject") or ""))


def attributes_in(blob: str):
    """Contact attributes referenced in a piece of HTML/subject text (same rules as above)."""
    found = set()
    for rx in _ATTR_REFS:
        found.update(rx.findall(blob or ""))
    return sorted(found)


def template_params(template_id: int):
    """Liquid `params.*` references. Any of them makes the template unusable as a campaign."""
    t = template(template_id)
    return params_in((t.get("htmlContent") or "") + " " + (t.get("subject") or ""))


def params_in(blob: str):
    return sorted({m.rstrip(".") for m in _PARAM_REF.findall(blob or "")})


def discount_shaped_pairs(attributes):
    """Attributes forming a standard-price / final-price pair: a discount with no percent sign."""
    attrs = set(attributes)
    pairs = []
    for a in sorted(attrs):
        for std in _PRICE_STANDARD:
            if a.endswith(std):
                stem = a[: -len(std)]
                for fin in _PRICE_FINAL:
                    if stem + fin in attrs:
                        pairs.append(f"{a}+{stem}{fin}")
    return pairs


def template_discount_attributes(template_id: int):
    """Annotation only: which KNOWN discount names a template renders. Decides nothing."""
    return [a for a in DISCOUNT_ATTRIBUTES if a in set(template_attributes(template_id))]


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


# --------------------------------------------------------------------------- #
# UTM SEAM v1 (MAIN, 2026-09-11) - issued in the same words to the template builder:
#   "Veidnes saitēs utm_campaign vērtība ir __UTM_WEEK__-<bāze>, kur <bāze> ir veidnes pastāvīgā,
#    klientam droša daļa (piemēram, papildinam). Kampaņu slānis, veidojot melnrakstu, nolasa
#    veidnes HTML, aizvieto katru __UTM_WEEK__ ar utm.py nedēļas daļu (YYYY-Www) un veido kampaņu
#    ar šo HTML. Ja pēc aizvietošanas HTML vēl satur __UTM_WEEK__, melnraksts tiek atteikts.
#    mkt_control.utm_dictionary rindas raksta TIKAI kampaņu slānis, ar MERGE, vienu rindu katram
#    (utm_campaign, utm_content) pārim. Veidņu būvētājs tur neraksta."
# --------------------------------------------------------------------------- #
UTM_WEEK_MARKER = "__UTM_WEEK__"
# Spellings the plain replace would NOT catch - lower case, URL-encoded, HTML-entity encoded. If any
# survives, the letter would carry a literal marker in a customer's address bar.
_MARKER_LEFTOVER = re.compile(r"__\s*utm_week\s*__|%5F%5FUTM_WEEK%5F%5F|&#0*95;&#0*95;UTM_WEEK", re.I)
# Not anchored on `?`/`&`: in the house idiom the parameter follows a Liquid `{% endif %}`.
_UTM_CAMPAIGN = re.compile(r"(?<![A-Za-z0-9_])utm_campaign=([^&\"'#\s{}<>]*)", re.I)
_UTM_CONTENT = re.compile(r"(?<![A-Za-z0-9_])utm_content=([^&\"'#\s{}<>]*)", re.I)


class UtmSeamRefused(RuntimeError):
    """The template does not carry the seam form, or the replacement left something behind."""


def hrefs_in(html_text: str):
    """Every href value, Liquid-aware.

    A plain `href="([^"]+)"` stops at the first quote INSIDE a Liquid tag - the house link idiom is
    `{{ contact.URL }}{% if "?" in contact.URL %}&amp;{% else %}?{% endif %}utm_source=...`, so the
    old pattern captured `{{ contact.URL }}{% if ` and nothing after it. Tags are consumed whole.
    """
    out = []
    for m in _HREF_LIQUID.finditer(html_text or ""):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


_HREF_LIQUID = re.compile(
    r"""href\s*=\s*(?:"((?:\{%.*?%\}|\{\{.*?\}\}|[^"])*)"|'((?:\{%.*?%\}|\{\{.*?\}\}|[^'])*)')""",
    re.I | re.S)


def utm_pairs(html_text: str):
    """(utm_campaign, utm_content or None) for every link that carries a utm_campaign."""
    pairs = set()
    for href in hrefs_in(html_text):
        h = _html.unescape(href)
        c = _UTM_CAMPAIGN.search(h)
        if not c or not c.group(1):
            continue
        ct = _UTM_CONTENT.search(h)
        pairs.add((c.group(1), (ct.group(1) or None) if ct else None))
    return sorted(pairs, key=lambda p: (p[0], p[1] or ""))


def apply_utm_week(html_text: str, week: str):
    """Replace every __UTM_WEEK__ with the ISO week part; refuse anything that is not the seam form.

    Returns (html_after, pairs). Refuses, BEFORE any campaign exists, when:
      - the template carries no marker at all (its week is hardcoded, so it would ship the same
        week's slug every week - the collision the ISO week was introduced to remove);
      - a marker survives the replacement in any spelling (the contract's own refusal);
      - there is no utm_campaign at all (an untagged letter - rule #13);
      - any utm_campaign after replacement does not start with `<week>-` (one link hardcoded).
    """
    if not re.fullmatch(r"\d{4}-w\d{2}", week or ""):
        raise UtmSeamRefused(f"week {week!r} is not the utm.py form YYYY-Www")
    if UTM_WEEK_MARKER not in (html_text or ""):
        found = sorted({c for c, _ in utm_pairs(html_text)})
        raise UtmSeamRefused(
            f"the template carries no {UTM_WEEK_MARKER}; its links say utm_campaign="
            f"{', '.join(found) or '(none)'}. A week written into the template ships the same slug "
            f"every week. The seam form is {UTM_WEEK_MARKER}-<base>; the template builder owns it.")
    after = html_text.replace(UTM_WEEK_MARKER, week)
    if UTM_WEEK_MARKER in after or _MARKER_LEFTOVER.search(after):
        raise UtmSeamRefused(
            f"a {UTM_WEEK_MARKER} marker survived the replacement (encoded or in another case). "
            f"Refusing the draft: a literal marker would reach the customer's address bar.")
    pairs = utm_pairs(after)
    if not pairs:
        raise UtmSeamRefused("no link carries utm_campaign after replacement - an untagged letter.")
    wrong = sorted({c for c, _ in pairs if not c.startswith(week + "-")})
    if wrong:
        raise UtmSeamRefused(
            f"utm_campaign value(s) not built from the marker: {', '.join(wrong)} (expected "
            f"{week}-<base>). At least one link has its week written in.")
    return after, pairs


def _guard_content(template_id: int, blob: str, approved_attributes):
    """The content refusals, run on the HTML that will actually be sent (after the seam)."""
    params = params_in(blob)
    if params:
        raise TemplateUsesParams(
            f"template {template_id} references {len(params)} Liquid param(s): "
            f"{', '.join(params[:8])}{' ...' if len(params) > 8 else ''}. params fill only through "
            f"the transactional API and are ALWAYS empty in a campaign, so every one of these "
            f"renders as a blank. Rewrite them as contact attributes or remove the block; this "
            f"check cannot be widened, because the value genuinely does not exist here.")
    rendered = attributes_in(blob)
    unapproved = [a for a in rendered if a not in approved_attributes]
    if unapproved:
        known_discount = [a for a in unapproved if a in DISCOUNT_ATTRIBUTES]
        pairs = discount_shaped_pairs(rendered)
        raise TemplateUsesUnapprovedAttribute(
            f"template {template_id} renders {len(unapproved)} attribute(s) outside the approved "
            f"set: {', '.join(unapproved)}."
            + (f" Of these, {', '.join(known_discount)} are known discount fields."
               if known_discount else "")
            + (f" It also renders discount-shaped price pair(s): {', '.join(pairs)} - a discount "
               f"can hide as a price." if pairs else "")
            + " Approve the attribute in mkt_control.template_attribute_allowlist or strip it from "
              "the template; do not widen this check.")
    # SECOND LAYER. Reached only when every rendered attribute IS approved, so it exists to catch a
    # wrong allowlist rather than an unlisted field.
    known_discount = [a for a in rendered if a in DISCOUNT_ATTRIBUTES]
    if known_discount:
        raise TemplateUsesDiscount(
            f"template {template_id} renders {', '.join(known_discount)}, which are known discount "
            f"fields, and they are approved in the allowlist. Refusing anyway: Raivis retired "
            f"discount codes on 2026-09-07, so an approval on one of these is far more likely to be "
            f"a slip than a decision.")


def create_draft(name: str, list_id: int, template_id: int, week: str,
                 approved_attributes, reply_to: str = "info@tiktik.lv") -> dict:
    """Create the campaign as a DRAFT from the template's HTML after the UTM seam. Never sent.

    SINCE 2026-09-11 THE CAMPAIGN IS BUILT FROM htmlContent, NOT templateId (UTM seam v1): the
    template's HTML is read, every __UTM_WEEK__ becomes the ISO week, and THAT HTML is the campaign.
    Every content refusal runs on the replaced HTML - the letter that would actually go out.

    WHAT CHANGED ABOUT ACTIVE TEMPLATES, stated rather than hidden: Brevo's 405 on an inactive
    template applied to `templateId`; a campaign from htmlContent does not need the template to be
    active, so a draft no longer requires it. template_active is still read and returned, and the
    DAY BATCH still blocks an inactive template (batch.py TEMPLATE_INACTIVE_IN_BREVO) - activation
    keeps its meaning as Marketing's "ready" signal for a real day; it is no longer a draft gate.

    utmCampaign is deliberately NOT sent: Brevo's field accepts only alphanumerics and spaces, so
    the slug cannot live there without becoming a second value. The links carry it.
    Returns {"id", "subject", "pairs", "template_active", "reachable"}.
    """
    if list_id not in ALLOWED_LIST_IDS:
        raise ListNotAllowed(
            f"list {list_id} is not in the hard allowlist {sorted(ALLOWED_LIST_IDS)}. Until Raivis' "
            f"first approval, this layer may address only a list whose entire membership is his own "
            f"address. Widening it is a code change, on purpose.")
    t = template(template_id)
    subject = t.get("subject") or ""
    html_after, pairs = apply_utm_week(t.get("htmlContent") or "", week)
    _guard_content(template_id, html_after + " " + subject, approved_attributes)
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
        "htmlContent": html_after,
        "recipients": {"listIds": [list_id], "exclusionListIds": [SUPPRESSION_LIST_ID]},
        "inlineImageActivation": False,
    }
    created = _call("POST", "/emailCampaigns", payload)
    log.info("DRAFT_CREATED id=%s list=%s reachable=%s week=%s pairs=%s",
             created.get("id"), list_id, reachable, week, len(pairs))
    return {"id": int(created["id"]), "subject": subject, "pairs": pairs,
            "template_active": bool(t.get("isActive")), "reachable": reachable}


def delete_draft(campaign_id: int) -> bool:
    """Delete a test draft and READ that it is gone (GET answers 404). True only then."""
    _call("DELETE", f"/emailCampaigns/{campaign_id}")
    try:
        campaign(campaign_id, statistics="globalStats")
    except urllib.error.HTTPError as e:
        return e.code == 404
    return False


# Minimal Liquid, for LINK CHECKING ONLY: `{% if <cond> %}...{% else %}...{% endif %}` where cond is
# `contact.X`, `contact.X and contact.Y ...`, or `"s" in contact.X`. Anything else is left untouched,
# so it stays a placeholder and blocks (fail-closed). Rendering blocks matters twice: a link inside
# `{% if contact.HERO_PRODUCT_URL %}` does not exist for a contact without one, and the house idiom
# `{% if "?" in contact.URL %}&amp;{% else %}?{% endif %}` picks the separator per contact.
_IF_BLOCK = re.compile(
    r"\{%\s*if\s+((?:(?!%\}).)+?)\s*%\}((?:(?!\{%\s*if\b).)*?)(?:\{%\s*else\s*%\}((?:(?!\{%\s*if\b).)*?))?\{%\s*endif\s*%\}",
    re.S)
_TERM_ATTR = re.compile(r"^contact\.([A-Za-z0-9_]+)$")
_TERM_IN = re.compile(r"^[\"']([^\"']*)[\"']\s+in\s+contact\.([A-Za-z0-9_]+)$")


def _cond(expr: str, attrs: dict):
    result = True
    for term in re.split(r"\s+and\s+", expr.strip()):
        m = _TERM_ATTR.match(term.strip())
        if m:
            result = result and bool(attrs.get(m.group(1)))
            continue
        m = _TERM_IN.match(term.strip())
        if m:
            result = result and (m.group(1) in str(attrs.get(m.group(2)) or ""))
            continue
        return None  # unknown syntax: do not guess
    return result


def render_for_contact(html_text: str, attrs: dict) -> str:
    text = html_text or ""
    while True:
        changed = False

        def _one(m):
            nonlocal changed
            c = _cond(m.group(1), attrs)
            if c is None:
                return m.group(0)
            changed = True
            return m.group(2) if c else (m.group(3) or "")
        new = _IF_BLOCK.sub(_one, text)
        if not changed or new == text:
            return new
        text = new




_PLACEHOLDER = re.compile(r"\{\{\s*(?:contact\.)?([A-Z0-9_]+)\s*\}\}")


def substitute(text: str, attributes: dict) -> str:
    """Replace {{ contact.X }} with the contact's real value. Unknown names are left alone.

    Left alone rather than blanked on purpose: a placeholder nobody can fill has to stay visible as
    a placeholder, or the link check passes on a URL that will be broken for every real reader.
    """
    return _PLACEHOLDER.sub(
        lambda m: str(attributes.get(m.group(1), m.group(0))), text or "")


def is_brevo_system_link(href: str) -> bool:
    """Is this href entirely one of Brevo's own merge tags. Whole match only - see the constant."""
    m = _WHOLE_TOKEN.match((href or "").strip())
    return bool(m) and m.group(1).lower() in BREVO_SYSTEM_TOKENS


def check_links(campaign_id: int, as_contact: str = None, expect_utm: str = None):
    """Every link in the campaign's HTML must answer 200 before the campaign may be scheduled.

    THREE classes, and the third was added on 2026-09-10 because conflating it with the second made
    a refusal that could never clear.

      static  - resolvable now, fetched now.
      system  - Brevo's own tag, whole-href (`{{ unsubscribe }}` and friends). We can never fill it
                and Brevo always does. Counted, reported, blocks nothing, and never counted as a
                real link either.
      dynamic - anything else still carrying a placeholder. Cannot be resolved without a real
                contact's attributes, so it is reported as unresolved and BLOCKS; resolving it
                needs a test send to the allowlisted list, which is the second stage.

    `checked` is the number of real http links actually fetched. ZERO of them is not success - the
    caller refuses it as no_real_links, which is a different fact from an unresolved placeholder
    and deserves its own word.
    """
    html = campaign(campaign_id).get("htmlContent") or ""
    attrs = contact_attributes(as_contact) if as_contact else {}
    # Render the letter AS this contact first: blocks the contact does not see carry no links for
    # them, and the separator idiom resolves per contact. Unknown Liquid stays and blocks.
    html = render_for_contact(html, attrs) if as_contact else html
    static, system, dynamic = [], [], []
    for href in set(hrefs_in(html)):
        resolved = _html.unescape(substitute(href, attrs)).strip()
        # A link is only checkable once its placeholders carry real values. This is why the check
        # is run AS a contact: the 24.08 breakage lived inside the substituted part of the URL and
        # is invisible in the template's own HTML.
        if is_brevo_system_link(resolved):
            system.append(resolved)
        elif "{{" in resolved or "{%" in resolved:
            dynamic.append(resolved)
        else:
            static.append(resolved)
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
    # UNSUBSCRIBE GATE (MAIN, 2026-09-11; rule #2 wants Brevo's OWN unsubscribe). Counted on the
    # letter as this contact sees it, links in the raw HTML (not inside a block the contact
    # does not see). EXACTLY one: zero means nobody can leave; two means two footers, which is a
    # template assembled twice. Either refuses as no_unsubscribe.
    unsub = unsubscribe_links(html)
    return {"ok": ok, "failed": failed, "dynamic_unresolved": sorted(dynamic),
            "brevo_system": sorted(system), "checked": len(ok) + len(failed),
            "missing_utm": missing_utm, "unsubscribe_links": unsub,
            "no_unsubscribe": unsub != 1}


def unsubscribe_links(html_text: str) -> int:
    """How many hrefs are exactly Brevo's `{{ unsubscribe }}` tag."""
    n = 0
    for href in hrefs_in(html_text):
        m = _WHOLE_TOKEN.match(_html.unescape(href).strip())
        if m and m.group(1).lower() == "unsubscribe":
            n += 1
    return n


def send_now(campaign_id: int, send_date: str, batch_id: str, build_id: str, approval_lookup):
    """Send a campaign. Raises unless Raivis approved exactly THIS batch at THIS build.

    THE SEND-TIME GATE (MAIN, 2026-09-11). NO_APPROVAL_ROW used to be a press check, which made the
    press wait for its own approval. It lives here now, with the same words - silence is not
    consent - and it is keyed on (batch_id, build_id), not on the date: an approval is for the day
    the human saw, and a batch rebuilt after his press is a different day.

    `approval_lookup(batch_id, build_id)` is injected (press_live.approval_for in production) so
    this module holds no BigQuery client and the refusal can be exercised without a warehouse. The
    decision is press.send_gate(), which re-checks both keys on whatever row the lookup returns.

    There is no flag that turns this off. SEND_MODE, DRY_RUN and an absent key are all things a
    person can change in thirty seconds under pressure; a raise at the call site is not.
    """
    import press  # pure module: no client, no clock
    gate = press.send_gate(send_date=send_date, batch_id=batch_id, build_id=build_id,
                           approval_row=approval_lookup(batch_id, build_id))
    if not gate["passed"]:
        raise SendRefused(f"{gate['reason_lv']} ({gate['detail']})")
    raise SendRefused(
        f"the send path is not built. Raivis' condition of 2026-09-09 stands - 'visam japaliek dry "
        f"run kamer nav viss lidz galam gatavs' - and enabling it is his call, not a code change "
        f"anyone here may make. campaign_id={campaign_id} send_date={send_date} batch_id={batch_id}")
