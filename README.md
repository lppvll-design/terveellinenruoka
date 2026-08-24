# paasto-ja-ruokaa

## Sivutarkistin (`tarkista.py`)

Komentorivityökalu, joka tarkastaa verkkosivuja: toimiiko sivu, kuinka nopeasti
se vastaa, onko TLS-varmenne voimassa, onko HTML-rakenne kunnossa ja johtavatko
sivun linkit jonnekin. Toimii sekä julkaistuille osoitteille että paikallisille
HTML-tiedostoille (esim. `index.html` ennen julkaisua). Ei vaadi asennuksia —
pelkkä Python 3.9+.

```bash
python3 tarkista.py index.html               # paikallinen tiedosto
python3 tarkista.py https://esimerkki.fi     # julkaistu sivu
python3 tarkista.py index.html --linkit      # tarkista myös kaikki linkit
python3 tarkista.py https://esimerkki.fi --json > raportti.json
```

### Mitä ohjelma tarkistaa

| Alue | Tarkistukset |
| --- | --- |
| Yhteys | HTTP-tilakoodi, uudelleenohjausketju, vasteaika, sivun koko |
| Tietoturva | HTTPS käytössä, varmenteen voimassaolo ja myöntäjä, HSTS- ja CSP-otsakkeet |
| Metatiedot | `<title>`, `<html lang>`, `<meta charset>`, viewport, meta description |
| Rakenne | `<h1>`:n olemassaolo ja määrä, otsikkotasojen hypyt |
| Saavutettavuus | kuvien `alt`-tekstit, nimeämättömät lomakekentät |
| Linkit (`--linkit`) | rikkinäiset linkit ja resurssit, uudelleenohjaukset, puuttuvat `#ankkurit` |

### Valitsimet

- `-l, --linkit` — tarkista sivun linkit, kuvat, tyylitiedostot ja skriptit
- `--ohita-ulkoiset` — rajaa linkkitarkistus saman sivuston osoitteisiin
- `-q, --hiljainen` — näytä vain varoitukset ja virheet
- `--json` — koneluettava tuloste esim. automaatiota varten
- `-t, --aikakatkaisu SEK` — pyynnön aikakatkaisu (oletus 10 s)
- `-s, --saikeet N` — rinnakkaiset linkkitarkistukset (oletus 8)
- `--user-agent` — lähetettävä User-Agent

Paluuarvo on `1`, jos virheitä löytyi, muuten `0` — sopii sellaisenaan
CI-ajoon tai omaan skriptiin.
