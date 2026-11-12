#!/usr/bin/env python3
"""
Rocky Score — notation d'une action selon le filtre de Marc.

Usage :
    python3 rocky-score.py HOOD
    python3 rocky-score.py HOOD IESC AXON CSU.TO

Filtre (7 checks, 1 point chacun) :
  1. Croissance BPA forward (parcours, amendement du 11 nov. 2026) : chaque
     année forward >= +10 % (plancher) ET parcours composé >= +20 %/an —
     jamais la moyenne du parcours (elle flatte un bond déjà réalisé).
  2. Croissance revenus forward >= +20 %/an (3 ans)
  3. P/E forward <= 20x
  4. FCF TTM > 0 (critère bloquant)
  5. Dette nette / EBITDA <= 2x
  6. Ancre prix : décote >= 25 % vs sommet 52 sem. OU prix >= 20 % sous la
     valeur DCF. Financières et assureurs : décote seule (DCF non fiable).
  7. Capitalisation >= 2 Md$

Verdict : 7/7 = PASSE, 6/7 = PAS ENCORE, <= 5/7 = NON.
Hors note : DCF inverse (croissance impliquée par le prix) et zones R-DCA.
La valeur DCF alimente la 2e branche du check 6 (ancre prix).

Données : Yahoo Finance (gratuit, sans clé). Les chiffres peuvent différer
légèrement d'une source à l'autre (Nasdaq, stockanalysis) — la fiche indique
la source de chaque valeur.
"""

import sys
from datetime import datetime, timezone

import yfinance as yf

PLANCHER_BPA = 0.10   # amendement 11 nov. 2026 : aucune année forward sous +10 %
BARRE_PARCOURS = 0.20  # amendement 11 nov. 2026 : parcours BPA composé >= +20 %/an
MARGE_DCF_ANCRE = 0.20  # ancre prix : prix >= 20 % sous la valeur DCF (2e branche)
BARRE_REV = 0.20
BARRE_PE = 20.0
BARRE_DECOTE = 0.25
PLANCHER_CAP = 2e9
TAUX_ACTU = 0.10      # taux d'actualisation DCF
CROISS_TERMINALE = 0.03
ANNEES_DCF = 5


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
    return f"{x:,.0f} $".replace(",", " ")


def fmt_pct(x, dec=1):
    if x is None:
        return "n/d"
    return f"{x * 100:+.{dec}f} %"


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

    # Prix / sommet 52 sem. / capitalisation / devise
    d["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    if d["price"] is None and fi is not None:
        d["price"] = getattr(fi, "last_price", None)
    d["high52"] = info.get("fiftyTwoWeekHigh")
    if d["high52"] is None and fi is not None:
        d["high52"] = getattr(fi, "year_high", None)
    d["cap"] = info.get("marketCap")
    if d["cap"] is None and fi is not None:
        d["cap"] = getattr(fi, "market_cap", None)
    d["currency"] = info.get("currency") or (getattr(fi, "currency", None) or "")
    d["name"] = info.get("longName") or info.get("shortName") or ticker.upper()
    d["pe_fwd"] = info.get("forwardPE")
    d["sector"] = info.get("sector")
    d["industry"] = info.get("industry")
    d["beta"] = info.get("beta")
    d["shares"] = info.get("sharesOutstanding")
    d["ebitda"] = info.get("ebitda")
    d["debt"] = info.get("totalDebt")
    d["cash"] = info.get("totalCash")

    # FCF TTM : OCF - capex sur les 4 derniers trimestres (noms tolérants).
    # La ligne "Free Cash Flow" de Yahoo est souvent trouée (NaN) : on ne
    # l'utilise que si au moins 3 trimestres sur 4 sont renseignés.
    d["fcf"] = None

    def _row(df, names):
        for n in names:
            if n in df.index:
                return df.loc[n]
        return None

    try:
        cf = tk.quarterly_cashflow
        if cf is not None and not cf.empty:
            ocf = _row(cf, ("Total Cash From Operating Activities",
                            "Cash Flow From Operations",
                            "Operating Cash Flow",
                            "Net Cash Provided By Operating Activities"))
            cap = _row(cf, ("Capital Expenditures", "Capital Expenditure"))
            if ocf is not None and cap is not None:
                o, c = ocf.iloc[:4], cap.iloc[:4]
                if o.notna().sum() >= 3 and c.notna().sum() >= 3:
                    d["fcf"] = float(o.sum()) + float(c.sum())  # capex négatif
                    d["fcf_source"] = "4 derniers trimestres, OCF - capex (Yahoo)"
            if d["fcf"] is None and "Free Cash Flow" in cf.index:
                f = cf.loc["Free Cash Flow"].iloc[:4]
                if f.notna().sum() >= 3:
                    d["fcf"] = float(f.sum())
                    d["fcf_source"] = "4 derniers trimestres (Yahoo)"
    except Exception:  # noqa: BLE001
        pass
    if d["fcf"] is None:
        d["fcf"] = info.get("freeCashflow")
        if d["fcf"] is not None:
            d["fcf_source"] = "Yahoo (info)"
    # EBITDA de repli (compte de résultat annuel) si info ne l'a pas
    if d["ebitda"] is None:
        try:
            fin = tk.financials
            if fin is not None and "EBITDA" in fin.index:
                d["ebitda"] = float(fin.loc["EBITDA"].iloc[0])
        except Exception:  # noqa: BLE001
            pass

    # Parcours annuel BPA (estimations) — examen annuel, pas moyenne 3 ans
    d["eps_path"] = []
    try:
        est = tk.earnings_estimate
        if est is not None and not est.empty and "growth" in est.columns:
            for idx, row in est.iterrows():
                if str(idx).endswith("y"):
                    g = pct(row.get("growth"))
                    if g is not None:
                        d["eps_path"].append((str(idx), g))
    except Exception:  # noqa: BLE001
        pass

    # Croissance revenus forward 3 ans (CAGR des estimations)
    d["rev_growth"] = None
    try:
        rev = tk.revenue_estimate
        if rev is not None and not rev.empty and "avg" in rev.columns:
            vals = []
            for idx, row in rev.iterrows():
                if str(idx).endswith("y") and row.get("avg"):
                    vals.append(float(row["avg"]))
            if len(vals) >= 3 and vals[0] > 0:
                d["rev_growth"] = (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1
        if d["rev_growth"] is None and rev is not None and "growth" in rev.columns:
            gs = [pct(v) for v in rev["growth"] if pct(v) is not None]
            if gs:
                d["rev_growth"] = sum(gs) / len(gs)
    except Exception:  # noqa: BLE001
        pass

    # P/E forward de repli : prix / BPA estimé année prochaine (+1y)
    if d["pe_fwd"] is None and d["eps_path"] and d.get("price"):
        try:
            est = tk.earnings_estimate
            if est is not None and "+1y" in est.index and est.loc["+1y"].get("avg"):
                d["pe_fwd"] = float(d["price"]) / float(est.loc["+1y"]["avg"])
                d["pe_source"] = "prix / BPA estimé +1y (Yahoo)"
            elif est is not None and "0y" in est.index and est.loc["0y"].get("avg"):
                d["pe_fwd"] = float(d["price"]) / float(est.loc["0y"]["avg"])
                d["pe_source"] = "prix / BPA estimé année en cours (Yahoo)"
        except Exception:  # noqa: BLE001
            pass
    d["earn_days"] = None
    try:
        cal = tk.get_earnings_dates(limit=2)
        if cal is not None and not cal.empty:
            nxt = cal.index[0]
            d["earn_days"] = (nxt.date() - datetime.now(timezone.utc).date()).days
            d["earn_date"] = str(nxt.date())
    except Exception:  # noqa: BLE001
        pass

    # Dilution : croissance annuelle du nombre d'actions (bilan)
    d["dilution"] = None
    try:
        bs = tk.balance_sheet
        if bs is not None and not bs.empty:
            row = None
            for key in ("Share Issued", "Ordinary Shares Number"):
                if key in bs.index:
                    row = bs.loc[key]
                    break
            if row is not None:
                vals = [float(v) for v in row if v]
                if len(vals) >= 2 and vals[-1] > 0:
                    d["dilution"] = (vals[0] / vals[-1]) ** (1.0 / (len(vals) - 1)) - 1
    except Exception:  # noqa: BLE001
        pass

    # R-DCA : ancre = plus haute clôture mensuelle (24 mois)
    d["rdca"] = None
    try:
        hist = tk.history(period="2y", interval="1mo")["Close"]
        if hist is not None and len(hist) > 0:
            anchor = float(hist.max())
            d["rdca"] = {
                "anchor": anchor,
                "buy": (0.40 * anchor, 0.49 * anchor),
                "sm": (0.34 * anchor, 0.40 * anchor),
            }
    except Exception:  # noqa: BLE001
        pass
    return d


def dcf_value(fcf, g, net_debt, shares, r=TAUX_ACTU, tg=CROISS_TERMINALE, n=ANNEES_DCF):
    """Valeur par action (DCF sur le FCF, croissance g pendant n ans)."""
    pv = 0.0
    f = fcf
    for t in range(1, n + 1):
        f = f * (1 + g)
        pv += f / (1 + r) ** t
    tv = f * (1 + tg) / (r - tg)
    pv += tv / (1 + r) ** n
    equity = pv - (net_debt or 0)
    return equity / shares if shares else None


def reverse_dcf(fcf, price, net_debt, shares):
    """Croissance annuelle impliquée par le prix actuel (recherche binaire)."""
    lo, hi = -0.20, 0.60
    if dcf_value(fcf, lo, net_debt, shares) > price:
        return None, "< -20 %"
    if dcf_value(fcf, hi, net_debt, shares) < price:
        return None, "> +60 %"
    for _ in range(60):
        mid = (lo + hi) / 2
        if dcf_value(fcf, mid, net_debt, shares) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, None


def analyse(ticker):
    d = fetch(ticker)
    price = d["price"]

    # DCF + DCF inverse — calculés AVANT les checks : le check 6 (ancre prix)
    # utilise la valeur DCF comme deuxième branche.
    dcf = {"value": None, "implied": None, "note": ""}
    net_debt = None
    if d["debt"] is not None and d["cash"] is not None:
        net_debt = d["debt"] - d["cash"]
    shares = d["shares"] or (d["cap"] / price if d["cap"] and price else None)
    if d["fcf"] and d["fcf"] > 0 and price and shares:
        path0 = d["eps_path"]
        if path0:
            g = sum(g for _, g in path0) / len(path0)
        elif d["rev_growth"] is not None:
            g = d["rev_growth"]
        else:
            g = None
        if g is not None:
            g = max(0.0, min(g, 0.25))
            v = dcf_value(d["fcf"], g, net_debt, shares)
            dcf["value"] = v
            dcf["g"] = g
            imp, imp_note = reverse_dcf(d["fcf"], price, net_debt, shares)
            dcf["implied"] = imp
            dcf["implied_note"] = imp_note
            dcf["note"] = (f"Hypothèses : FCF {fmt_money(d['fcf'])} en base, croissance {g * 100:.1f} %/an "
                           f"(plafonnée à 25 %), actualisation {TAUX_ACTU * 100:.0f} %, "
                           f"croissance terminale {CROISS_TERMINALE * 100:.0f} %, {ANNEES_DCF} ans.")

    checks = []

    # 1. BPA forward — parcours (amendement 11 nov. 2026) : plancher +10 %
    # sur chaque année ET parcours composé >= +20 %/an.
    path = d["eps_path"]
    if path:
        worst = min(g for _, g in path)
        comp = 1.0
        for _, g in path:
            comp *= (1 + g)
        cagr = comp ** (1.0 / len(path)) - 1
        ok = worst >= PLANCHER_BPA and cagr >= BARRE_PARCOURS
        val = " · ".join(f"{k} {g * 100:+.0f} %" for k, g in path)
        margin = (f"Année la plus faible : {worst * 100:+.0f} % (plancher +10 %) ; "
                  f"parcours composé : {cagr * 100:+.1f} %/an (barre +20 %/an). ")
        avg3 = sum(g for _, g in path) / len(path)
        margin += f"Moyenne du parcours : {avg3 * 100:+.1f} % — à titre de référence seulement."
    else:
        ok, val, margin = False, "n/d", "Estimations annuelles indisponibles (Yahoo)."
    checks.append(("Croissance BPA forward (parcours)", ok, val, "plancher +10 % · parcours >= +20 %/an", margin))

    # 2. Revenus forward
    rg = d["rev_growth"]
    if rg is not None:
        ok = rg >= BARRE_REV
        margin = f"{(rg - BARRE_REV) * 100:+.1f} pts vs la barre."
    else:
        ok, margin = False, "Estimations de revenus indisponibles."
    checks.append(("Croissance revenus forward (3 ans)", ok, fmt_pct(rg), ">= +20 %/an", margin))

    # 3. P/E forward
    pe = d["pe_fwd"]
    if pe:
        ok = pe <= BARRE_PE
        margin = f"{(pe / BARRE_PE - 1) * 100:+.0f} % vs la barre de 20x."
        if d.get("pe_source"):
            margin += f" Source : {d['pe_source']}."
    else:
        ok, margin = False, "P/E forward indisponible."
    checks.append(("P/E forward", ok, f"{pe:.2f}x" if pe else "n/d", "<= 20x", margin))

    # 4. FCF TTM
    fcf = d["fcf"]
    if fcf is not None:
        ok = fcf > 0
        margin = f"Source : {d.get('fcf_source', 'Yahoo')}."
    else:
        ok, margin = False, "FCF indisponible."
    checks.append(("FCF TTM", ok, fmt_money(fcf), "> 0", margin))

    # 5. Dette nette / EBITDA — le cash net passe sans EBITDA
    debt, cash, ebitda = d["debt"], d["cash"], d["ebitda"]
    if debt is not None and cash is not None:
        nd = debt - cash
        if nd <= 0:
            ok, val, margin = True, f"Cash net {fmt_money(-nd)}", "Aucune dette nette."
        elif ebitda:
            lev = nd / ebitda
            ok = lev <= 2
            val, margin = f"{lev:.2f}x", f"Dette nette {fmt_money(nd)} / EBITDA {fmt_money(ebitda)}."
        else:
            ok, val, margin = False, "n/d", "Dette nette positive mais EBITDA indisponible."
    else:
        ok, val, margin = False, "n/d", "Dette ou cash indisponible."
    checks.append(("Dette nette / EBITDA", ok, val, "<= 2x", margin))

    # 6. Ancre prix (amendement 11 nov. 2026) : décote >= 25 % OU prix
    # >= 20 % sous la valeur DCF. Financières/assureurs : décote seule.
    if price and d["high52"]:
        dd = price / d["high52"] - 1
        ok_dd = dd <= -BARRE_DECOTE
        val = f"{dd * 100:+.1f} %"
        is_fin = (d.get("sector") or "") == "Financial Services"
        v = dcf["value"]
        if is_fin:
            ok = ok_dd
            margin = (f"Décote {'passe' if ok_dd else 'échoue'} (sommet 52 sem. : "
                      f"{d['high52']:,.2f} {d['currency']}). Branche DCF non appliquée — "
                      f"secteur financier ({d.get('industry') or 'Financial Services'}) : "
                      f"seule la décote compte.")
        elif v is not None and v > 0:
            marge_dcf = 1 - price / v
            ok_dcf = marge_dcf >= MARGE_DCF_ANCRE
            ok = ok_dd or ok_dcf
            margin = (f"Décote {dd * 100:+.1f} % (barre 25 % — {'passe' if ok_dd else 'échoue'}) · "
                      f"marge sous le DCF {marge_dcf * 100:+.1f} % (prix {price:,.2f} vs valeur "
                      f"{v:,.2f} {d['currency']} — barre 20 % — {'passe' if ok_dcf else 'échoue'}).")
        else:
            ok = ok_dd
            margin = (f"Décote {'passe' if ok_dd else 'échoue'} (sommet 52 sem. : "
                      f"{d['high52']:,.2f} {d['currency']}). DCF indisponible — seule la décote compte.")
    else:
        ok, val, margin = False, "n/d", "Prix ou sommet 52 sem. indisponible."
    checks.append(("Ancre prix (décote ou DCF)", ok, val, ">= 25 % ou >= 20 % sous le DCF", margin))

    # 7. Capitalisation
    cap = d["cap"]
    if cap:
        ok = cap >= PLANCHER_CAP
        margin = f"{cap / PLANCHER_CAP:.1f}x le plancher."
    else:
        ok, margin = False, "Capitalisation indisponible."
    checks.append(("Capitalisation", ok, fmt_money(cap), ">= 2 Md$", margin))

    score = sum(1 for c in checks if c[1])
    verdict = "PASSE" if score == 7 else ("PAS ENCORE" if score == 6 else "NON")
    fails = [c[0] for c in checks if not c[1]]

    # Drapeaux
    flags = []
    if d["earn_days"] is not None and d["earn_days"] < 30:
        flags.append(f"Print dans {d['earn_days']} jours ({d.get('earn_date', '')}) — binaire, pas d'achat avant.")
    if d["dilution"] is not None and d["dilution"] > 0.05:
        flags.append(f"Dilution : actions {d['dilution'] * 100:+.1f} %/an (> 5 %).")
    elif d["dilution"] is not None:
        flags.append(f"Actions {d['dilution'] * 100:+.1f} %/an (dilution sous contrôle).")
    if d["beta"] is not None:
        flags.append(f"Bêta {d['beta']:.2f}.")

    return d, checks, score, verdict, fails, flags, dcf


def zone_rdca(d):
    """Zone R-DCA et multiplicateur pour le prix actuel."""
    rdca, price = d.get("rdca"), d.get("price")
    if not rdca or not price:
        return None
    a = rdca["anchor"]
    buy_lo, buy_hi = rdca["buy"]
    sm_lo, sm_hi = rdca["sm"]
    if price < sm_lo:
        zone, mult = "Sous Smart Money", "4X DCA"
    elif price < sm_hi:
        zone, mult = "Smart Money Zone ★ (zone gold)", "3X DCA"
    elif price < buy_hi:
        zone, mult = "Buy Zone", "2X DCA"
    elif price < 0.74 * a:
        zone, mult = "Add Zone", "1X DCA"
    else:
        zone, mult = "Au-dessus des zones d'achat", "—"
    return {"anchor": a, "buy": (buy_lo, buy_hi), "sm": (sm_lo, sm_hi),
            "strong_trim": a, "zone": zone, "mult": mult}


def print_fiche(ticker):
    d, checks, score, verdict, fails, flags, dcf = analyse(ticker)
    cur = d.get("currency", "")
    price = d.get("price")
    print("=" * 64)
    print(f"{d['name']} ({d['ticker']}) — {price:,.2f} {cur}" if price else f"{d['name']} ({d['ticker']})")
    print(f"NOTE : {score}/7 — {verdict}")
    if fails:
        print(f"Échecs : {', '.join(fails)}")
    print("-" * 64)
    for label, ok, val, barre, margin in checks:
        mark = "✓" if ok else "✗"
        print(f"[{mark}] {label}\n      Valeur : {val}  |  Barre : {barre}\n      {margin}")
    if flags:
        print("-" * 64)
        print("Drapeaux :")
        for f in flags:
            print(f"  • {f}")
    # DCF
    print("-" * 64)
    print("Valorisation (DCF) :")
    if dcf["value"] is not None:
        v, p = dcf["value"], price
        mos = (v / p - 1) if p else None
        print(f"  Valeur DCF : {v:,.2f} {cur}  |  Prix : {p:,.2f} {cur}  |  "
              f"Marge de sécurité : {mos * 100:+.1f} %" if mos is not None else "")
        imp = dcf["implied"]
        print(f"  DCF inverse : le prix actuel suppose une croissance de "
              f"{imp * 100:+.1f} %/an" if imp is not None else
              f"  DCF inverse : croissance impliquée {dcf.get('implied_note', 'n/d')}")
        print(f"  {dcf['note']}")
    else:
        print("  n/d — FCF ou données insuffisantes pour un DCF.")
    # R-DCA
    z = zone_rdca(d)
    print("-" * 64)
    print("Zones R-DCA (ancre = plus haute clôture mensuelle, 24 mois) :")
    if z:
        print(f"  Ancre / Strong Trim : {z['anchor']:,.2f} {cur}")
        print(f"  Buy Zone : {z['buy'][0]:,.2f} – {z['buy'][1]:,.2f} {cur}")
        print(f"  Smart Money Zone ★ : {z['sm'][0]:,.2f} – {z['sm'][1]:,.2f} {cur}")
        print(f"  Position : {z['zone']} → {z['mult']}")
    else:
        print("  n/d — historique mensuel indisponible.")
    print("=" * 64)


# ---------------------------------------------------------------------------
# Export pour l'appli (ajout) — le filtre ci-dessus n'est pas modifié.
#   python3 rocky-score.py --json data.json            -> liste de tickers.txt
#   python3 rocky-score.py --json data.json HOOD AXON  -> ces tickers seulement
# Les fiches sont fusionnées dans le fichier existant (clé = ticker).
# ---------------------------------------------------------------------------
import json
import os
import re

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


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


def fiche_dict(ticker):
    """Même analyse que print_fiche, mais renvoie un dict sérialisable."""
    d, checks, score, verdict, fails, flags, dcf = analyse(ticker)
    price = _num(d.get("price"))
    if price is None:
        raise ValueError("aucune donnée Yahoo pour ce symbole (ticker inconnu ou source indisponible)")
    z = zone_rdca(d)
    v = _num(dcf.get("value"))
    out = {
        "ticker": d["ticker"],
        "name": str(d.get("name") or d["ticker"]),
        "price": price,
        "currency": str(d.get("currency") or ""),
        "sector": d.get("sector"),
        "industry": d.get("industry"),
        "score": int(score),
        "verdict": verdict,
        "fails": list(fails),
        "checks": [{"label": c[0], "ok": bool(c[1]), "val": str(c[2]),
                    "barre": str(c[3]), "margin": str(c[4])} for c in checks],
        "flags": list(flags),
        "dcf": None,
        "rdca": None,
        "date": _now(),
    }
    if v is not None:
        out["dcf"] = {
            "value": v,
            "mos": (v / price - 1) if price else None,
            "implied": _num(dcf.get("implied")),
            "implied_note": dcf.get("implied_note"),
            "note": dcf.get("note") or "",
        }
    if z:
        a = _num(z["anchor"])
        out["rdca"] = {
            "anchor": a,
            "buy": [_num(z["buy"][0]), _num(z["buy"][1])],
            "sm": [_num(z["sm"][0]), _num(z["sm"][1])],
            "add_hi": 0.74 * a if a else None,
            "zone": z["zone"],
            "mult": z["mult"],
        }
    return out


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
    for t in tickers:
        t = t.strip().upper()
        if not TICKER_RE.match(t):
            print(f"Ignoré (ticker invalide) : {t!r}")
            continue
        try:
            fiches[t] = fiche_dict(t)
            print(f"{t} : {fiches[t]['score']}/7 — {fiches[t]['verdict']}")
        except Exception as e:  # noqa: BLE001
            print(f"Erreur pour {t} : {e}")
            old = fiches.get(t)
            if isinstance(old, dict) and old.get("checks"):
                # on garde la dernière bonne fiche, en signalant l'échec
                old["erreur_maj"] = str(e)
                old["date_tentative"] = _now()
            else:
                fiches[t] = {"ticker": t, "erreur": str(e), "date": _now()}
    out = {"updated": _now(), "fiches": fiches}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, allow_nan=False)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--json" in args:
        k = args.index("--json")
        if k + 1 >= len(args):
            sys.exit("Usage : rocky-score.py --json data.json [TICKERS...]")
        chemin = args[k + 1]
        tickers = [t for t in args[:k] + args[k + 2:] if not t.startswith("-")]
        export_json(chemin, tickers or lire_tickers() or ["HOOD"])
    else:
        tickers = [t for t in args if not t.startswith("-")] or ["HOOD"]
        for t in tickers:
            try:
                print_fiche(t)
            except Exception as e:  # noqa: BLE001
                print(f"Erreur pour {t} : {e}")
