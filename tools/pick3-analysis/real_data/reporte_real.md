# Informe cuantitativo — pares (00-99) Pick 3 Florida

Fuente de datos: CSV proporcionado por el usuario: C:\Users\luisd\AppData\Local\Temp\claude\C--Users-luisd-OneDrive-Desktop\7d08c1ef-f338-4266-b307-3ce9ac466dc8\scratchpad\pick3\historico_real.csv
Sorteos analizados (N): 20701

## 1. Top de frecuencias de pares (00-99)

### Los 10 mas frecuentes
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

### Los 10 menos frecuentes
| Par | Apariciones |
|---|---|
| 12 | 176 |
| 76 | 180 |
| 07 | 185 |
| 57 | 185 |
| 91 | 185 |
| 00 | 187 |
| 40 | 189 |
| 03 | 190 |
| 35 | 191 |
| 51 | 191 |

## 2. Analisis de retrasos criticos (ausencia actual)

Sorteos transcurridos desde la ultima aparicion de cada par, ranking de los 10 mas atrasados:

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

## 3. Distribucion por decenas

### Historico completo
| Decena | Apariciones | Esperado (uniforme) |
|---|---|---|
| 00-09 | 2054 | 2070.1 |
| 10-19 | 2038 | 2070.1 |
| 20-29 | 2088 | 2070.1 |
| 30-39 | 2091 | 2070.1 |
| 40-49 | 2050 | 2070.1 |
| 50-59 | 2072 | 2070.1 |
| 60-69 | 2079 | 2070.1 |
| 70-79 | 2069 | 2070.1 |
| 80-89 | 2074 | 2070.1 |
| 90-99 | 2086 | 2070.1 |

Chi-cuadrado (historico, df=9): 1.35 — critico 95%: 16.919

### Ultimos 200 sorteos
| Decena | Apariciones | Esperado (uniforme) |
|---|---|---|
| 00-09 | 22 | 20.0 |
| 10-19 | 19 | 20.0 |
| 20-29 | 18 | 20.0 |
| 30-39 | 11 | 20.0 |
| 40-49 | 23 | 20.0 |
| 50-59 | 20 | 20.0 |
| 60-69 | 24 | 20.0 |
| 70-79 | 18 | 20.0 |
| 80-89 | 21 | 20.0 |
| 90-99 | 24 | 20.0 |

Chi-cuadrado (ventana reciente, df=9): 6.80 — critico 95%: 16.919

## 4. Comportamiento de invertidos (espejos)

Brecha media observada hasta la aparicion del espejo: 99.99 sorteos (sobre 18459 pares con espejo distinto de si mismos).
Brecha media esperada bajo aleatorizacion (baseline, 200 barajados): 99.85 sorteos.
Percentil del valor observado respecto al baseline: 59.5%.
Un percentil cercano a 50% indica que no hay efecto distinguible del azar puro; valores extremos (cerca de 0% o 100%) sugeririan una asociacion no explicada por aleatoriedad.

## 5. Evaluacion de aleatoriedad

- Chi-cuadrado sobre las 100 categorias de pares (df=99): 83.44 — critico 95%: 123.225
- Prueba de rachas (paridad par/impar del resultado): 10346 rachas observadas, 10351.5 esperadas, z = -0.08 (|z| > 1.96 indicaria desviacion significativa al 95%).

**Conclusion tecnica:**
Ninguna de las pruebas anteriores supera el umbral de significancia del 95%. La muestra es consistente con un proceso de extraccion independiente y uniforme: no hay evidencia de patrones ciclicos explotables. Los rankings de frecuencia y retraso de las secciones 1 y 2 reflejan variacion aleatoria esperada (fluctuacion muestral), no una tendencia real, y no tienen valor predictivo sobre el proximo sorteo.
