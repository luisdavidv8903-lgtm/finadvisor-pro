#!/usr/bin/env python3
"""Auditoria estadistica de pares (00-99) para Florida Lottery Pick 3 / Cash 3.

Uso:
    python3 pick3_analysis.py --csv historico.csv --out reporte.md
    python3 pick3_analysis.py --demo --draws 5000 --out reporte_demo.md

Formato de CSV esperado (columnas por nombre, orden libre):
    date     - fecha del sorteo, YYYY-MM-DD
    period   - MID o EVE (mediodia / noche); opcional si hay una sola columna
    number   - el resultado de 3 cifras del sorteo, p.ej. "348" (con o sin ceros a la izquierda)

El archivo debe venir ordenado cronologicamente ascendente (mas antiguo primero).
Si existe una columna "draw_id" o "sequence" con un contador estrictamente
creciente, se usa esa para el orden en vez de date+period.

El "par" analizado es el numero de dos cifras formado por las decenas y
unidades del resultado (se ignora la centena), es decir number % 100.
Esto corresponde a la apuesta conocida como "Back Pair" en Pick 3.
"""

import argparse
import csv
import random
import statistics
import sys
from collections import Counter, defaultdict

CHI2_CRITICAL_95 = {
    9: 16.919,
    99: 123.225,
}


def load_draws_csv(path):
    pairs = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    has_seq = rows and ("draw_id" in rows[0] or "sequence" in rows[0])
    if has_seq:
        key = "draw_id" if "draw_id" in rows[0] else "sequence"
        rows.sort(key=lambda r: int(r[key]))
    else:
        period_rank = {"MID": 0, "EVE": 1}
        rows.sort(key=lambda r: (r["date"], period_rank.get(r.get("period", "MID"), 0)))
    for r in rows:
        raw = r["number"].strip()
        n = int(raw)
        pairs.append(n % 100)
    return pairs


def generate_synthetic_draws(n, seed=42):
    rng = random.Random(seed)
    return [rng.randint(0, 999) % 100 for _ in range(n)]


def frequency_analysis(pairs):
    counts = Counter(pairs)
    full = [(p, counts.get(p, 0)) for p in range(100)]
    top10 = sorted(full, key=lambda x: (-x[1], x[0]))[:10]
    bottom10 = sorted(full, key=lambda x: (x[1], x[0]))[:10]
    return counts, top10, bottom10


def delay_analysis(pairs):
    last_seen = {}
    for idx, p in enumerate(pairs):
        last_seen[p] = idx
    n = len(pairs)
    gaps = []
    for p in range(100):
        if p in last_seen:
            gap = (n - 1) - last_seen[p]
        else:
            gap = n  # nunca ha salido en la muestra
        gaps.append((p, gap))
    ranking = sorted(gaps, key=lambda x: (-x[1], x[0]))[:10]
    return dict(gaps), ranking


def decade_analysis(pairs, recent_window=200):
    def bucket_counts(seq):
        c = [0] * 10
        for p in seq:
            c[p // 10] += 1
        return c

    full_counts = bucket_counts(pairs)
    n = len(pairs)
    expected_full = n / 10.0
    chi2_full = sum((c - expected_full) ** 2 / expected_full for c in full_counts) if n else 0.0

    recent = pairs[-recent_window:] if n > recent_window else pairs[:]
    recent_counts = bucket_counts(recent)
    m = len(recent)
    expected_recent = m / 10.0 if m else 1.0
    chi2_recent = sum((c - expected_recent) ** 2 / expected_recent for c in recent_counts) if m else 0.0

    return {
        "full_counts": full_counts,
        "chi2_full": chi2_full,
        "recent_counts": recent_counts,
        "recent_window": m,
        "chi2_recent": chi2_recent,
    }


def mirror_of(p):
    tens, units = divmod(p, 10)
    return units * 10 + tens


def mirror_analysis(pairs, n_shuffles=200, seed=7):
    n = len(pairs)

    def mean_gap_to_mirror(seq):
        positions = defaultdict(list)
        for idx, p in enumerate(seq):
            positions[p].append(idx)
        gaps = []
        for idx, p in enumerate(seq):
            m = mirror_of(p)
            if m == p:
                continue
            later = [j for j in positions[m] if j > idx]
            if later:
                gaps.append(later[0] - idx)
        return statistics.mean(gaps) if gaps else None, len(gaps)

    observed_mean, observed_count = mean_gap_to_mirror(pairs)

    rng = random.Random(seed)
    shuffled_means = []
    for _ in range(n_shuffles):
        shuf = pairs[:]
        rng.shuffle(shuf)
        m, _ = mean_gap_to_mirror(shuf)
        if m is not None:
            shuffled_means.append(m)

    if observed_mean is None or not shuffled_means:
        return {
            "observed_mean_gap": observed_mean,
            "observed_pairs_with_mirror": observed_count,
            "baseline_mean": None,
            "percentile": None,
        }

    baseline_mean = statistics.mean(shuffled_means)
    more_extreme = sum(1 for m in shuffled_means if m <= observed_mean)
    percentile = more_extreme / len(shuffled_means)

    return {
        "observed_mean_gap": observed_mean,
        "observed_pairs_with_mirror": observed_count,
        "baseline_mean": baseline_mean,
        "percentile": percentile,
        "n_shuffles": len(shuffled_means),
    }


def randomness_evaluation(pairs, counts):
    n = len(pairs)
    expected = n / 100.0
    chi2_100 = sum((counts.get(p, 0) - expected) ** 2 / expected for p in range(100)) if n else 0.0

    # runs test simple: alterna par/impar de la suma de digitos del par (proxy de paridad)
    seq = [1 if p % 2 == 0 else 0 for p in pairs]
    runs = 1
    for i in range(1, len(seq)):
        if seq[i] != seq[i - 1]:
            runs += 1
    n1 = seq.count(1)
    n0 = seq.count(0)
    if n1 and n0:
        expected_runs = (2 * n1 * n0) / n + 1
        var_runs = (2 * n1 * n0 * (2 * n1 * n0 - n)) / (n * n * (n - 1))
        z_runs = (runs - expected_runs) / (var_runs ** 0.5) if var_runs > 0 else 0.0
    else:
        expected_runs = None
        z_runs = None

    return {
        "chi2_100": chi2_100,
        "chi2_100_critical_95": CHI2_CRITICAL_95[99],
        "runs_observed": runs,
        "runs_expected": expected_runs,
        "runs_z": z_runs,
    }


def fmt_pair(p):
    return f"{p:02d}"


def build_report(pairs, source_label, decade_window=200):
    n = len(pairs)
    counts, top10, bottom10 = frequency_analysis(pairs)
    gaps, delay_ranking = delay_analysis(pairs)
    decades = decade_analysis(pairs, recent_window=decade_window)
    mirror = mirror_analysis(pairs)
    rnd = randomness_evaluation(pairs, counts)

    lines = []
    lines.append(f"# Informe cuantitativo — pares (00-99) Pick 3 Florida\n")
    lines.append(f"Fuente de datos: {source_label}")
    lines.append(f"Sorteos analizados (N): {n}\n")

    lines.append("## 1. Top de frecuencias de pares (00-99)\n")
    lines.append("### Los 10 mas frecuentes")
    lines.append("| Par | Apariciones |")
    lines.append("|---|---|")
    for p, c in top10:
        lines.append(f"| {fmt_pair(p)} | {c} |")
    lines.append("\n### Los 10 menos frecuentes")
    lines.append("| Par | Apariciones |")
    lines.append("|---|---|")
    for p, c in bottom10:
        lines.append(f"| {fmt_pair(p)} | {c} |")

    lines.append("\n## 2. Analisis de retrasos criticos (ausencia actual)\n")
    lines.append("Sorteos transcurridos desde la ultima aparicion de cada par, ranking de los 10 mas atrasados:\n")
    lines.append("| Par | Sorteos sin salir |")
    lines.append("|---|---|")
    for p, g in delay_ranking:
        lines.append(f"| {fmt_pair(p)} | {g} |")

    lines.append("\n## 3. Distribucion por decenas\n")
    lines.append("### Historico completo")
    lines.append("| Decena | Apariciones | Esperado (uniforme) |")
    lines.append("|---|---|---|")
    expected_full = n / 10.0
    for i, c in enumerate(decades["full_counts"]):
        lines.append(f"| {i}0-{i}9 | {c} | {expected_full:.1f} |")
    lines.append(f"\nChi-cuadrado (historico, df=9): {decades['chi2_full']:.2f} — critico 95%: {CHI2_CRITICAL_95[9]}")

    lines.append(f"\n### Ultimos {decades['recent_window']} sorteos")
    lines.append("| Decena | Apariciones | Esperado (uniforme) |")
    lines.append("|---|---|---|")
    expected_recent = decades["recent_window"] / 10.0
    for i, c in enumerate(decades["recent_counts"]):
        lines.append(f"| {i}0-{i}9 | {c} | {expected_recent:.1f} |")
    lines.append(f"\nChi-cuadrado (ventana reciente, df=9): {decades['chi2_recent']:.2f} — critico 95%: {CHI2_CRITICAL_95[9]}")

    lines.append("\n## 4. Comportamiento de invertidos (espejos)\n")
    if mirror["observed_mean_gap"] is not None:
        lines.append(f"Brecha media observada hasta la aparicion del espejo: {mirror['observed_mean_gap']:.2f} sorteos "
                      f"(sobre {mirror['observed_pairs_with_mirror']} pares con espejo distinto de si mismos).")
        lines.append(f"Brecha media esperada bajo aleatorizacion (baseline, {mirror['n_shuffles']} barajados): "
                      f"{mirror['baseline_mean']:.2f} sorteos.")
        lines.append(f"Percentil del valor observado respecto al baseline: {mirror['percentile']*100:.1f}%.")
        lines.append("Un percentil cercano a 50% indica que no hay efecto distinguible del azar puro; "
                      "valores extremos (cerca de 0% o 100%) sugeririan una asociacion no explicada por aleatoriedad.")
    else:
        lines.append("Muestra insuficiente para estimar la brecha hacia el numero espejo.")

    lines.append("\n## 5. Evaluacion de aleatoriedad\n")
    lines.append(f"- Chi-cuadrado sobre las 100 categorias de pares (df=99): {rnd['chi2_100']:.2f} "
                 f"— critico 95%: {rnd['chi2_100_critical_95']}")
    if rnd["runs_expected"] is not None:
        lines.append(f"- Prueba de rachas (paridad par/impar del resultado): {rnd['runs_observed']} rachas observadas, "
                     f"{rnd['runs_expected']:.1f} esperadas, z = {rnd['runs_z']:.2f} "
                     f"(|z| > 1.96 indicaria desviacion significativa al 95%).")
    conclusion_flags = []
    if rnd["chi2_100"] > rnd["chi2_100_critical_95"]:
        conclusion_flags.append("la distribucion de pares se desvia de la uniformidad esperada")
    if decades["chi2_full"] > CHI2_CRITICAL_95[9]:
        conclusion_flags.append("la distribucion por decenas se desvia de la uniformidad esperada")
    if rnd["runs_z"] is not None and abs(rnd["runs_z"]) > 1.96:
        conclusion_flags.append("la secuencia de paridad muestra dependencia serial significativa")

    lines.append("\n**Conclusion tecnica:**")
    if conclusion_flags:
        lines.append("Se detectaron senales estadisticamente significativas al 95%: " + "; ".join(conclusion_flags) +
                      ". Esto amerita revisar el tamano de muestra, la fuente de datos y posibles sesgos de recoleccion "
                      "antes de interpretar cualquier patron como real, ya que Pick 3 es un sorteo de extraccion "
                      "independiente por diseno y estas pruebas tambien producen falsos positivos por azar "
                      "(especialmente al examinar muchas categorias simultaneamente).")
    else:
        lines.append("Ninguna de las pruebas anteriores supera el umbral de significancia del 95%. La muestra es "
                      "consistente con un proceso de extraccion independiente y uniforme: no hay evidencia de "
                      "patrones ciclicos explotables. Los rankings de frecuencia y retraso de las secciones 1 y 2 "
                      "reflejan variacion aleatoria esperada (fluctuacion muestral), no una tendencia real, y no "
                      "tienen valor predictivo sobre el proximo sorteo.")

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="ruta al CSV con el historico real de sorteos")
    ap.add_argument("--demo", action="store_true", help="usar datos sinteticos (aleatorios) en vez de --csv")
    ap.add_argument("--draws", type=int, default=5000, help="numero de sorteos sinteticos a generar en modo --demo")
    ap.add_argument("--seed", type=int, default=42, help="semilla del generador aleatorio en modo --demo")
    ap.add_argument("--recent-window", type=int, default=200, help="tamano de la ventana reciente para la seccion 3")
    ap.add_argument("--out", default="reporte.md", help="archivo de salida (Markdown)")
    args = ap.parse_args()

    if args.demo:
        pairs = generate_synthetic_draws(args.draws, seed=args.seed)
        source_label = (f"**DATOS SINTETICOS (DEMO)** — {args.draws} sorteos generados con random.Random(seed={args.seed}). "
                         f"NO son resultados reales de la Florida Lottery.")
    elif args.csv:
        pairs = load_draws_csv(args.csv)
        source_label = f"CSV proporcionado por el usuario: {args.csv}"
    else:
        print("Debes indicar --csv <archivo> o --demo", file=sys.stderr)
        sys.exit(1)

    report = build_report(pairs, source_label, decade_window=args.recent_window)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Informe escrito en {args.out} ({len(pairs)} sorteos procesados)")


if __name__ == "__main__":
    main()
