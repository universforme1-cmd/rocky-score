#!/usr/bin/env python3
"""
Trident — valorisation d'une action (méthode Trident, Romain Paillet).

Formule (vérifiée sur Netflix, 27 nov. 2026) :
    BPA dans 5 ans  = BPA aujourd'hui x (1 + g)^5
    Prix dans 5 ans = BPA 5 ans x PER final
    Rendement estimé = (prix 5 ans / prix actuel)^(1/5) - 1
    Prix à payer (pour 15 %/an) = prix 5 ans / 1,15^5

Régularité (mesure validée : MA = 99,9 % calculé vs 99 % affiché par
Trident) : R² de la régression du log(BPA) sur l'historique annuel du
BPA dilué. Un exercice à BPA <= 0 rend la régularité n/d (les bénéfices
n'ont pas « grimpé régulièrement »).

Défauts :
  - g (croissance sur 5 ans) = parcours BPA forward (CAGR des taux
    annuels estimés par Yahoo), plafonné à 25 % ; sinon CAGR historique
    du BPA, plafonné à 25 %.
  - PER final = 17x (celui de l'exemple Netflix).
  - BPA aujourd'hui = BPA 12 mois (somme des 4 derniers trimestres,
    Yahoo) ; repli = dernier BPA annuel.

Vérification Netflix (Trident) : 2,95 $ x ~19 %/an -> 7,02 $ x 17
= 119 $ vs 59,4 $ -> 15 %/an. Le « 18 %/an » affiché est arrondi.
(Le calcul exact avec 2,9 $ et 18 % donne 6,63 $ -> 113 $ -> 13,8 %/an.)

Usage :
    python3 trident.py NFLX
    python3 trident.py NFLX MA GOOGL
    python3 trident.py --json data-trident.json NFLX MA
    python3 trident.py --json data-trident.json "Mueller Industries"

Données : Yahoo Finance (gratuit, sans clé). Les chiffres peuvent différer
légèrement d'une source à l'autre — la fiche indique la source de chaque
valeur. L'historique annuel du BPA est limité à ce que Yahoo expose
(4 à 5 exercices en général) : la régularité est calculée sur les
exercices disponibles (au moins 4).

Limites connues :
  - info['trailingEps'] de Yahoo est parfois faux (ex. GOOGL, NVDA) :
    on ne l'utilise jamais, le BPA 12 mois est recalculé depuis les
    trimestres. Le trimestriel est lui-même parfois corrompu (GOOGL :
    9,11 $ et 5,11 $ au T1/T2 2026) : si la somme des 4 trimestres
    s'écarte de plus de 50 % du dernier BPA annuel, on retombe sur
    l'annuel. Le BPA 12 mois de Yahoo peut donc différer de celui de
    Trident (ex. NFLX : 3,46 $ Yahoo vs 2,95 $ Trident) : la formule
    est la même, la source diffère.
  - Si la devise des états financiers diffère de celle du cours
    (ex. ASML : états en EUR, ADR en USD), la projection mélange les
    devises : la fiche le signale (devise_mix) et l'appli affiche un
    avertissement — les chiffres ne sont pas convertis.
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

import yfinance as yf

PER_FINAL_DEFAUT = 17.0
CIBLE_RENDEMENT = 0.15
PLAFOND_G = 0.25
ANNEES_PROJECTION = 5
MIN_EXERCICES_REGULARITE = 4


def _num(x):
    """Convertit les types numpy/pandas en float JSON (None si absent/NaN)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pct(x):
    """Normalise une croissance : 0.42 ou 42 -> 0.42."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if abs(x) > 1.5:
        x = x / 100.0
    return x


def fmt_money(x, cur=""):
    if x is None:
        return "n/d"
    ax = abs(x)
    if ax >= 1e9:
        return f"{x / 1e9:,.2f} Md$".replace(",", " ")
    if ax >= 1e6:
        return f"{x / 1e6:,.0f} M$".replace(",", " ")
    return f"{x:,.2f} {cur}".replace(",", " ")


def _row(df, names):
    """Première ligne présente parmi names (tolérant aux intitulés Yahoo)."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _annee(colonne):
    """Année d'une colonne de financials (Timestamp de fin d'exercice)."""
    try:
        return int(str(colonne)[:4])
    except (TypeError, ValueError):
        return None


def _serie_eps_annuelle(tk):
    """Historique annuel du BPA dilué : [(année, bpa), ...] chronologique.

    Ligne « Diluted EPS » d'abord ; repli = bénéfice net / actions
    diluées moyennes (même calcul que rocky-score.py).
    """
    try:
        fin = tk.financials
    except Exception:  # noqa: BLE001
        return []
    if fin is None or fin.empty:
        return []
    row = _row(fin, ("Diluted EPS",))
    if row is None:
        try:
            ni = _row(fin, ("Net Income",))
            sh = _row(fin, ("Diluted Average Shares",))
            if ni is not None and sh is not None:
                row = ni / sh
        except Exception:  # noqa: BLE001
            row = None
    if row is None:
        return []
    out = []
    for col, val in row.items():
        v = _num(val)
        an = _annee(col)
        if v is not None and an is not None:
            out.append((an, v))
    out.sort(key=lambda t: t[0])
    return out


def _bpa_ttm(tk, eps_annuel_recent=None):
    """BPA 12 mois = somme des 4 derniers BPA trimestriels, validé.

    On ne se fie pas à info['trailingEps'] (parfois faux : GOOGL, NVDA).
    Le trimestriel Yahoo est lui-même parfois corrompu (ex. GOOGL :
    9,11 $ et 5,11 $ au T1/T2 2026, impossibles pour un BPA trimestriel
    ~2,50 $) : si la somme trimestrielle s'écarte de plus de 50 % du
    dernier BPA annuel, on retombe sur l'annuel (vérifié).
    Repli : dernier BPA annuel.
    """
    ttm, note = None, None
    try:
        qf = tk.quarterly_financials
    except Exception:  # noqa: BLE001
        qf = None
    if qf is not None and not qf.empty:
        row = _row(qf, ("Diluted EPS",))
        if row is None:
            try:
                ni = _row(qf, ("Net Income",))
                sh = _row(qf, ("Diluted Average Shares",))
                if ni is not None and sh is not None:
                    row = ni / sh
            except Exception:  # noqa: BLE001
                row = None
        if row is not None:
            vals = [_num(v) for v in row.iloc[:4]]
            vals = [v for v in vals if v is not None]
            if vals:
                ttm = sum(vals)
                note = f"somme des {len(vals)} derniers trimestres (Yahoo)"
    if (ttm is not None and eps_annuel_recent is not None
            and eps_annuel_recent > 0):
        ratio = ttm / eps_annuel_recent
        if ratio > 1.5 or ratio < 0.5:
            return eps_annuel_recent, ("dernier BPA annuel (Yahoo) — "
                                       "trimestriel écarté (incohérent)")
    if ttm is not None:
        return ttm, note
    hist = _serie_eps_annuelle(tk)
    if hist:
        return hist[-1][1], "dernier BPA annuel (Yahoo)"
    return None, None


def _prix_annuels(tk, annee_min=None):
    """Clôtures de fin d'année : [(année, prix), ...] chronologique.

    Depuis l'historique mensuel 10 ans (dernière clôture de chaque année).
    """
    try:
        hist = tk.history(period="10y", interval="1mo")["Close"]
    except Exception:  # noqa: BLE001
        return []
    if hist is None or len(hist) == 0:
        return []
    par_annee = {}
    for ts, val in hist.items():
        try:
            an = int(str(ts)[:4])
        except (TypeError, ValueError):
            continue
        v = _num(val)
        if v is None:
            continue
        # l'index mensuel est chronologique : la dernière valeur de
        # l'année écrase les précédentes -> clôture de fin d'année.
        par_annee[an] = v
    out = sorted(par_annee.items())
    if annee_min is not None:
        out = [(a, p) for a, p in out if a >= annee_min]
    return out


def _parcours_bpa(tk, info):
    """Parcours BPA forward (estimations Yahoo) : [(label, g), ...] et CAGR.

    Même lecture que rocky-score.py : les années dont la base (BPA de
    l'année précédente) est <= 0 sont exclues (croissance non significative).
    """
    path, nd = [], []
    try:
        est = tk.earnings_estimate
        if est is None or est.empty or "growth" not in est.columns:
            return path, nd
        rows = [(str(idx), row) for idx, row in est.iterrows()
                if str(idx).endswith("y")]

        def _horizon(lbl):
            try:
                return int(lbl.replace("y", "").replace("+", ""))
            except ValueError:
                return 99

        rows.sort(key=lambda t: _horizon(t[0]))
        levels = {}
        for lbl, row in rows:
            try:
                avg = row.get("avg")
                levels[lbl] = float(avg) if avg else None
            except (TypeError, ValueError):
                levels[lbl] = None
        prev_level = info.get("trailingEps")
        for lbl, row in rows:
            g = pct(row.get("growth"))
            if g is None:
                continue
            if prev_level is not None and prev_level <= 0:
                nd.append(lbl)
            else:
                path.append((lbl, g))
            if levels.get(lbl) is not None:
                prev_level = levels[lbl]
    except Exception:  # noqa: BLE001
        pass
    return path, nd


def _cagr_parcours(path):
    if not path:
        return None
    comp = 1.0
    for _, g in path:
        comp *= (1 + g)
    return comp ** (1.0 / len(path)) - 1


def regularite(eps):
    """R² de la régression du log(BPA) sur le temps (0 à 1, ou None).

    eps : liste chronologique de BPA annuels. Tout BPA <= 0 -> n/d.
    Au moins 4 exercices requis.
    """
    if not eps or len(eps) < MIN_EXERCICES_REGULARITE:
        return None
    if any(v is None or v <= 0 for v in eps):
        return None
    n = len(eps)
    xs = list(range(n))
    ys = [math.log(v) for v in eps]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot == 0:
        return 1.0
    return 1 - ss_res / ss_tot


def cagr_eps(eps):
    """CAGR du BPA sur l'historique annuel (None si BPA <= 0)."""
    if not eps or len(eps) < 2:
        return None
    if any(v is None or v <= 0 for v in eps):
        return None
    return (eps[-1] / eps[0]) ** (1.0 / (len(eps) - 1)) - 1


def projection(eps_base, g, per_final, prix):
    """Projection Trident sur 5 ans. Renvoie un dict de chiffres."""
    eps5 = eps_base * (1 + g) ** ANNEES_PROJECTION
    prix5 = eps5 * per_final
    rend = (prix5 / prix) ** (1.0 / ANNEES_PROJECTION) - 1 if prix else None
    payer = prix5 / (1 + CIBLE_RENDEMENT) ** ANNEES_PROJECTION
    return {
        "eps_5ans": eps5,
        "prix_5ans": prix5,
        "rendement_estime": rend,
        "prix_a_payer": payer,
    }


def fetch(ticker):
    """Récupère les données Yahoo pour un ticker. Renvoie un dict."""
    tk = yf.Ticker(ticker)
    d = {"ticker": ticker.upper(), "errors": []}
    try:
        info = tk.info or {}
    except Exception as e:  # noqa: BLE001
        info = {}
        d["errors"].append(f"info: {e}")
    d["info"] = info
    fi = None
    try:
        fi = tk.fast_info
    except Exception:  # noqa: BLE001
        pass
    d["fast"] = fi

    d["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    if d["price"] is None and fi is not None:
        d["price"] = getattr(fi, "last_price", None)
    d["currency"] = info.get("currency") or (getattr(fi, "currency", None) or "")
    d["devise_fin"] = info.get("financialCurrency") or ""
    d["name"] = info.get("longName") or info.get("shortName") or ticker.upper()
    d["sector"] = info.get("sector")
    d["industry"] = info.get("industry")
    d["cap"] = info.get("marketCap")
    if d["cap"] is None and fi is not None:
        d["cap"] = getattr(fi, "market_cap", None)

    d["eps_hist"] = _serie_eps_annuelle(tk)
    eps_annuel_recent = d["eps_hist"][-1][1] if d["eps_hist"] else None
    d["eps_ttm"], d["eps_base_note"] = _bpa_ttm(tk, eps_annuel_recent)
    annee_min = d["eps_hist"][0][0] if d["eps_hist"] else None
    d["prix_hist"] = _prix_annuels(tk, annee_min=annee_min)

    d["eps_path"], d["eps_nd"] = _parcours_bpa(tk, info)
    d["cagr_parcours"] = _cagr_parcours(d["eps_path"])
    eps_vals = [v for _, v in d["eps_hist"]]
    d["regularite"] = regularite(eps_vals)
    d["cagr_hist"] = cagr_eps(eps_vals)
    return d


def analyse(ticker):
    d = fetch(ticker)
    price = _num(d.get("price"))
    eps_base = _num(d.get("eps_ttm"))
    nd = None
    if price is None:
        raise ValueError("aucune donnée Yahoo pour ce symbole (ticker inconnu ou source indisponible)")
    if eps_base is None:
        nd = "BPA indisponible — historique des bénéfices introuvable (Yahoo)."
    elif eps_base <= 0:
        nd = "BPA 12 mois négatif ou nul : pas de bénéfices à projeter."
    elif not d["eps_hist"]:
        nd = "Historique annuel du BPA indisponible — régularité non calculable."
    # Croissance par défaut : parcours forward, sinon CAGR historique.
    g, g_source = None, None
    if d.get("cagr_parcours") is not None:
        g = max(0.0, min(d["cagr_parcours"], PLAFOND_G))
        g_source = "parcours BPA forward (Yahoo, plafonné à 25 %)"
    elif d.get("cagr_hist") is not None:
        g = max(0.0, min(d["cagr_hist"], PLAFOND_G))
        g_source = "CAGR historique du BPA (plafonné à 25 %)"
    if g is None and nd is None:
        nd = "Croissance indisponible — ni parcours forward ni historique du BPA."
    proj = None
    if nd is None:
        proj = projection(eps_base, g, PER_FINAL_DEFAUT, price)
    d["g_defaut"] = g
    d["g_source"] = g_source
    d["proj"] = proj
    d["nd"] = nd
    return d


def fiche_dict(ticker):
    """Même analyse que print_fiche, mais renvoie un dict sérialisable."""
    d = analyse(ticker)
    price = _num(d.get("price"))
    if price is None:
        raise ValueError("aucune donnée Yahoo pour ce symbole (ticker inconnu ou source indisponible)")
    eps_base = _num(d.get("eps_ttm"))
    devise_mix = bool(d.get("currency") and d.get("devise_fin")
                      and d["currency"] != d["devise_fin"])
    out = {
        "ticker": d["ticker"],
        "name": str(d.get("name") or d["ticker"]),
        "price": price,
        "currency": str(d.get("currency") or ""),
        "devise_fin": str(d.get("devise_fin") or ""),
        "devise_mix": devise_mix,
        "sector": d.get("sector"),
        "industry": d.get("industry"),
        "cap": _num(d.get("cap")),
        "eps_ttm": eps_base,
        "eps_base_note": d.get("eps_base_note") or "",
        "eps_hist": [{"annee": int(a), "eps": _num(v)} for a, v in d["eps_hist"]],
        "prix_hist": [{"annee": int(a), "prix": _num(v)} for a, v in d["prix_hist"]],
        "regularite": _num(d.get("regularite")),
        "cagr_hist": _num(d.get("cagr_hist")),
        "g_defaut": _num(d.get("g_defaut")),
        "g_source": d.get("g_source") or "",
        "per_final": PER_FINAL_DEFAUT,
        "eps_5ans": None,
        "prix_5ans": None,
        "rendement_estime": None,
        "prix_a_payer": None,
        "date": _now(),
    }
    if d.get("nd"):
        out["nd"] = d["nd"]
    if d.get("proj"):
        p = d["proj"]
        out["eps_5ans"] = _num(p["eps_5ans"])
        out["prix_5ans"] = _num(p["prix_5ans"])
        out["rendement_estime"] = _num(p["rendement_estime"])
        out["prix_a_payer"] = _num(p["prix_a_payer"])
    if devise_mix:
        out["devise_note"] = (f"Devises différentes : cours en {d.get('currency')}, "
                               f"états financiers en {d.get('devise_fin')}. Le BPA n'est "
                               f"pas converti : la projection mélange les devises et "
                               f"n'est pas comparable au cours.")
    return out


def print_fiche(ticker):
    d = analyse(ticker)
    cur = d.get("currency", "")
    price = d.get("price")
    print("=" * 64)
    print(f"{d['name']} ({d['ticker']}) — {price:,.2f} {cur}" if price else f"{d['name']} ({d['ticker']})")
    print("-" * 64)
    print("Historique du BPA dilué (annuel, Yahoo) :")
    for a, v in d["eps_hist"]:
        print(f"  {a} : {v:,.2f} {cur}")
    reg = d.get("regularite")
    print(f"Régularité (R² du log-BPA) : {reg * 100:.1f} %" if reg is not None
          else "Régularité : n/d (moins de 4 exercices ou BPA <= 0).")
    ch = d.get("cagr_hist")
    print(f"CAGR historique du BPA : {ch * 100:+.1f} %/an" if ch is not None
          else "CAGR historique du BPA : n/d.")
    print(f"BPA 12 mois : {d.get('eps_ttm'):,.2f} {cur} ({d.get('eps_base_note') or ''})"
          if d.get("eps_ttm") is not None else "BPA 12 mois : n/d.")
    if d.get("eps_path"):
        print("Parcours BPA forward : " + " · ".join(f"{k} {g * 100:+.0f} %" for k, g in d["eps_path"]))
    if d.get("nd"):
        print(f"Non significatif : {d['nd']}")
    if d.get("proj"):
        p = d["proj"]
        g = d["g_defaut"]
        print("-" * 64)
        print(f"Projection Trident (croissance {g * 100:.1f} %/an, PER final {PER_FINAL_DEFAUT:.0f}x) :")
        print(f"  BPA dans 5 ans : {p['eps_5ans']:,.2f} {cur}")
        print(f"  Prix dans 5 ans : {p['prix_5ans']:,.2f} {cur}")
        print(f"  Rendement estimé : {p['rendement_estime'] * 100:+.1f} %/an")
        print(f"  Prix à payer (15 %/an) : {p['prix_a_payer']:,.2f} {cur}")
        print(f"  Source de la croissance : {d.get('g_source') or ''}")
    if d.get("devise_fin") and d.get("currency") != d.get("devise_fin"):
        print(f"Attention : devise des états {d.get('devise_fin')} ≠ devise du cours {d.get('currency')} — projection non comparable.")
    print("=" * 64)


# ---------------------------------------------------------------------------
# Export pour l'appli — même pattern que rocky-score.py.
#   python3 trident.py --json data-trident.json            -> liste de tickers.txt
#   python3 trident.py --json data-trident.json NFLX MA   -> ces tickers seulement
# Les fiches sont fusionnées dans le fichier existant (clé = ticker).
# Relance automatique unique (attente 20 s) en cas d'échec d'une fiche.
# ---------------------------------------------------------------------------
import json
import os
import re
import time

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


def lire_tickers(path="tickers.txt"):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            t = line.split("#")[0].strip().upper()
            if t:
                out.append(t)
    return out


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
        fiche, erreur = None, None
        for tentative in (1, 2):
            try:
                fiche = fiche_dict(t)
                erreur = None
                break
            except Exception as e:  # noqa: BLE001
                erreur = e
                if tentative == 1:
                    print(f"{t} : échec ({e}) — nouvelle tentative dans 20 s…")
                    time.sleep(20)
        if fiche is not None:
            fiches[t] = fiche
            reg = fiche.get("regularite")
            reg_txt = f"{reg * 100:.1f} %" if reg is not None else "n/d"
            rend = fiche.get("rendement_estime")
            rend_txt = f"{rend * 100:+.1f} %/an" if rend is not None else "n/d"
            print(f"{t} : régularité {reg_txt} — rendement estimé {rend_txt}")
        else:
            e = erreur
            print(f"Erreur pour {t} : {e}")
            old = fiches.get(t)
            if isinstance(old, dict) and old.get("eps_hist"):
                # on garde la dernière bonne fiche, en signalant l'échec
                old["erreur_maj"] = str(e)
                old["date_tentative"] = _now()
            else:
                fiches[t] = {"ticker": t, "erreur": str(e), "date": _now()}
    out = {"updated": _now(), "aliases": aliases, "fiches": fiches}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, allow_nan=False)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--json" in args:
        k = args.index("--json")
        if k + 1 >= len(args):
            sys.exit("Usage : trident.py --json data-trident.json [TICKERS...]")
        chemin = args[k + 1]
        tickers = entrees_brutes([t for t in args[:k] + args[k + 2:]
                                   if not t.startswith("-")])
        export_json(chemin, tickers or lire_tickers() or ["NFLX"])
    else:
        tickers = [t for t in args if not t.startswith("-")] or ["NFLX"]
        for t in tickers:
            try:
                print_fiche(t)
            except Exception as e:  # noqa: BLE001
                print(f"Erreur pour {t} : {e}")
