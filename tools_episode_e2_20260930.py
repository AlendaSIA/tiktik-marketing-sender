"""CADENCE v1 K3/K7 (Raivis 2026-09-30 19:11): E2 letter of each price episode, derived from that rung's E1 file.
Every replacement is count-asserted. E2 = same price, same OFFER_VALID_UNTIL as E1, one week later:
'the price holds 7 more days, then the standard shop price'. No progression words (L4)."""
import io, os
OVU = "{{ contact.OFFER_VALID_UNTIL }}"
IFD, END = "{% if contact.OFFER_VALID_UNTIL %}", "{% endif %}"
L4 = " Saviem esošajiem klientiem šobrīd preces ar atzīmi „TAVA CENA“ varam piedāvāt lētāk nekā veikalā."

def rep(t, old, new, n=1):
    c = t.count(old); assert c == n, (old[:70], c); return t.replace(old, new)

def build(src, dst, pairs):
    t = io.open(src, encoding="utf-8").read()
    for old, new in pairs: t = rep(t, old, new)
    io.open(dst, "w", encoding="utf-8").write(t)

# rung 1 E2 <- winback_1 (180)
build("templates/winback_1.html", "templates/winback_1_e2.html", [
 ("<title>Sen neesam redzējušies</title>", f"<title>Tava cena paliek spēkā līdz {OVU}</title>"),
 (f"{IFD}Tavs personīgais piedāvājums — spēkā līdz {OVU}.{END}",
  f"{IFD}Vēl 7 dienas — tavām ierastajām precēm, kamēr prece ir noliktavā.{END}"),
 (f"{IFD}Tavs personīgais piedāvājums{{% else %}}Sen neesam redzējušies{END}</h1>",
  f"{IFD}Tava cena — vēl 7 dienas{{% else %}}Tavas ierastās preces{END}</h1>"),
 ("Sen neesam redzējušies. Paskatījāmies — tavas preces joprojām ir plauktā, tās pašas, ko tu mēdz ņemt. Lai tev nav jāmeklē no jauna, tās visas ir šeit." + IFD + L4 + END,
  "Pagājušajā nedēļā tev rakstījām par tavām ierastajām precēm — tās visas ir šeit." + IFD + L4 + " Šo cenu turam vēl 7 dienas, pēc tam — parastā veikala cena." + END),
 ("Sen neesam redzējušies. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.",
  "Pagājušajā nedēļā tev rakstījām par tavām ierastajām precēm. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim."),
 (f"Tava cena ir spēkā līdz {OVU} — kamēr prece ir noliktavā.", f"Tava cena ir spēkā līdz {OVU} — pēc tam parastā veikala cena."),
])
# rung 2 E2 <- winback_2 (232)
build("templates/winback_2.html", "templates/winback_2_e2.html", [
 ("<title>Tikai tev: 7 dienas — tava cena tavām ierastajām precēm</title>", f"<title>Līdz {OVU} — tavas cenas vēl ir spēkā</title>"),
 (f"{IFD}Tā ir spēkā līdz {OVU}.{END}", f"{IFD}Pēdējā nedēļa tavām cenām — kamēr prece ir noliktavā.{END}"),
 ("<h1 style=\"margin:0;font-size:19px;font-weight:800;color:#17578c;line-height:1.3;\">Tikai tev: 7 dienas — tava cena tavām ierastajām precēm</h1>",
  f"<h1 style=\"margin:0;font-size:19px;font-weight:800;color:#17578c;line-height:1.3;\">{IFD}Tavas cenas — vēl nedēļu{{% else %}}Tavas ierastās preces{END}</h1>"),
 ("{% if contact.P1_NAME %}{% if contact.P1_FRESH %}Tikko iepirkām jaunu partiju ({{ contact.P1_NAME }}).{% else %}Šonedēļ pārskatījām tavu ierasto preču cenas.{% endif %}{% else %}Šonedēļ pārskatījām tavu ierasto preču cenas.{% endif %}" + IFD + L4 + END,
  "Tavu cenu ierastajām precēm saglabājam vēl nedēļu." + IFD + L4 + " Ja krājumi sāk iet uz beigām — tagad ir īstais brīdis papildināt." + END),
 ("Šonedēļ pārskatījām preču cenas. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.",
  "Ja krājumi sāk iet uz beigām, atbildi uz šo vēstuli — sagatavosim."),
 (f"Šī cena tev ir spēkā līdz {OVU} — kamēr prece ir noliktavā.", f"Šī cena tev ir spēkā līdz {OVU} — pēc tam parastā veikala cena."),
])
# rung 3 E2 <- winback_3 (233)
build("templates/winback_3.html", "templates/winback_3_e2.html", [
 (f"<title>Tikai tev: tava cena ierastajām precēm līdz {OVU}</title>", f"<title>Tava cena — pēdējās 7 dienas, līdz {OVU}</title>"),
 (f"{IFD}Tava cena spēkā līdz {OVU} — kamēr prece ir noliktavā.{END}", f"{IFD}Pēc {OVU} — parastā veikala cena.{END}"),
 (">Tavs personīgais piedāvājums</h1>", f">{IFD}Pēdējās 7 dienas tavai cenai{{% else %}}Tavas ierastās preces{END}</h1>"),
 ("{% if contact.P1_NAME %}{% if contact.P1_FRESH %}Šorīt skaitījām kastes — jaunā partija ({{ contact.P1_NAME }}) vēl nav izpārdota.{% else %}Šorīt skaitījām kastes — tavas preces vēl ir noliktavā.{% endif %}{% else %}Šorīt skaitījām kastes — tavas preces vēl ir noliktavā.{% endif %}" + IFD + L4 + END,
  "Tavas ierastās preces ir šeit." + IFD + L4 + " Tava cena ir spēkā vēl 7 dienas, pēc tam preces būs par parasto veikala cenu." + END),
 ("Šorīt skaitījām kastes un iedomājāmies par tevi. Ja kaut ko vajag, atbildi uz šo vēstuli — sagatavosim.",
  "Ja vajag ko no tavām ierastajām precēm, atbildi uz šo vēstuli — sagatavosim."),
 (f"Tava cena ir spēkā līdz {OVU} — kamēr prece ir noliktavā.", f"Tava cena ir spēkā līdz {OVU} — pēc tam parastā veikala cena."),
])
# rung 2 E1 (232): the window is now OVU = first send + 13 (K3), so '7 dienas' in the head is false
t = io.open("templates/winback_2.html", encoding="utf-8").read()
t = rep(t, "<title>Tikai tev: 7 dienas — tava cena tavām ierastajām precēm</title>", "<title>Tikai tev: tava cena tavām ierastajām precēm</title>")
t = rep(t, ">Tikai tev: 7 dienas — tava cena tavām ierastajām precēm</h1>", ">Tikai tev: tava cena tavām ierastajām precēm</h1>")
io.open("templates/winback_2.html", "w", encoding="utf-8").write(t)
print("ok")
