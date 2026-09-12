#!/usr/bin/env python3
"""Informe enfocado (4 secciones pedidas) sobre historico_real.csv.

Reutiliza el motor de pick3_analysis.py; solo cambia el recorte y la
presentacion. No reemplaza el informe completo (reporte_real.md).
"""
import csv
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pick3_analysis import (
    frequency_analysis, delay_analysis, decade_analysis, mirror_analysis, fmt_pair
)

CSV_PATH = Path(__file__).resolve().parent / "historico_real.csv"
OUT_PATH = Path(__file__).resolve().parent / "reporte_focus_4secciones.md"


def load_with_dates(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    period_rank = {"MID": 0, "EVE": 1}
    rows.sort(key=lambda r: (r["date"], period_rank.get(r.get("period", "MID"), 0)))
    dated_pairs = []
    for r in rows:
        d = datetime.date.fromisoformat(r["date"])
        pair = int(r["number"].strip()) % 100
        dated_pairs.append((d, pair))
    return dated_pairs


def main():
    dated = load_with_dates(CSV_PATH)
    pairs = [p for _, p in dated]
    n = len(pairs)
    last_date = dated[-1][0]

    lines = []
    lines.append("# Informe enfocado — pares (00-99) Pick 3 Florida\n")
    lines.append(f"Fuente: historico_real.csv ({n} sorteos, {dated[0][0]} a {last_date})\n")

    # 1. Top de pares: 10 mas frecuentes + 10 con mayor retraso (historico completo)
    counts, top10, bottom10 = frequency_analysis(pairs)
    _, delay_ranking = delay_analysis(pairs)
    lines.append("## 1. Top de pares\n")
    lines.append("### 10 mas frecuentes (historico completo)")
    lines.append("| Par | Apariciones |")
    lines.append("|---|---|")
    for p, c in top10:
        lines.append(f"| {fmt_pair(p)} | {c} |")
    lines.append("\n### 10 con mayor retraso actual (sorteos sin salir)")
    lines.append("| Par | Sorteos sin salir |")
    lines.append("|---|---|")
    for p, g in delay_ranking:
        lines.append(f"| {fmt_pair(p)} | {g} |")

    # 2. Bloques de decenas: ultimos 100 sorteos vs media historica
    decades = decade_analysis(pairs, recent_window=100)
    lines.append("\n## 2. Bloques de decenas — ultimos 100 sorteos vs. media historica\n")
    lines.append("| Decena | Ultimos 100 | % ultimos 100 | Media historica (%) |")
    lines.append("|---|---|---|---|")
    expected_pct = 10.0  # uniforme
    hist_pct_by_decade = [c / n * 100 for c in decades["full_counts"]]
    for i, c in enumerate(decades["recent_counts"]):
        lines.append(f"| {i}0-{i}9 | {c} | {c/decades['recent_window']*100:.1f}% | {hist_pct_by_decade[i]:.1f}% |")
    lines.append(f"\nChi-cuadrado ultimos 100 (df=9): {decades['chi2_recent']:.2f} — critico 95%: 16.919 "
                 f"(si supera el critico, la ventana reciente se aparta de la uniformidad esperada)")

    # 3. Invertidos/espejos tras ciclo de ausencia
    mirror = mirror_analysis(pairs)
    lines.append("\n## 3. Analisis de invertidos (espejos)\n")
    if mirror["observed_mean_gap"] is not None:
        lines.append(f"Tras la aparicion de un par, su espejo (dígitos invertidos, p.ej. 27 -> 72) tarda en "
                      f"promedio **{mirror['observed_mean_gap']:.2f} sorteos** en volver a salir "
                      f"(sobre {mirror['observed_pairs_with_mirror']} ocurrencias con espejo distinto de si mismo).")
        lines.append(f"Baseline esperado bajo puro azar (barajando el mismo historico, {mirror['n_shuffles']} veces): "
                      f"{mirror['baseline_mean']:.2f} sorteos.")
        lines.append(f"El valor observado cae en el percentil **{mirror['percentile']*100:.1f}%** de esa distribucion "
                      f"aleatoria — cercano a 50% significa que el ciclo de ausencia del espejo no se comporta distinto "
                      f"de lo que produciria el azar puro (no hay ventaja explotable en 'esperar' al invertido).")
    else:
        lines.append("Muestra insuficiente para estimar la brecha hacia el numero espejo.")

    # 4. Ventana reciente: ultimos 30 dias calendario
    cutoff = last_date - datetime.timedelta(days=30)
    recent_30 = [(d, p) for d, p in dated if d > cutoff]
    recent_pairs_30 = [p for _, p in recent_30]
    m = len(recent_pairs_30)
    counts_30 = {}
    for p in recent_pairs_30:
        counts_30[p] = counts_30.get(p, 0) + 1
    ranked_30 = sorted(counts_30.items(), key=lambda x: (-x[1], x[0]))
    expected_30 = m / 100.0 if m else 0

    lines.append(f"\n## 4. Ventana reciente — ultimos 30 dias calendario ({cutoff} a {last_date}, {m} sorteos)\n")
    lines.append("### Pares que salieron 2+ veces en la ventana (posible tendencia de corto plazo)")
    repeats = [(p, c) for p, c in ranked_30 if c >= 2]
    if repeats:
        lines.append("| Par | Apariciones en 30d | Esperado bajo uniformidad |")
        lines.append("|---|---|---|")
        for p, c in repeats:
            lines.append(f"| {fmt_pair(p)} | {c} | {expected_30:.2f} |")
    else:
        lines.append("Ningun par se repitio 2+ veces en la ventana — comportamiento esperable con "
                      f"~{expected_30:.2f} apariciones esperadas por par en {m} sorteos.")
    n_distinct = len(counts_30)
    lines.append(f"\nDe {m} sorteos salieron {n_distinct} pares distintos de los 100 posibles "
                 f"(bajo puro azar, con {m} sorteos se esperan aprox. {100*(1-((99/100)**m)):.0f} pares distintos).")
    lines.append("\n**Nota:** una ventana de 30 dias (~60 sorteos, hay 2 por dia) es estadisticamente pequena — "
                  "cualquier repeticion aislada es ruido esperado, no una tendencia real. Ver seccion 5 del "
                  "informe completo (reporte_real.md) para la evaluacion formal de aleatoriedad sobre el "
                  "historico completo.")

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Informe escrito en {OUT_PATH} ({n} sorteos historicos, {m} en ventana de 30 dias)")


if __name__ == "__main__":
    main()
