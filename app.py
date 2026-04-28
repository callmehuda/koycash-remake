import json
import os
import time
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

FILM_CACHE_TTL = int(os.environ.get("FILM_CACHE_TTL", 30 * 60))
MAX_WORKERS    = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://koyzone.xyz/",
}

GENRE_ALIASES: dict[str, list[str]] = {
    "Action"          : ["Aksi"],
    "Adult"           : ["Dewasa", "Soft Adult", "NSFW", "sexy"],
    "Adventure"       : ["Petualangan"],
    "Casual"          : ["Kasual", "Santai", "Relaksasi"],
    "Comedy"          : ["Komedi"],
    "Dark Fantasy"    : ["Fantasi Gelap"],
    "Emotional"       : ["Emosional"],
    "Erotic"          : ["Erotica", "Eroge"],
    "Exploration"     : ["Eksplorasi"],
    "Fantasy"         : ["Fantasi"],
    "Fantasy Horror"  : ["Fantasi Horor"],
    "Farming"         : ["Pertanian"],
    "Fighting"        : ["Pertarungan"],
    "Horror"          : ["Horor"],
    "Interactive"     : ["Interaktif", "Interactive Fiction"],
    "Life Simulation" : ["Life Sim", "Simulasi Kehidupan"],
    "Management"      : ["Manajemen"],
    "Mystery"         : ["Misteri"],
    "Narrative"       : ["Narasi", "Naratif"],
    "Psychological"   : ["Psikologi", "Psikologis"],
    "Romance"         : ["Romansa", "Romantis", "romance"],
    "School Life"     : ["School", "Sekolah", "Kehidupan Sekolah", "High School"],
    "Simulation"      : ["Simulasi"],
    "Slice of Life"   : ["Kehidupan"],
    "Strategy"        : ["Strategi"],
    "Taboo"           : ["Tabu"],

    "Femdom"          : ["Female Domination"],
    "NTR"             : ["Netorare"],
    "Pixel Art"       : ["Pixel"],
    "RPG"             : ["Role-playing"],
    "Sci-Fi"          : ["Sci-fi"],          
    "Trainer"         : ["Training"],
    "Yuri"            : ["lesbi"],
}
_CANONICAL_MAP: dict[str, str] = {}
for _canon, _aliases in GENRE_ALIASES.items():
    _CANONICAL_MAP[_canon.lower()] = _canon
    for _alias in _aliases:
        _CANONICAL_MAP[_alias.lower()] = _canon


def _to_canonical(genre: str) -> str:
    """Return the canonical genre name for *genre*, or *genre* itself if unknown."""
    return _CANONICAL_MAP.get(genre.strip().lower(), genre.strip())


_state_lock    = threading.Lock()
_building      = False
_progress      = {"done": 0, "total": 0}
_refresh_timer: threading.Timer | None = None


def fetch_json(url: str):
    try:
        r = req_lib.get(url, timeout=10, headers=HEADERS)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def unwrap_films(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("films") or data.get("games") or []
    return []


def file_age(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        return float("inf")


def load_json_file(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_json_file(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)

# ── Genre helpers ─────────────────────────────────────────────────────────────

def _dedupe_genres(films: list) -> list:
    seen: set[str] = set()
    result: list[str] = []
    for film in films:
        for raw in (film.get("genre") or "").split(","):
            canonical = _to_canonical(raw)
            if not canonical:
                continue
            key = canonical.lower()
            if key not in seen:
                seen.add(key)
                result.append(canonical)
    return sorted(result, key=str.lower)


def _genre_matches(genre_name: str, film: dict) -> bool:
    needle = _to_canonical(genre_name).lower()
    for raw in (film.get("genre") or "").split(","):
        if _to_canonical(raw).lower() == needle:
            return True
    return False


def _enrich_film(film: dict) -> dict:
    detail = fetch_json(f"{BASE_API}/data_movie.php?film_id={film['id']}")
    if detail and isinstance(detail, dict):
        info = detail.get("film", {})
        film = dict(film)
        film["genre"]    = info.get("genre", "")
        film["synopsis"] = info.get("synopsis", "")
    return film


def _fetch_all_films() -> list | None:
    films = unwrap_films(fetch_json(f"{BASE_API}/data_list.php"))
    if not films:
        return None

    with _state_lock:
        _progress["total"] = len(films)
        _progress["done"]  = 0

    enriched: list = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_enrich_film, f): f for f in films}
        for fut in as_completed(futures):
            enriched.append(fut.result())
            with _state_lock:
                _progress["done"] += 1

    return enriched


def _set_building(value: bool) -> None:
    global _building
    with _state_lock:
        _building = value


def build_film_cache() -> bool:
    if _building:
        return False
    _set_building(True)
    try:
        enriched = _fetch_all_films()
        if enriched is None:
            return False
        save_json_file(FILM_CACHE_FILE, enriched)
        print(f"[cache] Film cache updated: {len(enriched)} films")
        return True
    except Exception as exc:
        print(f"[cache] Film cache error: {exc}")
        return False
    finally:
        _set_building(False)


def build_genre_cache(films: list | None = None) -> bool:
    try:
        if films is None:
            films = load_json_file(FILM_CACHE_FILE) or []
        genres = _dedupe_genres(films)
        save_json_file(GENRE_CACHE_FILE, genres)
        print(f"[cache] Genre cache built: {len(genres)} canonical genres")
        return True
    except Exception as exc:
        print(f"[cache] Genre cache error: {exc}")
        return False


def build_full_cache(force: bool = False, rebuild_genres: bool = False) -> None:
    with _state_lock:
        if _building and not force:
            return
    _set_building(True)
    try:
        enriched = _fetch_all_films()
        if enriched is None:
            return
        save_json_file(FILM_CACHE_FILE, enriched)
        print(f"[cache] Film cache updated: {len(enriched)} films")
        if rebuild_genres or not os.path.exists(GENRE_CACHE_FILE):
            build_genre_cache(enriched)
    except Exception as exc:
        print(f"[cache] Full cache error: {exc}")
    finally:
        _set_building(False)


def _run_refresh() -> None:
    print("[cache] Scheduled film refresh started")
    build_film_cache()
    _arm_refresh_timer(FILM_CACHE_TTL)


def _arm_refresh_timer(delay: float) -> None:
    global _refresh_timer
    with _state_lock:
        if _refresh_timer is not None:
            _refresh_timer.cancel()
        _refresh_timer = threading.Timer(delay, _run_refresh)
        _refresh_timer.daemon = True
        _refresh_timer.start()
    print(f"[cache] Next film refresh in {int(delay)}s ({delay/60:.1f} min)")


def start_refresh_scheduler() -> None:
    age   = file_age(FILM_CACHE_FILE)
    delay = max(60, FILM_CACHE_TTL - age)
    _arm_refresh_timer(delay)


def get_cached_films() -> list | None:
    return load_json_file(FILM_CACHE_FILE)


def get_cached_genres() -> list | None:
    return load_json_file(GENRE_CACHE_FILE)


def ensure_cache_async() -> None:
    missing = (
        not os.path.exists(FILM_CACHE_FILE)
        or not os.path.exists(GENRE_CACHE_FILE)
    )
    if missing and not _building:
        threading.Thread(target=build_full_cache, daemon=True).start()


@app.route("/")
def index():
    ensure_cache_async()
    return render_template("index.html")


@app.route("/game/<film_id>")
def game(film_id):
    return render_template("movie.html", film_id=film_id)


@app.route("/search")
def search():
    return render_template("search.html", query=request.args.get("q", ""))


@app.route("/genre")
def genre_list():
    ensure_cache_async()
    return render_template("genre_list.html")


@app.route("/genre/<path:genre_name>")
def genre_detail(genre_name):
    return render_template("genre_detail.html", genre_name=genre_name)


@app.route("/api/list")
def api_list():
    return jsonify(
        get_cached_films()
        or unwrap_films(fetch_json(f"{BASE_API}/data_list.php"))
    )


@app.route("/api/top")
def api_top():
    return jsonify(unwrap_films(fetch_json(f"{BASE_API}/top.php")))


@app.route("/api/genres")
def api_genres():
    genres = get_cached_genres()
    if genres is not None:
        return jsonify(genres)
    with _state_lock:
        building = _building
        prog     = dict(_progress)
    if building:
        return jsonify({"building": True, **prog}), 202
    ensure_cache_async()
    return jsonify({"building": True, "done": 0, "total": 0}), 202


@app.route("/api/genres/status")
def api_genres_status():
    if os.path.exists(GENRE_CACHE_FILE):
        return jsonify({"ready": True})
    with _state_lock:
        return jsonify({"ready": False, "building": _building, **_progress})


@app.route("/api/genre/<path:genre_name>")
def api_genre(genre_name):
    films = get_cached_films()
    if films is None:
        return jsonify({"building": True}), 202
    filtered = [f for f in films if _genre_matches(genre_name, f)]
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


@app.route("/api/cache/status")
def api_cache_status():
    age = file_age(FILM_CACHE_FILE)
    with _state_lock:
        return jsonify({
            "films": {
                "ready":       os.path.exists(FILM_CACHE_FILE),
                "age_seconds": round(age) if age != float("inf") else None,
                "ttl_seconds": FILM_CACHE_TTL,
                "stale":       age > FILM_CACHE_TTL,
            },
            "genres": {
                "ready": os.path.exists(GENRE_CACHE_FILE),
            },
            "building": _building,
            "progress": dict(_progress),
        })


@app.route("/api/cache/rebuild")
def api_cache_rebuild():
    rebuild_genres = request.args.get("genres") == "1"
    with _state_lock:
        already = _building
    if not already:
        threading.Thread(
            target=build_full_cache,
            kwargs={"force": True, "rebuild_genres": rebuild_genres},
            daemon=True,
        ).start()
        _arm_refresh_timer(FILM_CACHE_TTL)
    return jsonify({"started": not already, "rebuild_genres": rebuild_genres})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_cache_async()
    start_refresh_scheduler()
    app.run(debug=True, port=5000)
