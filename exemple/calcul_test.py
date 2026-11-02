"""Fichier de test du relais : calculs déterministes, bibliothèque standard uniquement.

Les sommes de flottants utilisent math.fsum (arrondi exact) : depuis Python 3.12,
sum() applique une sommation compensée, ce qui changerait les dernières décimales
selon la version de Python du conteneur.

Comparaison avec exemple/attendu.json : égalité exacte pour la somme des premiers et la
moyenne ; pour Simpson, tolérance absolue 1e-14, car math.sin dépend de la bibliothèque
mathématique du système (écart possible d'une unité sur le dernier bit).

1. Somme des 1000 premiers nombres premiers (crible d'Ératosthène) : valeur entière exacte.
2. Intégrale de sin sur [0, pi] par la méthode de Simpson composite (n = 1000) :
   valeur exacte 2 ; l'erreur théorique est d'ordre h^4.
3. Moyenne de 10 000 tirages uniformes avec random.seed(2026) (Mersenne Twister) :
   l'algorithme et l'initialisation par entier sont stables en CPython.
"""

import json
import math
import random


def premiers(n: int) -> list[int]:
    borne = 8000  # le 1000e nombre premier vaut 7919
    crible = bytearray([1]) * (borne + 1)
    crible[0:2] = b"\x00\x00"
    for i in range(2, int(borne**0.5) + 1):
        if crible[i]:
            crible[i * i :: i] = bytearray(len(crible[i * i :: i]))
    liste = [i for i, est_premier in enumerate(crible) if est_premier]
    return liste[:n]


def simpson(f, a: float, b: float, n: int) -> float:
    if n % 2:
        raise ValueError("n doit être pair")
    h = (b - a) / n
    impairs = math.fsum(f(a + k * h) for k in range(1, n, 2))
    pairs = math.fsum(f(a + k * h) for k in range(2, n, 2))
    return math.fsum([f(a), f(b), 4 * impairs, 2 * pairs]) * h / 3


def main() -> None:
    random.seed(2026)
    tirages = [random.random() for _ in range(10_000)]
    integrale = simpson(math.sin, 0.0, math.pi, 1000)
    resultat = {
        "somme_1000_premiers": sum(premiers(1000)),
        "simpson_sin_0_pi_n1000": f"{integrale:.15f}",
        "erreur_simpson": f"{abs(integrale - 2.0):.3e}",
        "moyenne_uniforme_graine_2026": f"{math.fsum(tirages) / len(tirages):.15f}",
    }
    print(json.dumps(resultat, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
