# Auditoría de pares (00-99) — Florida Pick 3 / Cash 3

## Aviso importante sobre la fuente de datos

Este entorno de ejecución tiene bloqueado por política organizacional el
acceso de red a los dominios de la Florida Lottery y a los agregadores de
terceros consultados (flalottery.com, files.floridalottery.com,
lotterypost.com, lotteryguru.com, entre otros — todos devuelven un rechazo
de política, no un error transitorio). Por lo tanto **esta herramienta no
trae datos reales precargados**: no existe un informe con el histórico
verdadero de Pick 3 generado en este repositorio.

Lo que sí incluye:

- `pick3_analysis.py` — el motor de análisis completo, listo para correr
  contra un CSV real que tú descargues (tu navegador sí tiene acceso a
  flalottery.com/floridalottery.com).
- `sample_data/formato_ejemplo.csv` — únicamente ilustra el formato de
  columnas esperado, con números inventados. No son resultados reales.
- `example_report_demo.md` — informe de ejemplo generado con datos
  **sintéticos aleatorios** (`--demo`), solo para validar que las 5
  secciones del análisis funcionan correctamente. Está marcado en el propio
  archivo como demo; no debe interpretarse como resultado real.

## Cómo obtener el CSV real

1. En `floridalottery.com` (sección "Winning Numbers" / "Pick 3" / historial),
   exporta o copia el histórico de sorteos que quieras auditar.
2. Arma un CSV con columnas `date,period,number` (`period` = `MID` o `EVE`),
   ordenado cronológicamente ascendente. Ver `sample_data/formato_ejemplo.csv`.
   Si tienes un identificador secuencial propio, puedes usar una columna
   `draw_id` o `sequence` en su lugar del orden por fecha.
3. El análisis toma el "par" como las dos últimas cifras del resultado de 3
   dígitos (se ignora la centena) — corresponde a la apuesta "Back Pair".

## Uso

```bash
# Con datos reales
python3 pick3_analysis.py --csv historico_real.csv --out reporte.md

# Demo con datos sintéticos (para validar la herramienta, NO es un análisis real)
python3 pick3_analysis.py --demo --draws 5000 --out reporte_demo.md
```

Parámetros opcionales: `--recent-window N` (tamaño de la ventana reciente
usada en la sección de decenas, por defecto 200 sorteos), `--seed` y
`--draws` (solo modo `--demo`).

## Qué calcula el informe

1. **Frecuencias** — top 10 y bottom 10 de los 100 pares posibles (00-99).
2. **Retrasos críticos** — sorteos transcurridos desde la última aparición
   de cada par, ranking de los 10 más atrasados.
3. **Distribución por decenas** — conteo por bloques de 10 (00-09 … 90-99)
   en el histórico completo y en la ventana reciente, con prueba
   chi-cuadrado contra la hipótesis de uniformidad.
4. **Invertidos/espejos** — compara la brecha media observada hasta que
   aparece el número espejo (dígitos invertidos) contra una línea base
   generada barajando la misma secuencia, para distinguir señal real de
   azar.
5. **Evaluación de aleatoriedad** — chi-cuadrado global sobre las 100
   categorías y una prueba de rachas de paridad; concluye si la muestra se
   aparta de un proceso uniforme e independiente al 95% de confianza.

## Nota estadística de fondo

Pick 3 / Cash 3 se extrae con un proceso físico o RNG certificado e
independiente en cada sorteo. Bajo ese diseño, "números atrasados" o
"fríos" no tienen mayor probabilidad de salir en el próximo sorteo que
cualquier otro — es la falacia del jugador. Las secciones 1 y 2 del informe
son descriptivas (muestran la variación muestral esperada), no predictivas.
La sección 5 es la que da la conclusión objetiva sobre si hay o no
desviación estadísticamente significativa de la aleatoriedad pura.
