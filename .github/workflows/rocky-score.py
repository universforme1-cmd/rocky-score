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
  2. Croissance revenus forward >= +20 %/an (horizon réel nommé sur la
     fiche : Yahoo donne rarement 3 années d'estimations)
  3. P/E forward <= 20x
  4. FCF TTM > 0 (critère bloquant)
  5. Dette nette / EBITDA <= 2x
  6. Ancre prix : décote >= 25 % vs sommet 52 sem. OU prix >= 20 % sous la
     valeur DCF. Financières et assureurs : décote seule (DCF non fiable).
  7. Capitalisation >= 2 Md$

Verdict : 7/7 = PASSE, 6/7 = PAS ENCORE, <= 5/7 = NON.
Hors note : DCF inverse (croissance impliquée par le prix) et zones R-DCA.
La valeur DCF alimente la 2e branche du check 6 (ancre prix).
Qualité du capital (hors note) : ROE, ROIC, ROIIC (incrémental 3 ans), ROCE
— mini-jauges texte, seuils repères 15 % / 10 % / 10 % / 10 %.

Données : Yahoo Finance (gratuit, sans clé). Les chiffres peuvent différer
légèrement d'une source à l'autre (Nasdaq, stockanalysis) — la fiche indique
la source de chaque valeur.

Correctifs du 18 nov. 2026 :
  - Parcours BPA : une année dont la base (BPA de l'année précédente) est
    négative ou nulle est « n/d » — exclue du plancher et du parcours, et
    signalée sur la fiche (le taux de Yahoo est alors non significatif).
  - DCF : si la marge FCF TTM est < 2 % des revenus, la valeur DCF est un
    artefact mécanique — affichée « non significatif », seule la décote
    compte pour le check 6.
  - Zones R-DCA : l'ancre est le vrai sommet (max de la plus haute clôture
    mensuelle 24 mois et du sommet 52 sem.) et la position du prix est
    revérifiée contre les bornes avant d'afficher un libellé de zone.
  - Drapeaux « qualité du reporting » (hors note, via SEC EDGAR) : dépôt
    périodique (10-Q/10-K) en retard après un print, prospectus/émission
    récent (titres annoncés possiblement non livrés — invisibles au drapeau
    de dilution rétrospectif), dérive du DSO (créances clients).
"""

import json
import sys
import urllib.request
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
SEUIL_MARGE_FCF_DCF = 0.02  # sous 2 % de marge FCF, le DCF est « non significatif » (18 nov. 2026)


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
    d["revenue"] = info.get("totalRevenue")

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

    # Parcours annuel BPA (estimations) — examen annuel, pas moyenne 3 ans.
    # Correctif du 18 nov. 2026 : si la base d'une année (BPA de l'année
    # précédente) est négative ou nulle, son taux de croissance est non
    # significatif (« n/d ») — l'année est exclue du parcours et signalée
    # dans d["eps_nd"]. Base de « 0y » : BPA réalisé (trailingEps) ; base
    # de « +1y » : niveau estimé de « 0y » (colonne avg).
    d["eps_path"] = []
    d["eps_nd"] = []
    try:
        est = tk.earnings_estimate
        if est is not None and not est.empty and "growth" in est.columns:
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
                    d["eps_nd"].append(lbl)
                else:
                    d["eps_path"].append((lbl, g))
                if levels.get(lbl) is not None:
                    prev_level = levels[lbl]
    except Exception:  # noqa: BLE001
        pass

    # Croissance revenus forward — honnête sur l'horizon (correction du
    # 12 nov. 2026) : Yahoo donne rarement plus de 2 années d'estimations.
    # On compose les niveaux annuels disponibles (CAGR) ; à défaut, moyenne
    # des seules lignes ANNUELLES de la colonne growth — jamais les lignes
    # trimestrielles, qui faussaient l'ancien calcul étiqueté « 3 ans ».
    d["rev_growth"] = None
    d["rev_note"] = None
    try:
        rev = tk.revenue_estimate
        if rev is not None and not rev.empty:
            annual = [(str(idx), row) for idx, row in rev.iterrows()
                      if str(idx).endswith("y")]
            if "avg" in rev.columns:
                vals = [float(r["avg"]) for _, r in annual if r.get("avg")]
                if len(vals) >= 2 and vals[0] > 0:
                    d["rev_growth"] = (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1
                    d["rev_note"] = f"CAGR sur {len(vals)} années d'estimations de revenus (Yahoo)"
            if d["rev_growth"] is None and "growth" in rev.columns:
                gs = [pct(r.get("growth")) for _, r in annual]
                gs = [g for g in gs if g is not None]
                if gs:
                    d["rev_growth"] = sum(gs) / len(gs)
                    d["rev_note"] = f"Moyenne des croissances annuelles estimées ({len(gs)} année(s), Yahoo)"
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
    d["last_earn_date"] = None
    try:
        cal = tk.get_earnings_dates(limit=12)
        if cal is not None and not cal.empty:
            today = datetime.now(timezone.utc).date()
            dates = sorted({ts.date() for ts in cal.index})
            future = [dt for dt in dates if dt >= today]
            past = [dt for dt in dates if dt < today]
            if future:
                d["earn_days"] = (future[0] - today).days
                d["earn_date"] = str(future[0])
            if past:
                d["last_earn_date"] = past[-1]
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

    # Dividende et historique de FCF annuel — pour le mode compounders
    # (brouillon du 18 nov. 2026) : rendement du dividende et croissance
    # du FCF sur les exercices disponibles.
    dy = info.get("dividendYield")
    if dy is not None and dy > 0.25:  # Yahoo le donne parfois en %
        dy = dy / 100.0
    d["div_yield"] = dy
    d["fcf_hist"] = []
    try:
        cfa = tk.cashflow
        if cfa is not None and not cfa.empty:
            ocf = _row(cfa, ("Total Cash From Operating Activities",
                             "Cash Flow From Operations",
                             "Operating Cash Flow",
                             "Net Cash Provided By Operating Activities"))
            cap = _row(cfa, ("Capital Expenditures", "Capital Expenditure"))
            if ocf is not None and cap is not None:
                for o, c in zip(ocf.iloc[:5], cap.iloc[:5]):
                    if o == o and c == c:  # pas NaN
                        d["fcf_hist"].append(float(o) + float(c))
    except Exception:  # noqa: BLE001
        pass

    # Qualité du capital (ROE / ROIC / ROIIC / ROCE) — AFFICHAGE SEULEMENT,
    # hors note (demande de Marc, 12 nov. 2026). Calculs tolérants : « n/d »
    # (None) dès qu'une donnée manque ou qu'un dénominateur est inutilisable.
    d["roe"] = info.get("returnOnEquity")
    d["roic"] = d["roiic"] = d["roce"] = None
    d["roic_avg"] = None
    d["roic_years"] = 0
    try:
        fin, bs = tk.financials, tk.balance_sheet

        def _rows(df, names):
            if df is None or df.empty:
                return None
            for n in names:
                if n in df.index:
                    out = {}
                    for k, v in df.loc[n].items():
                        try:
                            fv = float(v)
                        except (TypeError, ValueError):
                            continue
                        if fv == fv:  # pas NaN
                            out[str(k)[:10]] = fv
                    if out:
                        return out
            return None

        ebit = _rows(fin, ("EBIT", "Operating Income"))
        debt_s = _rows(bs, ("Total Debt",))
        eq_s = _rows(bs, ("Stockholders Equity", "Total Equity Gross Minority Interest",
                          "Common Stock Equity"))
        cash_s = _rows(bs, ("Cash And Cash Equivalents",
                            "Cash Cash Equivalents And Short Term Investments")) or {}
        tax_s = _rows(fin, ("Tax Rate For Calcs",))
        pret_s = _rows(fin, ("Pretax Income",))
        prov_s = _rows(fin, ("Tax Provision",))
        if ebit and debt_s and eq_s:
            ic = {dt: debt_s[dt] + eq_s[dt] - cash_s.get(dt, 0.0)
                  for dt in ebit if dt in debt_s and dt in eq_s}
            dates = sorted(ic, reverse=True)
            if dates:
                def _tax(dt):
                    if tax_s and dt in tax_s:
                        return min(0.40, max(0.0, tax_s[dt]))
                    if pret_s and prov_s and dt in pret_s and pret_s[dt] > 0:
                        return min(0.40, max(0.0, prov_s[dt] / pret_s[dt]))
                    return 0.21

                nopat = {dt: ebit[dt] * (1 - _tax(dt)) for dt in ic}
                last = dates[0]
                if ic[last] > 0:
                    d["roic"] = nopat[last] / ic[last]
                    d["roce"] = ebit[last] / ic[last]
                roics = [nopat[dt] / ic[dt] for dt in dates if ic[dt] > 0]
                if roics:
                    d["roic_avg"] = sum(roics) / len(roics)
                    d["roic_years"] = len(roics)
                if len(dates) >= 4:
                    old = dates[3]
                    dic = ic[last] - ic[old]
                    if dic > 0.02 * ic[last]:
                        d["roiic"] = (nopat[last] - nopat[old]) / dic
    except Exception:  # noqa: BLE001
        pass

    # R-DCA : ancre = vrai sommet — max de la plus haute clôture mensuelle
    # (24 mois) et du sommet 52 sem. (correctif du 18 nov. 2026 : une ancre
    # sous le sommet récent faussait les bornes des zones).
    d["rdca"] = None
    try:
        hist = tk.history(period="2y", interval="1mo")["Close"]
        if hist is not None and len(hist) > 0:
            anchor = float(hist.max())
            if d.get("high52"):
                anchor = max(anchor, float(d["high52"]))
            d["rdca"] = {
                "anchor": anchor,
                "buy": (0.40 * anchor, 0.49 * anchor),
                "sm": (0.34 * anchor, 0.40 * anchor),
            }
    except Exception:  # noqa: BLE001
        pass
    return d


def cagr_parcours(path):
    """Croissance composée d'un parcours de taux annuels [(label, g), ...]."""
    if not path:
        return None
    comp = 1.0
    for _, g in path:
        comp *= (1 + g)
    return comp ** (1.0 / len(path)) - 1


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
    # Correctif du 18 nov. 2026 : base FCF minuscule -> DCF non significatif.
    if d["fcf"] and d["fcf"] > 0 and d.get("revenue"):
        marge_fcf = d["fcf"] / d["revenue"]
        if marge_fcf < SEUIL_MARGE_FCF_DCF:
            dcf["insignificant"] = True
            dcf["note"] = (f"DCF non significatif : marge FCF TTM {marge_fcf * 100:.1f} % des revenus "
                           f"(sous le seuil de {SEUIL_MARGE_FCF_DCF * 100:.0f} %) — sur une base aussi mince, "
                           f"la valeur DCF et le DCF inverse sont des artefacts mécaniques. "
                           f"Seule la décote compte pour l'ancre prix.")
    if d["fcf"] and d["fcf"] > 0 and price and shares and not dcf.get("insignificant"):
        # Hypothèse PRUDENTE (correction du 12 nov. 2026) : le minimum des
        # mesures de croissance disponibles — jamais la moyenne du parcours,
        # que le check 1 interdit (elle flatte un bond déjà réalisé).
        candidats = []
        c_eps = cagr_parcours(d["eps_path"])
        if c_eps is not None:
            candidats.append(c_eps)
        if d["rev_growth"] is not None:
            candidats.append(d["rev_growth"])
        g = min(candidats) if candidats else None
        if g is not None:
            g = max(0.0, min(g, 0.25))
            v = dcf_value(d["fcf"], g, net_debt, shares)
            dcf["value"] = v
            dcf["g"] = g
            imp, imp_note = reverse_dcf(d["fcf"], price, net_debt, shares)
            dcf["implied"] = imp
            dcf["implied_note"] = imp_note
            dcf["note"] = (f"Hypothèses : FCF {fmt_money(d['fcf'])} en base, croissance prudente (min. des mesures) {g * 100:.1f} %/an "
                           f"(plafonnée à 25 %), actualisation {TAUX_ACTU * 100:.0f} %, "
                           f"croissance terminale {CROISS_TERMINALE * 100:.0f} %, {ANNEES_DCF} ans.")

    checks = []

    # 1. BPA forward — parcours (amendement 11 nov. 2026) : plancher +10 %
    # sur chaque année ET parcours composé >= +20 %/an.
    path = d["eps_path"]
    nd_years = d.get("eps_nd") or []
    if path:
        worst = min(g for _, g in path)
        cagr = cagr_parcours(path)
        ok = worst >= PLANCHER_BPA and cagr >= BARRE_PARCOURS
        val = " · ".join(f"{k} {g * 100:+.0f} %" for k, g in path)
        if nd_years:
            val += " · " + " · ".join(f"{k} n/d" for k in nd_years)
        margin = (f"Année la plus faible : {worst * 100:+.0f} % (plancher +10 %) ; "
                  f"parcours composé : {cagr * 100:+.1f} %/an (barre +20 %/an). ")
        avg3 = sum(g for _, g in path) / len(path)
        margin += f"Moyenne du parcours : {avg3 * 100:+.1f} % — à titre de référence seulement."
        if nd_years:
            margin += (f" Année(s) {', '.join(nd_years)} exclue(s) : n/d — base BPA <= 0, "
                       f"croissance non significative (correctif du 18 nov. 2026).")
    elif nd_years:
        ok, val = False, "n/d"
        margin = ("Toutes les années forward sont non significatives (base BPA <= 0) — "
                  "parcours n/d (correctif du 18 nov. 2026).")
    else:
        ok, val, margin = False, "n/d", "Estimations annuelles indisponibles (Yahoo)."
    checks.append(("Croissance BPA forward (parcours)", ok, val, "plancher +10 % · parcours >= +20 %/an", margin))

    # 2. Revenus forward — l'horizon réel est nommé dans la marge
    # (correction du 12 nov. 2026 : plus d'étiquette « 3 ans » mensongère).
    rg = d["rev_growth"]
    if rg is not None:
        ok = rg >= BARRE_REV
        margin = f"{(rg - BARRE_REV) * 100:+.1f} pts vs la barre."
        if d.get("rev_note"):
            margin += f" Base : {d['rev_note']}."
    else:
        ok, margin = False, "Estimations de revenus indisponibles."
    checks.append(("Croissance revenus forward", ok, fmt_pct(rg), ">= +20 %/an", margin))

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
        if dcf.get("insignificant"):
            ok = ok_dd
            margin = (f"Décote {'passe' if ok_dd else 'échoue'} (sommet 52 sem. : "
                      f"{d['high52']:,.2f} {d['currency']}). DCF non significatif — "
                      f"base FCF trop faible : seule la décote compte.")
        elif is_fin:
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
    for rf in reporting_flags(d):
        flags.append(rf)
    if d["beta"] is not None:
        flags.append(f"Bêta {d['beta']:.2f}.")

    return d, checks, score, verdict, fails, flags, dcf


SEUILS_QUALITE = {"ROE": 0.15, "ROIC": 0.10, "ROIIC": 0.10, "ROCE": 0.10}


def gauge_txt(val, lo, hi, width=20):
    """Mini-jauge texte : position de la valeur entre lo et hi."""
    if val is None:
        return "[" + "░" * width + "]"
    pos = (val - lo) / (hi - lo)
    pos = min(1.0, max(0.0, pos))
    n = int(round(pos * width))
    return "[" + "█" * n + "░" * (width - n) + "]"


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


# ---------------------------------------------------------------------------
# Qualité du reporting (SEC EDGAR) — drapeaux d'information, hors note.
# Correctif du 18 nov. 2026. Best-effort : un échec réseau ou une donnée
# absente ne produit simplement aucun drapeau.
# ---------------------------------------------------------------------------
_CIK_CACHE = None


def _edgar_json(url):
    # La SEC exige un User-Agent déclaratif (nom + contact), ASCII pur —
    # un UA générique ou accentué est rejeté (403) ou fait échouer l'en-tête.
    req = urllib.request.Request(
        url, headers={"User-Agent": "Rocky Score personal research admin@example.com"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cik_for(ticker):
    global _CIK_CACHE
    if _CIK_CACHE is None:
        try:
            data = _edgar_json("https://www.sec.gov/files/company_tickers.json")
            _CIK_CACHE = {v["ticker"].upper(): int(v["cik_str"])
                          for v in data.values()}
        except Exception:  # noqa: BLE001
            _CIK_CACHE = {}
    return _CIK_CACHE.get(ticker.upper())


def reporting_flags(d):
    """Drapeaux : dépôt périodique en retard, émission récente, DSO."""
    out = []
    # EDGAR ne couvre que les émetteurs US : aucun drapeau EDGAR pour un
    # ticker suffixé (.TO, .DE, ...) — un CIK homonyme serait un faux positif.
    if "." not in d["ticker"]:
        try:
            cik = _cik_for(d["ticker"])
            if cik:
                sub = _edgar_json(
                    f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
                recent = sub.get("filings", {}).get("recent", {})
                filings = list(zip(recent.get("form", []),
                                   recent.get("filingDate", [])))
                today = datetime.now(timezone.utc).date()

                def _parse(s):
                    try:
                        return datetime.strptime(s, "%Y-%m-%d").date()
                    except (TypeError, ValueError):
                        return None

                # (a) Résultats publiés depuis > 21 jours sans 10-Q/10-K
                # déposé à compter de la date du print -> dépôt en retard.
                last_earn = d.get("last_earn_date")
                if last_earn and (today - last_earn).days > 21:
                    filed = any(
                        f in ("10-Q", "10-K")
                        and (_parse(dt) is not None and _parse(dt) >= last_earn)
                        for f, dt in filings)
                    if not filed:
                        out.append(
                            f"Qualité du reporting : résultats publiés le "
                            f"{last_earn} sans 10-Q/10-K déposé depuis "
                            f"({(today - last_earn).days} jours) — dépôt "
                            f"périodique en retard.")
                # (b) Prospectus récent (< 120 jours) : des titres annoncés
                # peuvent ne pas être encore livrés (forward sale, ATM) —
                # invisibles au drapeau de dilution, qui est rétrospectif.
                for f, dt in filings:
                    fd = _parse(dt)
                    if (f in ("424B5", "424B3", "424B2", "FWP") and fd
                            and (today - fd).days <= 120):
                        out.append(
                            f"Qualité du reporting : prospectus {f} déposé "
                            f"le {fd} — émission récente ; vérifier les titres "
                            f"annoncés non encore livrés (forward sale, ATM).")
                        break
        except Exception:  # noqa: BLE001
            pass
    # (c) Dérive du DSO : créances / revenus du trimestre x 91 jours,
    # comparé au même trimestre un an plus tôt (Yahoo, trimestriel).
    try:
        tk = yf.Ticker(d["ticker"])
        bs = tk.quarterly_balance_sheet
        fin = tk.quarterly_financials
        if bs is not None and not bs.empty and fin is not None and not fin.empty:
            def _qrow(df, names):
                for n in names:
                    if n in df.index:
                        return df.loc[n]
                return None

            ar = _qrow(bs, ("Accounts Receivable", "Receivables",
                            "Net Receivables"))
            rev = _qrow(fin, ("Total Revenue", "Operating Revenue"))
            if (ar is not None and rev is not None
                    and len(ar) >= 5 and len(rev) >= 5):
                def _dso(i):
                    a, r = ar.iloc[i], rev.iloc[i]
                    if a and r and float(r) > 0:
                        return float(a) / float(r) * 91.0
                    return None

                now_dso, old_dso = _dso(0), _dso(4)
                if now_dso and old_dso and now_dso - old_dso > 10:
                    out.append(
                        f"Qualité du reporting : DSO ~{now_dso:.0f} jours, "
                        f"en hausse de {now_dso - old_dso:.0f} jours vs le "
                        f"même trimestre l'an passé ({old_dso:.0f} jours) — "
                        f"créances à surveiller.")
    except Exception:  # noqa: BLE001
        pass
    return out


def analyse_compounders(d, checks):
    """Mode compounders — amendement du 18 nov. 2026, ADOPTÉ par Marc
    le 18 nov. 2026. Deuxième porte d'entrée, distincte du filtre
    principal : un nom entre par une porte, jamais par la plus clémente
    après coup. Hors thème IA par définition.
    Checks : (1) rendement espéré >= 12 %/an (formule du 12 nov. 2026 :
    croissance BPA composée plafonnée à 25 % + dividende + rachats nets
    - contraction du multiple vers 15x en 5 ans) ET croissance composée
    >= +7 %/an ; (2) ROIC moyen >= 15 % ; (3) intouchables (FCF > 0,
    levier <= 2x, cap >= 2 Md$) ; (4) retour de capital >= 2 %/an sans
    dilution OU réinvestissement (croissance FCF >= +10 %/an à ROIC
    >= 15 %) ; (5) ancre prix = check 6 du filtre principal."""
    rows = []
    path = d.get("eps_path") or []
    cagr = cagr_parcours(path) if path else None
    g = min(cagr, 0.25) if cagr is not None else None
    dy_raw = d.get("div_yield") or 0.0
    dy = min(dy_raw, 0.08)  # au-delà de 8 %, artefact de données quasi certain
    dy_note = f" (plafonné à 8 % — donnée Yahoo brute {dy_raw * 100:.1f} %)" if dy_raw > 0.08 else ""
    dil = d.get("dilution")
    rachats = max(0.0, -dil) if dil is not None else 0.0
    pe = d.get("pe_fwd")
    contraction = 0.0
    if pe and pe > 15:
        contraction = 1 - (15.0 / pe) ** (1 / 5)
    if g is not None:
        rend = g + dy + rachats - contraction
        ok1 = rend >= 0.12 and cagr >= 0.07
        det1 = (f"Croissance {g * 100:+.1f} % (plafonnée à 25 %) + dividende "
                f"{dy * 100:.1f} %{dy_note} + rachats nets {rachats * 100:.1f} % "
                f"- contraction {contraction * 100:.1f} % = {rend * 100:+.1f} %/an "
                f"(barre 12 % ; croissance brute {cagr * 100:+.1f} %/an, plancher +7 %).")
    else:
        ok1 = False
        det1 = "Parcours BPA indisponible — rendement espéré n/d."
    rows.append(("Rendement espéré >= 12 %/an", ok1, det1))

    roic = d.get("roic_avg")
    roic_lbl = f"moyenne sur {d.get('roic_years', 0)} exercice(s)"
    if roic is None:
        roic = d.get("roic")
        roic_lbl = "dernier exercice"
    ok2 = roic is not None and roic >= 0.15
    det2 = (f"ROIC {roic * 100:.1f} % ({roic_lbl}) — barre 15 %."
            if roic is not None else "ROIC n/d.")
    rows.append(("ROIC soutenu >= 15 %", ok2, det2))

    debt, cash, ebitda = d.get("debt"), d.get("cash"), d.get("ebitda")
    lev_ok = False
    if debt is not None and cash is not None:
        nd = debt - cash
        lev_ok = nd <= 0 or (bool(ebitda) and nd / ebitda <= 2)
    ok3 = (bool(d.get("fcf")) and d["fcf"] > 0 and lev_ok
           and bool(d.get("cap")) and d["cap"] >= PLANCHER_CAP)
    det3 = ("FCF > 0, levier <= 2x, cap >= 2 Md$ : respectés." if ok3
            else "FCF > 0, levier <= 2x, cap >= 2 Md$ : au moins un intouchable échoue.")
    rows.append(("Intouchables (FCF, levier, cap)", ok3, det3))

    retour = dy + rachats
    # Dilution indisponible : aucun passe sur donnée manquante, et le
    # détail le dit (n/d) au lieu d'afficher un faux « 0,0 % ».
    branche_retour = retour >= 0.02 and dil is not None and dil <= 0
    hist = d.get("fcf_hist") or []
    fcf_g = None
    if len(hist) >= 3 and hist[-1] > 0 and hist[0] > 0:
        fcf_g = (hist[0] / hist[-1]) ** (1 / (len(hist) - 1)) - 1
    branche_reinv = (fcf_g is not None and fcf_g >= 0.10
                     and roic is not None and roic >= 0.15)
    ok4 = branche_retour or branche_reinv
    det4 = (f"Retour {retour * 100:.1f} %/an (dividende + rachats nets"
            + (")" if dil is not None else " n/d — historique d'actions indisponible)")
            + (" — branche retour OK" if branche_retour else "")
            + (f" ; croissance FCF {fcf_g * 100:+.1f} %/an" if fcf_g is not None
               else " ; croissance FCF n/d")
            + (" — branche réinvestissement OK." if branche_reinv else "."))
    rows.append(("Discipline du capital", ok4, det4))

    ok5 = checks[5][1] if len(checks) > 5 else False
    rows.append(("Ancre prix (comme le filtre principal)", ok5,
                 "Reprend le check 6 principal : décote >= 25 % ou >= 20 % sous le DCF."))
    score = sum(1 for _, ok, _ in rows if ok)
    verdict = "PASSE" if score == 5 else ("PAS ENCORE" if score >= 4 else "NON")
    return rows, verdict


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
    elif dcf.get("insignificant"):
        print(f"  {dcf['note']}")
    else:
        print("  n/d — FCF ou données insuffisantes pour un DCF.")
    # Qualité du capital — jauges d'information, hors note /7
    print("-" * 64)
    print("Qualité du capital (information — hors note /7) :")
    for label, val, lo in (("ROE", d.get("roe"), 0.0), ("ROIC", d.get("roic"), 0.0),
                           ("ROIIC", d.get("roiic"), -0.10), ("ROCE", d.get("roce"), 0.0)):
        seuil = SEUILS_QUALITE[label]
        if val is None:
            print(f"  {label:<6}: n/d  (seuil {seuil * 100:.0f} %)")
        else:
            etat = "au-dessus du seuil" if val >= seuil else "EN DESSOUS du seuil"
            print(f"  {label:<6}: {gauge_txt(val, lo, 0.40)} {val * 100:+.1f} %  "
                  f"(seuil {seuil * 100:.0f} %) — {etat}")
    # Mode compounders — poche adoptée le 18 nov. 2026
    crows, cverdict = analyse_compounders(d, checks)
    print("-" * 64)
    print(f"Mode compounders : {cverdict} "
          f"(poche adoptée le 18 nov. 2026 — plafond 20-25 % du livre, "
          f"hors thème IA, une porte par nom)")
    for label, ok, det in crows:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {label}\n        {det}")
    # R-DCA
    z = zone_rdca(d)
    print("-" * 64)
    print("Zones R-DCA (ancre = vrai sommet : max clôture mensuelle 24 mois / sommet 52 sem.) :")
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
# Correctif du 24 nov. 2026 : relance automatique unique (attente 20 s) en
# cas d'échec d'une fiche — les erreurs Yahoo passagères (ex. KeyError
# 'currentTradingPeriod' en pré-marché) ne produisent plus de fiche d'erreur.
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
        "qualite": [],
        "date": _now(),
    }
    # Mode compounders — poche adoptée le 18 nov. 2026
    try:
        crows, cverdict = analyse_compounders(d, checks)
        out["compounders"] = {"verdict": cverdict,
                              "rows": [{"label": l, "ok": bool(ok), "det": str(det)} for l, ok, det in crows]}
    except Exception:  # noqa: BLE001
        out["compounders"] = None
    # Qualité du capital — information hors note, mêmes seuils que print_fiche
    for label, lo in (("ROE", 0.0), ("ROIC", 0.0), ("ROIIC", -0.10), ("ROCE", 0.0)):
        val = _num(d.get(label.lower()))
        seuil = SEUILS_QUALITE[label]
        out["qualite"].append({"label": label, "val": val, "seuil": seuil, "lo": lo, "hi": 0.40,
                               "ok": (val >= seuil) if val is not None else None})
    if dcf.get("insignificant"):
        out["dcf_nd"] = dcf.get("note") or "DCF non significatif."
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
            print(f"{t} : {fiche['score']}/7 — {fiche['verdict']}")
        else:
            e = erreur
            print(f"Erreur pour {t} : {e}")
            old = fiches.get(t)
            if isinstance(old, dict) and old.get("checks"):
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
            sys.exit("Usage : rocky-score.py --json data.json [TICKERS...]")
        chemin = args[k + 1]
        tickers = entrees_brutes([t for t in args[:k] + args[k + 2:]
                                   if not t.startswith("-")])
        export_json(chemin, tickers or lire_tickers() or ["HOOD"])
    else:
        tickers = [t for t in args if not t.startswith("-")] or ["HOOD"]
        for t in tickers:
            try:
                print_fiche(t)
            except Exception as e:  # noqa: BLE001
                print(f"Erreur pour {t} : {e}")
