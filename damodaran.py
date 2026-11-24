#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Damodaran — évaluation intrinsèque par les flux de trésorerie (DCF FCFF).

Usage : python3 damodaran.py NVDA [AUTRE_TICKER ...]

La méthode (Aswath Damodaran), en quatre temps : une histoire, des chiffres,
la confrontation des deux, puis la valeur.

  HISTOIRE → CHIFFRES. Pour chaque ticker, le script lit la situation actuelle
  (revenus, marge d'exploitation, rendement du capital — 12 mois, yfinance)
  puis projette les FCFF sur 10 ans en deux phases :
    - années 1 à 5 : croissance des revenus des estimations d'analystes
      (repli : croissance historique sur 3 ans), marge constante ;
    - années 6 à 10 : la croissance décroît linéairement vers la croissance
      terminale ; le réinvestissement suit le ratio ventes/capital de
      l'entreprise (combien de $ investis pour 1 $ de ventes nouvelles).
  La valeur terminale suppose un rendement du capital stable qui converge
  vers le coût du capital (la moitié de l'écart actuel est conservée au
  scénario central — un avantage concurrentiel s'érode, il ne dure pas
  éternellement), et une croissance terminale plafonnée (≤ taux sans risque
  et ≤ 2,5 %).

  COÛT DU CAPITAL, BÂTI PAR LE BAS. Taux sans risque = obligation 10 ans US ;
  coût des fonds propres = taux sans risque + bêta sectoriel (table intégrée,
  approximative, d'après les données publiées par Damodaran) réendetté selon
  la structure de l'entreprise × prime de risque actions (4,50 %) ; coût de
  la dette = taux sans risque + écart synthétique selon la couverture des
  intérêts, après impôt.

  CONFRONTATION. La fiche rapporte les contrôles : revenu implicite à l'an 10,
  rendement du capital implicite à l'an 10 contre coût du capital, et part de
  la valeur terminale dans la valeur totale (drapeau si > 75 %).

  VALEUR. Trois histoires chiffrées (Pessimiste 25 %, Centrale 50 %,
  Optimiste 25 %) donnent trois valeurs par action et une valeur pondérée.
  Écart = valeur centrale contre cours actuel.

  Cas « non significatif » (nd) plutôt qu'un chiffre fabriqué : résultat
  d'exploitation négatif (il faudrait normaliser la marge), entreprise
  financière (le FCFF ne s'applique pas — Damodaran évalue les financières
  par les flux aux actionnaires), capital investi négatif ou devises
  différentes entre le cours et les états financiers.

Tous les pourcentages des fiches JSON sont en unités de % (9,2 = 9,2 %).
"""

import sys

import yfinance as yf

# ------------------------------------------------------------- paramètres
PRIME_RISQUE = 4.50          # prime de risque actions (%), réglable
CROISSANCE_TERMINALE_MAX = 2.5   # plafond de la croissance terminale (%)
PROBAS = {"Pessimiste": 25, "Centrale": 50, "Optimiste": 25}

# Bêtas désendettés par secteur Yahoo — valeurs approximatives d'après les
# jeux de données publiés par Aswath Damodaran (moyennes US). À ajuster.
BETA_SECTEUR = {
    "Technology": 1.02,
    "Healthcare": 0.86,
    "Consumer Cyclical": 0.94,
    "Consumer Defensive": 0.64,
    "Industrials": 0.87,
    "Energy": 0.86,
    "Basic Materials": 0.92,
    "Real Estate": 0.66,
    "Communication Services": 0.84,
    "Utilities": 0.46,
    # "Financial Services" : volontairement absent — voir les cas nd.
}


# ------------------------------------------------------------------ outils
def _f(x):
    """float ou None (NaN/infini -> None)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def ligne(df, noms):
    """Première ligne trouvée dans un état financier, selon les noms possibles."""
    if df is None or getattr(df, "empty", True):
        return None
    for nom in noms:
        if nom in df.index:
            return df.loc[nom]
    return None


def somme_ttm(serie, n=4):
    """Somme des n dernières périodes (flux)."""
    if serie is None:
        return None
    vals = [_f(v) for v in list(serie.iloc[:n])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals)


def dernier(serie):
    if serie is None:
        return None
    for v in list(serie.iloc[:]):
        v = _f(v)
        if v is not None:
            return v
    return None


_RF_CACHE = {}


def taux_sans_risque():
    """Rendement de l'obligation US 10 ans (%), via ^TNX. Repli : 4,25 %."""
    if "rf" in _RF_CACHE:
        return _RF_CACHE["rf"]
    rf = 4.25
    try:
        h = yf.Ticker("^TNX").history(period="1mo")
        if h is not None and not h.empty:
            v = _f(h["Close"].iloc[-1])
            if v:
                rf = v / 10.0 if v > 15 else v  # ^TNX est coté en % x 10
    except Exception:  # noqa: BLE001
        pass
    rf = min(max(rf, 1.0), 8.0)
    _RF_CACHE["rf"] = rf
    return rf


def spread_synth(couverture):
    """Écart de crédit (%) selon la couverture des intérêts (note synthétique)."""
    if couverture is None:
        return 1.75
    if couverture > 8.5:
        return 0.60
    if couverture > 6.5:
        return 0.85
    if couverture > 5.5:
        return 1.00
    if couverture > 4.25:
        return 1.25
    if couverture > 3.0:
        return 1.75
    if couverture > 2.5:
        return 2.50
    if couverture > 2.0:
        return 3.50
    if couverture > 1.75:
        return 4.50
    if couverture > 1.5:
        return 6.00
    if couverture > 1.25:
        return 8.00
    return 10.00


# ------------------------------------------------------------------ données
def donnees(ticker):
    t = yf.Ticker(ticker)
    info = t.info or {}
    prix = info.get("regularMarketPrice") or info.get("currentPrice")
    actions = info.get("sharesOutstanding")
    capi = info.get("marketCap") or (prix * actions if prix and actions else None)
    nom = info.get("longName") or info.get("shortName") or ticker
    if not prix or not actions:
        return None
    secteur = info.get("sector")
    if secteur == "Financial Services":
        # Retour minimal : evaluer() rendra le cas « nd » financier sans
        # exiger les lignes d'états financiers (souvent absentes pour les banques).
        return {"nom": nom, "secteur": secteur, "prix": float(prix),
                "actions": float(actions), "capi": float(capi),
                "devise": info.get("currency"),
                "devise_fin": info.get("financialCurrency")}

    # Compte de résultat — 12 mois (trimestriel, repli annuel)
    inc = t.quarterly_income_stmt
    revenus = somme_ttm(ligne(inc, ["Total Revenue"]))
    ebit = somme_ttm(ligne(inc, ["Operating Income"]))
    impots = somme_ttm(ligne(inc, ["Tax Provision"]))
    avant_impot = somme_ttm(ligne(inc, ["Pretax Income"]))
    interets = somme_ttm(ligne(inc, ["Interest Expense"]))
    if revenus is None or ebit is None:
        inc = t.income_stmt
        revenus = dernier(ligne(inc, ["Total Revenue"]))
        ebit = dernier(ligne(inc, ["Operating Income"]))
        impots = dernier(ligne(inc, ["Tax Provision"]))
        avant_impot = dernier(ligne(inc, ["Pretax Income"]))
        interets = dernier(ligne(inc, ["Interest Expense"]))
    if revenus is None or ebit is None:
        return None

    # Bilan — dernière période
    bs = t.quarterly_balance_sheet
    if bs is None or bs.empty:
        bs = t.balance_sheet
    dette = dernier(ligne(bs, ["Total Debt"]))
    if dette is None:
        lt = dernier(ligne(bs, ["Long Term Debt"]))
        ct = dernier(ligne(bs, ["Current Debt"]))
        if lt is None and ct is None:
            # Aucune ligne de dette au bilan : entreprise sans dette
            # déclarée (ex. NXT, ANET, ISRG) — dette = 0, pas une erreur.
            dette = 0.0
        else:
            dette = (lt or 0.0) + (ct or 0.0)
    cash = dernier(ligne(bs, ["Cash Cash Equivalents And Short Term Investments",
                              "Cash And Cash Equivalents"]))
    fonds = dernier(ligne(bs, ["Stockholders Equity",
                               "Total Equity Gross Minority Interest"]))
    if dette is None or cash is None or fonds is None:
        return None

    # Croissance phase 1 : estimation de revenus des analystes pour l'année
    # SUIVANTE (+1y) de préférence — l'année en cours (0y) peut être une année
    # de pic (matières, cycle) qu'il ne faut pas projeter cinq ans.
    g1 = None
    try:
        est = t.get_revenue_estimate()
        if est is not None and not est.empty and "growth" in est.columns:
            for k in ("+1y", "0y"):
                if k in est.index:
                    g1 = _f(est.loc[k, "growth"])
                    if g1 is not None:
                        g1 *= 100.0
                        break
    except Exception:  # noqa: BLE001
        g1 = None
    if g1 is None:
        # Repli : croissance annuelle composée des revenus sur ~3 ans
        try:
            ann = t.income_stmt
            rev = ligne(ann, ["Total Revenue"])
            vals = [_f(v) for v in list(rev.iloc[:])] if rev is not None else []
            vals = [v for v in vals if v]
            if len(vals) >= 2 and vals[-1] > 0:
                n = min(3, len(vals) - 1)
                g1 = ((vals[0] / vals[n]) ** (1.0 / n) - 1.0) * 100.0
        except Exception:  # noqa: BLE001
            g1 = None
    if g1 is None:
        g1 = 5.0
    g1 = min(max(g1, -5.0), 30.0)

    taux_impot = 0.25
    if impots is not None and avant_impot and avant_impot > 0:
        taux_impot = min(max(impots / avant_impot, 0.0), 0.40)

    return {
        "nom": nom, "secteur": secteur, "prix": float(prix),
        "actions": float(actions), "capi": float(capi),
        "devise": info.get("currency"), "devise_fin": info.get("financialCurrency"),
        "revenus": float(revenus), "ebit": float(ebit),
        "dette": float(dette), "cash": float(cash), "fonds_propres": float(fonds),
        "interets": abs(float(interets)) if interets else None,
        "taux_impot": taux_impot, "croissance_1_5": g1,
        "beta_yahoo": _f(info.get("beta")),
    }


# ------------------------------------------------------------------ calcul
def projeter(d, wacc, g1, marge_cible, g_term, roc_stable, s2c):
    """Projette les FCFF sur 10 ans + valeur terminale. Tout en décimal."""
    rev = d["revenus"]
    ic = d["capital_investi"]
    t = d["taux_impot"]
    marge0 = d["ebit"] / rev
    va, ic_fin, rev_fin, nopat_fin = 0.0, ic, rev, None
    for an in range(1, 11):
        if an <= 5:
            g = g1
            marge = marge0 + (marge_cible - marge0) * an / 5.0
        else:
            g = g1 + (g_term - g1) * (an - 5) / 5.0
            marge = marge_cible
        rev_new = rev * (1 + g)
        ebit = rev_new * marge
        nopat = ebit * (1 - t)
        reinvest = (rev_new - rev) / s2c
        fcff = nopat - reinvest
        va += fcff / (1 + wacc) ** an
        ic_fin += reinvest
        rev, rev_fin, nopat_fin = rev_new, rev_new, nopat
    # Année 11 (terminale) : croissance g_term, réinvestissement = g / ROC stable
    ebit11 = (rev * marge_cible) * (1 + g_term)
    nopat11 = ebit11 * (1 - t)
    fcff11 = nopat11 * (1 - g_term / roc_stable) if roc_stable > g_term else 0.0
    tv = fcff11 / (wacc - g_term) if wacc > g_term else 0.0
    va_tv = tv / (1 + wacc) ** 10
    roc10 = (nopat_fin / ic_fin) if (nopat_fin is not None and ic_fin > 0) else None
    return {"va_operations": va + va_tv, "va_terminale": va_tv,
            "revenu_an10": rev_fin, "roc_an10": roc10}


def evaluer(d):
    """Retourne (fiche_partielle, None) ou (None, raison_nd)."""
    if d["secteur"] == "Financial Services":
        return None, ("Entreprise financière : le DCF sur flux à l'entreprise ne "
                      "s'applique pas (Damodaran évalue les financières par les "
                      "flux aux actionnaires / dividendes).")
    if d["devise"] and d["devise_fin"] and d["devise"] != d["devise_fin"]:
        return None, (f"Devises différentes : cours en {d['devise']}, états "
                      f"financiers en {d['devise_fin']} — non converti, valeur non calculée.")
    if d["ebit"] <= 0:
        return None, ("Résultat d'exploitation (12 mois) négatif ou nul : la méthode "
                      "exige une marge normalisée avant de projeter — valeur non calculée.")
    ic = d["fonds_propres"] + d["dette"] - d["cash"]
    if ic <= 0:
        return None, ("Capital investi comptable négatif ou nul (capitaux propres "
                      "comptables négatifs, effet des rachats massifs) : la méthode "
                      "exige un capital investi reconstruit — valeur non calculée.")
    d["capital_investi"] = ic

    rf = taux_sans_risque() / 100.0
    erp = PRIME_RISQUE / 100.0
    t = d["taux_impot"]
    dette, capi = d["dette"], d["capi"]
    de = dette / capi if capi else 0.0

    beta_u = BETA_SECTEUR.get(d["secteur"] or "")
    if beta_u is None:
        if d["beta_yahoo"]:
            beta_u = d["beta_yahoo"] / (1 + (1 - t) * de)
        else:
            beta_u = 0.90
    beta_l = beta_u * (1 + (1 - t) * de)
    ke = rf + beta_l * erp
    couv = (d["ebit"] / d["interets"]) if d["interets"] else None
    kd = (rf + spread_synth(couv) / 100.0) * (1 - t)
    w_d = dette / (dette + capi) if (dette + capi) else 0.0
    wacc = ke * (1 - w_d) + kd * w_d

    g_term_base = min(CROISSANCE_TERMINALE_MAX / 100.0, rf)
    if wacc <= g_term_base + 0.01:
        g_term_base = max(wacc - 0.015, 0.0)

    nopat0 = d["ebit"] * (1 - t)
    roc0 = nopat0 / ic
    s2c = min(max(d["revenus"] / ic, 0.4), 3.5)
    marge0 = d["ebit"] / d["revenus"]
    g1 = d["croissance_1_5"] / 100.0

    roc_stable_central = wacc if roc0 <= wacc else min(wacc + 0.5 * (roc0 - wacc), 0.30)
    scen = {
        "Pessimiste": {
            "g1": g1 * 0.5 if g1 >= 0 else g1 * 1.5,
            "marge": max(marge0 - 0.03, 0.01),
            "g_term": max(g_term_base - 0.005, 0.0),
            "roc_stable": wacc,
        },
        "Centrale": {
            "g1": g1, "marge": marge0,
            "g_term": g_term_base, "roc_stable": roc_stable_central,
        },
        "Optimiste": {
            "g1": min(g1 * 1.3, 0.35) if g1 >= 0 else g1 * 0.5,
            "marge": marge0 + 0.02,
            "g_term": g_term_base,
            "roc_stable": min(roc_stable_central + 0.03, 0.35),
        },
    }
    resultats, details = {}, {}
    for nom_s, p in scen.items():
        r = projeter(d, wacc, p["g1"], p["marge"], p["g_term"], p["roc_stable"], s2c)
        valeur_eq = r["va_operations"] + d["cash"] - d["dette"]
        resultats[nom_s] = valeur_eq / d["actions"]
        details[nom_s] = r
    ponderee = sum(resultats[s] * PROBAS[s] / 100.0 for s in PROBAS)
    part_tv = (details["Centrale"]["va_terminale"] / details["Centrale"]["va_operations"]
               if details["Centrale"]["va_operations"] > 0 else None)

    def sens():
        grille = []
        for dw in (-0.01, 0.0, 0.01):
            rang = []
            for dg in (-0.005, 0.0, 0.005):
                gt = min(max(g_term_base + dg, 0.0), wacc + dw - 0.005)
                r = projeter(d, wacc + dw, g1, marge0, gt, roc_stable_central, s2c)
                rang.append((r["va_operations"] + d["cash"] - d["dette"]) / d["actions"])
            grille.append(rang)
        return {"wacc": [round((wacc + dw) * 100, 2) for dw in (-0.01, 0.0, 0.01)],
                "g": [round((g_term_base + dg) * 100, 2) for dg in (-0.005, 0.0, 0.005)],
                "valeurs": [[round(v, 2) for v in rang] for rang in grille]}

    fiche = {
        "valeur": resultats["Centrale"],
        "valeur_ponderee": ponderee,
        "scenarios": {s: {"valeur": resultats[s], "proba": PROBAS[s]} for s in PROBAS},
        "hypotheses": {
            "revenus": d["revenus"], "marge_actuelle": marge0 * 100,
            "croissance_1_5": g1 * 100, "croissance_terminale": g_term_base * 100,
            "taux_sans_risque": rf * 100, "prime_risque": PRIME_RISQUE,
            "beta_sectoriel": beta_u, "beta": beta_l,
            "cout_fonds_propres": ke * 100, "cout_dette_apres_impot": kd * 100,
            "wacc": wacc * 100, "taux_impot": t * 100,
            "roc_actuel": roc0 * 100, "roc_stable": roc_stable_central * 100,
            "ventes_sur_capital": s2c, "dette_nette": d["dette"] - d["cash"],
        },
        "controles": {
            "revenu_an10": details["Centrale"]["revenu_an10"],
            "multiple_revenu_10ans": details["Centrale"]["revenu_an10"] / d["revenus"],
            "roc_an10": (details["Centrale"]["roc_an10"] * 100
                         if details["Centrale"]["roc_an10"] is not None else None),
            "part_valeur_terminale": part_tv * 100 if part_tv is not None else None,
        },
        "sensibilite": sens(),
    }
    return fiche, None


def fmt_montant(x):
    if abs(x) >= 1e9:
        return f"{x / 1e9:,.2f} Md$".replace(",", " ")
    return f"{x / 1e6:,.0f} M$".replace(",", " ")


# ------------------------------------------------------------------- sortie
def analyser(ticker):
    print("=" * 64)
    d = donnees(ticker)
    if d is None:
        print(f"{ticker} : données introuvables ou incomplètes.")
        return
    print(f"{d['nom']} · {ticker} — {d['prix']:,.2f} $")
    fiche, nd = evaluer(d)
    if nd:
        print(f"Non significatif : {nd}")
        return
    h = fiche["hypotheses"]
    print(f"Revenus 12 mois : {fmt_montant(h['revenus'])} | marge {h['marge_actuelle']:.1f} % | "
          f"ROC actuel {h['roc_actuel']:.1f} %")
    print(f"Coût du capital : {h['wacc']:.2f} % (sans risque {h['taux_sans_risque']:.2f} %, "
          f"bêta {h['beta']:.2f}, dette après impôt {h['cout_dette_apres_impot']:.2f} %)")
    print(f"Histoire centrale : croissance {h['croissance_1_5']:+.1f} %/an (ans 1-5), "
          f"terminale {h['croissance_terminale']:.1f} %, ROC stable {h['roc_stable']:.1f} %")
    for nom_s in ("Pessimiste", "Centrale", "Optimiste"):
        v = fiche["scenarios"][nom_s]["valeur"]
        print(f"  {nom_s:<11} {v:>10,.2f} $  ({(v / d['prix'] - 1) * 100:+.1f} % vs cours)")
    print(f"Valeur pondérée : {fiche['valeur_ponderee']:,.2f} $ "
          f"({(fiche['valeur_ponderee'] / d['prix'] - 1) * 100:+.1f} % vs cours)")
    c = fiche["controles"]
    print(f"Contrôles : revenu an 10 x{c['multiple_revenu_10ans']:.1f}, "
          f"part de la valeur terminale {c['part_valeur_terminale']:.0f} %")


# ---------------------------------------------------------------------------
# Export pour l'appli — mêmes conventions que reverse-dcf.py.
#   python3 damodaran.py --json data-valeur.json [TICKERS...]
# ---------------------------------------------------------------------------
import json
import os
import re
from datetime import datetime, timezone

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


def _recherche(q):
    """Résultats Yahoo Search pour une requête (nom de compagnie ou ticker)."""
    try:
        r = yf.Search(q, max_results=8, news_count=0)
        return [x for x in (r.quotes or []) if x.get("symbol")]
    except Exception:  # noqa: BLE001
        return []


def resoudre(requete):
    """Nom de compagnie ou ticker -> symbole Yahoo.

    Priorité : symbole exact, puis action ordinaire dont le nom correspond,
    puis première action ordinaire. Repli : la requête en majuscules
    (comportement historique si Yahoo Search ne répond pas).
    """
    q = " ".join(str(requete).split()).strip()
    if not q:
        return q
    quotes = _recherche(q)
    if not quotes:
        return q.upper()
    qu = q.upper()
    for x in quotes:
        if str(x["symbol"]).upper() == qu:
            return qu
    actions = [x for x in quotes if x.get("quoteType") == "EQUITY"]
    ql = q.lower()
    for x in actions:
        nom = (x.get("shortname") or x.get("longname") or "").lower()
        if nom and ql in nom:
            return str(x["symbol"]).upper()
    if actions:
        return str(actions[0]["symbol"]).upper()
    return str(quotes[0]["symbol"]).upper()


def entrees_brutes(liste):
    """Regroupe les arguments en requêtes.

    « NVDA MSFT » = deux tickers si chaque mot est un symbole exact chez
    Yahoo ; sinon, la chaîne entière est un seul nom de compagnie
    (« Mueller Industries »). Virgules acceptées comme séparateurs.
    """
    s = " ".join(liste).strip()
    if not s:
        return []
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    mots = s.split()
    if len(mots) > 1 and all(
            any(str(x["symbol"]).upper() == m.upper() for x in _recherche(m))
            for m in mots):
        return mots
    return [s]


def _num(x):
    return _f(x)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fiche_dict(ticker):
    d = donnees(ticker)
    if d is None:
        raise ValueError("données introuvables ou incomplètes (ticker inconnu ou source indisponible)")
    fiche = {
        "ticker": ticker, "nom": str(d["nom"]), "secteur": d["secteur"],
        "prix": _num(d["prix"]), "capi": _num(d["capi"]), "actions": _num(d["actions"]),
        "devise": d["devise"], "devise_fin": d["devise_fin"], "date": _now(),
    }
    res, nd = evaluer(d)
    if nd:
        fiche["nd"] = nd
        return fiche
    fiche.update({
        "valeur": _num(res["valeur"]),
        "valeur_ponderee": _num(res["valeur_ponderee"]),
        "ecart": _num((res["valeur"] / d["prix"] - 1) * 100),
        "ecart_pondere": _num((res["valeur_ponderee"] / d["prix"] - 1) * 100),
        "scenarios": {s: {"valeur": _num(v["valeur"]), "proba": v["proba"]}
                      for s, v in res["scenarios"].items()},
        "hypotheses": {k: _num(v) for k, v in res["hypotheses"].items()},
        "controles": {k: _num(v) for k, v in res["controles"].items()},
        "sensibilite": res["sensibilite"],
    })
    return fiche


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
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict):
        aliases = {}
    for brut in tickers:
        q = " ".join(str(brut).split()).strip()
        if not q:
            continue
        t = resoudre(q)
        if not TICKER_RE.match(t):
            print(f"Ignoré (aucun symbole trouvé) : {brut!r}")
            continue
        if t != q.upper():
            aliases[q.upper()] = t
            print(f"« {q} » → {t}")
        try:
            fiches[t] = fiche_dict(t)
            f = fiches[t]
            if f.get("nd"):
                print(f"{t} : non significatif")
            else:
                print(f"{t} : valeur centrale {f['valeur']:,.2f} $ (écart {f['ecart']:+.1f} %)")
        except Exception as e:  # noqa: BLE001
            print(f"Erreur pour {t} : {e}")
            old = fiches.get(t)
            if isinstance(old, dict) and old.get("capi"):
                old["erreur_maj"] = str(e)
                old["date_tentative"] = _now()
            else:
                fiches[t] = {"ticker": t, "erreur": str(e), "date": _now()}
    out = {"updated": _now(),
           "parametres": {"prime_risque": PRIME_RISQUE,
                          "croissance_terminale_max": CROISSANCE_TERMINALE_MAX,
                          "probas": PROBAS},
           "aliases": aliases,
           "fiches": fiches}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, allow_nan=False)


def main():
    args = sys.argv[1:]
    if "--json" in args:
        k = args.index("--json")
        if k + 1 >= len(args):
            sys.exit("Usage : damodaran.py --json data.json [TICKERS...]")
        chemin = args[k + 1]
        tickers = entrees_brutes([t for t in args[:k] + args[k + 2:]
                                   if not t.startswith("-")])
        export_json(chemin, tickers or lire_tickers() or ["NVDA"])
        return
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for ticker in sys.argv[1:]:
        analyser(ticker.upper())


if __name__ == "__main__":
    main()
