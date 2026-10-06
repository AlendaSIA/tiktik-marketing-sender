import json,subprocess,base64,html,re
q='''select c.name,c.url,c.image,c.std_price,c.eff_price,c.pop_buyers,c.handle,c.cat_leaf_slug leaf,regexp_extract(c.cat_url, r"/veikals/([^/]+)/") top from business_marts.product_catalog c join (select distinct handle from business_marts.product_maker where promo_group="ZARYS") m using(handle) where on_sale and url is not null and image is not null and stock>0 and eff_price<std_price order by pop_buyers desc, name'''
rows=json.loads(subprocess.check_output(["bq","query","--use_legacy_sql=false","--format=json","--max_rows=500",q],stderr=subprocess.DEVNULL))
eur=lambda v: ("%.2f"%float(v)).replace(".",",")+" €"
U="?utm_source=brevo&amp;utm_medium=email&amp;utm_campaign=__UTM_WEEK__-akcija"
def card(r):
    n=html.escape(r["name"],quote=True).replace("%","％"); u=html.escape(r["url"],quote=True)+U; im=html.escape(r["image"],quote=True)
    return ('<td width="25%" valign="top" style="padding:6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e3e9e6;border-radius:8px;"><tr><td align="center" style="padding:10px 8px 4px;"><a href="'+u+'"><img src="'+im+'" alt="'+n+'" width="240" style="display:block;width:100%;max-width:240px;height:auto;border:0;border-radius:6px;"></a></td></tr><tr><td align="center" style="padding:6px 8px 2px;"><span style="font-size:12px;line-height:1.35;color:#20302b;font-weight:bold;">'+n+'</span></td></tr><tr><td align="center" style="padding:2px 8px 6px;"><s style="font-size:12px;color:#9aa5a0;">'+eur(r["std_price"])+'</s><br><span style="font-size:18px;color:#d7263d;font-weight:bold;">'+eur(r["eff_price"])+'</span></td></tr><tr><td align="center" style="padding:2px 8px 14px;"><a href="'+u+'" style="display:inline-block;background:#068a65;color:#ffffff;font-size:12px;font-weight:bold;text-decoration:none;padding:8px 18px;border-radius:6px;">Pirkt</a></td></tr></table></td>')
OWN=json.load(open("/tmp/own.json")); own=[r for h in OWN for r in rows if r["handle"]==h]
rows=[r for r in rows if r not in own]   # a good shown in "already bought" is not repeated in the blocks below
thin=[r for r in rows if r["leaf"] in ("nitrila-cimdi-bez-pudera","lateksa-cimdi","vinila-cimdi")][:12]
thick=[r for r in rows if r["leaf"]=="biezie-nitrila-cimdi-smagam-darbam" and "Gripzzly" in r["name"]][:4]
tp=[r for r in rows if r["leaf"]=="sporta-un-kineziologiskie-teipi"]
kit=[r for r in tp if r["name"].startswith("Komplekts")][:1]
t55=[r for r in tp if re.search(r"(?<![,\d])5\s*cm\.?\s*x\s*5\s*m",r["name"],re.I) and r not in kit][:3]
used=thin+thick+kit+t55
pool=[r for r in rows if r not in used and r["top"]!="yellow-sport-teipi-yoga-pretestibas-gumijas-fizioterapija" and r["top"]!="cimdi"]
# DIVERSITY RULE (Raivis 2026-10-06 15:34): by popularity, but never two goods of one kind (same first word of the
# name) and at most 2 from one shop category - so the four are four different things whatever is on sale that week.
other=[]; kinds=set(); percat={}
for r in pool:
    k=re.sub(r"[^a-zāčēģīķļņšūž]","",r["name"].lower().split()[0]); c=r["leaf"]
    if k in kinds or percat.get(c,0)>=2: continue
    other.append(r); kinds.add(k); percat[c]=percat.get(c,0)+1
    if len(other)==4: break
secs=([("Tavas jau pirktās ZARYS preces",own)] if own else [])+[("Plānie cimdi",thin),("Biezie cimdi",thick),("Citas ZARYS preces",other),("Teipi",kit+t55)]
out=""; allr=[]
for title,rs in secs:
    allr+=rs
    out+='<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:14px 6px 2px;"><p style="margin:0;font-size:17px;font-weight:bold;color:#20302b;border-bottom:2px solid #d7263d;padding-bottom:6px;">'+title+'</p></td></tr></table>'
    for i in range(0,len(rs),4):
        ch=rs[i:i+4]; out+='<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'+"".join(card(r) for r in ch)+'<td width="25%"></td>'*(4-len(ch))+'</tr></table>'
    print(title,len(rs),[(r["name"][:34],r["pop_buyers"]) for r in rs])
old=json.loads(base64.b64decode(json.load(open("/tmp/f.json"))[0][2]).decode())
old["⟦NEDĒĻAS PREČU BLOKI⟧"]=out; old["⟦CENA⟧"]=eur(min(float(r["eff_price"]) for r in allr))
print(old["⟦CENA⟧"],len(out),len(allr))
# PREVIEW (Raivis 15:50 + 16:02): subject with the contact's TOP good (hand-written here; needs a contract field),
# H1 "ZARYS produktu nedēļa", description without "ražo Polijā" (ZARYS is Polish but produces mostly in China).
old={"⟦PRECE⟧ no ⟦CENA⟧ — tikai šonedēļ":"{{ contact.UZRUNA | default : 'Sveiki' }}, Koka špāteles un citas ZARYS preces šonedēļ par īpaši labām cenām",
     '⟦PRECE⟧ no <span style="color:#d7263d;">⟦CENA⟧</span>':"ZARYS produktu nedēļa",**old}
old["⟦KĀPĒC ŠIS RAŽOTĀJS⟧"]="cimdus, plāksterus, teipus un citas ikdienā vajadzīgas preces"
# Raivis 2026-10-06 16:39: the akcija week runs TUESDAY to MONDAY. 12.10.2026 is a Monday (the 01.10 fill said Sunday).
old["⟦LĪDZ⟧"]="pirmdienai, 12. oktobrim"
open("/tmp/fill.b64","w").write(base64.b64encode(json.dumps(old,ensure_ascii=False).encode()).decode())
