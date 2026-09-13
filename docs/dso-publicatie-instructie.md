# Publicatie-instructie — robots.txt en llms.txt op document-viewer.dso.kadaster.nl

Voor wie de webserver van de documentviewer beheert. Vier punten, in volgorde van
belang. Het eerste en het derde maken het verschil tussen een document dat werkt en
een document dat er alleen staat.

Waargenomen situatie op 2026-09-01, op alle drie de bestanden
(`/robots.txt`, `/llms.txt`, `/llms-voorbeeld-regels-op-adres.md`):

```
Content-Type: text/plain
Cache-Control: max-age=604800, must-revalidate
X-Content-Type-Options: nosniff
X-Robots-Tag: noindex, nofollow
```

---

## 1. Content-Type mist een charset — leestekens vallen om

`text/plain` zonder `charset` laat een browser terugvallen op een legacy-codering
(windows-1252 in West-Europa). `nosniff` verhindert dat hij zichzelf corrigeert. Elke
UTF-8-byte boven 127 wordt daardoor als twee losse tekens getekend: `é` wordt `Ã©`,
`—` wordt `â€"`, `ö` wordt `Ã¶`.

Dat raakt 129 tekens in `llms.txt`, 73 in het voorbeeldbestand en 10 in `robots.txt`.
De bestanden zelf zijn correcte UTF-8; er is niets mee aan de hand.

**Fix:**

```nginx
location ~ \.(txt|md)$ {
    charset utf-8;                    # voegt "; charset=utf-8" aan Content-Type toe
    charset_types text/plain;
}
```

**Tot dat gebeurd is** dragen de drie bestanden een UTF-8 BOM als noodgreep — die
laat een browser de standaardcodering overrulen. Zodra de charset in de header staat,
mag de BOM eruit: hij laat bij programmatische lezers een losse `U+FEFF` achter aan
het begin van de eerste regel. In `robots.txt` is dat nu onschadelijk omdat de eerste
regel een commentaarregel is, maar het blijft een pleister.

## 2. Cache-Control staat op een week

`max-age=604800` betekent dat een client het bestand zeven dagen als vers beschouwt.
`must-revalidate` verandert daar niets aan; dat werkt pas ná die week. Gevolg: elke
correctie — ook de charset-fix hierboven — bereikt bestaande lezers pas over zeven
dagen.

Dit is een levend document. Zolang het nog beweegt:

```nginx
location ~ ^/(robots\.txt|llms.*\.(txt|md))$ {
    add_header Cache-Control "max-age=3600, must-revalidate" always;
}
```

Een uur is ruim genoeg om de server te ontlasten en kort genoeg om een correctie
dezelfde dag te laten landen. Staat het document stil, dan kan het weer omhoog.

## 3. X-Robots-Tag zet het document op noindex, nofollow

Dit is het punt met de grootste impact en waarschijnlijk onbedoeld: de header geldt
kennelijk host-breed — logisch voor een viewer-applicatie — maar treft nu ook de
documenten die juist gevonden móéten worden.

Twee concrete gevolgen:

- Crawlers en LLM-indexen die zich aan de header houden, nemen `llms.txt` **niet** op.
  Precies de partijen voor wie het geschreven is.
- `nofollow` betekent dat de verwijzing naar `/llms-voorbeeld-regels-op-adres.md`
  niet wordt gevolgd. Het uitgewerkte voorbeeld blijft dan onbereikbaar via de gids.

Er zit ook een directe tegenspraak in: `robots.txt` bevat een `Sitemap:`-regel die
naar `llms.txt` wijst, terwijl de server diezelfde crawler vertelt dat bestand te
negeren.

**Fix — uitzondering op de host-brede regel:**

```nginx
location ~ ^/(robots\.txt|llms.*\.(txt|md))$ {
    add_header X-Robots-Tag "index, follow" always;
}
```

Agents die het bestand rechtstreeks ophalen worden hier niet door gehinderd; het gaat
puur om vindbaarheid. Wie de host-brede `noindex` bewust wil houden, moet zich
realiseren dat de gids dan alleen werkt voor wie het adres al kent.

## 4. Twee backends met verschillende bestandstijden

Twee opeenvolgende requests op dezelfde URL gaven verschillende waarden:

```
Last-Modified: Tue, 01 Sep 2026 11:38:16 GMT   Etag: "6a96b928-279c"
Last-Modified: Tue, 01 Sep 2026 11:37:58 GMT   Etag: "6a96b916-279c"
```

Achttien seconden verschil, gelijke inhoud. Dat wijst op meerdere nodes achter een
load balancer die het bestand elk apart hebben gekregen. Gevolg: de ETag verschilt per
node, waardoor conditionele requests (`If-None-Match`) afwisselend wél en niet
matchen. Clients halen het bestand dan vaker op dan nodig.

Geen storing, wel onnodig verkeer. Bij een volgende uitrol is het de moeite om de
bestanden met gelijke mtime te plaatsen, of de ETag op de inhoud te baseren in plaats
van op tijd-plus-grootte.

---

## Verificatie na de wijziging

```bash
curl -sI https://document-viewer.dso.kadaster.nl/llms.txt \
  | grep -iE 'content-type|cache-control|x-robots-tag'
```

Verwacht:

```
Content-Type: text/plain; charset=utf-8
Cache-Control: max-age=3600, must-revalidate
X-Robots-Tag: index, follow
```

En in de browser: `coördinaat`, `één` en `—` moeten leesbaar zijn. Zijn ze dat, dan
kan de BOM uit de drie bestanden.
