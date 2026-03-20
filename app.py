import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
import requests as req_lib

app = Flask(__name__)
BASE_API  = "https://koyzone.xyz"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

GENRE_CACHE_FILE = os.path.join(CACHE_DIR, "genres.json")
FILM_CACHE_FILE  = os.path.join(CACHE_DIR, "films_with_genre.json")

# ── helpers ───────────────────────────────────────────────────────────────────

def fetch_json(url):
    try:
        r = req_lib.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def unwrap_films(data):
    if isinstance(data, list):  return data
    if isinstance(data, dict):  return data.get("films") or data.get("games") or []
    return []


# ── genre cache builder ───────────────────────────────────────────────────────

_building = False
_build_progress = {"done": 0, "total": 0}


def _fetch_genre(film):
    detail = fetch_json(f"{BASE_API}/data_movie.php?film_id={film['id']}")
    if detail and isinstance(detail, dict):
        f = detail.get("film", {})
        genre = f.get("genre", "")
        synopsis = f.get("synopsis", "")
        film = dict(film)
        film["genre"]    = genre
        film["synopsis"] = synopsis
    return film


def build_genre_cache(force=False):
    global _building
    if _building and not force:
        return
    _building = True
    try:
        films = unwrap_films(fetch_json(f"{BASE_API}/data_list.php"))
        if not films:
            return

        _build_progress["total"] = len(films)
        _build_progress["done"]  = 0

        enriched = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_fetch_genre, f): f for f in films}
            for fut in as_completed(futures):
                enriched.append(fut.result())
                _build_progress["done"] += 1

        genres = set()
        for f in enriched:
            for g in (f.get("genre") or "").split(","):
                g = g.strip()
                if g:
                    genres.add(g)

        with open(FILM_CACHE_FILE, "w") as fp:
            json.dump(enriched, fp, ensure_ascii=False)
        with open(GENRE_CACHE_FILE, "w") as fp:
            json.dump(sorted(genres), fp, ensure_ascii=False)

        print(f"[cache] Built: {len(enriched)} films, {len(genres)} genres")
    except Exception as e:
        print(f"[cache] Error: {e}")
    finally:
        _building = False


def get_cached_films():
    if os.path.exists(FILM_CACHE_FILE):
        with open(FILM_CACHE_FILE) as f:
            return json.load(f)
    return None


def get_cached_genres():
    if os.path.exists(GENRE_CACHE_FILE):
        with open(GENRE_CACHE_FILE) as f:
            return json.load(f)
    return None


def ensure_cache_async():
    if not os.path.exists(GENRE_CACHE_FILE) and not _building:
        t = threading.Thread(target=build_genre_cache, daemon=True)
        t.start()


# ── pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    ensure_cache_async()
    return render_template("index.html")

@app.route("/game/<film_id>")
def game(film_id):
    return render_template("movie.html", film_id=film_id)

@app.route("/search")
def search():
    q = request.args.get("q", "")
    return render_template("search.html", query=q)

@app.route("/genre")
def genre_list():
    ensure_cache_async()
    return render_template("genre_list.html")

@app.route("/genre/<path:genre_name>")
def genre_detail(genre_name):
    return render_template("genre_detail.html", genre_name=genre_name)


# ── api ───────────────────────────────────────────────────────────────────────

@app.route("/api/list")
def api_list():
    cached = get_cached_films()
    if cached:
        return jsonify(cached)
    return jsonify(unwrap_films(fetch_json(f"{BASE_API}/data_list.php")))

@app.route("/api/top")
def api_top():
    return jsonify(unwrap_films(fetch_json(f"{BASE_API}/top.php")))

@app.route("/api/genres")
def api_genres():
    genres = get_cached_genres()
    if genres is not None:
        return jsonify(genres)
    if _building:
        return jsonify({"building": True,
                        "done": _build_progress["done"],
                        "total": _build_progress["total"]}), 202
    ensure_cache_async()
    return jsonify({"building": True, "done": 0, "total": 0}), 202

@app.route("/api/genres/status")
def api_genres_status():
    if os.path.exists(GENRE_CACHE_FILE):
        return jsonify({"ready": True})
    return jsonify({"ready": False,
                    "building": _building,
                    "done": _build_progress["done"],
                    "total": _build_progress["total"]})

@app.route("/api/genre/<path:genre_name>")
def api_genre(genre_name):
    films = get_cached_films()
    if films is None:
        return jsonify({"building": True}), 202
    filtered = [f for f in films
                if genre_name.lower() in (f.get("genre") or "").lower()]
    return jsonify(filtered)

@app.route("/api/movie/<film_id>")
def api_movie(film_id):
    return jsonify(fetch_json(f"{BASE_API}/data_movie.php?film_id={film_id}") or {})

@app.route("/api/game/<film_id>")
def api_game(film_id):
    return jsonify(fetch_json(f"{BASE_API}/game.php?film_id={film_id}") or {})

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    return jsonify(unwrap_films(fetch_json(f"{BASE_API}/data_cari.php?query={q}")))

@app.route("/api/recommendations")
def api_recommendations():
    return jsonify(unwrap_films(fetch_json(f"{BASE_API}/recom.php")))

@app.route("/api/cache/rebuild")
def api_rebuild():
    if not _building:
        t = threading.Thread(target=lambda: build_genre_cache(force=True), daemon=True)
        t.start()
    return jsonify({"started": True})


if __name__ == "__main__":
    ensure_cache_async()
    app.run(debug=True, port=5000)
