# Tarjousbotti

Seuraa omaa tuotelistaa ympäri vuoden ja lähettää Discord-hälytyksen, kun
- hinta alittaa tuotteelle asetetun **tavoitehinnan**
- hinta putoaa **≥ 10 %** edellisestä tarkistuksesta
- hinta on **alin**, jonka botti on nähnyt
- tuote **palaa varastoon**

## Suoja feikkialennuksia vastaan

Jokainen hälytys tarkistetaan kolmella tavalla:

1. **Botin oma historia**: jos kauppa näyttää yliviivatun normaalihinnan, mutta sama hinta
   on ollut voimassa jo viimeisen 30 päivän aikana → ⚠️
2. **Hintaoppaan historia** (3 kk, kaikki kaupat): jos tuote on ollut viimeisen 30 päivän aikana
   halvempi → ⚠️. Tämä toimii myös, jos tuote lisätään seurantaan vasta Black Weekillä.
   Hintaoppaan jäsenhinnat jätetään huomiotta. Huom: Hintaopas näyttää tuotteen halvimman
   version (väri/koko) hinnan.
3. **Claude Haiku** arvioi hälytyksen: onko ale aito, ja kannattaako ostaa nyt vai odottaa.

Lisäksi `--katsaus` lähettää joka päivä Clauden katsauksen kaikista tuotteista: aidosti hyvät
hinnat, epäilyttävät tapaukset ja erityisesti **ennen Black Weekiä tehdyt hinnankorotukset**.

## Tuetut kaupat

| Kauppa | Hinta luetaan |
|---|---|
| Happy Angler | Shopifyn `/products/…js` (variantti `?variant=` URLista) |
| Ruthless Fishing | WooCommerce Store API |
| Eräkellari | Nethitin tuote-API (sis. normaalihinnan) |
| Tokmanni | sivun hintatiedot (sis. normaalihinnan) |
| Kärkkäinen | sivun JSON-LD |
| Hintaopas | JSON-LD (kaikkien kauppojen halvin hinta) + hintahistoria |

Muut kaupat toimivat usein samoilla tavoilla. Kokeile: `python tarjousbotti.py --test URL`.

## Asennus Pille

```bash
scp tarjousbotti.py tuotteet.yaml tedy@<pi>:~/tarjousbotti/
ssh tedy@<pi>
cd ~/tarjousbotti
~/osakebot-env/bin/pip install requests pyyaml anthropic
```

Laita Discord-webhook tiedostoon `tuotteet.yaml` ja lisää tuotteet (ohje tiedostossa).
Claude käyttää samaa `ANTHROPIC_API_KEY`-avainta kuin osakeagentti.

Testaa:

```bash
~/osakebot-env/bin/python tarjousbotti.py --test "https://hintaopas.fi/product.php?p=..."
~/osakebot-env/bin/python tarjousbotti.py --force --dry-run   # ei lähetä Discordiin
```

`--test` Hintaopas-linkillä tulostaa myös 30 ja 90 päivän alimman hinnan. Jos se toimii,
Hintaopas ei estä Pin hakuja.

## Cron (`crontab -e`)

```
ANTHROPIC_API_KEY=sk-ant-...
*/15 * * * * cd ~/tarjousbotti && ~/osakebot-env/bin/python tarjousbotti.py >> tarjousbotti.log 2>&1
0 18 * * *   cd ~/tarjousbotti && ~/osakebot-env/bin/python tarjousbotti.py --katsaus >> tarjousbotti.log 2>&1
```

(Jos avain on jo muualla, esim. `~/.bashrc` tai `.env`, cron ei lue niitä automaattisesti –
siksi `ANTHROPIC_API_KEY` crontabin alkuun.)

Cron käynnistää botin 15 minuutin välein. Botti tarkistaa hinnat itse 3 tunnin välein ja
Black Weekin aikana (9.11.–1.12.) 15 minuutin välein. Katsaus lähtee joka päivä klo 18.
Välit ja päivät voi muuttaa `tuotteet.yaml`:ssa.

## Tuotteiden lisääminen myöhemmin

Muokkaa `~/tarjousbotti/tuotteet.yaml` – muutos on voimassa seuraavalla ajokerralla,
mitään ei tarvitse käynnistää uudelleen. Tarkista uusi linkki komennolla `--test`.

## Muut komennot

- `--list` näyttää viimeisimmät hinnat terminaalissa
- `--summary` lähettää numerokoonnin Discordiin (ilman Claudea)
- `--force` tekee tarkistuksen heti, vaikka ajoväli ei olisi vielä täynnä

Jos hintaa ei saada luettua, botti ilmoittaa siitä Discordiin enintään kerran vuorokaudessa.
Jos Claude ei vastaa, hälytykset lähtevät silti ja katsauksen tilalle tulee numerokoonti.
Hintahistoria tallentuu tiedostoon `hinnat.db` (SQLite).
