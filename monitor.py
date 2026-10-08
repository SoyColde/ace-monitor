import hashlib
import os
import sqlite3
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

# ============ CONFIGURACIÓN: REEMPLAZA LO QUE DIGA CAMBIAR ============
LOGIN_URL = "https://apiv2.aceenlinea.com/api/auth/login"          # la URL del POST de login
JOBS_URL = "https://apiv2.aceenlinea.com/api/candidate/candidatejobsavailable"
API_KEY_HEADER = "Api-Key"  # ej: x-api-key (cópialo tal cual de Network)
EMAIL_FIELD = "email"                         # nombre del campo en el JSON del login
PASSWORD_FIELD = "password"                   # nombre del campo en el JSON del login
# Formato del header de autorización. Ajusta si no es "Bearer <token>":
AUTH_HEADER_NAME = "Authorization"
AUTH_HEADER_PREFIX = "Bearer "
DB_FILE = "jobs.db"
MAX_FAILS = 3                                 # fallos seguidos antes de avisarte
# =======================================================================

API_KEY = os.getenv("ACE_API_KEY")
EMAIL = os.getenv("ACE_EMAIL")
PASSWORD = os.getenv("ACE_PASSWORD")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")


def login():
    """Hace login y devuelve el token (la respuesta llega como text/plain)."""
    r = requests.post(
        LOGIN_URL,
        json={EMAIL_FIELD: EMAIL, PASSWORD_FIELD: PASSWORD},
        headers={API_KEY_HEADER: API_KEY},
        timeout=30,
    )
    r.raise_for_status()
    token = r.text.strip().strip('"')  # quita espacios y comillas si las trae
    if not token:
        raise RuntimeError("El login no devolvió token")
    return token


def fetch_jobs(token):
    """Devuelve la lista de plazas (JSON)."""
    r = requests.get(
        JOBS_URL,
        headers={
            API_KEY_HEADER: API_KEY,
            AUTH_HEADER_NAME: AUTH_HEADER_PREFIX + token,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen_jobs "
        "(id TEXT PRIMARY KEY, title TEXT, first_seen TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)")
    return con


def fingerprints(j):
    """Identifica una plaza por su contenido y por workPlaceItemID (no por subCode)."""
    def n(x):
        return str(x if x is not None else "").strip().lower()

    campos = ("companyName", "positionName", "state", "city",
              "paymentByHour", "paymentByHourExtra",
              "dateBegin", "dateEnd", "housingIsIncluded")
    fp = hashlib.sha1("|".join(n(j.get(c)) for c in campos).encode()).hexdigest()
    keys = ["fp:" + fp]
    if j.get("workPlaceItemID") is not None:
        keys.append("wp:" + str(j["workPlaceItemID"]))
    return keys


def find_new(con, jobs):
    """Devuelve las plazas nuevas: no coincide ni el contenido ni el workPlaceItemID."""
    new = []
    for j in jobs:
        keys = fingerprints(j)
        marcas = ",".join("?" * len(keys))
        vista = con.execute(
            f"SELECT 1 FROM seen_jobs WHERE id IN ({marcas})", keys
        ).fetchone()
        if not vista:
            new.append(j)
        for k in keys:
            con.execute(
                "INSERT OR IGNORE INTO seen_jobs VALUES (?,?,datetime('now'))",
                (k, j.get("positionName")),
            )
    con.commit()
    return new


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": text},
        timeout=30,
    )
    r.raise_for_status()


def format_job(j):
    vivienda = "Sí" if j.get("housingIsIncluded") else "No"
    return (
        f"NUEVA PLAZA: {j.get('positionName')}\n"
        f"Empresa: {j.get('companyName')}\n"
        f"Lugar: {j.get('city')}, {j.get('state')}\n"
        f"Pago/hora: ${j.get('paymentByHour')}\n"
        f"Vivienda incluida: {vivienda}\n"
        f"Fechas: {str(j.get('dateBegin'))[:10]} a {str(j.get('dateEnd'))[:10]}\n"
        f"Cupos: {j.get('cantidad')}\n"
        f"Código: {j.get('subCode')}"
    )


def get_fails(con):
    row = con.execute("SELECT value FROM state WHERE key='fails'").fetchone()
    return row[0] if row else 0


def set_fails(con, n):
    con.execute("INSERT OR REPLACE INTO state VALUES ('fails', ?)", (n,))
    con.commit()


def main():
    con = get_db()
    try:
        first_run = con.execute(
            "SELECT COUNT(*) FROM seen_jobs WHERE id LIKE 'fp:%'"
        ).fetchone()[0] == 0
        token = login()
        jobs = fetch_jobs(token)
        new = find_new(con, jobs)
        set_fails(con, 0)

        if first_run:
            # Primera vez: guarda todo sin avisar plaza por plaza
            send_telegram(f"Monitor iniciado. {len(jobs)} plazas guardadas como ya vistas.")
        else:
            for j in new:
                send_telegram(format_job(j))
        print(f"Plazas: {len(jobs)} | Nuevas: {0 if first_run else len(new)}")
    except Exception as e:
        fails = get_fails(con) + 1
        set_fails(con, fails)
        print(f"Error ({fails} seguidos): {type(e).__name__}: {e}", file=sys.stderr)
        if fails == MAX_FAILS:
            try:
                send_telegram(f"El monitor lleva {fails} fallos seguidos: {type(e).__name__}. Revísalo.")
            except Exception:
                pass
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
