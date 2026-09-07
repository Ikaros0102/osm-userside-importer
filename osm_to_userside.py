import time
import argparse
import logging
from typing import List, Tuple, Dict, Any

import pandas as pd
import requests
from shapely.geometry import Point, Polygon
from tqdm import tqdm

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CHUNK_SIZE = 25
SEARCH_RADIUS_M = 30           # Радиус поиска в Overpass (метры)
MATCH_TOLERANCE_DEG = 0.0003   # Макс. погрешность привязки точки к зданию в градусах (~30 метров)


def polygon_to_userside(coords: List[Tuple[float, float]]) -> str:
    """
    Преобразует список координат в строку для импорта в Userside.
    Формат: lat,lon,lat,lon...
    """
    return ",".join(f"{lat},{lon}" for lon, lat in coords)


def fetch_osm_buildings(df: pd.DataFrame) -> Dict[int, Dict[str, Any]]:
    """
    Скачивает полигоны зданий из OSM Overpass API для набора точек.
    """
    all_buildings = {}
    chunks = [df[i:i + CHUNK_SIZE] for i in range(0, len(df), CHUNK_SIZE)]
    logging.info(f"Всего точек: {len(df)}. Запросов к Overpass API: {len(chunks)}")

    session = requests.Session()

    for chunk_idx, chunk in enumerate(chunks):
        retries = 3
        success = False

        around_queries = "".join(
            f'  way["building"](around:{SEARCH_RADIUS_M}, {row["Широта"]}, {row["Долгота"]});\n'
            for _, row in chunk.iterrows()
        )

        query = f"[out:json][timeout:90];\n(\n{around_queries});\nout tags geom;"

        while not success and retries > 0:
            try:
                r = session.post(OVERPASS_URL, data=query.encode('utf-8'))
                
                if r.status_code == 429:
                    logging.warning(f"[429] Лимит запросов. Ждем 300 сек (осталось попыток: {retries}).")
                    time.sleep(300)
                    retries -= 1
                    continue
                    
                if r.status_code == 504:
                    logging.warning(f"[504] Сервер перегружен. Ждем 10 сек (осталось попыток: {retries}).")
                    time.sleep(10)
                    retries -= 1
                    continue
                    
                r.raise_for_status() 
                data = r.json()
                
                for el in data.get("elements", []):
                    if "geometry" not in el:
                        continue
                        
                    osm_id = el["id"]
                    if osm_id in all_buildings:
                        continue 
                        
                    coords = [(g["lon"], g["lat"]) for g in el["geometry"]]
                    if len(coords) < 3:
                        continue
                        
                    tags = el.get("tags", {})
                    all_buildings[osm_id] = {
                        "polygon": Polygon(coords),
                        "coords": coords,
                        "levels": tags.get("building:levels", ""),
                        "street": tags.get("addr:street", ""),
                        "housenumber": tags.get("addr:housenumber", "")
                    }
                
                success = True
                logging.info(f"Обработана группа {chunk_idx + 1}/{len(chunks)}")
                
            except requests.exceptions.JSONDecodeError:
                logging.error("Ошибка JSON. Ждем 5 сек...")
                time.sleep(5)
                retries -= 1
            except requests.exceptions.RequestException as e:
                logging.error(f"Сетевая ошибка: {e}. Ждем 5 сек...")
                time.sleep(5)
                retries -= 1

        if not success:
            logging.error(f"Не удалось загрузить данные для чанка {chunk_idx + 1} после всех попыток.")

        time.sleep(2)  # Защитная пауза между запросами

    return all_buildings


def match_points_to_buildings(df: pd.DataFrame, buildings_dict: Dict[int, Dict[str, Any]]) -> pd.DataFrame:
    """
    Сопоставляет точки из DataFrame с ближайшими зданиями.
    """
    polygons, levels, streets, houses = [], [], [], []
    buildings_list = list(buildings_dict.values())

    logging.info("Сопоставление точек со зданиями...")
    
    for _, row in tqdm(df.iterrows(), total=len(df)):
        point = Point(row["Долгота"], row["Широта"])
        
        found_building = None
        min_dist = float('inf')

        for b in buildings_list:
            dist = b["polygon"].distance(point)
            if dist < min_dist:
                min_dist = dist
                found_building = b

        # Примечание: дистанция вычисляется в градусах WGS84. 
        # MATCH_TOLERANCE_DEG ~0.0003 это грубо 30 метров.
        if found_building and min_dist < MATCH_TOLERANCE_DEG:
            polygons.append(polygon_to_userside(found_building["coords"]))
            levels.append(found_building["levels"])
            streets.append(found_building["street"])
            houses.append(found_building["housenumber"])
        else:
            polygons.append(None)
            levels.append(None)
            streets.append(None)
            houses.append(None)

    df_out = df.copy()
    df_out["polygon"] = polygons
    df_out["levels"] = levels
    df_out["street"] = streets
    df_out["housenumber"] = houses
    
    return df_out


def main():
    parser = argparse.ArgumentParser(description="Скрипт для получения полигонов зданий из OSM по координатам.")
    parser.add_argument("-i", "--input", default="points.xlsx", help="Путь к входящему Excel файлу")
    parser.add_argument("-o", "--output", default="userside_import_final.csv", help="Путь к исходящему CSV файлу")
    args = parser.parse_args()

    logging.info(f"Чтение файла {args.input}...")
    try:
        df = pd.read_excel(args.input)
    except Exception as e:
        logging.error(f"Ошибка при чтении файла: {e}")
        return

    # 1. Скачиваем здания
    buildings_dict = fetch_osm_buildings(df)
    logging.info(f"Всего уникальных зданий скачано: {len(buildings_dict)}")

    if not buildings_dict:
        logging.warning("Здания не найдены, завершение работы.")
        return

    # 2. Сопоставляем
    df_result = match_points_to_buildings(df, buildings_dict)

    # 3. Сохраняем
    df_result.to_csv(args.output, index=False, encoding="utf-8-sig")
    logging.info(f"Готово! Результат сохранен в {args.output}")


if __name__ == "__main__":
    main()