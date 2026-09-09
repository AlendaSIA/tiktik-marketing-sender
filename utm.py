"""UTM slug emission for the lifecycle engine.

The contract is `_knowledge/utm-tracking-convention.md` -> "Lifecycle engine", and the whole
point of it is that ~20 variants going out up to twice a week cannot be tagged by hand: the
slug is a FUNCTION of (ISO week, variant, language), computed the same way everywhere.

`utm_campaign = YYYY-Www-<theme>` (+ `-en` for the English variant). ISO week and not month,
because the same variant can go out twice in a week and a month-granular slug collides with
itself in GA4 - the one place the two sends can no longer be told apart.

THE ASYMMETRY, and it is deliberate. Every money variant's theme is an explicit lookup and a
miss is a BLOCKER: a new commercial campaign without an agreed customer-facing theme is not
routine and must not be invented by code. Educational themes are DERIVED, so a new category
can never block a build: a new educational category is routine.

CUSTOMER-SAFE BY CONSTRUCTION. These values sit in the customer's address bar after a click,
so no internal framing ever appears here - never `winback`, `lost`, `reaktivacija`,
`neaktivs`. That is why `winback_1` is `tava-cena` and `lost_quarterly` is `izdevigi`. The
internal meaning lives in mkt_control.utm_dictionary.internal_label, never in the URL.
"""
import datetime as dt
import re


class UnknownVariantTheme(RuntimeError):
    """A variant with no theme and no derivation rule. Deliberately fatal."""


# The variant axis is email_type, never track (from track the winback rungs collapse into one
# label). This map is a KNOWN PARTIAL: 6 of the ~20 campaigns exist as email_type values today
# and the rest are created by the ladder-step work. Tagging what is actually sent is correct;
# assuming the list is whole is not.
THEMES = {
    "welcome_1": "sveiciens",
    "welcome_2": "sveiciens-2",
    "welcome_3": "sveiciens-3",
    "welcome_4": "sveiciens-4",
    "welcome_5": "sveiciens-5",
    "welcome_6": "sveiciens-6",
    "signup_welcome": "iepazisanas",
    "active_xsell": "komplekts",
    "reorder_1": "papildinam",
    "reorder_2": "papildinam-2",
    "winback_1": "tava-cena",
    "winback_2": "tava-cena-2",
    "winback_3": "tava-cena-3",
    "lost_quarterly": "izdevigi",
    # NOT DERIVABLE, and that is the correct answer rather than a gap. The weekly akcija is
    # brand rotation (`zarys`, ...), so its theme is chosen per campaign by Marketing and
    # cannot be a function of the variant. None means "Marketing names this one at campaign
    # build time"; it is not the same state as "nobody has named this variant", which raises.
    "akcija_weekly": None,
}

# The educational email_type is <category>_info_<step>. Two live categories are abbreviated
# there while the contract's theme table spells them out, so these two are ALIASES of the
# abbreviation - not a theme lookup. A category that is not in here still derives fine, which
# is the property that keeps a new category from ever blocking a build.
EDU_CATEGORY_ALIASES = {"dez": "dezinfekcija", "tir": "tirisana"}
_EDU = re.compile(r"^(?P<cat>[a-z0-9]+)_info_(?P<step>\d+)$")

# lv carries no suffix, because it is the default the slugs were written for. Any other
# language gets its own suffix rather than sharing lv's slug: two different letters under one
# utm_campaign in one week is exactly the collision the ISO week was introduced to remove.
DEFAULT_LANGUAGE = "lv"
KNOWN_LANGUAGES = ("lv", "en")


def iso_week(week_start) -> str:
    """`YYYY-Www` from the assignment's week_start. Accepts a date or an ISO string."""
    if isinstance(week_start, str):
        week_start = dt.date.fromisoformat(week_start[:10])
    year, week, _ = week_start.isocalendar()
    return f"{year}-w{week:02d}"


def theme(email_type: str):
    """The customer-facing theme for a variant, or None when it is chosen per campaign.

    Raises UnknownVariantTheme for a variant that is neither in the map nor derivable. That
    is a blocker on purpose: a slug invented by code is a slug nobody agreed to show a
    customer, and it would reach the address bar before anyone noticed.
    """
    if email_type in THEMES:
        return THEMES[email_type]
    m = _EDU.match(email_type or "")
    if m:
        category = EDU_CATEGORY_ALIASES.get(m.group("cat"), m.group("cat"))
        step = int(m.group("step"))
        # Step 1 keeps the bare theme the contract already names (`cimdi-padomi`); step 2+
        # carries its number, or two different letters of one category share a slug inside a
        # single week and GA4 cannot separate them.
        return f"{category}-padomi" if step <= 1 else f"{category}-padomi-{step}"
    raise UnknownVariantTheme(
        f"no customer-facing theme for email_type={email_type!r}. Add it to utm.THEMES "
        f"(Marketing owns the name) or, if it is educational, name it <category>_info_<step>.")


def slug(week_start, email_type: str, language: str):
    """`YYYY-Www-<theme>[-<lang>]`, or None when the theme is chosen per campaign."""
    t = theme(email_type)
    if t is None:
        return None
    lang = (language or DEFAULT_LANGUAGE).lower()
    if lang not in KNOWN_LANGUAGES:
        raise UnknownVariantTheme(
            f"language={language!r} has no slug suffix convention. Sharing lv's slug would put "
            f"two different letters under one utm_campaign in one week.")
    suffix = "" if lang == DEFAULT_LANGUAGE else f"-{lang}"
    return f"{iso_week(week_start)}-{t}{suffix}"
