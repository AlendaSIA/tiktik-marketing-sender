# review_B: winback_2, winback_3, lost_quarterly, akcija_weekly

Reviewer B (independent, read-only), 2026-09-25. Read against style_sheet.md (A–D), contract v2.7 and the approved letters 179 and 180.
Each letter was rendered three ways (full, single card, has_products=false). `check.py` passes on all four. I left the ⟦…⟧ tokens in place because they are allowed.
Severity levels:
- **MUST-FIX**: false, broken or unsendable as it stands.
- **SHOULD-FIX**: wrong register, unclear, or a gap worth closing.
- **OK**: no change needed.

---

## 0. Cross-cutting: the three fresh-batch letters (winback_2, winback_3, lost_quarterly)

**X1 · MUST-FIX before activation · owner MAIN / Raivis. The fix is data. The B2 frame stays.**
- Current:
  - winback_2: `Tikko iepirkām jaunu partiju tavu ierasto preču.`
  - lost_quarterly: `Noliktavā ir jauna partija tieši tavu preču.`
  - both deadline lines: `… kamēr jaunā partija ir noliktavā.`
  - winback_2 subject: `Jauna preču partija — par tavu cenu`
- When it is false: whenever a product shown in a P slot was not actually received recently. P1..P8 hold the customer's most-bought SKUs that are in stock today (RULE 3/5/14). Nothing in v2.7 knows when a batch arrived. A slow SKU last received months ago still fills P1, and a customer with 8 slots is told about 8 fresh deliveries "tikko".
- Proposed: no text change, provided MAIN fills the P slots of these three variants only with SKUs that have a Paytraq purchase receipt in the last N days (MAIN or Raivis picks N).
  - If that filter cannot be built, this wording stays true:
    - winback_2 subject/h1/title: `Tava cena tavām ierastajām precēm`
    - winback_2 S1: `Šonedēļ pārskatījām tavu ierasto preču cenas.`
    - lost S: `Tavas ierastās preces ir noliktavā.`
    - both deadline lines: `… — kamēr prece ir noliktavā.` / `…, kamēr prece ir noliktavā.` (C8 verbatim [222, 226])
- Why: "new batch" is the only fact in the frame the data cannot confirm. In stock, own price and date are all in the data.

**X2 · MUST-FIX before activation · owner MAIN: where the personal price is honoured**
- Current: every P card, product name and the green button link to `KABINETS_URL`.
- The cabinet prices each SKU with the same expression as `Pn_PRICE` (RULE 3/4). That is the standard price, which these letters strike through as ⟦PARASTĀ CENA⟧. Contract v2.7 carries no personal price anywhere.
- When it is false: a reader who follows the letter's only path sees a higher price than the letter promised. An order placed there is not guaranteed ⟦TAVA CENA⟧.
- Proposed: MAIN decides one of two options.
  - (a) The cabinet shows and applies the personal price until ⟦LĪDZ DATUMAM⟧. No text change is needed.
  - (b) The price is applied when the order is processed. Then append to the cabinet paragraph of all three letters: `Kabinetā redzēsi parasto cenu — rēķinā būs tava.`
- Why: the price promise is only true if some system or person honours it.

**X3 · SHOULD-FIX · owner MAIN: P1 must exist**
- Current: the deadline row sits only inside `{% if contact.KABINETS_HAS_PRODUCTS %}` (verified), not inside `P1_NAME`.
- When it is false: `KABINETS_HAS_PRODUCTS=true` does not by contract imply `P1_NAME`, because P slots survive only if the SKU is in stock and not dropped by rule 5/14. With P1 empty, the letter prints the fresh-batch intro, "To redzi zemāk, pie katras tavas preces" and the deadline line over an empty grid. Only "Šobrīd nav noliktavā" follows.
- Proposed: send winback_2, winback_3 and lost_quarterly only to contacts with `P1_NAME` filled. Alternatively, define `KABINETS_HAS_PRODUCTS` as "P1_NAME is filled".
- Why: without a P card the letter talks about a price it never shows.

---

## 1. winback_2

**W2-1 · MUST-FIX (truth): the bold line in the cabinet box**
- Current: `Tava cena jau gaida kabinetā.`
- Proposed: `Ātrākais veids pasūtīt — tavs kabinets.` (the line Raivis approved in 179/180; winback_3 keeps it too)
- Why: the cabinet shows the standard price (see X2). Restore the current line only if MAIN chooses X2 (a).

**W2-2 · SHOULD-FIX (language)**
- Current: `Pasūtot domājām arī par tevi — tev ir sava cena.`
- Proposed: `Domājām arī par tevi — tev ir sava cena.`
- Why: every other "pasūtīt" in the letter is the reader's action ("Ātrākais veids pasūtīt", "nav jāpasūta"). A sentence that opens with "Pasūtot" first reads as "when (you) order". Our own buying was already "iepirkām".

**OK (checked, no change)**
- Subject/h1 `Jauna preču partija — par tavu cenu`: 35 characters, unique in the ladder, natural Latvian (truth: see X1).
- Preheader `Tieši tās preces, ko tu pie mums parasti ņem.`: one fact, ends with a full stop. It is close to 179's "tieši tās, ko tu mēdz ņemt", which is acceptable because B3 covers only the subject, h1 and opening.
- S1: grammar is correct (see X1). `To redzi zemāk, pie katras tavas preces.` is correct and true in the letter.
- No-products branch: fine.
- Deadline line: the B2 example verbatim. The 7 days are carried by the date token, as B2 says.
- R subtext `Šīs ir par veikala cenu. …`: good, and exactly what B2 requires for R rows.
- Cabinet paragraph and its C9 door: fine.
- Price block: identical to the D spec in all 9 places (P1 pair and single card, P2–P8).
- Gender-neutral forms, "!" only in the greeting, no %, no emoji, no forbidden words.

## 2. winback_3

**W3-1 · MUST-FIX (truth + language): second intro sentence**
- Current: `Šī ir pēdējā reize, kad tai turam tavu cenu.`
- Proposed: `Tava cena arī vēl ir spēkā.`
- When it is false: always, by the system's own design. A winback_3 reader who stays silent reaches lost_quarterly (12+ months), which gives them "sava cena" again for 14 days. Personal-price campaigns such as 221 also run outside the ladder.
- Language: "tai" (for the batch) is awkward, and on first reading it is ambiguous.

**W3-2 · MUST-FIX (truth): first intro sentence**
- Current: `Šorīt skaitījām kastes — partija ar tavām precēm iet uz beigām.`
- Proposed: `Šorīt skaitījām kastes — tavu preču partija vēl nav izpārdota.`
- When it is false: "iet uz beigām" is false for any P SKU with normal stock. P slots are chosen by purchase rank and in-stock status, never by low stock. "vēl nav izpārdota" is true for every in-stock slot and keeps a calm stock fact (C8).
- Keep the current sentence only if MAIN limits winback_3's P slots to SKUs with genuinely low stock.
- Language: "partija ar tavām precēm" sounds translated. The natural genitive is "tavu preču partija".
- Resulting intro: `Šorīt skaitījām kastes — tavu preču partija vēl nav izpārdota. Tava cena arī vēl ir spēkā.`

**W3-3 · MUST-FIX (same claim): preheader, in both the HTML div and the json**
- Current: `Partija ar tavām precēm iet uz beigām — kas palicis, tas vēl ir plauktā.`
- Proposed: `Tava cena spēkā līdz ⟦LĪDZ DATUMAM⟧ — kamēr partija ir noliktavā.`
- Why:
  - It makes the same false scarcity claim as W3-2.
  - "kas palicis, tas vēl ir plauktā" is a tautology.
  - The date is the one new fact against the subject (C13) and the calm urgency (C8).
  - The token is allowed in the preheader (check.py scans it).
- Also update the json `name` and `purpose` ("tava cena pēdējo reizi" / "last time").

**OK (checked, no change)**
- Subject/h1 `Tavu cenu turam vēl šoreiz`: 26 characters, natural, unique. It hints at an end without promising "last".
- No-products branch `Šorīt skaitījām kastes un iedomājāmies par tevi. …`: fine.
- Deadline line `Tava cena ir spēkā līdz ⟦LĪDZ DATUMAM⟧ — kamēr partijas atlikums ir noliktavā.`: a condition, not a claim, so it stays true after W3-2.
- The cabinet paragraph's C9 door and the dropped R block: fine. All other conditions are intact.
- Price block: matches the D spec in all 9 places.

## 3. lost_quarterly

**L-1 · SHOULD-FIX (voice C2/C6): opener, in both branches**
- Current, has-products paragraph 1: `Laiks paskrējis nemanot — pie mums šis tas ir mainījies.`
- Proposed: `Šonedēļ pāršķirstījām vecos pasūtījumus — arī tavējos.`
- Current, no-products: `Laiks paskrējis nemanot — pie mums šis tas ir mainījies. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.`
- Proposed: `Šonedēļ pāršķirstījām vecos pasūtījumus — arī tavējos. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.`
- Why:
  - C2: open with what we did, plus a time word, not a stock phrase.
  - "šis tas ir mainījies" is a vague teaser that the letter never names.
  - A customer who has been gone for a year may read it as "prices or range changed".

**L-2 · SHOULD-FIX (truth clarity, B2): R subtext**
- Current: `Šīs preces bieži ņem kopā ar tavējām — ja tāpat vajag, nebūs jāpasūta atsevišķi.`
- Proposed: `Šīs ir par veikala cenu. Tās bieži ņem kopā ar tavējām — ja tāpat vajag, nebūs jāpasūta atsevišķi.`
- Why: R rows carry no personal price. They sit directly under "Tava cena ir spēkā 14 dienas…" and a "same box" heading, so a reader can assume they have one. winback_2 already says this.

**L-3 · NOTE for MAIN / Raivis**
- If one person can receive lost_quarterly in more than one quarter, they get the identical subject, h1 and opening every time (B3), plus the same "jauna partija" claim. Either accept this explicitly or rotate the subject.

**OK (checked, no change)**
- Subject/h1 `Atceramies, ko tu mēdzi ņemt`: 28 characters. "mēdzi" is the correct past form. Clearly different from 180, winback_2 and winback_3.
- Preheader `Tām pašām precēm tev tagad ir sava cena.`: one fact, ends with a full stop.
- `Tā kā tu jau pirki no mums, tev ir sava cena.`: the simple past keeps it gender-neutral (avoids "esi pircis/pirkusi"). The "new batch" sentence is covered by X1.
- Deadline line `Tava cena ir spēkā 14 dienas — līdz ⟦LĪDZ DATUMAM⟧, kamēr jaunā partija ir noliktavā.`: 14 days as B2 says. MAIN must set the date to send date + 14 so the two agree.
- R heading `Vēl kaut ko tajā pašā kastē?`: fine.
- Cabinet bold and text: fine. "Ja kas mainījies, atbildi uz šo vēstuli — nokārtosim." is a good door for a lapsed customer.
- Price block: matches the D spec in all 9 places.

## 4. akcija_weekly

**A-1 · MUST-FIX (mechanics, missed by check.py) · owner MAIN, before first use: the weekly block has no allowed link**
- Current: `<tr><td …>⟦NEDĒĻAS PIEDĀVĀJUMS⟧</td></tr>`. The only real links in the frame are the cabinet button (inside `KABINETS_HAS_PRODUCTS`) and the D links.
- A6 forbids shop links in the block. A shop link with utm fails `template_side_utm`. Without the week's utm it fails draft_test.
- The problem:
  - For a list-3 contact with no cabinet products and no D slot, the render has 0 real links. check.py exempts the false render, but for akcija that is the main audience, not a fallback.
  - draft_test fails such a contact as `no_real_links`. The send path has no link check at all (RULE 11), so an untested contact of that kind gets an offer they cannot click.
  - Contacts with a cabinet can't click the offered goods either, because the cabinet shows only their own goods.
- Proposed: MAIN defines the block's destination. The natural one is the featured page `…/veikals/params/category/featured/`, which RULE 14 already calls the week's "Nedēļas akcijas" page and which draft_test's markers already accept. It needs a contract field, or a written A6/rule-10 exception carrying the week's utm. The fallback is to exclude contacts without cabinet products, which contradicts the frame's purpose.

**A-2 · MUST-FIX (mechanics) · owner campaign layer, before first use: nothing stops a forgotten token**
- Current: draft_test.py only *reports* ⟦…⟧ tokens (its docstring: "A placeholder is REPORTED, not failed"). The send path checks no content at all: RULE 11 says the press verdict looks only at audience, lists and credits.
- When it breaks: one week where a token is left in ships `⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti` as the subject to the whole list. The same applies to the price tokens if a fresh-batch track is switched on before MAIN fills them.
- Proposed: a hard stop on `⟦` in the rendered subject, preheader and body on the path to send.

**A-3 · SHOULD-FIX (fill rule, belongs in the json purpose / Raivis' weekly checklist)**
- ⟦NEDĒĻAS TĒMA⟧ must be a brand name exactly as printed on the pack.
  - Verified: `Mercator nedēļa: lētāk nekā parasti` / `Šonedēļ nolaidām cenas Mercator precēm.`; `ZARYS nedēļa: …` / `… cenas ZARYS precēm.`; `HYGOSTAR nedēļa: …` / `… cenas HYGOSTAR precēm.`; also `Franz Mensch`. All are correct, 32–39 characters.
  - A product group breaks both sentences: "Nitrila cimdi nedēļa", "cenas Nitrila cimdi precēm". The token's name, "TĒMA", invites exactly that.
- Every item in ⟦NEDĒĻAS PIEDĀVĀJUMS⟧ must really be lowered this week. Otherwise "lētāk nekā parasti" and "nolaidām cenas" are false.
- If the brand's prices return to normal after the week, `Cenas spēkā, kamēr prece ir noliktavā.` (in the preheader and the body) is false and must name the end date.

**OK (text)**
- Subject/h1/title are identical.
- Intro: C2 verbatim from 222, a C9 door from 221, correct agreement ("preces" → "Tās").
- Preheader: one fact, ends with a full stop.
- C8 and A14 lines: verbatim.
- Cabinet box `Preces, ko tu mēdz ņemt — tavā kabinetā.` / `Ja vajag arī tās, var likt tajā pašā kastē. …`: natural, and correctly inside `KABINETS_HAS_PRODUCTS`.
- No "tu" in the subject, which is right for a mass promo. The D block and footer are unchanged.

---

## 5. Uniqueness (subject = h1, and the opening sentence)

| letter | subject / h1 | opening sentence |
|---|---|---|
| 180 winback_1 | Sen neesam redzējušies | Sen neesam redzējušies. |
| winback_2 | Jauna preču partija — par tavu cenu | Tikko iepirkām jaunu partiju tavu ierasto preču. |
| winback_3 | Tavu cenu turam vēl šoreiz | Šorīt skaitījām kastes — … (after W3-2: "tavu preču partija vēl nav izpārdota") |
| lost_quarterly | Atceramies, ko tu mēdzi ņemt | Laiks paskrējis nemanot — … (after L-1: "Šonedēļ pāršķirstījām vecos pasūtījumus — arī tavējos.") |
| akcija_weekly | ⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti | Šonedēļ nolaidām cenas ⟦NEDĒĻAS TĒMA⟧ precēm. |

- **Verdict: all distinct.** They also differ from welcome_1, reorder_2, reorder_3, active_xsell, 179 and the B3 list. "tavu cenu" appears in two consecutive winback subjects, but as clearly different phrases, so it is acceptable.
- FYI for reviewer A, outside my set: reorder_2 `Vai krājumu vēl pietiek?` is close to the already-taken `Vai krājumi vēl turas?` (old 107, B3).
