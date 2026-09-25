# Review A: welcome_1, reorder_2, reorder_3, active_xsell

Independent reviewer (I did not write these templates). 2026-09-25. Read-only: no template file was changed, and no MCP or external tool was called.
Binding references: style_sheet.md (A–D), contract_v27.md, ref/179.html, ref/180.html. The files reviewed are the ones in tpl/SHA256.txt; their hashes were re-verified and have not changed.

Method:
- I rendered each letter with `draft_test.render` for check.py's three contacts (full, min, false) and for these edge cases: no R rows; no P rows but D rows present; one P row only.
- `check.py` gives PASS for all four as delivered.
- I applied every proposed replacement below to scratch copies and ran check.py again. All four still PASS.

Applying a fix means:
1. Change the HTML (and `<title>`/h1 for a subject).
2. Change the matching field in `<variant>.json` (subject, preheader or text_lv).
3. Re-run check.py and refresh SHA256.txt.
4. Re-upload the template. Brevo ids from brevo_ids.json: welcome_1 229, reorder_2 230, reorder_3 231, active_xsell 235.

## Ladder uniqueness (item 4)

| ladder rung | subject = h1 | opening sentence | verdict |
|---|---|---|---|
| reorder_1 (179) | Laiks papildināt krājumus? | Šodien staigājām pa noliktavu un skatāmies — … | approved |
| reorder_2 | Vai krājumu vēl pietiek? | Šonedēļ padomājām par tevi — … | **subject is a near-repeat** (R2-1) |
| reorder_3 | Varbūt ir kādas vajadzības? | Šonedēļ pamanījām — … | subject OK; opener mirrors reorder_2 (R3-1) |
| winback_1 (180) | Sen neesam redzējušies | Sen neesam redzējušies. Paskatījāmies — … | approved |
| winback_2 | Jauna preču partija — par tavu cenu | Tikko iepirkām … | see note |
| winback_3 | Tavu cenu turam vēl šoreiz | Šorīt skaitījām kastes — … | see note |
| lost_quarterly | Atceramies, ko tu mēdzi ņemt | Laiks paskrējis nemanot — … | see note |

These subjects are outside the ladders: welcome_1 "Tavs kabinets ir gatavs", active_xsell "Ko citi ņem pie tavām precēm" and akcija_weekly "⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti". None of them clashes with another subject or with B3's taken list.

Cross-check note for the winback reviewer (these are not my templates and are not graded here):
- winback_2 and winback_3 are consecutive rungs, and both subjects carry "tavu cenu". This is a borderline near-repeat.
- The lost_quarterly subject "Atceramies, ko tu mēdzi ņemt" echoes the "ko tu mēdz ņemt" phrase in 180's preheader and opening.

---

## welcome_1

**W1 · SHOULD-FIX · truthfulness (intro, has-products branch)**
- Current: "Pēc tava pirmā pasūtījuma iekārtojām tev kabinetu. Nekas nav jāmeklē no jauna — tavas preces jau ir kabinetā."
- Proposed: "Pēc tava pirmā pasūtījuma salikām tavas preces kabinetā. Nekas nav jāmeklē no jauna — nākamreiz tās ir turpat."
- Why: some people had a kabinets before their first order, and for them "iekārtojām tev kabinetu" (we set up a cabinet for you) is false. Non-buyers get a cabinet through the plan tool: kabinets_links holds kind = plans/leads (contract rule 8b), tab=plans exists (rule 9), and 226 already sent "tavā tiktik.lv kabinetā". The new sentence ("we put your goods in the cabinet") is true for everyone.

**W2 · SHOULD-FIX · truthfulness (cabinet box)**
- Current: "Kabinetā ieraksti daudzumu un nosūti — adrese un rekvizīti jau ir saglabāti."
- Proposed: "Kabinetā ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna."
- Why: "jau ir saglabāti" (already saved) is false for a private first-time buyer, who has no company rekvizīti. Nothing in the references says the address from a first shop order is stored in the cabinet. Raivis' approved 179 clause is true in every case.

**W3 · SHOULD-FIX · language (no-products branch)**
- Current: "Tavs pirmais pasūtījums pie mums ir izdarīts."
- Proposed: "Nesen saņēmām tavu pirmo pasūtījumu."
- Why: "izdarīt pasūtījumu" is a calque of Russian "сделать заказ"; standard Latvian says "pasūtīt" or "veikt pasūtījumu". The sentence also tells readers what they already know. The replacement is a C2 opener: mēs, past tense and a time word. This branch is defence-in-depth only, so this is low priority.

**W4 · OK (verify) · truthfulness**
- Current: "pirmā pasūtījuma" (in the preheader "Tur jau ir preces no tava pirmā pasūtījuma." and in the intro).
- Proposed: no change if welcome_1's audience is "first-ever order for that person across shop AND invoice (Paytraq) sales". If the audience is only "first shop order", drop "pirmā" in both places.
- Why: otherwise a customer who bought by invoice earlier is told this is their first order.

**W5 · OK · layout**
- The R block is cut to R1 only, using 179's own single-card branch; R2–R4 and their conditions are removed.
- D says to keep the R block's conditions intact. The cut is acceptable only because the welcome purpose asks for one item.
- It renders cleanly with 0, 1 or 4 R rows, and the heading "…arī šo" is correctly singular.

**Everything else in welcome_1 is OK:**
- The subject is 23 characters. The preheader adds one fact and ends with ".".
- The A14 shipping line is verbatim.
- Prices are normal, with no discount and no deadline.
- All "tu" forms are gender-neutral.
- There is no "!" outside the greeting, and all dashes are spaced.

---

## reorder_2

**R2-1 · MUST-FIX · uniqueness (subject = h1 = `<title>`)**
- Current: "Vai krājumu vēl pietiek?"
- Proposed: "Kā tev ar ierastajām precēm?"
- Why: it nearly repeats B3's taken subject "Vai krājumi vēl turas?" (old 107), with the same "Vai krājum- vēl …?" frame and the same meaning. It also nearly repeats 179 "Laiks papildināt krājumus?" one rung earlier: same noun, same yes/no question about stock. The proposed subject is 28 characters.

**R2-2 · SHOULD-FIX · truthfulness (intro AND no-products branch, both occurrences)**
- Current: "ap šo laiku tu parasti pasūti atkal"
- Proposed: "varbūt tavi krājumi jau iet uz beigām"
- Why: the template has no interval data (AVG_INTERVAL_DAYS is outside the contract, A1). The claim is false in two cases:
  - reorder_2 fires only after 179 went unanswered, so every reader is already past their usual moment;
  - a reader with only one or two earlier orders has no "parasti".
- The hedged line is true for every reader. It still leads naturally into "Ja vēl pietiek — labi. Ja ne — …".

**R2-3 · SHOULD-FIX · truthfulness (cabinet box)**
- Current: "Ieraksti daudzumu un nosūti — adrese un rekvizīti tur jau ir."
- Proposed: "Ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna."
- Why: the same problem as W2. "tur jau ir" is false for a buyer without company rekvizīti, while the approved 179 wording is always true. Keep "Pārējo izdarīsim mēs." as it is.

**R2-4 · SHOULD-FIX · language (R subtext)**
- Current: "Ja kaut kas noder — pieliksim tajā pašā kastē."
- Proposed: "Ja kaut kas noder — ieliksim tajā pašā kastē."
- Why: the natural collocation is "ielikt kastē". "pielikt" needs "klāt".

**R2-5 · SHOULD-FIX · mechanics (check.py does not catch this)**
- Current: build_reorder_2.py still carries SUBJECT "Varbūt ir kas vajadzīgs no mums?" and the intro "… Varbūt vēl pietiek? Ja ne — …". The delivered HTML and JSON were edited after the build.
- Proposed: put the final texts (after R2-1 to R2-4) into the build script.
- Why: re-running the script silently reverts the delivered files. Its old subject also nearly repeats reorder_3's "Varbūt ir kādas vajadzības?".

**Everything else in reorder_2 is OK:**
- Prices are normal: Pn_PRICE is unchanged, with no deadline and no struck-through price.
- The preheader is fine.
- The extra reply line "Ja šoreiz vajag ko citu — atbildi uz šo vēstuli, saliksim." is fine.
- The R heading, the voice and the grammar are fine.

---

## reorder_3

**R3-1 · SHOULD-FIX · uniqueness of the opening sentence (has-products AND no-products branch)**
- Current: "Šonedēļ pamanījām — no tevis tāds garāks klusums."
- Proposed: "Pamanījām — pēdējā laikā no tevis tāds garāks klusums."
- Why: reorder_2, one rung earlier, opens with "Šonedēļ padomājām par tevi — …". Two consecutive letters that both open "Šonedēļ pa…jām —" read as the same letter, and B3 gives each letter its own opening sentence. The replacement keeps Raivis' REM words, and C2 still holds: past tense plus the time word "pēdējā laikā".

**R3-2 · SHOULD-FIX · preheader (C13)**
- Current: "Tavas preces gaida kabinetā — kad vien vajadzēs."
- Proposed: "Pietiek atbildēt ar vienu rindiņu — pārējo sagatavosim mēs."
- Why: the first half is identical to reorder_2's preheader ("Tavas preces gaida kabinetā — pasūtīt var vienā solī."), so two consecutive inbox previews look the same. It also repeats this letter's own intro clause ("kad vajadzēs, tavas preces gaida kabinetā"). The replacement adds one new, true fact in the C11 register.

**R3-3 · NOTE · mechanics outside the template**
- `sender/utm.py` THEMES has reorder_1 and reorder_2 but no reorder_3. Planning reorder_3 therefore raises UnknownVariantTheme.
- Marketing has to name the slug (e.g. "papildinam-3") before this template can be planned.

**Everything else in reorder_3 is OK:**
- The subject uses Raivis' own words.
- Prices are normal.
- The R block is dropped, which D allows.
- The cabinet text is 179 verbatim, and the reply door is present.
- The language is natural and gender-neutral.

---

## active_xsell

**X1 · MUST-FIX · truthfulness (assignment gate)**
- Current: the letter still renders when R1_NAME is empty, and it keeps promising items that never appear:
  - subject "Ko citi ņem pie tavām precēm";
  - preheader "Ja kaut kas noder, nākamreiz tas var atnākt tajā pašā kastē.";
  - intro "Daudzi paņem arī preces, kas iet kopā ar tavējām.";
  - only "Salikām tās zemāk…" is guarded.
- Proposed:
  - Assign active_xsell only when R1_NAME is filled, and write that condition into the purpose and the assignment rule.
  - Belt and braces: move the intro's second sentence into the existing guard: `{% if contact.R1_NAME %}Daudzi paņem arī preces, kas iet kopā ar tavējām. Salikām tās zemāk — lai nav jāmeklē pa visu veikalu.{% endif %}`
- Why: the subject cannot be made conditional. Without the gate, every recipient who has no R rows gets a letter whose headline promise is missing.

**X2 · MUST-FIX unless MAIN confirms · truthfulness (cabinet box)**
- Current: "Tur ir tavas preces — jaunās var ielikt tajā pašā pasūtījumā. Ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna. Ja ērtāk, atbildi uz šo vēstuli — saliksim paši."
- Proposed: "Tur ir tavas preces. Ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna. Ja gribi klāt arī kaut ko jaunu, atbildi uz šo vēstuli — saliksim paši."
- Why: the only approved description of the cabinet is "Tur ir tieši tavas preces", meaning the customer's own goods. 179's approved line says R items can go in the same box, but it does not say the cabinet can add them. The contract says nothing about ordering a never-bought R item in the cabinet either: rule 9 has one URL and no per-product anchor.
  - If tab=preces lists only the customer's own goods, "jaunās var ielikt tajā pašā pasūtījumā" is false for every recipient.
  - Keep the original only if MAIN confirms the cabinet can do this. Even then, an R card's KABINETS_URL click must land where the item is.

**X3 · SHOULD-FIX · truthfulness/mechanics (the extra line above the P grid)**
- Current: `<tr><td …><p …><strong>Tavas ierastās preces</strong> <span …>— kad pienāks laiks, tās ir tepat.</span></p></td></tr>`. It is unconditional inside KABINETS_HAS_PRODUCTS.
- Proposed: wrap that `<tr>` in `{% if contact.P1_NAME %}…{% endif %}`. The text stays unchanged.
- Why: I rendered a contact with no P rows and some D rows. The letter says "tās ir tepat" above an empty grid, then lists the same own goods under "Šobrīd nav noliktavā".

**X4 · SHOULD-FIX · clarity (R heading)**
- Current: "Bieži tajā pašā kastē"
- Proposed: "Bieži vienā kastē ar tavējām"
- Why: the R block now comes before the reader's own goods, so "tajā pašā" (the same box) has nothing to point back to, and the heading reads as a fragment.

**X5 · SHOULD-FIX · mechanics (check.py does not catch this)**
- Current: build_active_xsell.py builds the intro without the R1 guard. It also asserts that the Liquid tags equal 179's, which the delivered file no longer satisfies.
- Proposed: put the guard and the X1/X3 changes into the build script.
- Why: re-running the script silently removes the guard.

**X6 · OK (verify) · truthfulness**
- These three claims hold only if customer_related_products is built from other customers' co-purchases:
  - "Vakar paskatījāmies, ko pie mums ņem tie, kas pērk to pašu, ko tu.";
  - "Daudzi paņem …";
  - the subject "Ko citi ņem …".
- That is the same basis as 179's approved heading "Ko parasti ņem kopā ar tavām precēm", so the wording is OK if the basis holds.
- "Vakar" is narrative, in the same way as 179's "Šodien".

**Everything else in active_xsell is OK:**
- The category widening works: the R block comes first, and D allows moving it.
- Prices are normal, with no discount and no deadline.
- All "tu" forms are gender-neutral.
- Grammar and case endings are correct ("tavējām", "savas preces", "pielikt klāt").
- There is no "!" outside the greeting, and all dashes are spaced.
