"""Overvaak maanedens doegnmaks/min fra Frost, med historiske maanedsrekorder.

    python overvak_maanedstemperatur.py
    python overvak_maanedstemperatur.py --stasjon SN18950
    python overvak_maanedstemperatur.py --maaned 10 --aar 2026
    python overvak_maanedstemperatur.py --oppdater-minutter 60

Krever FROST_CLIENT_ID og matplotlib. Standardstasjon: SN18700 (Oslo - Blindern).
Bytt STANDARDSTASJON nedenfor, eller overstyr med --stasjon SN18950.
Hvert tidligere aar tegnes med lyse doegnmaks/min-kurver, valgt aar med moerke.
Gylne prikker markerer varmeste doegnmaks for samme kalenderdato i tidligere aar,
inkludert tangering. Gylne diamanter markerer tangering eller ny maanedsrekord
mot tidligere aar og tidligere dager i valgt aar. Kvalitetskode 0 og 2 brukes.
Kode 2 er ukontrollerte, litt usikre verdier og inngaar ogsaa i rekordgrunnlaget.
Dato er Frosts doegndato (referenceTime), ikke nodvendigvis et lokalt kalenderdoegn.
Bare doegndatoer foer dagens UTC-dato vises. Ingen interpolasjon.
Historikken starter ved tidligste startdato for stasjonen eller temperaturseriene,
eller --historikk-fra. Eldre doegnavgrensninger beholdes; per dato foretrekkes
PT18H, deretter PT0H, deretter foerste tilgjengelige offset sortert alfabetisk.
Rekorder gjelder tilgjengelige, godkjente data, ikke en sertifisert rekordliste.
PNG og CSV overskrives ved oppdatering. Avslutt overvaaking med Ctrl+C.
Frost-hjelpere gjenbrukes fra sammenlign_veg_og_lufttemperatur.py.
"""

import argparse
import calendar
import csv
import math
import re
import sys
import time
import urllib.error
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from sammenlign_veg_og_lufttemperatur import hent_data, les_tid


STANDARDSTASJON = "SN18700"
MAKS = "max(air_temperature P1D)"
MIN = "min(air_temperature P1D)"
MAANEDER = (
    "", "Januar", "Februar", "Mars", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Desember",
)


def neste_maaned(aar: int, maaned: int) -> date:
    return date(aar + (maaned == 12), maaned % 12 + 1, 1)


def hent_doegn(stasjon: str, periode: str) -> list[dict]:
    return hent_data("observations", {
        "sources": f"{stasjon}:0", "referencetime": periode,
        "elements": f"{MAKS},{MIN}", "timeresolutions": "P1D",
        "timeseriesids": "0", "levels": "default",
    })


def finn_startaar(stasjon: str, metadata: dict) -> int:
    serier = hent_data("observations/availableTimeSeries", {
        "sources": f"{stasjon}:0", "elements": f"{MAKS},{MIN}",
        "timeresolutions": "P1D", "timeseriesids": "0", "levels": "default",
    })
    startdatoer = [rad["validFrom"] for rad in [metadata, *serier] if rad.get("validFrom")]
    if not startdatoer:
        raise RuntimeError("Fant ingen historisk startdato; oppgi --historikk-fra.")
    return min(les_tid(dato).year for dato in startdatoer)


def les_doegn(
    data: list[dict], stasjon: str, maaned: int, fra_aar: int,
    til_aar: int, foer: date,
) -> tuple[list[dict], Counter]:
    kandidater = defaultdict(list)
    utelatt = Counter()
    for rad in data:
        if rad.get("sourceId") not in (stasjon, f"{stasjon}:0"):
            continue
        dato = les_tid(rad["referenceTime"]).date()
        if dato.month != maaned or not fra_aar <= dato.year <= til_aar or dato >= foer:
            continue
        for observasjon in rad.get("observations", []):
            element = observasjon.get("elementId")
            if element not in (MAKS, MIN):
                continue
            if str(observasjon.get("timeSeriesId")) != "0" or observasjon.get("timeResolution") != "P1D":
                utelatt["annen_serie_eller_opplosning"] += 1
                continue
            if str(observasjon.get("qualityCode")) not in ("0", "2"):
                utelatt[f"kvalitet_{observasjon.get('qualityCode')}"] += 1
                continue
            if observasjon.get("unit") != "degC":
                utelatt["annen_enhet"] += 1
                continue
            try:
                verdi = float(observasjon["value"])
            except (KeyError, TypeError, ValueError):
                utelatt["ugyldig_verdi"] += 1
                continue
            if not math.isfinite(verdi):
                utelatt["ugyldig_verdi"] += 1
                continue
            kandidater[dato, element].append({
                "dato": dato, "element": element, "verdi": verdi,
                "timeoffset": observasjon.get("timeOffset", "ukjent"),
            })
    doegn = []
    for verdier in kandidater.values():
        offset = min(
            {rad["timeoffset"] for rad in verdier},
            key=lambda verdi: (verdi != "PT18H", verdi != "PT0H", verdi),
        )
        valgte = [rad for rad in verdier if rad["timeoffset"] == offset]
        utelatt["alternativ_doegnavgrensning"] += len(verdier) - len(valgte)
        if len(valgte) != 1:
            utelatt["tvetydig_doegn"] += 1
            continue
        doegn.append(valgte[0])
    return sorted(doegn, key=lambda rad: (rad["dato"], rad["element"])), utelatt


def finn_rekorder(historikk: list[dict]) -> dict[str, dict]:
    rekorder = {}
    for element, velg in ((MAKS, max), (MIN, min)):
        kandidater = [rad for rad in historikk if rad["element"] == element]
        if kandidater:
            rekorder[element] = velg(kandidater, key=lambda rad: rad["verdi"])
    return rekorder


def hent_historikk(
    stasjon: str, maaned: int, start: int, slutt: int, foer: date,
) -> tuple[list[dict], Counter]:
    historikk = []
    utelatt = Counter()
    for aar in range(start, slutt + 1):
        periode = f"{date(aar, maaned, 1)}/{neste_maaned(aar, maaned)}"
        data = hent_doegn(stasjon, periode)
        doegn, forkastet = les_doegn(data, stasjon, maaned, aar, aar, foer)
        historikk.extend(doegn)
        utelatt.update(forkastet)
        mottatt = Counter(
            str(observasjon.get("qualityCode"))
            for rad in data for observasjon in rad.get("observations", [])
            if observasjon.get("elementId") in (MAKS, MIN)
        )
        antall = Counter(rad["element"] for rad in doegn)
        print(
            f"{aar}: mottatt {sum(mottatt.values())} verdier, "
            f"godkjent maks {antall[MAKS]} / min {antall[MIN]}. "
            f"Kvalitetskoder: {dict(mottatt)}. "
            f"Utelatt: {dict(+forkastet)}",
            flush=True,
        )
    aarganger = {rad["dato"].year for rad in historikk}
    mangler = sorted(set(range(start, slutt + 1)) - aarganger)
    if aarganger:
        print(f"Faktisk historikk til plott: {min(aarganger)}-{max(aarganger)}, {len(aarganger)} aar.", flush=True)
    if mangler:
        print("Aar uten godkjente data: " + ", ".join(map(str, mangler)), flush=True)
    return historikk, +utelatt


def lag_tabell(
    doegn: list[dict], rekorder: dict[str, dict], aar: int, maaned: int,
    historikk: list[dict] | None = None,
) -> list[dict]:
    oppslag = {(rad["dato"].day, rad["element"]): rad for rad in doegn}
    lopende = {element: rekord["verdi"] for element, rekord in rekorder.items()}
    datorekorder = {}
    for observasjon in historikk or []:
        dato = observasjon["dato"]
        if observasjon["element"] != MAKS or dato.month != maaned or dato.year >= aar:
            continue
        rekord = datorekorder.get(dato.day)
        if rekord is None or observasjon["verdi"] > rekord["verdi"]:
            datorekorder[dato.day] = observasjon
    tabell = []
    for dag in range(1, calendar.monthrange(aar, maaned)[1] + 1):
        rad = {"dato": date(aar, maaned, dag).isoformat()}
        for element, navn in ((MAKS, "maks"), (MIN, "min")):
            observasjon = oppslag.get((dag, element))
            verdi = observasjon["verdi"] if observasjon else None
            rekord = rekorder.get(element)
            grense = lopende.get(element)
            er_rekord = (
                verdi is not None and grense is not None
                and (verdi >= grense if element == MAKS else verdi <= grense)
            )
            if er_rekord:
                lopende[element] = verdi
            rad[f"{navn}_degC"] = verdi
            rad[f"{navn}_rekord"] = int(er_rekord)
            rad[f"historisk_{navn}_degC"] = rekord["verdi"] if rekord else None
            rad[f"historisk_{navn}_dato"] = rekord["dato"].isoformat() if rekord else ""
            rad[f"{navn}_timeoffset"] = observasjon["timeoffset"] if observasjon else ""
        datorekord = datorekorder.get(dag)
        rad["maks_datorekord"] = int(
            rad["maks_degC"] is not None and datorekord is not None
            and rad["maks_degC"] >= datorekord["verdi"]
        )
        rad["historisk_datomaks_degC"] = datorekord["verdi"] if datorekord else None
        rad["historisk_datomaks_dato"] = datorekord["dato"].isoformat() if datorekord else ""
        tabell.append(rad)
    return tabell


def skriv_resultat(
    tabell: list[dict], rekorder: dict[str, dict], historikk: list[dict],
    utfil: Path, navn: str, stasjon: str, aar: int, maaned: int,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as feil:
        raise RuntimeError("Installer plottpakken: python -m pip install matplotlib") from feil
    utfil.parent.mkdir(parents=True, exist_ok=True)
    figur, akse = plt.subplots(figsize=(12, 6))
    dager = list(range(1, len(tabell) + 1))
    aarganger = sorted({rad["dato"].year for rad in historikk})
    historisk_oppslag = {
        (rad["dato"].year, rad["element"], rad["dato"].day): rad["verdi"]
        for rad in historikk
    }
    for historisk_aar in aarganger:
        for element, navn_type, lys in ((MAKS, "maks", "#efb1b1"), (MIN, "min", "#a9d2ec")):
            verdier = [historisk_oppslag.get((historisk_aar, element, dag), float("nan")) for dag in dager]
            akse.plot(dager, verdier, color=lys, linewidth=0.8, alpha=0.65,
                      marker=".", markersize=2, zorder=1,
                      label=f"Doegn{navn_type} tidligere aar" if historisk_aar == aarganger[0] else "_nolegend_")
    for navn_type, mork in (("maks", "#9e182b"), ("min", "#15598e")):
        verdier = [rad[f"{navn_type}_degC"] for rad in tabell]
        akse.plot(dager, [float("nan") if verdi is None else verdi for verdi in verdier],
                  color=mork, linewidth=1.8, marker="o", markersize=3, zorder=3,
                  label=f"Doegn{navn_type} {aar}")
    datorekorder = [rad for rad in tabell if rad["maks_datorekord"]]
    if datorekorder:
        akse.scatter([date.fromisoformat(rad["dato"]).day for rad in datorekorder],
                     [rad["maks_degC"] for rad in datorekorder],
                     s=65, color="#d4a017", edgecolors="#775700", linewidths=0.6,
                     zorder=5, label="Ny eller tangert varmerekord for datoen")
    rekord_dager = [dag for dag, rad in zip(dager, tabell) for navn_type in ("maks", "min") if rad[f"{navn_type}_rekord"]]
    rekord_verdier = [rad[f"{navn_type}_degC"] for rad in tabell for navn_type in ("maks", "min") if rad[f"{navn_type}_rekord"]]
    if rekord_dager:
        akse.scatter(rekord_dager, rekord_verdier, s=100, marker="D", facecolors="none",
                 edgecolors="#775700", linewidths=1.2, zorder=6,
                     label="Ny eller tangert maanedsrekord")
    akse.set(title=f"{navn} ({stasjon}) | {MAANEDER[maaned]} {aar}",
             xlabel="Dag i maaneden (Frosts doegndato)", ylabel="Temperatur (\N{DEGREE SIGN}C)",
             xlim=(0.6, len(tabell) + 0.4))
    akse.set_xticks(dager)
    akse.grid(axis="y", alpha=0.25)
    akse.spines[["top", "right"]].set_visible(False)
    akse.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=2, fontsize=9)
    dekning = f"{aarganger[0]}-{aarganger[-1]} ({len(aarganger)} aar med data)" if aarganger else "ingen godkjent historikk"
    antall = {element: sum(rad["element"] == element for rad in historikk) for element in (MAKS, MIN)}
    forskyvninger = ", ".join(sorted({rad["timeoffset"] for rad in historikk})) or "ukjent"
    figur.text(0.5, 0.025,
               f"Historikk: {dekning}; maks {antall[MAKS]} / min {antall[MIN]} doegn. Kvalitet 0 og 2 (2: ukontrollert).\n"
               f"Historiske doegn-offset: {forskyvninger}. Manglende dager er ikke interpolert.\n"
               f"Oppdatert {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | MET Norway / Frost (NLOD 2.0)",
               ha="center", fontsize=8)
    figur.subplots_adjust(bottom=0.32, top=0.9)
    try:
        figur.savefig(utfil, dpi=170)
    finally:
        plt.close(figur)
    with utfil.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as fil:
        skriver = csv.DictWriter(fil, fieldnames=["stasjon", *tabell[0]], delimiter=";")
        skriver.writeheader()
        skriver.writerows({"stasjon": stasjon, **rad} for rad in tabell)


def main() -> None:
    naa = datetime.now(timezone.utc)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stasjon", default=STANDARDSTASJON, help="Frost-stasjons-ID, f.eks. SN18950. Standard: %(default)s.")
    parser.add_argument("--aar", type=int, default=naa.year)
    parser.add_argument("--maaned", type=int, choices=range(1, 13), default=naa.month)
    parser.add_argument("--historikk-fra", type=int, help="Foerste historiske aar; standard: hele tilgjengelige observasjonsperioden.")
    parser.add_argument("--utfil", type=Path, help="PNG-sti; CSV faar samme filstamme.")
    parser.add_argument("--oppdater-minutter", type=float, default=0, help="0: kjor en gang. Positiv verdi: oppdater gjentatte ganger.")
    args = parser.parse_args()
    stasjon = args.stasjon.upper()
    if not re.fullmatch(r"SN\d+", stasjon):
        parser.error("--stasjon maa ha format SN18700.")
    if not 2 <= args.aar <= naa.year or (args.aar, args.maaned) > (naa.year, naa.month):
        parser.error("Velg en maaned som har startet, og et aar fra 2 til innevaerende aar.")
    if not math.isfinite(args.oppdater_minutter) or args.oppdater_minutter < 0:
        parser.error("--oppdater-minutter maa vaere et endelig, ikke-negativt tall.")
    utfil = args.utfil or Path(__file__).with_name(f"temperatur_{stasjon}_{args.aar}_{args.maaned:02d}.png")
    if utfil.suffix.lower() != ".png":
        parser.error("--utfil maa ende med .png.")
    metadata = hent_data("sources", {"ids": stasjon})
    if len(metadata) != 1:
        raise RuntimeError(f"Fant ikke entydig stasjonsmetadata for {stasjon}.")
    navn = metadata[0].get("name") or stasjon
    start = args.historikk_fra
    if start is None:
        start = finn_startaar(stasjon, metadata[0])
    if not 1 <= start < args.aar:
        parser.error("Historikken maa starte minst ett aar foer valgt aar.")
    print(f"Henter historikk for {navn} ({stasjon}), maaned {args.maaned}, {start}-{args.aar - 1} ...", flush=True)
    historikk, utelatt = hent_historikk(stasjon, args.maaned, start, args.aar - 1, naa.date())
    rekorder = finn_rekorder(historikk)
    if len(rekorder) < 2:
        raise RuntimeError(f"Mangler historisk doegnmaks eller doegnmin. Utelatt: {dict(utelatt)}")
    for element, rekord in rekorder.items():
        print(f"{element}: {rekord['verdi']:.1f} grader C ({rekord['dato']}).")
    if utelatt:
        print(f"Utelatt i historikken: {dict(utelatt)}")
    while True:
        naa = datetime.now(timezone.utc)
        periode = f"{date(args.aar, args.maaned, 1)}/{neste_maaned(args.aar, args.maaned)}"
        doegn, utelatt = les_doegn(hent_doegn(stasjon, periode), stasjon, args.maaned, args.aar, args.aar, naa.date())
        if not doegn:
            print(f"Ingen godkjente doegnverdier ennaa for valgt maaned. Utelatt: {dict(utelatt)}", flush=True)
        elif utelatt:
            print(f"Utelatte verdier for valgt aar: {dict(utelatt)}", flush=True)
        tabell = lag_tabell(doegn, rekorder, args.aar, args.maaned, historikk)
        skriv_resultat(tabell, rekorder, historikk, utfil, navn, stasjon, args.aar, args.maaned)
        print(f"Skrev {utfil} og {utfil.with_suffix('.csv')} ({len(doegn)} doegnverdier).", flush=True)
        if not args.oppdater_minutter:
            break
        time.sleep(args.oppdater_minutter * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOvervaaking avsluttet.")
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as feil:
        print(f"Feil: {feil}", file=sys.stderr)
        sys.exit(1)