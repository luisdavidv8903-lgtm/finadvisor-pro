# Informe enfocado — pares (00-99) Pick 3 Florida

Fuente: historico_real.csv (20701 sorteos, 1988-04-29 a 2026-09-11)

## 1. Top de pares

### 10 mas frecuentes (historico completo)
| Par | Apariciones |
|---|---|
| 11 | 244 |
| 74 | 239 |
| 06 | 234 |
| 44 | 234 |
| 58 | 232 |
| 95 | 232 |
| 01 | 229 |
| 62 | 229 |
| 99 | 225 |
| 33 | 224 |

### 10 con mayor retraso actual (sorteos sin salir)
| Par | Sorteos sin salir |
|---|---|
| 51 | 422 |
| 84 | 358 |
| 26 | 327 |
| 63 | 322 |
| 68 | 304 |
| 73 | 297 |
| 34 | 263 |
| 86 | 248 |
| 47 | 244 |
| 24 | 241 |

## 2. Bloques de decenas — ultimos 100 sorteos vs. media historica

| Decena | Ultimos 100 | % ultimos 100 | Media historica (%) |
|---|---|---|---|
| 00-09 | 9 | 9.0% | 9.9% |
| 10-19 | 6 | 6.0% | 9.8% |
| 20-29 | 9 | 9.0% | 10.1% |
| 30-39 | 6 | 6.0% | 10.1% |
| 40-49 | 13 | 13.0% | 9.9% |
| 50-59 | 10 | 10.0% | 10.0% |
| 60-69 | 16 | 16.0% | 10.0% |
| 70-79 | 9 | 9.0% | 10.0% |
| 80-89 | 10 | 10.0% | 10.0% |
| 90-99 | 12 | 12.0% | 10.1% |

Chi-cuadrado ultimos 100 (df=9): 8.40 — critico 95%: 16.919 (si supera el critico, la ventana reciente se aparta de la uniformidad esperada)

## 3. Analisis de invertidos (espejos)

Tras la aparicion de un par, su espejo (dígitos invertidos, p.ej. 27 -> 72) tarda en promedio **99.99 sorteos** en volver a salir (sobre 18459 ocurrencias con espejo distinto de si mismo).
Baseline esperado bajo puro azar (barajando el mismo historico, 200 veces): 99.85 sorteos.
El valor observado cae en el percentil **59.5%** de esa distribucion aleatoria — cercano a 50% significa que el ciclo de ausencia del espejo no se comporta distinto de lo que produciria el azar puro (no hay ventaja explotable en 'esperar' al invertido).

## 4. Ventana reciente — ultimos 30 dias calendario (2026-08-12 a 2026-09-11, 60 sorteos)

### Pares que salieron 2+ veces en la ventana (posible tendencia de corto plazo)
| Par | Apariciones en 30d | Esperado bajo uniformidad |
|---|---|---|
| 09 | 3 | 0.60 |
| 25 | 3 | 0.60 |
| 44 | 3 | 0.60 |
| 66 | 3 | 0.60 |
| 11 | 2 | 0.60 |
| 28 | 2 | 0.60 |
| 39 | 2 | 0.60 |
| 40 | 2 | 0.60 |
| 55 | 2 | 0.60 |
| 60 | 2 | 0.60 |
| 67 | 2 | 0.60 |
| 89 | 2 | 0.60 |

De 60 sorteos salieron 44 pares distintos de los 100 posibles (bajo puro azar, con 60 sorteos se esperan aprox. 45 pares distintos).

**Nota:** una ventana de 30 dias (~60 sorteos, hay 2 por dia) es estadisticamente pequena — cualquier repeticion aislada es ruido esperado, no una tendencia real. Ver seccion 5 del informe completo (reporte_real.md) para la evaluacion formal de aleatoriedad sobre el historico completo.
