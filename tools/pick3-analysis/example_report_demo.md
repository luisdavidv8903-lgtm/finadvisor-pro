# Informe cuantitativo — pares (00-99) Pick 3 Florida

Fuente de datos: **DATOS SINTETICOS (DEMO)** — 5000 sorteos generados con random.Random(seed=42). NO son resultados reales de la Florida Lottery.
Sorteos analizados (N): 5000

## 1. Top de frecuencias de pares (00-99)

### Los 10 mas frecuentes
| Par | Apariciones |
|---|---|
| 70 | 72 |
| 56 | 66 |
| 20 | 64 |
| 38 | 64 |
| 40 | 63 |
| 95 | 63 |
| 90 | 62 |
| 46 | 61 |
| 79 | 61 |
| 02 | 60 |

### Los 10 menos frecuentes
| Par | Apariciones |
|---|---|
| 13 | 28 |
| 99 | 36 |
| 22 | 37 |
| 98 | 38 |
| 15 | 39 |
| 55 | 39 |
| 00 | 40 |
| 04 | 40 |
| 37 | 40 |
| 28 | 41 |

## 2. Analisis de retrasos criticos (ausencia actual)

Sorteos transcurridos desde la ultima aparicion de cada par, ranking de los 10 mas atrasados:

| Par | Sorteos sin salir |
|---|---|
| 46 | 743 |
| 57 | 507 |
| 08 | 455 |
| 52 | 388 |
| 32 | 318 |
| 76 | 317 |
| 37 | 306 |
| 19 | 301 |
| 88 | 291 |
| 10 | 284 |

## 3. Distribucion por decenas

### Historico completo
| Decena | Apariciones | Esperado (uniforme) |
|---|---|---|
| 00-09 | 482 | 500.0 |
| 10-19 | 468 | 500.0 |
| 20-29 | 482 | 500.0 |
| 30-39 | 479 | 500.0 |
| 40-49 | 509 | 500.0 |
| 50-59 | 516 | 500.0 |
| 60-69 | 504 | 500.0 |
| 70-79 | 532 | 500.0 |
| 80-89 | 523 | 500.0 |
| 90-99 | 505 | 500.0 |

Chi-cuadrado (historico, df=9): 8.09 — critico 95%: 16.919

### Ultimos 200 sorteos
| Decena | Apariciones | Esperado (uniforme) |
|---|---|---|
| 00-09 | 21 | 20.0 |
| 10-19 | 15 | 20.0 |
| 20-29 | 17 | 20.0 |
| 30-39 | 20 | 20.0 |
| 40-49 | 22 | 20.0 |
| 50-59 | 17 | 20.0 |
| 60-69 | 22 | 20.0 |
| 70-79 | 19 | 20.0 |
| 80-89 | 22 | 20.0 |
| 90-99 | 25 | 20.0 |

Chi-cuadrado (ventana reciente, df=9): 4.10 — critico 95%: 16.919

## 4. Comportamiento de invertidos (espejos)

Brecha media observada hasta la aparicion del espejo: 98.24 sorteos (sobre 4462 pares con espejo distinto de si mismos).
Brecha media esperada bajo aleatorizacion (baseline, 200 barajados): 96.95 sorteos.
Percentil del valor observado respecto al baseline: 73.5%.
Un percentil cercano a 50% indica que no hay efecto distinguible del azar puro; valores extremos (cerca de 0% o 100%) sugeririan una asociacion no explicada por aleatoriedad.

## 5. Evaluacion de aleatoriedad

- Chi-cuadrado sobre las 100 categorias de pares (df=99): 108.28 — critico 95%: 123.225
- Prueba de rachas (paridad par/impar del resultado): 2502 rachas observadas, 2500.7 esperadas, z = 0.04 (|z| > 1.96 indicaria desviacion significativa al 95%).

**Conclusion tecnica:**
Ninguna de las pruebas anteriores supera el umbral de significancia del 95%. La muestra es consistente con un proceso de extraccion independiente y uniforme: no hay evidencia de patrones ciclicos explotables. Los rankings de frecuencia y retraso de las secciones 1 y 2 reflejan variacion aleatoria esperada (fluctuacion muestral), no una tendencia real, y no tienen valor predictivo sobre el proximo sorteo.
