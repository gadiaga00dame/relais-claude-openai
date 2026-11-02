FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.py relais.py adaptateur_openai.py serveur.py ./

# La base SQLite (fils, tâches, journal, compteurs) doit être sur un disque persistant.
ENV CHEMIN_BASE=/donnees/relais.sqlite3
RUN mkdir -p /donnees

EXPOSE 8000
# Un seul processus : l'état des tâches en cours est tenu en mémoire et la base est locale.
CMD ["sh", "-c", "uvicorn serveur:creer_application_depuis_environnement --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
