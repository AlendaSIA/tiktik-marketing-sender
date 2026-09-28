"""One-off restyle, MAIN COMMAND 2 (2026-09-28): the 9 lifecycle templates take the frame of our best personal-offer
campaign 126 ("tavs personīgais piedāvājums", 4.67 % click) and the product cards of the weekly campaign 222 (Mercator
nedēļa); 236 akcija_weekly is rebuilt as the weekly format of 222. Copy is kept; only the pieces listed here change.
Every replacement asserts its count, so a template that drifted fails loudly. Run once from the repo root."""
import re

T = "templates/"
LIFECYCLE = ["welcome_1", "reorder_1", "reorder_2", "reorder_3", "winback_1", "winback_2", "winback_3",
             "lost_quarterly", "active_xsell"]
PRICE = {"winback_1", "winback_2", "winback_3", "lost_quarterly"}
GREETING = "{{ contact.GREETING | default : 'Sveiki!' }}"
FONT = "font-family:'Manrope','Segoe UI',Arial,sans-serif;"

SUBJECTS = {  # (subject/title, h1) - None keeps the file's own
    "winback_2": ("Tikai tev: 7 dienas — tava cena tavām ierastajām precēm", None),
    "winback_3": ("Tikai tev: tava cena ierastajām precēm līdz {{ contact.OFFER_VALID_UNTIL }}",
                  "Tavs personīgais piedāvājums"),
}
H1_SWITCH = {  # h1 follows the offer: 126's headline only when a personal price is in the letter
    "winback_1": "{% if contact.OFFER_VALID_UNTIL %}Tavs personīgais piedāvājums{% else %}Sen neesam redzējušies{% endif %}",
    "lost_quarterly": "{% if contact.OFFER_VALID_UNTIL %}Tavs personīgais piedāvājums{% else %}Atceramies, ko tu mēdzi ņemt{% endif %}",
}
PRE_180 = ('<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{% if contact.OFFER_VALID_UNTIL %}'
           'Tavs personīgais piedāvājums — spēkā līdz {{ contact.OFFER_VALID_UNTIL }}.{% endif %}</div>')


def sub1(t, old, new, n=1):
    c = t.count(old)
    assert c == n, (old[:70], c, n)
    return t.replace(old, new)


def lifecycle(name):
    p = T + name + ".html"
    t = open(p, encoding="utf-8").read()
    t = sub1(t, '<body style="margin:0;padding:0;background:#f4f5f7;font-family:Arial,Helvetica,sans-serif;color:#222;">',
             '<body style="margin:0;padding:0;background:#f4f6fb;">')
    t = sub1(t, '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">\n'
                '<tr><td align="center" style="padding:18px 12px;">',
             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;">\n'
             '<tr><td align="center" style="padding:24px 12px;">')
    t = sub1(t, 'style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;">',
             'style="max-width:600px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;' + FONT + 'color:#23303a;">')
    t = sub1(t, '<tr><td align="center" style="padding:22px 24px 6px;">', '<tr><td align="center" style="padding:26px 34px 6px;">')
    # greeting first (126), then the headline in 126's blue, then the body
    m = re.search(r'<tr><td align="center" style="padding:14px 28px 0;">\n<h1 style="margin:0;font-size:22px;line-height:1.3;color:#111;">(.*?)</h1>\n</td></tr>\n\n', t)
    assert m, name
    h1 = m.group(1)
    t = t.replace(m.group(0), "")
    if name in SUBJECTS:
        subj, newh1 = SUBJECTS[name]
        t = sub1(t, "<title>%s</title>" % h1, "<title>%s</title>" % subj)
        h1 = newh1 or subj
    if name in H1_SWITCH:
        h1 = H1_SWITCH[name]
    t = sub1(t, '<tr><td style="padding:14px 28px 4px;font-size:15px;line-height:1.6;">\n<p style="margin:0 0 10px;">' + GREETING + '</p>\n',
             '<tr><td style="padding:18px 34px 0;">\n<div style="font-size:22px;font-weight:800;color:#23303a;">' + GREETING + '</div>\n</td></tr>\n'
             '<tr><td style="padding:14px 34px 0;">\n<h1 style="margin:0;font-size:19px;font-weight:800;color:#17578c;line-height:1.3;">' + h1 + '</h1>\n</td></tr>\n'
             '<tr><td style="padding:12px 34px 4px;font-size:15px;line-height:1.65;color:#23303a;">\n')
    if name == "winback_1":
        t = re.sub(r'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">[^<]*</div>', lambda _: PRE_180, t, count=1)
    # product cards: 222's card border and name link
    n = t.count('border:1px solid #e9e9ec;border-radius:8px;')
    t = t.replace('border:1px solid #e9e9ec;border-radius:8px;', 'border:1px solid #e4e9f2;border-radius:10px;')
    t = t.replace('style="color:#1f6fb2;text-decoration:underline;font-weight:bold;"', 'style="color:#26346e;text-decoration:none;font-weight:700;"')
    assert n > 0, name
    # the date line (L4) in 126's offer box
    if name in PRICE:
        m = re.search(r'\{% if contact.OFFER_VALID_UNTIL %\}<tr><td style="padding:14px 28px 0;font-size:15px;line-height:1.6;">\n<p style="margin:0 0 4px;">([^<]*)</p>\n</td></tr>\{% endif %\}', t)
        assert m, name
        t = t.replace(m.group(0),
                      '{% if contact.OFFER_VALID_UNTIL %}<tr><td style="padding:16px 34px 0;">\n'
                      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef4fb;border:1px solid #d3dcee;border-radius:12px;">\n'
                      '<tr><td style="padding:14px 18px;text-align:center;font-size:16px;font-weight:800;color:#17578c;line-height:1.4;">' + m.group(1) + '</td></tr>\n'
                      '</table>\n</td></tr>{% endif %}')
    # the cabinet box: 126's box colours and its button
    t = sub1(t, 'background:#f2f7f4;border-radius:10px;', 'background:#eef4fb;border:1px solid #d3dcee;border-radius:12px;')
    btn_old = 'style="display:inline-block;background:#12603f;color:#fff;text-decoration:none;font-weight:bold;padding:14px 28px;border-radius:6px;font-size:16px;">Atvērt savu kabinetu &rarr;</a>'
    label = "Apskatīt manas cenas un pasūtīt &rarr;" if name in PRICE else "Atvērt savu kabinetu &rarr;"
    t = sub1(t, btn_old, 'style="display:inline-block;background:#1f6fb2;color:#ffffff;text-decoration:none;font-weight:800;padding:16px 40px;border-radius:11px;font-size:16px;">' + label + '</a>')
    # sign-off (126) + footer (126)
    m = re.search(r'<tr><td style="padding:16px 28px 22px;border-top:1px solid #eee;font-size:11px;line-height:1.5;color:#999;">.*?</td></tr>', t, re.S)
    assert m, name
    extra = ""  # 126 adds "Priecāsimies atkal tevi redzēt!" - "atkal" is an L4 word, left out
    t = t.replace(m.group(0),
                  '<tr><td style="padding:20px 34px 0;">\n'
                  '<p style="font-size:15px;line-height:1.6;color:#23303a;margin:0;">Ja rodas jautājumi — vienkārši atbildi uz šo e-pastu.' + extra + '</p>\n'
                  '<p style="font-size:15px;line-height:1.6;color:#23303a;margin:12px 0 0;">tiktik.lv komanda</p>\n'
                  '</td></tr>\n\n'
                  '<tr><td style="padding:22px 34px 28px;">\n'
                  '<div style="border-top:1px solid #e6eaf2;padding-top:16px;font-size:12px;line-height:1.6;color:#9aa4b2;text-align:center;">\n'
                  'SIA Alenda &middot; tiktik.lv &middot; info@tiktik.lv<br>\n'
                  'Šo e-pastu saņem, jo esi iepircies tiktik.lv veikalā.<br>\n'
                  '<a href="{{ unsubscribe }}" style="color:#9aa4b2;">Atteikties no šiem e-pastiem</a>\n'
                  '</div>\n</td></tr>')
    open(p, "w", encoding="utf-8").write(t)


FEATURED = "https://www.tiktik.lv/veikals/params/category/featured/?utm_source=brevo&amp;utm_medium=email&amp;utm_campaign=__UTM_WEEK__-akcija"
D_BLOCK = re.compile(r"\{% if contact.D1_NAME %\}.*?</td></tr>\n\{% endif %\}", re.S)


def weekly():
    """236 = the weekly campaign of 222 (Mercator nedēļa), with ⟦…⟧ where the week's content goes."""
    p = T + "akcija_weekly.html"
    old = open(p, encoding="utf-8").read()
    d = D_BLOCK.search(old)
    assert d
    html = f'''<!DOCTYPE html>
<html lang="lv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>⟦RAŽOTĀJS⟧ nedēļa: ⟦PRECE⟧ no ⟦CENA⟧</title>
</head>
<body style="margin:0;padding:0;background:#f4f6fb;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">⟦RAŽOTĀJS⟧ visai līnijai nolaistas cenas — kamēr ir noliktavā.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6fb;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1f2a28;">

<tr><td align="center" style="padding:22px 28px 6px 28px;">
<img src="https://site-944391.mozfiles.com/files/944391/logobox/94817333/logo-default-332f03c83834e77366e4cfe90a1ef087.png" alt="tiktik.lv" width="150" style="display:block;border:0;width:150px;max-width:150px;">
</td></tr>

<tr><td style="padding:12px 28px 0 28px;">
<h1 style="margin:0 0 10px 0;font-size:23px;line-height:1.3;font-weight:800;">⟦RAŽOTĀJS⟧ nedēļa</h1>
<p style="margin:0 0 16px 0;font-size:15px;line-height:1.6;color:#3d474f;">{GREETING} Šonedēļ nolaidām cenas visai ⟦RAŽOTĀJS⟧ līnijai — ⟦KĀPĒC ŠIS RAŽOTĀJS⟧. ⟦PRECE⟧ sākas no ⟦CENA⟧.</p>
</td></tr>

<tr><td style="padding:0 22px 0 22px;">⟦NEDĒĻAS PREČU BLOKI⟧</td></tr>

{{% if contact.KABINETS_HAS_PRODUCTS %}}
<tr><td style="padding:14px 28px 0 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px dashed #c9d4e6;border-radius:10px;background:#f8fafd;">
<tr><td style="padding:14px 16px;">
<div style="font-size:14px;font-weight:700;line-height:1.4;">Preces, ko tu mēdz ņemt — tavā kabinetā.</div>
<div style="margin-top:6px;font-size:13px;line-height:1.5;color:#67727d;">Ja vajag arī tās, var likt tajā pašā kastē. Ieraksti daudzumu un nosūti — adrese un rekvizīti nav jāievada no jauna. <a href="{{{{ contact.KABINETS_URL }}}}" style="color:#26346e;font-weight:700;">Atvērt savu kabinetu &rarr;</a></div>
</td></tr>
</table>
</td></tr>
{{% endif %}}

<tr><td align="center" style="padding:20px 28px 6px 28px;">
<a href="{FEATURED}" style="display:inline-block;background:#12603f;color:#ffffff;font-size:17px;font-weight:800;text-decoration:none;padding:16px 36px;border-radius:10px;">Skatīt visas nedēļas akcijas</a>
</td></tr>

<tr><td style="padding:14px 28px 0 28px;">
<p style="margin:0 0 6px 0;font-size:13px;line-height:1.6;color:#67727d;">No 59 € piegāde uz Venipak pakomātu — bez maksas.</p>
<p style="margin:0 0 18px 0;font-size:13px;line-height:1.6;color:#67727d;">Cenas spēkā, kamēr prece ir noliktavā. Ja vajag izmēru, kura sarakstā nav — atraksti uz šo vēstuli.</p>
</td></tr>

{d.group(0)}

<tr><td style="padding:0 28px 26px 28px;border-top:1px solid #edf1f7;">
<p style="margin:16px 0 0 0;font-size:11px;line-height:1.6;color:#98a2ad;">
Alenda SIA &middot; tiktik.lv &middot; info@tiktik.lv<br>
Šo vēstuli saņem, jo esi iepircies tiktik.lv. <a href="{{{{ unsubscribe }}}}" style="color:#98a2ad;text-decoration:underline;">Atteikties no vēstulēm</a>
</p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>
'''
    open(p, "w", encoding="utf-8").write(html)


if __name__ == "__main__":
    for n in LIFECYCLE:
        lifecycle(n)
    weekly()
    print("restyled", len(LIFECYCLE) + 1)
