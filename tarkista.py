#!/usr/bin/env python3
"""Nettisivujen tarkistusohjelma.

Tarkistaa yhden tai useamman verkkosivun (tai paikallisen HTML-tiedoston):
saavutettavuuden, uudelleenohjaukset, vasteajan, TLS-varmenteen voimassaolon,
sivun rakenteen (otsikot, metatiedot, kuvien alt-tekstit) sekä halutessa
kaikki sivulta löytyvät linkit.

Käyttöesimerkkejä:
    python3 tarkista.py https://example.com
    python3 tarkista.py index.html --linkit
    python3 tarkista.py https://example.com --linkit --json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

OLETUS_UA = "tarkista.py/1.0 (+sivutarkistus)"
OLETUS_AIKAKATKAISU = 10.0
HIDAS_VASTE_MS = 1500
SUURI_SIVU_TAVUA = 1_000_000

OK, VARO, VIRHE, INFO = "ok", "varoitus", "virhe", "info"

MERKIT = {OK: "OK  ", VARO: "HUOM", VIRHE: "VIKA", INFO: "    "}
VARIT = {OK: "\033[32m", VARO: "\033[33m", VIRHE: "\033[31m", INFO: "\033[90m"}
LOPPU = "\033[0m"


# --------------------------------------------------------------------------
# HTML-jäsennys
# --------------------------------------------------------------------------

class SivunJasennin(HTMLParser):
    """Poimii HTML:stä tarkistuksissa tarvittavat tiedot."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.kieli: str | None = None
        self.otsikko: str | None = None
        self.kuvaus: str | None = None
        self.viewport: str | None = None
        self.merkisto: str | None = None
        self.otsikkotasot: list[tuple[int, str]] = []
        self.linkit: list[str] = []
        self.resurssit: list[str] = []
        self.tunnisteet: set[str] = set()
        self.kuvat_ilman_alt: list[str] = []
        self.kuvia = 0
        self.lomakekentat_ilman_nimea = 0
        self._otsikossa = False
        self._nykyinen_otsikkotaso: int | None = None
        self._otsikkoteksti: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {nimi.lower(): (arvo or "") for nimi, arvo in attrs}

        if tag == "html":
            self.kieli = a.get("lang") or None
        elif tag == "title":
            self._otsikossa = True
        elif tag == "meta":
            nimi = a.get("name", "").lower()
            if nimi == "description":
                self.kuvaus = a.get("content", "").strip()
            elif nimi == "viewport":
                self.viewport = a.get("content", "").strip()
            if "charset" in a:
                self.merkisto = a["charset"]
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nykyinen_otsikkotaso = int(tag[1])
            self._otsikkoteksti = []
        elif tag == "a":
            kohde = a.get("href", "").strip()
            if kohde:
                self.linkit.append(kohde)
        elif tag == "img":
            self.kuvia += 1
            if "alt" not in a:
                self.kuvat_ilman_alt.append(a.get("src", "(ei src-määrettä)"))
            if a.get("src"):
                self.resurssit.append(a["src"].strip())
        elif tag in ("script", "iframe", "source", "video", "audio"):
            if a.get("src"):
                self.resurssit.append(a["src"].strip())
        elif tag == "link":
            suhde = a.get("rel", "").lower()
            if a.get("href") and ("stylesheet" in suhde or "icon" in suhde):
                self.resurssit.append(a["href"].strip())
        elif tag in ("input", "select", "textarea"):
            if not a.get("name") and not a.get("id") and a.get("type") != "submit":
                self.lomakekentat_ilman_nimea += 1

        if a.get("id"):
            self.tunnisteet.add(a["id"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._otsikossa = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._nykyinen_otsikkotaso:
            teksti = " ".join("".join(self._otsikkoteksti).split())
            self.otsikkotasot.append((self._nykyinen_otsikkotaso, teksti))
            self._nykyinen_otsikkotaso = None

    def handle_data(self, data: str) -> None:
        if self._otsikossa:
            self.otsikko = ((self.otsikko or "") + data).strip()
        if self._nykyinen_otsikkotaso:
            self._otsikkoteksti.append(data)


# --------------------------------------------------------------------------
# Nouto
# --------------------------------------------------------------------------

@dataclass
class Vastaus:
    url: str
    tila: int | None = None
    ketju: list[tuple[int, str]] = field(default_factory=list)
    kesto_ms: float = 0.0
    tyyppi: str = ""
    koko: int = 0
    runko: str = ""
    otsakkeet: dict[str, str] = field(default_factory=dict)
    virhe: str | None = None
    paikallinen: bool = False


class _OhjausTallennin(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.ketju: list[tuple[int, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.ketju.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def on_paikallinen(kohde: str) -> bool:
    return urllib.parse.urlsplit(kohde).scheme not in ("http", "https")


def normalisoi(kohde: str) -> str:
    """Täydentää puuttuvan protokollan ja muuntaa tiedostopolun URL-muotoon."""
    if on_paikallinen(kohde):
        if os.path.exists(kohde):
            return "file://" + os.path.abspath(kohde)
        if "." in kohde.split("/")[0]:
            return "https://" + kohde
    return kohde


def hae(url: str, aikakatkaisu: float, ua: str, metodi: str = "GET",
        lue_runko: bool = True) -> Vastaus:
    if url.startswith("file://"):
        polku = urllib.request.url2pathname(urllib.parse.urlsplit(url).path)
        alku = time.perf_counter()
        try:
            with open(polku, "rb") as f:
                data = f.read()
        except OSError as e:
            return Vastaus(url=url, virhe=str(e), paikallinen=True)
        return Vastaus(
            url=url, tila=200, kesto_ms=(time.perf_counter() - alku) * 1000,
            tyyppi="text/html", koko=len(data), paikallinen=True,
            runko=data.decode("utf-8", "replace") if lue_runko else "",
        )

    ohjaukset = _OhjausTallennin()
    avaaja = urllib.request.build_opener(ohjaukset)
    pyynto = urllib.request.Request(url, method=metodi, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "fi,en;q=0.8",
    })

    alku = time.perf_counter()
    try:
        with avaaja.open(pyynto, timeout=aikakatkaisu) as v:
            data = v.read() if lue_runko else b""
            kesto = (time.perf_counter() - alku) * 1000
            pituus = v.headers.get("Content-Length")
            return Vastaus(
                url=v.geturl(), tila=v.status, ketju=ohjaukset.ketju, kesto_ms=kesto,
                tyyppi=(v.headers.get("Content-Type") or "").split(";")[0].strip(),
                koko=len(data) if data else int(pituus or 0),
                runko=data.decode(_merkisto(v.headers.get("Content-Type")), "replace"),
                otsakkeet={k.lower(): v for k, v in v.headers.items()},
            )
    except urllib.error.HTTPError as e:
        data = e.read() if lue_runko else b""
        return Vastaus(
            url=url, tila=e.code, ketju=ohjaukset.ketju,
            kesto_ms=(time.perf_counter() - alku) * 1000,
            tyyppi=(e.headers.get("Content-Type") or "").split(";")[0].strip() if e.headers else "",
            koko=len(data),
            runko=data.decode("utf-8", "replace"),
            otsakkeet={k.lower(): v for k, v in (e.headers or {}).items()},
        )
    except urllib.error.URLError as e:
        return Vastaus(url=url, virhe=str(e.reason), ketju=ohjaukset.ketju)
    except (socket.timeout, TimeoutError):
        return Vastaus(url=url, virhe=f"aikakatkaisu ({aikakatkaisu:.0f} s)")
    except Exception as e:  # esim. virheellinen URL
        return Vastaus(url=url, virhe=f"{type(e).__name__}: {e}")


def _merkisto(content_type: str | None) -> str:
    if content_type and "charset=" in content_type.lower():
        return content_type.lower().split("charset=")[1].split(";")[0].strip() or "utf-8"
    return "utf-8"


def varmenteen_tiedot(url: str, aikakatkaisu: float) -> dict | None:
    """Palauttaa TLS-varmenteen voimassaolotiedot, tai None jos ei saatavilla."""
    osat = urllib.parse.urlsplit(url)
    if osat.scheme != "https":
        return None
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return {"ohitettu": "välityspalvelin käytössä"}

    ca = os.environ.get("SSL_CERT_FILE")
    konteksti = ssl.create_default_context(cafile=ca if ca and os.path.exists(ca) else None)
    try:
        with socket.create_connection((osat.hostname, osat.port or 443), aikakatkaisu) as s:
            with konteksti.wrap_socket(s, server_hostname=osat.hostname) as ts:
                varmenne = ts.getpeercert()
                versio = ts.version()
    except Exception as e:
        return {"virhe": f"{type(e).__name__}: {e}"}

    loppuu = datetime.strptime(varmenne["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    myontaja = dict(x[0] for x in varmenne.get("issuer", ()) if x)
    return {
        "voimassa_asti": loppuu.date().isoformat(),
        "paivia_jaljella": (loppuu - datetime.now(timezone.utc)).days,
        "myontaja": myontaja.get("organizationName", "?"),
        "protokolla": versio,
    }


# --------------------------------------------------------------------------
# Tarkistukset
# --------------------------------------------------------------------------

@dataclass
class Havainto:
    taso: str
    aihe: str
    viesti: str


def tarkista_vastaus(v: Vastaus) -> list[Havainto]:
    h: list[Havainto] = []

    if v.virhe:
        return [Havainto(VIRHE, "yhteys", f"sivua ei saatu haettua: {v.virhe}")]

    if v.paikallinen:
        h.append(Havainto(INFO, "lähde", "paikallinen tiedosto (ei HTTP-tarkistuksia)"))
    else:
        if v.tila and 200 <= v.tila < 300:
            h.append(Havainto(OK, "tila", f"HTTP {v.tila}"))
        elif v.tila and 300 <= v.tila < 400:
            h.append(Havainto(VARO, "tila", f"HTTP {v.tila} — uudelleenohjaus jäi kesken"))
        elif v.tila == 404:
            h.append(Havainto(VIRHE, "tila", "HTTP 404 — sivua ei löydy"))
        elif v.tila and v.tila >= 500:
            h.append(Havainto(VIRHE, "tila", f"HTTP {v.tila} — palvelinvirhe"))
        else:
            h.append(Havainto(VIRHE, "tila", f"HTTP {v.tila}"))

        if v.ketju:
            polku = " → ".join(f"{koodi} {kohde}" for koodi, kohde in v.ketju)
            taso = VARO if len(v.ketju) > 2 else INFO
            h.append(Havainto(taso, "ohjaus", f"{len(v.ketju)} uudelleenohjausta: {polku}"))

        nopeus = OK if v.kesto_ms < HIDAS_VASTE_MS else VARO
        h.append(Havainto(nopeus, "vasteaika", f"{v.kesto_ms:.0f} ms"))

        if urllib.parse.urlsplit(v.url).scheme != "https":
            h.append(Havainto(VARO, "salaus", "sivu tarjoillaan ilman HTTPS-salausta"))

        if "strict-transport-security" not in v.otsakkeet and v.url.startswith("https"):
            h.append(Havainto(INFO, "otsakkeet", "Strict-Transport-Security puuttuu"))
        if "content-security-policy" not in v.otsakkeet:
            h.append(Havainto(INFO, "otsakkeet", "Content-Security-Policy puuttuu"))

    if v.koko:
        taso = VARO if v.koko > SUURI_SIVU_TAVUA else INFO
        h.append(Havainto(taso, "koko", _koko(v.koko)))

    return h


def _koko(tavua: int) -> str:
    if tavua < 1024:
        return f"{tavua} tavua"
    if tavua < 1024 * 1024:
        return f"{tavua / 1024:.1f} kt"
    return f"{tavua / (1024 * 1024):.1f} Mt"


def tarkista_sisalto(j: SivunJasennin) -> list[Havainto]:
    h: list[Havainto] = []

    if not j.otsikko:
        h.append(Havainto(VIRHE, "otsikko", "<title> puuttuu"))
    elif len(j.otsikko) > 65:
        h.append(Havainto(VARO, "otsikko", f"pitkä otsikko ({len(j.otsikko)} merkkiä): {j.otsikko}"))
    else:
        h.append(Havainto(OK, "otsikko", j.otsikko))

    if not j.kieli:
        h.append(Havainto(VARO, "kieli", "<html lang=\"...\"> puuttuu"))
    else:
        h.append(Havainto(OK, "kieli", j.kieli))

    if not j.merkisto:
        h.append(Havainto(VARO, "merkistö", "<meta charset> puuttuu"))
    elif j.merkisto.lower() not in ("utf-8", "utf8"):
        h.append(Havainto(VARO, "merkistö", f"muu kuin UTF-8: {j.merkisto}"))

    if not j.viewport:
        h.append(Havainto(VARO, "mobiili", "viewport-metatieto puuttuu"))

    if not j.kuvaus:
        h.append(Havainto(VARO, "kuvaus", "meta description puuttuu"))
    elif len(j.kuvaus) > 160:
        h.append(Havainto(VARO, "kuvaus", f"yli 160 merkkiä ({len(j.kuvaus)})"))

    ykkoset = [t for taso, t in j.otsikkotasot if taso == 1]
    if not ykkoset:
        h.append(Havainto(VARO, "rakenne", "sivulta puuttuu <h1>"))
    elif len(ykkoset) > 1:
        h.append(Havainto(VARO, "rakenne", f"{len(ykkoset)} kpl <h1>-otsikoita"))

    edellinen = 0
    for taso, teksti in j.otsikkotasot:
        if edellinen and taso > edellinen + 1:
            h.append(Havainto(VARO, "rakenne",
                              f"otsikkotaso hyppää h{edellinen} → h{taso}: {teksti[:40]}"))
        edellinen = taso

    if j.kuvat_ilman_alt:
        h.append(Havainto(VARO, "saavutettavuus",
                          f"{len(j.kuvat_ilman_alt)}/{j.kuvia} kuvalta puuttuu alt-teksti "
                          f"({', '.join(j.kuvat_ilman_alt[:3])})"))
    elif j.kuvia:
        h.append(Havainto(OK, "saavutettavuus", f"kaikilla {j.kuvia} kuvalla on alt-teksti"))

    if j.lomakekentat_ilman_nimea:
        h.append(Havainto(VARO, "lomakkeet",
                          f"{j.lomakekentat_ilman_nimea} kenttää ilman name- tai id-määrettä"))

    return h


def kerää_linkit(j: SivunJasennin, perusta: str, mukaan_ulkoiset: bool) -> tuple[list[str], list[Havainto]]:
    """Palauttaa tarkistettavat absoluuttiset osoitteet ja sivun sisäisistä
    ankkureista tehdyt havainnot."""
    h: list[Havainto] = []
    osoitteet: list[str] = []
    nähdyt: set[str] = set()

    for viite in j.linkit + j.resurssit:
        if viite.startswith("#"):
            tunniste = viite[1:]
            if tunniste and tunniste not in j.tunnisteet:
                h.append(Havainto(VIRHE, "ankkuri", f"sivulta puuttuu kohde {viite}"))
            continue
        if viite.split(":")[0].lower() in ("mailto", "tel", "javascript", "data") and ":" in viite:
            continue

        koko = urllib.parse.urljoin(perusta, viite)
        koko = urllib.parse.urldefrag(koko).url
        if not koko or koko in nähdyt:
            continue
        skeema = urllib.parse.urlsplit(koko).scheme
        if skeema not in ("http", "https", "file"):
            continue
        if not mukaan_ulkoiset and _isanta(koko) != _isanta(perusta):
            continue
        nähdyt.add(koko)
        osoitteet.append(koko)

    return osoitteet, h


def _isanta(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def tarkista_linkit(osoitteet: list[str], aikakatkaisu: float, ua: str,
                    saikeet: int) -> list[Havainto]:
    def yksi(url: str) -> Havainto:
        v = hae(url, aikakatkaisu, ua, metodi="HEAD", lue_runko=False)
        if v.virhe or (v.tila and v.tila in (403, 405, 501)):
            v = hae(url, aikakatkaisu, ua, metodi="GET", lue_runko=False)
        if v.virhe:
            return Havainto(VIRHE, "linkki", f"{url} — {v.virhe}")
        if v.tila and v.tila >= 400:
            return Havainto(VIRHE, "linkki", f"{url} — HTTP {v.tila}")
        if v.ketju:
            return Havainto(INFO, "linkki", f"{url} → {v.ketju[-1][1]} (HTTP {v.ketju[0][0]})")
        return Havainto(OK, "linkki", url)

    if not osoitteet:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=saikeet) as pool:
        return list(pool.map(yksi, osoitteet))


# --------------------------------------------------------------------------
# Raportointi
# --------------------------------------------------------------------------

def tarkista_sivu(kohde: str, asetukset: argparse.Namespace) -> dict:
    url = normalisoi(kohde)
    vastaus = hae(url, asetukset.aikakatkaisu, asetukset.user_agent)
    havainnot = tarkista_vastaus(vastaus)

    varmenne = None
    if not vastaus.virhe and not vastaus.paikallinen:
        varmenne = varmenteen_tiedot(vastaus.url, asetukset.aikakatkaisu)
        if varmenne and "paivia_jaljella" in varmenne:
            paivia = varmenne["paivia_jaljella"]
            taso = VIRHE if paivia < 0 else VARO if paivia < 21 else OK
            havainnot.append(Havainto(
                taso, "varmenne",
                f"voimassa {varmenne['voimassa_asti']} asti ({paivia} pv), "
                f"myöntäjä {varmenne['myontaja']}, {varmenne['protokolla']}"))

    on_html = "html" in (vastaus.tyyppi or "") or vastaus.runko.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))
    jasennin = None
    if vastaus.runko and on_html:
        jasennin = SivunJasennin()
        try:
            jasennin.feed(vastaus.runko)
        except Exception as e:
            havainnot.append(Havainto(VARO, "jäsennys", f"HTML:n jäsennys keskeytyi: {e}"))
        havainnot += tarkista_sisalto(jasennin)
    elif not vastaus.virhe:
        havainnot.append(Havainto(INFO, "sisältö", f"ei HTML-sivu ({vastaus.tyyppi or 'tuntematon tyyppi'})"))

    linkkihavainnot: list[Havainto] = []
    if jasennin and asetukset.linkit:
        osoitteet, ankkurit = kerää_linkit(jasennin, vastaus.url, not asetukset.ohita_ulkoiset)
        linkkihavainnot = ankkurit + tarkista_linkit(
            osoitteet, asetukset.aikakatkaisu, asetukset.user_agent, asetukset.saikeet)

    return {
        "kohde": kohde,
        "url": vastaus.url,
        "tila": vastaus.tila,
        "kesto_ms": round(vastaus.kesto_ms, 1),
        "koko_tavua": vastaus.koko,
        "varmenne": varmenne,
        "havainnot": havainnot,
        "linkit": linkkihavainnot,
    }


def _varita(taso: str, teksti: str, varit: bool) -> str:
    if not varit:
        return teksti
    return f"{VARIT[taso]}{teksti}{LOPPU}"


def tulosta(tulos: dict, asetukset: argparse.Namespace, varit: bool) -> None:
    print()
    print(_varita(INFO, "═" * 72, varit))
    print(f"  {tulos['url']}")
    print(_varita(INFO, "═" * 72, varit))

    for h in tulos["havainnot"]:
        if asetukset.hiljainen and h.taso in (OK, INFO):
            continue
        print(f"  {_varita(h.taso, MERKIT[h.taso], varit)}  {h.aihe:<14} {h.viesti}")

    if tulos["linkit"]:
        rikki = [h for h in tulos["linkit"] if h.taso == VIRHE]
        ohjatut = [h for h in tulos["linkit"] if h.taso == INFO]
        kunnossa = [h for h in tulos["linkit"] if h.taso == OK]
        print()
        print(f"  Linkit: {len(tulos['linkit'])} tarkistettu — "
              f"{len(kunnossa)} kunnossa, {len(ohjatut)} ohjattu, {len(rikki)} rikki")
        for h in rikki + ohjatut:
            print(f"  {_varita(h.taso, MERKIT[h.taso], varit)}  {h.aihe:<14} {h.viesti}")
        if not asetukset.hiljainen:
            for h in kunnossa:
                print(f"  {_varita(h.taso, MERKIT[h.taso], varit)}  {h.aihe:<14} {h.viesti}")


def json_muoto(tulokset: list[dict]) -> str:
    def muunna(t: dict) -> dict:
        k = dict(t)
        k["havainnot"] = [vars(h) for h in t["havainnot"]]
        k["linkit"] = [vars(h) for h in t["linkit"]]
        return k

    return json.dumps({
        "tarkistettu": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sivut": [muunna(t) for t in tulokset],
    }, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Tarkistaa verkkosivujen saavutettavuuden, rakenteen ja linkit.",
        epilog="Esimerkki: python3 tarkista.py index.html --linkit",
    )
    p.add_argument("kohteet", nargs="+", metavar="KOHDE",
                   help="URL-osoite tai paikallinen HTML-tiedosto")
    p.add_argument("-l", "--linkit", action="store_true",
                   help="tarkista myös sivulta löytyvät linkit ja resurssit")
    p.add_argument("--ohita-ulkoiset", action="store_true",
                   help="tarkista linkeistä vain saman sivuston osoitteet")
    p.add_argument("-q", "--hiljainen", action="store_true",
                   help="näytä vain varoitukset ja virheet")
    p.add_argument("--json", action="store_true", help="tulosta koneluettava JSON")
    p.add_argument("-t", "--aikakatkaisu", type=float, default=OLETUS_AIKAKATKAISU,
                   metavar="SEK", help=f"pyynnön aikakatkaisu (oletus {OLETUS_AIKAKATKAISU:.0f} s)")
    p.add_argument("-s", "--saikeet", type=int, default=8, metavar="N",
                   help="rinnakkaisten linkkitarkistusten määrä (oletus 8)")
    p.add_argument("--user-agent", default=OLETUS_UA, help="lähetettävä User-Agent")
    asetukset = p.parse_args(argv)

    varit = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    tulokset = [tarkista_sivu(kohde, asetukset) for kohde in asetukset.kohteet]

    if asetukset.json:
        print(json_muoto(tulokset))
    else:
        for tulos in tulokset:
            tulosta(tulos, asetukset, varit)

        virheita = sum(1 for t in tulokset for h in t["havainnot"] + t["linkit"] if h.taso == VIRHE)
        varoituksia = sum(1 for t in tulokset for h in t["havainnot"] + t["linkit"] if h.taso == VARO)
        print()
        yhteenveto = (f"Yhteensä {len(tulokset)} sivua — "
                      f"{virheita} virhettä, {varoituksia} varoitusta")
        print(_varita(VIRHE if virheita else VARO if varoituksia else OK, yhteenveto, varit))
        print()

    return 1 if any(h.taso == VIRHE for t in tulokset for h in t["havainnot"] + t["linkit"]) else 0


if __name__ == "__main__":
    sys.exit(main())
