"""Genera el segmento nuevo de tráfico: claims en inglés de etiquetas para exportación a EE. UU.

Escenario de data drift del game day (INC-03): CertiBajío empieza a recibir etiquetas de
clientes que exportan a EE. UU. (21 CFR 101). El modelo v1 se entrenó solo con claims en
español, así que estos textos caen fuera de su vocabulario.

Los claims se arman con plantillas (como el dataset de la Actividad 4) y siguen las mismas
reglas de etiquetado: NUTRIMENTAL = contenido/comparación/cantidad de un nutrimento,
SALUD = relación nutrimento-salud o beneficio, AUSENCIA = ausencia de alérgeno, aditivo o
ingrediente que no es nutrimento, MARKETING = origen, proceso, sabor o sostenibilidad.

Salida: data/claims_exportacion_en.csv con dos particiones
  - trafico  (75 %): se envía a la API durante el game day; los revisores lo etiquetan.
  - holdout  (25 %): nunca se envía ni se usa para entrenar; sirve a la compuerta de validación.

Uso:  python scripts/generar_segmento_exportacion.py
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "data" / "claims_exportacion_en.csv"
SEMILLA = 2026
POR_CATEGORIA = 60

CLAIMS = {
    "NUTRIMENTAL": [
        "Low fat", "Fat free", "Low sodium", "Very low sodium", "Reduced sugar", "Sugar free",
        "No added sugars", "Excellent source of fiber", "Good source of calcium", "High in protein",
        "Rich in vitamin D", "Light", "25% less sodium than our original recipe",
        "30% less fat than regular potato chips", "10 g of protein per serving",
        "Contains 3 g of fiber per serving", "Provides 20% of the Daily Value of iron", "Low calorie",
        "Calorie free", "Half the sugar of leading brands", "Good source of omega-3",
    ],
    "SALUD": [
        "Calcium helps build strong bones",
        "Adequate calcium and vitamin D may reduce the risk of osteoporosis",
        "Diets low in sodium may reduce the risk of high blood pressure",
        "Soluble fiber from oats may reduce the risk of heart disease",
        "Supports immune health", "Helps maintain healthy digestion", "Boosts your natural energy",
        "Supports a healthy heart", "Promotes restful sleep", "Vitamin C supports normal immune function",
        "Potassium supports healthy blood pressure", "Probiotics support gut health",
        "Helps you stay focused", "Folate may reduce the risk of neural tube defects",
        "Supports healthy weight management", "Antioxidants help protect your cells",
        "Helps your muscles recover after exercise",
    ],
    "AUSENCIA": [
        "Gluten free", "Contains no peanuts", "Dairy free", "Made without eggs", "No artificial colors",
        "No artificial flavors", "No preservatives", "Non-GMO", "No high fructose corn syrup",
        "Free from the top 9 allergens", "Lactose free", "Soy free", "Caffeine free", "No MSG added",
        "Palm oil free", "Made in a nut free facility", "No artificial sweeteners",
    ],
    "MARKETING": [
        "Made in Texas", "Farm fresh taste", "New and improved recipe", "Family favorite since 1952",
        "Small batch", "Hand crafted", "Kids love it", "Premium quality", "100% recyclable packaging",
        "Ready in 3 minutes", "Fair trade certified", "Grown in California", "Award winning taste",
        "Deliciously crunchy", "Original recipe", "Sustainably sourced ingredients", "Baked fresh daily",
    ],
}

PRODUCTOS = ["Granola bar", "Greek yogurt", "Whole wheat bread", "Orange juice", "Crackers",
             "Peanut butter", "Oat milk", "Tortilla chips", "Cheddar cheese", "Tomato soup",
             "Pasta sauce", "Breakfast cereal", "Trail mix", "Almond beverage", "Frozen burrito"]

PATRONES = ["{c}", "{p}: {c}", "{c}!", "{p} - {c}", "{c}. {p}"]


def generar() -> list[dict]:
    rng = random.Random(SEMILLA)
    filas = []
    for categoria, base in CLAIMS.items():
        candidatos = {}
        for c in base:
            for p in PRODUCTOS:
                for patron in PATRONES:
                    texto = patron.format(c=c, p=p) if "{p}" in patron else patron.format(c=c)
                    candidatos[texto] = c  # dedup: los patrones sin producto se repiten
        textos = sorted(candidatos)
        rng.shuffle(textos)
        # Primero un ejemplo de cada claim base (cobertura) y luego el resto al azar.
        elegidos, vistos = [], set()
        for t in textos:
            if candidatos[t] not in vistos:
                elegidos.append(t)
                vistos.add(candidatos[t])
        for t in textos:
            if len(elegidos) >= POR_CATEGORIA:
                break
            if t not in elegidos:
                elegidos.append(t)
        rng.shuffle(elegidos)
        corte = int(round(POR_CATEGORIA * 0.75))
        for i, t in enumerate(elegidos[:POR_CATEGORIA]):
            filas.append({"texto_claim": t, "categoria": categoria, "claim_base": candidatos[t],
                          "idioma": "en", "mercado": "EE. UU. (21 CFR 101)",
                          "particion": "trafico" if i < corte else "holdout"})
    rng.shuffle(filas)
    for i, f in enumerate(filas, 1):
        f["id_claim"] = f"EXP-{i:04d}"
    return filas


def main() -> None:
    filas = generar()
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    columnas = ["id_claim", "texto_claim", "categoria", "claim_base", "idioma", "mercado", "particion"]
    with open(SALIDA, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas)
        w.writeheader()
        w.writerows(filas)
    n_t = sum(f["particion"] == "trafico" for f in filas)
    print(f"{len(filas)} claims → {SALIDA.relative_to(RAIZ)} (tráfico {n_t}, holdout {len(filas) - n_t})")


if __name__ == "__main__":
    main()
