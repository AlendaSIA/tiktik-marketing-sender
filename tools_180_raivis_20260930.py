"""Raivis 2026-09-30 19:48/19:50: 180 (CADENCE v1 rung 1 E1) - no 'Sen neesam redzējušies' anywhere; subject with the
customer's name + 'tikai tev: labāka cena tavām ierastajām precēm'; preheader shows a concrete price; reason sentence
kept truthful (L4 wording) pending Raivis' answer on 'iepirkām izdevīgāk'. Count-asserted replacements."""
import io
OVU = "{{ contact.OFFER_VALID_UNTIL }}"; IFD, END = "{% if contact.OFFER_VALID_UNTIL %}", "{% endif %}"
SUBJ = "{{ contact.UZRUNA | default : 'Sveiki' }}, tikai tev: labāka cena tavām ierastajām precēm"
L4 = " Saviem esošajiem klientiem šobrīd preces ar atzīmi „TAVA CENA“ varam piedāvāt lētāk nekā veikalā."
def rep(t, o, n, c=1):
    assert t.count(o) == c, (o[:60], t.count(o)); return t.replace(o, n)
p = "templates/winback_1.html"; t = io.open(p, encoding="utf-8").read()
t = rep(t, "<title>Sen neesam redzējušies</title>", "<title>" + SUBJ + "</title>")
t = rep(t, f"{IFD}Tavs personīgais piedāvājums — spēkā līdz {OVU}.{END}",
        IFD + "{% if contact.P1_REF_PRICE %}{{ contact.P1_NAME }}: {{ contact.P1_PRICE }} (veikalā {{ contact.P1_REF_PRICE }}) — līdz "
        + OVU + ".{% else %}Tavas cenas spēkā līdz " + OVU + ".{% endif %}" + END)
t = rep(t, f"{IFD}Tavs personīgais piedāvājums{{% else %}}Sen neesam redzējušies{END}</h1>",
        f"{IFD}Tikai tev: labāka cena tavām ierastajām precēm{{% else %}}Tavas ierastās preces{END}</h1>")
t = rep(t, "Sen neesam redzējušies. Paskatījāmies — tavas preces joprojām ir plauktā, tās pašas, ko tu mēdz ņemt. Lai tev nav jāmeklē no jauna, tās visas ir šeit." + IFD + L4 + END,
        IFD + L4[1:] + " Tavas cenas ir zemāk — spēkā līdz " + OVU + ".{% else %}Tavas ierastās preces ir šeit — lai nav jāmeklē no jauna." + END)
t = rep(t, "Sen neesam redzējušies. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.",
        "Ja vajag ko no tavām ierastajām precēm, atbildi uz šo vēstuli — sagatavosim.")
assert "Sen neesam" not in t
io.open(p, "w", encoding="utf-8").write(t)
print("ok")
