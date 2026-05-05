#!/usr/bin/env python3
# ============================================================================
# Mesh-net Тропы — оффлайн-загрузчик OSM-тайлов
# ============================================================================
# Скачивает растровые OSM-тайлы для заданного bbox и диапазона zoom в
# локальную директорию /var/lib/mesh-net/tiles/{z}/{x}/{y}.png.
# rescue-api монтирует эту папку на /tiles, дашборд берёт тайлы оттуда.
#
# Использование:
#   python3 download_tiles.py --bbox 38.7,44.8,39.4,45.3 --zoom 10-14
#
# bbox: min_lon,min_lat,max_lon,max_lat в десятичных градусах
#       (порядок именно такой, как в большинстве GIS-инструментов)
#
# ============================================================================
# ⚠ ПРО OSM TILE USAGE POLICY
# ============================================================================
# Формально OSM запрещает массовую загрузку тайлов с tile.openstreetmap.org.
# Этот скрипт для ОБРАЗОВАТЕЛЬНЫХ целей — он ставит User-Agent,
# идентифицирующий проект, и rate-limit 1 запрос/сек. На сотнях-тысячах
# тайлов с такой скоростью OSM-сервера обычно не трогают.
#
# Для production / грантовой работы НУЖНО переехать на свой tile-сервер:
#   - tileserver-gl + MBTiles от openmaptiles.org (вектор, бесплатно нон-комм)
#   - либо локальный рендер mapnik из OSM-extract (geofabrik.de)
# См. TODO в web/rescue-dashboard/CLAUDE.md.
# ============================================================================

import argparse
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Один сервер (без {a,b,c}-subdomain): subdomains устарели и OSM рекомендует
# использовать tile.openstreetmap.org напрямую.
TILE_SERVER = "https://tile.openstreetmap.org"

USER_AGENT = "MeshNetTropy/0.1 (educational mesh project; +https://github.com/)"

DEFAULT_DST  = "/var/lib/mesh-net/tiles"
DEFAULT_RATE = 1.0   # секунд между запросами (1 req/sec — вежливо к OSM)


def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Web Mercator: широта/долгота → (x, y) тайла на данном зуме.
    Это стандартная XYZ-проекция, которую использует и Leaflet, и OSM, и Google."""
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    # Кламп — на полюсах формула вылетает за пределы
    xtile = max(0, min(int(n) - 1, xtile))
    ytile = max(0, min(int(n) - 1, ytile))
    return xtile, ytile


def tile_path(dst: Path, z: int, x: int, y: int) -> Path:
    return dst / str(z) / str(x) / f"{y}.png"


def download_tile(url: str, dst_path: Path, timeout: float = 30.0) -> int:
    """Скачивает один тайл. Возвращает кол-во байт или 0 если уже на диске.
    Бросает исключение при ошибке сети."""
    if dst_path.exists() and dst_path.stat().st_size > 0:
        return 0
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    # Простая sanity-проверка: PNG начинается с \x89PNG
    if not data.startswith(b"\x89PNG"):
        raise ValueError(f"not a PNG ({len(data)} bytes)")
    dst_path.write_bytes(data)
    return len(data)


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [x.strip() for x in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox: нужно 4 числа через запятую")
    min_lon, min_lat, max_lon, max_lat = map(float, parts)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise argparse.ArgumentTypeError(
            f"bbox: min_lon ({min_lon}) должен быть < max_lon ({max_lon}), "
            f"то же для широт"
        )
    return min_lon, min_lat, max_lon, max_lat


def parse_zoom(s: str) -> tuple[int, int]:
    if "-" in s:
        a, b = s.split("-", 1)
        zlo, zhi = int(a), int(b)
    else:
        zlo = zhi = int(s)
    if not (0 <= zlo <= zhi <= 19):
        raise argparse.ArgumentTypeError(f"zoom 0..19, получили {zlo}..{zhi}")
    return zlo, zhi


def count_tiles(bbox, zlo, zhi) -> dict[int, int]:
    min_lon, min_lat, max_lon, max_lat = bbox
    by_zoom = {}
    for z in range(zlo, zhi + 1):
        x1, y2 = deg2num(min_lat, min_lon, z)
        x2, y1 = deg2num(max_lat, max_lon, z)
        cnt = (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1)
        by_zoom[z] = cnt
    return by_zoom


def main():
    ap = argparse.ArgumentParser(
        description="Качает OSM-тайлы для заданного района в локальный кеш.",
        epilog="Пример: --bbox 38.7,44.8,39.4,45.3 --zoom 10-14  (Краснодар, ~1500 тайлов)",
    )
    ap.add_argument("--bbox", required=True, type=parse_bbox,
                    help="min_lon,min_lat,max_lon,max_lat (десятичные градусы)")
    ap.add_argument("--zoom", required=True, type=parse_zoom,
                    help="диапазон zoom, например 10-14")
    ap.add_argument("--dst", default=DEFAULT_DST,
                    help=f"куда сохранять (default: {DEFAULT_DST})")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help=f"секунд между запросами (default: {DEFAULT_RATE})")
    ap.add_argument("--yes", action="store_true",
                    help="не спрашивать подтверждения")
    args = ap.parse_args()

    bbox = args.bbox
    zlo, zhi = args.zoom
    dst = Path(args.dst)

    # Проверим что в dst можно писать (или создать)
    try:
        dst.mkdir(parents=True, exist_ok=True)
        probe = dst / ".probe"
        probe.write_bytes(b"")
        probe.unlink()
    except (PermissionError, OSError) as e:
        print(f"[err] нет прав на запись в {dst}: {e}", file=sys.stderr)
        print("      Попробуй:", file=sys.stderr)
        print(f"        sudo mkdir -p {dst}", file=sys.stderr)
        print(f"        sudo chown -R $USER {dst}", file=sys.stderr)
        sys.exit(1)

    by_zoom = count_tiles(bbox, zlo, zhi)
    total = sum(by_zoom.values())
    eta_min = total * args.rate / 60.0

    print(f"[tiles] bbox={bbox}  zoom={zlo}..{zhi}  dst={dst}")
    for z, c in sorted(by_zoom.items()):
        print(f"        z={z:2d}: {c} тайлов")
    print(f"[tiles] всего: {total} тайлов, ~{eta_min:.1f} мин при rate={args.rate}s")

    if total > 50_000:
        print(f"[warn] {total} > 50000 — это много для OSM. Уменьши bbox или zoom.")
    elif total > 10_000:
        print(f"[warn] {total} > 10000 — займёт несколько часов. ОК?")

    if not args.yes:
        try:
            ans = input("Начать? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if ans not in ("y", "yes", "д", "да"):
            print("[tiles] отменено")
            sys.exit(0)

    # ---- основной цикл -----------------------------------------------------
    min_lon, min_lat, max_lon, max_lat = bbox
    downloaded = skipped = failed = 0
    bytes_total = 0
    started = time.monotonic()
    last_print = started

    try:
        for z in range(zlo, zhi + 1):
            x1, y2 = deg2num(min_lat, min_lon, z)
            x2, y1 = deg2num(max_lat, max_lon, z)
            xs = range(min(x1, x2), max(x1, x2) + 1)
            ys = range(min(y1, y2), max(y1, y2) + 1)

            for x in xs:
                for y in ys:
                    url = f"{TILE_SERVER}/{z}/{x}/{y}.png"
                    path = tile_path(dst, z, x, y)
                    try:
                        n = download_tile(url, path)
                        if n == 0:
                            skipped += 1
                        else:
                            downloaded += 1
                            bytes_total += n
                            time.sleep(args.rate)  # rate-limit только после новой скачки
                    except urllib.error.HTTPError as e:
                        failed += 1
                        print(f"[err] HTTP {e.code} z={z} x={x} y={y}: {e.reason}",
                              file=sys.stderr)
                        # Если 429 (rate-limit) или 403 (бан) — лучше остановиться
                        if e.code in (403, 429):
                            print(f"[err] OSM отвечает {e.code} — притормози (--rate увеличь)",
                                  file=sys.stderr)
                            raise
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        print(f"[err] z={z} x={x} y={y}: {e}", file=sys.stderr)

                    # Прогресс раз в 5 сек
                    now = time.monotonic()
                    if now - last_print >= 5.0:
                        done = downloaded + skipped + failed
                        pct = done / total * 100 if total else 0
                        print(f"  [{pct:5.1f}%] {done}/{total}  "
                              f"↓{downloaded} skip{skipped} err{failed}  "
                              f"({bytes_total / 1024 / 1024:.1f} MB)")
                        last_print = now
    except KeyboardInterrupt:
        print("\n[tiles] прервано пользователем")
    except urllib.error.HTTPError:
        print("[tiles] остановлено из-за блокировки сервером", file=sys.stderr)
        sys.exit(2)

    elapsed = time.monotonic() - started
    print(f"[tiles] Готово за {elapsed:.0f}s. "
          f"Скачано: {downloaded} ({bytes_total / 1024 / 1024:.1f} MB), "
          f"пропущено: {skipped}, ошибок: {failed}")
    print(f"        Каталог: {dst}")
    print(f"        Перезапусти скрипт чтобы докачать пропущенное.")


if __name__ == "__main__":
    main()
