#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Organizador automático de vídeos y subtítulos.
Crea carpetas 'Películas' y 'Series', consulta TMDb para nombres oficiales,
renombra archivos y mantiene subtítulos sincronizados.
"""

import os
import re
import sys
import shutil
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from guessit import guessit

# ========== CONFIGURACIÓN ==========
TMDB_API_KEY = "***REMOVED***"   # <-- Reemplazar con tu clave real
TMDB_BASE_URL = "https://api.themoviedb.org/3"
LANGUAGE = "es"                     # Cambiar según preferencia (es, en, etc.)

# Extensiones soportadas
VIDEO_EXT = {'.avi', '.mkv', '.mp4', '.mov', '.wmv', '.flv', '.webm', '.m4v'}
SUB_EXT   = {'.srt', '.sub', '.ass', '.ssa', '.vtt'}

# Carpetas destino
PELICULAS_DIR = "/path/to/movies"
SERIES_DIR    = "/path/to/tvshows"
SIN_CLASIFICAR_DIR = "/path/to/sin_clasificar"

# Formato de nombres
# Para películas: "Nombre de la película (año).ext"
# Para series:    "NombreSerie SXXEYY - Título episodio.ext"
# (el título del episodio se obtiene de TMDb)

# ========== FUNCIONES AUXILIARES ==========

def setup_logging(verbose: bool):
    """Configura el sistema de logs."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(message)s',
        level=level,
        handlers=[logging.StreamHandler()]
    )

def normalize_name(name: str) -> str:
    """
    Elimina caracteres no válidos para nombres de carpeta/archivo y
    convierte espacios múltiples en uno solo.
    """
    # Reemplazar caracteres problemáticos
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def find_matching_subs(video_path: Path) -> List[Path]:
    """
    Busca archivos de subtítulos en el mismo directorio que coincidan
    con el nombre base del vídeo (antes de la extensión).
    """
    video_stem = video_path.stem
    video_dir = video_path.parent
    subs = []
    for ext in SUB_EXT:
        # Búsqueda exacta: video.srt, video.esp.srt, etc.
        for sub_path in video_dir.glob(f"{video_stem}*{ext}"):
            # Evitar duplicados si hay varios idiomas, se tomarán todos
            if sub_path.is_file():
                subs.append(sub_path)
        # También buscar subtítulos con nombre similar pero sin el sufijo de idioma
        # Ejemplo: "video (1).srt" - pero es mejor usar el mismo stem exacto
    return subs

# ========== CONSULTAS A TMDB ==========

def search_tmdb(query: str, media_type: str, year: Optional[int] = None) -> Optional[Dict]:
    """
    Busca en TMDb.
    media_type: 'movie' o 'tv'
    Retorna el primer resultado o None.
    """
    endpoint = f"{TMDB_BASE_URL}/search/{media_type}"
    params = {
        'api_key': TMDB_API_KEY,
        'query': query,
        'language': LANGUAGE,
        'include_adult': False
    }
    if year:
        params['year' if media_type == 'movie' else 'first_air_date_year'] = year

    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data['results']:
            return data['results'][0]
    except Exception as e:
        logging.error(f"Error consultando TMDb para '{query}': {e}")
    return None

def get_movie_details(movie_id: int) -> Dict:
    """Obtiene detalles completos de una película."""
    url = f"{TMDB_BASE_URL}/movie/{movie_id}"
    params = {'api_key': TMDB_API_KEY, 'language': LANGUAGE}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def get_tv_details(series_id: int) -> Dict:
    """Obtiene detalles de una serie."""
    url = f"{TMDB_BASE_URL}/tv/{series_id}"
    params = {'api_key': TMDB_API_KEY, 'language': LANGUAGE}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def get_episode_title(series_id: int, season: int, episode: int) -> Optional[str]:
    """Obtiene el título de un episodio concreto."""
    url = f"{TMDB_BASE_URL}/tv/{series_id}/season/{season}/episode/{episode}"
    params = {'api_key': TMDB_API_KEY, 'language': LANGUAGE}
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get('name')
    except Exception as e:
        logging.warning(f"No se pudo obtener título del episodio S{season:02d}E{episode:02d}: {e}")
        return None

# ========== PROCESAMIENTO DE ARCHIVOS ==========

def process_video(video_path: Path, dry_run: bool):
    """
    Analiza un archivo de vídeo, determina si es película o serie,
    consulta TMDb, renombra y mueve a la carpeta correspondiente,
    junto con sus subtítulos.
    """
    logging.info(f"Procesando: {video_path}")

    # 1. Extraer información con guessit
    info = guessit(video_path.name)
    logging.debug(f"Guessit: {info}")

    # Determinar tipo
    if 'episode' in info and info.get('episode') is not None:
        # Es un episodio de serie
        media_type = 'tv'
        title = info.get('title')
        season = info.get('season')
        episode = info.get('episode')
        if not title or not season or not episode:
            logging.warning(f"No se pudo extraer serie/temporada/episodio de {video_path.name}")
            return False
        year = info.get('year')  # año de la serie, puede ser None
    else:
        # Asumimos película
        media_type = 'movie'
        title = info.get('title')
        year = info.get('year')
        if not title:
            logging.warning(f"No se pudo extraer título de la película: {video_path.name}")
            return False

    # 2. Buscar en TMDb para obtener nombre oficial y más datos
    if media_type == 'movie':
        tmdb_result = search_tmdb(title, 'movie', year)
        if not tmdb_result:
            logging.warning(f"No se encontró película para '{title}' (año {year})")
            return False
        movie_data = get_movie_details(tmdb_result['id'])
        official_title = movie_data.get('title')
        official_year = movie_data.get('release_date', '')[:4]
        # Formato: "Título oficial (año)"
        new_basename = f"{official_title} ({official_year})"
        target_dir = Path(PELICULAS_DIR) / new_basename
        target_dir.mkdir(parents=True, exist_ok=True)
        new_video_name = f"{new_basename}{video_path.suffix}"
        new_video_path = target_dir / new_video_name
    else:  # Serie
        # Buscar la serie
        tmdb_result = search_tmdb(title, 'tv', year)
        if not tmdb_result:
            logging.warning(f"No se encontró serie para '{title}'")
            return False
        series_data = get_tv_details(tmdb_result['id'])
        official_series_name = series_data.get('name')
        # Carpeta de la serie (sin año, opcional)
        series_folder = normalize_name(official_series_name)
        # Subcarpeta por temporada (Season XX)
        season_folder = f"Season {season:02d}"
        target_dir = Path(SERIES_DIR) / series_folder / season_folder
        target_dir.mkdir(parents=True, exist_ok=True)

        # Obtener título del episodio desde TMDb
        ep_title = get_episode_title(tmdb_result['id'], season, episode)
        # Construir nombre del archivo: "Serie S01E01 - Título episodio.ext"
        if ep_title:
            ep_title_clean = normalize_name(ep_title)
            new_video_name = f"{official_series_name} S{season:02d}E{episode:02d} - {ep_title_clean}{video_path.suffix}"
        else:
            new_video_name = f"{official_series_name} S{season:02d}E{episode:02d}{video_path.suffix}"
        new_video_path = target_dir / new_video_name

    # 3. Renombrar y mover vídeo
    if not dry_run:
        try:
            shutil.move(str(video_path), str(new_video_path))
            logging.info(f"Movido: {video_path} -> {new_video_path}")
        except Exception as e:
            logging.error(f"Error al mover vídeo {video_path}: {e}")
            return False
    else:
        logging.info(f"[DRY RUN] Se movería: {video_path} -> {new_video_path}")

    # 4. Procesar subtítulos asociados
    subs = find_matching_subs(video_path)
    if subs:
        # Para cada subtítulo, renombrar con el mismo nuevo nombre base
        new_base = new_video_path.stem
        for sub_path in subs:
            # Conservar el sufijo de idioma si lo hubiera (ej: .es.srt)
            sub_ext = sub_path.suffix
            # Detectar si hay un sufijo extra antes de la extensión (ej: .en.srt)
            # Formato típico: video.es.srt -> el stem completo sería "video.es"
            # Vamos a buscar el patrón: nombre_vídeo.[código_idioma].ext
            sub_stem = sub_path.stem
            # Si el stem del subtítulo comienza igual que el stem original del vídeo
            video_stem_orig = video_path.stem
            if sub_stem.startswith(video_stem_orig):
                rest = sub_stem[len(video_stem_orig):]  # ej: ".es" o ""
                # Reconstruir nuevo nombre: nuevo_base + rest + extensión
                new_sub_name = f"{new_base}{rest}{sub_ext}"
            else:
                # Sin coincidencia, simplemente usamos el nuevo_base
                new_sub_name = f"{new_base}{sub_ext}"
            new_sub_path = target_dir / new_sub_name
            if not dry_run:
                try:
                    shutil.move(str(sub_path), str(new_sub_path))
                    logging.info(f"Subtítulo movido: {sub_path} -> {new_sub_path}")
                except Exception as e:
                    logging.error(f"Error moviendo subtítulo {sub_path}: {e}")
            else:
                logging.info(f"[DRY RUN] Se movería subtítulo: {sub_path} -> {new_sub_path}")
    else:
        logging.debug("No se encontraron subtítulos asociados.")

    return True

def organize_directory(source_dir: Path, dry_run: bool = False, recursive: bool = False):
    """
    Escanea el directorio fuente en busca de vídeos (y sus subtítulos),
    procesa cada uno. recursive True cuando queramos recursividad
    """
    # Crear carpetas destino principales
    for d in [PELICULAS_DIR, SERIES_DIR, SIN_CLASIFICAR_DIR]:
        Path(d).mkdir(exist_ok=True)

    # Patrón de búsqueda
    if recursive:
        video_files = []
        for ext in VIDEO_EXT:
            video_files.extend(source_dir.rglob(f"*{ext}"))
    else:
        video_files = [f for f in source_dir.iterdir() if f.suffix in VIDEO_EXT]

    if not video_files:
        logging.info("No se encontraron archivos de vídeo.")
        return

    logging.info(f"Se encontraron {len(video_files)} vídeos.")

    for vf in video_files:
        success = process_video(vf, dry_run)
        if not success:
            # Mover a Sin_clasificar si falló
            if not dry_run:
                dest_fail = Path(SIN_CLASIFICAR_DIR) / vf.name
                shutil.move(str(vf), str(dest_fail))
                logging.warning(f"Vídeo no clasificado movido a {dest_fail}")
                # También mover subtítulos asociados al mismo destino
                for sub in find_matching_subs(vf):
                    shutil.move(str(sub), str(Path(SIN_CLASIFICAR_DIR) / sub.name))
            else:
                logging.warning(f"[DRY RUN] Vídeo sin clasificar: {vf} iría a {SIN_CLASIFICAR_DIR}")

# ========== PUNTO DE ENTRADA ==========

def main():
    parser = argparse.ArgumentParser(
        description="Organiza vídeos y subtítulos en Películas/Series usando TMDb."
    )
    parser.add_argument("directorio", nargs="?", default=".",
                        help="Directorio de origen (por defecto el actual)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula la organización sin mover archivos")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Muestra información detallada")
    parser.add_argument("--no-recursive", action="store_true",
                        help="No buscar en subdirectorios")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if TMDB_API_KEY == "TU_API_KEY_AQUI":
        logging.error("Debes configurar tu API key de TMDb dentro del script.")
        sys.exit(1)

    source = Path(args.directorio).resolve()
    if not source.is_dir():
        logging.error(f"El directorio {source} no existe.")
        sys.exit(1)

    logging.info(f"Organizando desde: {source}")
    organize_directory(source, dry_run=args.dry_run, recursive=not args.no_recursive)
    logging.info("Proceso completado.")

if __name__ == "__main__":
    main()
