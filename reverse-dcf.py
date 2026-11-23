#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reverse DCF — modèle « AlphaBourse » (grille de scénarios sur 30 ans, FCF après SBC).

Usage : python3 reverse-dcf.py NVDA [AUTRE_TICKER ...]

Pour chaque ticker :
  - récupère en direct (yfinance) : cours, capitalisation, actions en circulation,
    FCF sur 12 mois (flux opérationnel − capex, 4 derniers trimestres),
    SBC sur 12 mois (rémunération en actions, même fenêtre),
    FCF après SBC = FCF − SBC, P/FCF avant et après SBC ;
  - projette le FCF après SBC sur 30 ans selon 5 scénarios de croissance
    (4 périodes + croissance terminale), tous modifiables dans SCENARIOS ci-dessous ;
  - résout numériquement (bissection) le taux d'actualisation qui égalise la valeur
    actuelle des FCF projetés + valeur terminale à la capitalisation actuelle
    = « rendement annualisé implicite » du scénario.

Contrôle de cohérence : scénario « Zéro » (toutes croissances à 0) → le rendement
implicite doit égaler le rendement FCF après SBC (FCF après SBC / capitalisation).
"""

import sys

import yfinance as yf

# ---------------------------------------------------------------- scénarios
# Taux de croissance annuels (%) par période : années 1-5, 6-10, 11-20, 21-30,
# puis croissance terminale perpétuelle. Modifiables librement.
SCENARIOS = {
    "Négatif":  [-5, -5, -7, -10, -10],
    "Zéro":     [0, 0, 0, 0, 0],
    "Baissier": [15, 12, 8, 6, 1],
    "Central":  [20, 15, 12, 9, 2],
    "Haussier": [30, 20, 15, 10, 3],
}
PERIODES = [(1, 5), (6, 10), (11, 20), (21, 30)]  # (première, dernière année)


# ------------------------------------------------------------------- calcul
def taux_periode(annee, taux):
    """Taux de croissance (%) applicable à l'année donnée (1..30)."""
    for (debut, fin), g in zip(PERIODES, taux):
        if debut <= annee <= fin:
            return g
    raise ValueError(annee)


def valeur_actuelle(fcf0, capi, taux, r):
    """VA des FCF projetés + valeur terminale, au taux r (décimal)."""
    fcf = fcf0
    va = 0.0
    for annee in range(1, 31):
        fcf *= 1 + taux_periode(annee, taux) / 100.0
        va += fcf / (1 + r) ** annee
    g_term = taux[4] / 100.0
    if r <= g_term:  # valeur terminale infinie : VA au-dessus de toute cible
        return float("inf")
    valeur_terminale = fcf * (1 + g_term) / (r - g_term)
    va += valeur_terminale / (1 + r) ** 30
    return va - capi  # on cherche le zéro de cette fonction


def rendement_implicite(fcf0, capi, taux):
    """Résout le rendement annualisé implicite (%) par bissection."""
    if fcf0 <= 0:
        return None  # pas de rendement implicite significatif sur un FCF négatif
    lo, hi = -0.90, 5.0
    flo = valeur_actuelle(fcf0, capi, taux, lo)
    fhi = valeur_actuelle(fcf0, capi, taux, hi)
    if flo < 0:  # même au taux plancher, la VA n'atteint pas la capitalisation
        return None
    if fhi > 0:  # même au taux plafond, la VA dépasse la capitalisation
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = valeur_actuelle(fcf0, capi, taux, mid)
        if fm == float("inf") or fm > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100.0


# ------------------------------------------------------------------ données
def ligne(cf, noms):
    """Première ligne trouvée dans le tableau de flux, selon les noms possibles."""
    for nom in noms:
        if nom in cf.index:
            return cf.loc[nom]
    return None


def donnees(ticker):
    t = yf.Ticker(ticker)
    info = t.info or {}
    prix = info.get("regularMarketPrice") or info.get("currentPrice")
    actions = info.get("sharesOutstanding")
    capi = info.get("marketCap")
    nom = info.get("longName") or info.get("shortName") or ticker
    if not prix or not actions:
        return None
    if not capi:
        capi = prix * actions

    cf = t.quarterly_cashflow
    if cf is None or cf.empty:
        return None
    ocf = ligne(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"])
    capex = ligne(cf, ["Capital Expenditure"])
    sbc = ligne(cf, ["Stock Based Compensation"])
    if ocf is None or capex is None:
        return None
    n = min(4, len(cf.columns))
    fcf = float(ocf.iloc[:n].sum()) + float(capex.iloc[:n].sum())  # capex < 0
    sbc_val = float(sbc.iloc[:n].sum()) if sbc is not None else None
    return {
        "nom": nom, "prix": prix, "actions": actions, "capi": capi,
        "fcf": fcf, "sbc": sbc_val,
        "fcf_apres_sbc": fcf - sbc_val if sbc_val is not None else fcf,
    }


def fmt_montant(x):
    """1234567890 -> '1 234,57 M$' ; gère Md$ et M$."""
    if abs(x) >= 1e9:
        return f"{x / 1e9:,.2f} Md$".replace(",", " ")
    return f"{x / 1e6:,.0f} M$".replace(",", " ")


# ------------------------------------------------------------------- sortie
def analyser(ticker):
    d = donnees(ticker)
    print("=" * 64)
    if d is None:
        print(f"{ticker} : données introuvables ou incomplètes.")
        return
    print(f"{d['nom']} · {ticker}")
    print(f"Cours utilisé ($) : {d['prix']:,.2f}")
    print(f"Capitalisation : {fmt_montant(d['capi'])} | "
          f"Actions en circulation (millions) : {d['actions'] / 1e6:,.0f}".replace(",", " "))
    print(f"FCF sur 12 mois avant SBC : {fmt_montant(d['fcf'])}")
    if d["sbc"] is not None:
        print(f"SBC sur 12 mois : {fmt_montant(d['sbc'])}")
    else:
        print("SBC sur 12 mois : n/d (non ventilée — FCF après SBC = FCF)")
    print(f"FCF après SBC : {fmt_montant(d['fcf_apres_sbc'])}")
    p_avant = d["capi"] / d["fcf"] if d["fcf"] > 0 else float("nan")
    p_apres = d["capi"] / d["fcf_apres_sbc"] if d["fcf_apres_sbc"] > 0 else float("nan")
    print(f"P/FCF avant / après SBC : {p_avant:.2f}x / {p_apres:.2f}x")
    if d["fcf_apres_sbc"] > 0:
        print(f"Rendement FCF après SBC : {d['fcf_apres_sbc'] / d['capi'] * 100:.2f} %")
    print("-" * 64)
    print("Hypothèses de croissance (%, modifiables dans SCENARIOS) :")
    print(f"{'Période':<22}" + "".join(f"{s:>10}" for s in SCENARIOS))
    etiquettes = ["Années 1–5", "Années 6–10", "Années 11–20",
                  "Années 21–30", "Croissance terminale"]
    for i, etq in enumerate(etiquettes):
        print(f"{etq:<22}" + "".join(f"{SCENARIOS[s][i]:>10.0f}" for s in SCENARIOS))
    print("-" * 64)
    print("Rendement annualisé implicite avec ces hypothèses :")
    for nom_s, taux in SCENARIOS.items():
        r = rendement_implicite(d["fcf_apres_sbc"], d["capi"], taux)
        txt = f"{r:+.2f} %" if r is not None else "n/d (FCF après SBC ≤ 0)"
        print(f"  {nom_s:<10} {txt}")


# ---------------------------------------------------------------------------
# Export pour l'appli (ajout) — le calcul ci-dessus n'est pas modifié.
#   python3 reverse-dcf.py --json data.json            -> liste de tickers.txt
#   python3 reverse-dcf.py --json data.json NVDA AXON  -> ces tickers seulement
# Les fiches sont fusionnées dans le fichier existant (clé = ticker).
# ---------------------------------------------------------------------------
import json
import os
import re
from datetime import datetime, timezone


TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


def _num(x):
    """float JSON valide (None si absent, NaN ou infini)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fiche_dict(ticker):
    """Mêmes chiffres que analyser(), en dict sérialisable."""
    d = donnees(ticker)
    if d is None:
        raise ValueError("données introuvables ou incomplètes (ticker inconnu ou source indisponible)")
    # Devises : Yahoo donne parfois le cours en USD et les états financiers dans
    # la devise de l'émetteur (ex. TSM : dollars taïwanais). Signalé, pas corrigé.
    devise = devise_fin = None
    try:
        info = yf.Ticker(ticker).info or {}
        devise = info.get("currency")
        devise_fin = info.get("financialCurrency")
    except Exception:  # noqa: BLE001
        pass
    fcf, fas, capi = _num(d["fcf"]), _num(d["fcf_apres_sbc"]), _num(d["capi"])
    return {
        "ticker": ticker,
        "nom": str(d["nom"]),
        "prix": _num(d["prix"]),
        "capi": capi,
        "actions": _num(d["actions"]),
        "fcf": fcf,
        "sbc": _num(d["sbc"]),
        "fcf_apres_sbc": fas,
        "p_fcf": (capi / fcf) if capi and fcf and fcf > 0 else None,
        "p_fcf_sbc": (capi / fas) if capi and fas and fas > 0 else None,
        "devise": devise,
        "devise_fin": devise_fin,
        "rendements": {nom: _num(rendement_implicite(d["fcf_apres_sbc"], d["capi"], taux))
                       for nom, taux in SCENARIOS.items()},
        "date": _now(),
    }


def lire_tickers(path="tickers.txt"):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.split("#")[0].strip().upper() for l in f if l.split("#")[0].strip()]


def export_json(path, tickers):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            data = {}
    fiches = data.get("fiches") if isinstance(data, dict) else None
    if not isinstance(fiches, dict):
        fiches = {}
    for t in tickers:
        t = t.strip().upper()
        if not TICKER_RE.match(t):
            print(f"Ignoré (ticker invalide) : {t!r}")
            continue
        try:
            fiches[t] = fiche_dict(t)
            c = fiches[t]["rendements"].get("Central")
            print(f"{t} : scénario central {c:+.2f} %/an" if c is not None else f"{t} : scénario central n/d")
        except Exception as e:  # noqa: BLE001
            print(f"Erreur pour {t} : {e}")
            old = fiches.get(t)
            if isinstance(old, dict) and old.get("capi"):
                old["erreur_maj"] = str(e)
                old["date_tentative"] = _now()
            else:
                fiches[t] = {"ticker": t, "erreur": str(e), "date": _now()}
    out = {"updated": _now(),
           "scenarios": SCENARIOS,
           "periodes": ["Années 1–5", "Années 6–10", "Années 11–20", "Années 21–30", "Terminale"],
           "fiches": fiches}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, allow_nan=False)


def main():
    args = sys.argv[1:]
    if "--json" in args:
        k = args.index("--json")
        if k + 1 >= len(args):
            sys.exit("Usage : reverse-dcf.py --json data.json [TICKERS...]")
        chemin = args[k + 1]
        tickers = [t for t in args[:k] + args[k + 2:] if not t.startswith("-")]
        export_json(chemin, tickers or lire_tickers() or ["NVDA"])
        return
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for ticker in sys.argv[1:]:
        analyser(ticker.upper())


if __name__ == "__main__":
    main()
