# STYLE SHEET — tiktik.lv engine letters (v1, 2026-09-24)

Owner: tiktik.lv › Marketing · Vēstuļu šabloni (session_01QrczvcDF2eMQ9Hej8sFJ5T). Built from what Raivis really sent:
campaigns 221 ("Tikai tev: 7 dienas"), 222 (Mercator nedēļa), 110/111 (ZARYS), 152 (HYGOSTAR), educational 226/228,
his approved engine templates 179 (reorder_1) and 180 (winback_1), and his own 1:1 reminders. Every quoted phrase below is
verbatim from the source in brackets. Binding order when two rules collide: **A (fixed) > B (Raivis 24.09) > C (voice) > D (layout)**.

---

## A. FIXED RULES — contract v2.7 (sha 2154106c033c) + MAIN 24.09. Breaking any one of these fails the draft check.

A1. **Attributes.** A template may render ONLY these 48 names: `P1..P8_NAME/_IMG/_PRICE`, `R1..R4_NAME/_IMG/_PRICE`,
    `D1..D4_NAME/_URL`, `KABINETS_HAS_PRODUCTS`, `VARDS`, `UZRUNA`, `KABINETS_URL`. Nothing else — no SVEICIENS, HERO_*, XSELL_*,
    FAVORITE_CATEGORY, DAYS_SINCE_LAST, AVG_INTERVAL_DAYS, TAVA_*, PAP_*, params.* — not even in a comment or the subject.
A2. **Greeting = the neutral ladder (rule 7), verbatim, as the first paragraph:**
    `{% if contact.UZRUNA %}Sveiki, {{ contact.UZRUNA }}!{% else %}{% if contact.VARDS %}Sveiki, {{ contact.VARDS }}!{% else %}Sveiki!{% endif %}{% endif %}`
A3. **No "%" character anywhere** in visible text, subject, preheader, `<title>`, alt text. No "atlaide", no "-10", no percent words.
A4. **Prices are rendered as-is:** `{{ contact.P1_PRICE }}` is already "19,99 €" (rule 2); an R row may already read
    "no 5,92 €" (rule 6). Never format, compute, prefix "no" or append "€" yourself.
A5. **Images are rendered as-is:** `src="{{ contact.P1_IMG }}"`. The writer already wraps them in img.php (…&f=jpg).
    Never wrap, never add size parameters to the URL.
A6. **Links.** Every href is exactly one of: `{{ contact.KABINETS_URL }}`, `{{ contact.D1_URL }}`…`{{ contact.D4_URL }}`,
    `{{ unsubscribe }}`. No other URL of any kind (no shop links, no mailto, no plani links). The writer puts UTM on the
    URL, so the template carries NO `utm_` text and appends nothing (`?`, `&`) to a URL.
A7. **Complete HTML document:** `<!DOCTYPE html>`, `<html lang="lv">`, `<head>` with `<meta charset="utf-8">`, viewport meta and
    `<title>` (= the h1 text), `<body>…</body></html>`. Hidden preheader `<div>` as the first element of `<body>`.
A8. **Liquid subset only:** `{% if contact.X %}` / `{% else %}` / `{% endif %}`, nested as needed. No `elsif`, `unless`,
    `for`, `assign`, filters, or comparisons. Never put Liquid logic inside an attribute value; an href is exactly
    `href="{{ contact.KABINETS_URL }}"` (no quotes inside the tag).
A9. **Every slot is conditional (rule 5 — a slot is dropped, never substituted).** P cards only inside `{% if contact.Pn_NAME %}`
    with 179's pair logic (P1/P2 with the single-card case, then P3/P4, P5/P6, P7/P8); R block only inside
    `{% if contact.R1_NAME %}` with R2..R4 each conditional; D block only inside `{% if contact.D1_NAME %}` with D2..D4
    each conditional. Copy these structures from the skeleton (D below) unchanged.
A10. **KABINETS_HAS_PRODUCTS** is a Brevo boolean. Everything built on the cabinet (P grid, R block, cabinet box) sits inside
    `{% if contact.KABINETS_HAS_PRODUCTS %}`. The `{% else %}` branch is defence in depth only: one short text paragraph
    ending with a reply invitation, no products, no buttons.
A11. **D block wording is Raivis' approved sentence (rule 14), verbatim:** "Šobrīd nav noliktavā — apskati, kas šai grupā ir pieejams".
A12. **Footer verbatim from 179**, with exactly one unsubscribe link `{{ unsubscribe }}` ("Atrakstīties no šīm vēstulēm").
A13. **Forbidden words in anything a customer sees:** reaktivācija, winback, lost, atlaide, kods (as discount code), "jūs/jums/jūsu",
    "Labdien", "Cienījam". Never print an internal name, a track name or a category code.
A14. **Shipping line, if used, exactly:** "No 59 € piegāde uz Venipak pakomātu — bez maksas." (a bare "no 59 € bez maksas" is false).
A15. **Price-slot placeholders (winback/lost only).** The personal price is NOT yet in the contract. Mark it with these plain-text
    tokens (not Liquid) and nothing else: `⟦PARASTĀ CENA Pn⟧` (the struck-through standard price), `⟦TAVA CENA Pn⟧` (the
    customer's price), `⟦LĪDZ DATUMAM⟧` (the date the price holds until). n = the slot number of the card it sits in.
    MAIN will fix the field names for all sides; until then the tokens must stay visible and unmistakable.

## B. RAIVIS' CONTENT RULES (24.09) — per letter family

B1. **reorder_n** — a reminder at the NORMAL price, no discount, no deadline, no struck-through price. The card shows
    `{{ contact.Pn_PRICE }}`. The letter's job: remind them that their usual goods are here and ordering is one step.
B2. **winback_n and lost_quarterly — the "fresh batch" frame:** "we have bought in a new batch of your favourite product; as a
    regular customer you get price X for time X while the batch is in stock". Each P card shows the struck-through standard
    price and the customer's price (A15 tokens) plus a small caps label "TAVA CENA"; one calm deadline line under the grid,
    e.g. "Šī cena tev ir spēkā līdz ⟦LĪDZ DATUMAM⟧ — kamēr jaunā partija ir noliktavā." Deadline is 7 days for winback, 14 days
    for lost (the date itself is the placeholder). R rows (cross-sell) keep the plain `{{ contact.Rn_PRICE }}` — no personal price.
B3. **Subjects and headlines never repeat inside one person's ladder.** Taken already: "Laiks papildināt krājumus?" (179),
    "Sen neesam redzējušies" (180), "Vai krājumi vēl turas?" (old 107), "Paldies par pasūtījumu!" (old 35),
    "Tev varētu noderēt arī šis" (old 108). Each new letter gets its own subject, h1 and opening sentence.
B4. **Welcome** never discounts. **akcija** is the weekly residual letter (brand rotation, chosen by Raivis each week).

## C. VOICE (how Raivis writes)

C1. **"mēs" speaks, the reader is "tu".** "Paskatījāmies, ko tu pie mums pērc visbiežāk." [221] · "ko tu mēdz ņemt" [179, 180].
    "jūs/jums/jūsu" occur 0 times in 9 sources. "es" only in his personal 1:1 mails.
C2. **Open with what we did or noticed, past tense + a time word — never with thanks, a slogan or the offer.**
    "Šodien staigājām pa noliktavu…" [179] · "Šonedēļ nolaidām cenas visai Mercator Medical līnijai — …" [222] ·
    "Sen neesam redzējušies. Paskatījāmies — tavas preces joprojām ir plauktā…" [180].
C3. **About the reader's own goods and habits.** "Tieši tās, ko tu mēdz ņemt." [179] · "tava cena tavām TOP precēm" [221].
C4. **Take work away, then promise what we do.** "Grozs jau salikts. … nekas nav jāpasūta uzreiz." [221] ·
    "Ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna." [179, 180] · "saliksim" / "sagatavosim".
C5. **Short, level sentences (6–10 words), hinged on " — ".** Fragments welcome: "Grozs jau salikts." [221].
    "!" only in the greeting. No emoji in subject or preheader. At most one ":)" per letter (179 has the only one).
C6. **Numbers and facts instead of adjectives;** a superlative only when checkable ("Ātrākais veids pasūtīt — tavs kabinets.").
C7. **Price talk without "atlaide" or "%":** "tava cena", "zemāku nekā šodien veikalā" [221]; the struck-through standard price speaks.
C8. **Urgency is a calm fact:** "Šī cena ir spēkā līdz …" [221] · "Cenas spēkā, kamēr prece ir noliktavā." [222, 226].
C9. **Always a human door:** "Ja vajag citu izmēru vai kaut ko, kā šeit nav — atraksti uz šo vēstuli, saliksim." [221] ·
    "Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim." [179, 180].
C10. **Cross-sell in one sentence + the one-box argument:** "Lielākā daļa, kas pērk cimdus, pērk arī papīru — ja tāpat vajag, liec
    vienā kastē, lai nav jāpasūta atsevišķi." [222] · "Ko parasti ņem kopā ar tavām precēm" [179, 180].
C11. **The quietest register is his real reminder** (produced an order within a week, twice): "Sveika! Tāds garāks klusums no tevis,
    varbūt ir kādas vajadzības?" · "Sen klusums no tavas puses. Varbūt ir kādas aktualitātes no manis?" [REM] — two lines, a question,
    no pitch. Use this register for the softest rungs (it is "mēs" in engine letters, never signed "Raivis").
C12. **Physical-shop vocabulary:** noliktava, plaukts, kaste, partija, kabinets, pakomāts. Call the e-mail "vēstule".
    Verbatim phrases worth reusing: "Tieši tās, ko tu mēdz ņemt." · "Lai tev nav jāmeklē no jauna, tās visas ir šeit." ·
    "Ātrākais veids pasūtīt — tavs kabinets." · "nav jāpārmeklē viss veikals" · "lai nav jāpasūta atsevišķi" · "kamēr ir noliktavā".
C13. **Subjects:** engine subjects are one short phrase, 20–45 characters, a warm statement or a real question; no "!",
    no emoji, no price in a reorder subject, "tu/tev/tavs" only where it is personal. The preheader adds ONE new fact
    (never repeats the subject) and ends with a full stop. h1 = subject.

## D. LAYOUT — the engine house layout (template 179), reused unchanged

The skeleton is `/home/claude/d2/ref/179.html` (template 179, byte-exact, sha256 25742af5…). It is part of this style sheet.
Build every letter by copying it and changing ONLY:
- `<title>`, the hidden preheader `<div>`, the h1;
- the paragraph(s) after the greeting — in BOTH branches of `{% if contact.KABINETS_HAS_PRODUCTS %}`;
- the R block heading and its one-line subtext (you may drop the whole R block, or move it, but keep its conditions intact);
- the cabinet box: its bold line, its one paragraph, and the button label (keep "→" and the button style);
- winback/lost only: the price `<div>` inside each P card (A15 tokens + "TAVA CENA" label), and one deadline line after the grid;
- optionally one extra short line (e.g. the A14 shipping line or a C9 reply line) as its own `<tr>`.
Never change: attribute names, `{% if %}` conditions and their nesting, the grid tables, image and link markup, colours
(#12603f button, #f2f7f4 cabinet box, #1f6fb2 product links, #e9e9ec card borders), fonts (Arial 15px/1.6), widths, the D block, the footer.
Price-block style for B2 cards (A-layout convention from 221): old price `<span style="color:#98a2ad;text-decoration:line-through;font-size:13px;">`,
new price `<span style="color:#12603f;font-weight:bold;font-size:17px;">`, label `<div style="color:#12603f;font-size:11px;font-weight:bold;letter-spacing:.5px;">TAVA CENA</div>`.

## E. AMENDMENTS — MAIN 2026-09-25 (command 3). Binding like A.

E1. **akcija_weekly only (F6):** a contact WITHOUT cabinet products gets a featured-page box in the `{% else %}` branch
    of `{% if contact.KABINETS_HAS_PRODUCTS %}` (same style as the cabinet box). Its href is exactly
    `https://www.tiktik.lv/veikals/params/category/featured/?utm_source=brevo&amp;utm_medium=email&amp;utm_campaign=__UTM_WEEK__-akcija`
    — rule 14's fallback page, UTM by the campaign layer's seam v1 marker (campaign.apply_utm_week fills the week).
    This is the ONE exception to A6 and A10, for this href in this letter.
E2. **Tokens and the send path (F7):** A15 tokens may stay in templates and drafts and in test letters to Raivis;
    the send path refuses any ⟦ in the subject, preheader or HTML of a campaign (campaign.send_now → PlaceholderLeft).
E3. **Fresh-batch wording (F4; MAIN command 4):** 232/233/234 carry ONE neutral subject and preheader for everyone.
    Only ONE body sentence switches on the fresh flag (contract v2.8), and it names ONLY the P1 (hero) product, inside
    `{% if contact.P1_NAME %}` — e.g. "Tikko iepirkām jaunu partiju ({{ contact.P1_NAME }})." — never P2–P8; everything
    else (deadline lines included) is neutral: "… kamēr prece ir noliktavā" (C8). The neutral twin is in templates/neutral/.
E4. **Slot gates (F5; MAIN command 4):** 179, 180 and 232–234 go only to contacts with P1_NAME filled, 235 only with
    R1_NAME filled (sender plan decisions SLOT_GATE_P1_EMPTY / SLOT_GATE_R1_EMPTY).
