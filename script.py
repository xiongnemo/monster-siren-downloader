"""Monster Siren downloader.

This script keeps a local cache of all albums/songs metadata from
monster-siren.hypergryph.com, downloads audio/background/album art, and
embeds tags (artist, album, cover) into the audio files.

Usage (PowerShell):
	python script.py

Dependencies:
	pip install requests mutagen pydub
	# pydub needs ffmpeg available on PATH.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4, MP4Cover
from pydub import AudioSegment


BASE_URL = "https://monster-siren.hypergryph.com/api"
HEADERS = {
	"User-Agent": (
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
		"AppleWebKit/537.36 (KHTML, like Gecko) "
		"Chrome/142.0.0.0 Safari/537.36"
	),
	"Referer": "https://monster-siren.hypergryph.com/music",
	"DNT": "1",
	"sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
	"sec-ch-ua-platform": '"Windows"',
	"sec-ch-ua-mobile": "?0",
}


ROOT = Path(__file__).resolve().parent
SONGS_DIR = ROOT / "songs"
METADATA_DIR = ROOT / "metadata"


def slugify(name: str) -> str:
	"""Return a filesystem-friendly slug while preserving readability."""

	cleaned = name.replace("...", "").replace("…", "")
	cleaned = re.sub(r"[\\/:*?\"<>|]", " ", cleaned)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	return cleaned or "unknown"


def ensure_dirs() -> None:
	SONGS_DIR.mkdir(parents=True, exist_ok=True)
	METADATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_json(session: requests.Session, path: str) -> Any:
	url = f"{BASE_URL}/{path.lstrip('/')}"
	resp = session.get(url, timeout=30)
	resp.raise_for_status()
	data = resp.json()
	# API usually wraps payload under "data"; fall back to whole body.
	return data.get("data", data)


def download_binary(session: requests.Session, url: str, dest: Path) -> None:
	dest.parent.mkdir(parents=True, exist_ok=True)
	if dest.exists():
		return
	with session.get(url, stream=True, timeout=60) as resp:
		resp.raise_for_status()
		tmp = dest.with_suffix(dest.suffix + ".part")
		with tmp.open("wb") as fh:
			for chunk in resp.iter_content(chunk_size=8192):
				if chunk:
					fh.write(chunk)
		tmp.replace(dest)


def pick_url(record: Dict[str, Any], *keys: str) -> Optional[str]:
	for key in keys:
		url = record.get(key)
		if isinstance(url, str) and url.startswith("http"):
			return url
	return None


def extract_album_cover(album: Dict[str, Any]) -> Optional[str]:
	return pick_url(
		album,
		"coverUrl",
		"cover",
		"coverUrlLg",
		"coverUrlSm",
		"bgCover",
	)


def extract_background(album: Dict[str, Any]) -> Optional[str]:
	return pick_url(album, "backgroundUrl", "bgUrl", "wallpaper")


def extract_song_audio(song: Dict[str, Any]) -> Optional[str]:
	return pick_url(song, "sourceUrl", "source", "url")


def parse_extension_from_url(url: str) -> str:
	path = urlparse(url).path
	ext = Path(path).suffix
	return ext if ext else ".m4a"


def tag_mp3(path: Path, title: str, album: str, artists: List[str], track_no: int, cover_bytes: bytes) -> None:
	try:
		audio = ID3(path)
	except ID3NoHeaderError:
		audio = ID3()
	audio.add(TIT2(encoding=3, text=title))
	audio.add(TALB(encoding=3, text=album))
	audio.add(TPE1(encoding=3, text=artists))
	audio.add(TRCK(encoding=3, text=str(track_no)))
	audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
	audio.save(path)


def tag_wav(path: Path, title: str, album: str, artists: List[str], track_no: int, cover_bytes: bytes) -> None:
	try:
		audio = ID3(path)
	except ID3NoHeaderError:
		audio = ID3()
	audio.add(TIT2(encoding=3, text=title))
	audio.add(TALB(encoding=3, text=album))
	audio.add(TPE1(encoding=3, text=artists))
	audio.add(TRCK(encoding=3, text=str(track_no)))
	audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
	audio.save(path)


def tag_m4a(path: Path, title: str, album: str, artists: List[str], track_no: int, cover_bytes: bytes) -> None:
	audio = MP4(path)
	audio["\xa9nam"] = [title]
	audio["\xa9alb"] = [album]
	audio["\xa9ART"] = artists
	audio["trkn"] = [(track_no, 0)]
	audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
	audio.save()


def tag_flac(path: Path, title: str, album: str, artists: List[str], track_no: int, cover_bytes: bytes) -> None:
	audio = FLAC(path)
	audio["title"] = [title]
	audio["album"] = [album]
	audio["artist"] = artists
	audio["tracknumber"] = [str(track_no)]
	pic = Picture()
	pic.type = 3
	pic.mime = "image/jpeg"
	pic.desc = "Cover"
	pic.data = cover_bytes
	audio.clear_pictures()
	audio.add_picture(pic)
	audio.save()


def apply_tags(path: Path, title: str, album: str, artists: List[str], track_no: int, cover_path: Optional[Path]) -> None:
	cover_bytes: Optional[bytes] = None
	if cover_path and cover_path.exists():
		cover_bytes = cover_path.read_bytes()
	else:
		logging.warning("No cover art found for %s", path)

	ext = path.suffix.lower()
	if ext == ".mp3" and cover_bytes:
		tag_mp3(path, title, album, artists, track_no, cover_bytes)
	elif ext == ".wav" and cover_bytes:
		tag_wav(path, title, album, artists, track_no, cover_bytes)
	elif ext in {".m4a", ".mp4", ".aac"} and cover_bytes:
		tag_m4a(path, title, album, artists, track_no, cover_bytes)
	elif ext == ".flac" and cover_bytes:
		tag_flac(path, title, album, artists, track_no, cover_bytes)
	else:
		# Fallback: let mutagen try even if we do not embed art.
		audio = MutagenFile(path, easy=True)
		if audio is None:
			logging.warning("Unsupported media type for tagging: %s", path)
			return
		audio["title"] = title
		audio["album"] = album
		audio["artist"] = artists
		audio.save()


def convert_wav_to_flac(wav_path: Path) -> Optional[Path]:
	if wav_path.suffix.lower() != ".wav":
		return None
	flac_path = wav_path.with_suffix(".flac")
	if flac_path.exists():
		return flac_path
	if not wav_path.exists():
		logging.warning("WAV missing for conversion: %s", wav_path)
		return None
	logging.info("Converting to FLAC: %s", wav_path.name)
	audio = AudioSegment.from_file(wav_path)
	audio.export(flac_path, format="flac")
	return flac_path


def save_json(path: Path, data: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_all_albums(session: requests.Session) -> List[Dict[str, Any]]:
	albums = fetch_json(session, "albums")
	if isinstance(albums, dict) and "list" in albums:
		return albums.get("list", [])
	if isinstance(albums, list):
		return albums
	raise RuntimeError("Unexpected albums payload")


def fetch_album_detail(session: requests.Session, album_id: str) -> Dict[str, Any]:
	detail = fetch_json(session, f"album/{album_id}/detail")
	if isinstance(detail, dict):
		return detail
	raise RuntimeError(f"Unexpected album detail payload for {album_id}")


def fetch_song_detail(session: requests.Session, song_id: str) -> Dict[str, Any]:
	detail = fetch_json(session, f"song/{song_id}")
	if isinstance(detail, dict):
		return detail
	raise RuntimeError(f"Unexpected song payload for {song_id}")


def extract_album_songs(album_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
	for key in ("songs", "songList", "trackList", "tracks"):
		songs = album_detail.get(key)
		if isinstance(songs, list):
			return songs
	return []


def get_album_core(album_detail: Dict[str, Any]) -> Dict[str, Any]:
	for key in ("album", "detail", "info"):
		block = album_detail.get(key)
		if isinstance(block, dict):
			return block
	# Sometimes detail already is the album object.
	return album_detail


def collect_artist_names(item: Dict[str, Any]) -> List[str]:
	artists = item.get("artists") or item.get("artist") or []
	if isinstance(artists, str):
		return [artists]
	if isinstance(artists, list):
		names: List[str] = []
		for a in artists:
			if isinstance(a, str):
				names.append(a)
			elif isinstance(a, dict):
				name = a.get("name") or a.get("title")
				if name:
					names.append(str(name))
		return names
	return []


def build_album_dir(album: Dict[str, Any]) -> Path:
	album_id = str(album.get("cid") or album.get("id") or album.get("albumId") or "unknown")
	album_name = slugify(album.get("name") or album.get("title") or album_id)
	return SONGS_DIR / f"{album_id} - {album_name}"


def find_existing_track(album_dir: Path, idx: int, ext: str) -> Optional[Path]:
	pattern = f"{idx:02d} - *{ext}"
	for candidate in album_dir.glob(pattern):
		if candidate.is_file():
			return candidate
	return None


def main() -> None:
	logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
	ensure_dirs()
	session = requests.Session()
	session.headers.update(HEADERS)

	albums_payload = fetch_all_albums(session)
	logging.info("Found %d albums", len(albums_payload))

	albums_meta: List[Dict[str, Any]] = []
	songs_meta: List[Dict[str, Any]] = []
	download_tasks: List[Tuple[str, Path]] = []

	for album_summary in albums_payload:
		album_id = str(album_summary.get("cid") or album_summary.get("id") or album_summary.get("albumId"))
		if not album_id:
			logging.warning("Skipping album with no id: %s", album_summary)
			continue

		detail = fetch_album_detail(session, album_id)
		album_core = get_album_core(detail)
		album_dir = build_album_dir(album_core)
		album_dir.mkdir(parents=True, exist_ok=True)

		cover_url = extract_album_cover(album_core)
		bg_url = extract_background(album_core)

		cover_path = album_dir / "cover.jpg" if cover_url else None
		bg_path = album_dir / "background.jpg" if bg_url else None

		if cover_url and cover_path and not cover_path.exists():
			download_tasks.append((cover_url, cover_path))
		if bg_url and bg_path and not bg_path.exists():
			download_tasks.append((bg_url, bg_path))

		album_record = {
			"id": album_id,
			"name": album_core.get("name") or album_core.get("title"),
			"artists": collect_artist_names(album_core),
			"cover": str(cover_path.relative_to(ROOT)) if cover_path else None,
			"background": str(bg_path.relative_to(ROOT)) if bg_path else None,
			"raw": album_core,
		}
		albums_meta.append(album_record)

		songs_in_album = extract_album_songs(detail)
		if not songs_in_album:
			logging.warning("No songs listed for album %s", album_id)
			continue

		for idx, song_stub in enumerate(songs_in_album, start=1):
			song_id = str(song_stub.get("cid") or song_stub.get("id") or song_stub.get("songId"))
			if not song_id:
				logging.warning("Skipping song with no id in album %s", album_id)
				continue

			song_detail = fetch_song_detail(session, song_id)
			song_core = song_detail.get("song") if isinstance(song_detail, dict) else None
			if not song_core:
				song_core = song_detail if isinstance(song_detail, dict) else {}

			audio_url = extract_song_audio(song_core)
			if not audio_url:
				logging.warning("No audio URL for song %s", song_id)
				continue

			ext = parse_extension_from_url(audio_url)
			title = song_core.get("name") or song_core.get("title") or song_id
			artists = collect_artist_names(song_core)

			filename = f"{idx:02d} - {slugify(title)}{ext}"
			existing = find_existing_track(album_dir, idx, ext)
			audio_path = existing or (album_dir / filename)

			if not audio_path.exists():
				download_tasks.append((audio_url, audio_path))

			song_record = {
				"id": song_id,
				"albumId": album_id,
				"title": title,
				"artists": artists,
				"trackNo": idx,
				"path": str(audio_path.relative_to(ROOT)),
				"coverPath": str(cover_path.relative_to(ROOT)) if cover_path else None,
				"raw": song_core,
			}
			songs_meta.append(song_record)

	if download_tasks:
		logging.info("Starting %d parallel downloads", len(download_tasks))
		with ThreadPoolExecutor(max_workers=8) as executor:
			future_map = {executor.submit(download_binary, session, url, dest): (url, dest) for url, dest in download_tasks}
			for future in as_completed(future_map):
				url, dest = future_map[future]
				try:
					future.result()
				except Exception as exc:  # noqa: BLE001
					logging.error("Failed download %s -> %s: %s", url, dest, exc)
	else:
		logging.info("No downloads needed; assets already present")

	# After all downloads finish, convert and tag.
	album_name_by_id = {a["id"]: a.get("name") or a["id"] for a in albums_meta}

	for song in songs_meta:
		audio_path = ROOT / song["path"]
		cover_rel = song.get("coverPath")
		cover_path = ROOT / cover_rel if cover_rel else None

		flac_path = None
		if audio_path.suffix.lower() == ".wav":
			flac_path = convert_wav_to_flac(audio_path)

		apply_tags(
			audio_path,
			song["title"],
			album_name_by_id.get(song["albumId"], song["albumId"]),
			song["artists"],
			song["trackNo"],
			cover_path,
		)
		if flac_path:
			apply_tags(
				flac_path,
				song["title"],
				album_name_by_id.get(song["albumId"], song["albumId"]),
				song["artists"],
				song["trackNo"],
				cover_path,
			)
			song["flacPath"] = str(flac_path.relative_to(ROOT))

	save_json(METADATA_DIR / "albums.json", albums_meta)
	save_json(METADATA_DIR / "songs.json", songs_meta)
	logging.info("Done. Albums: %d, Songs: %d", len(albums_meta), len(songs_meta))


if __name__ == "__main__":
	main()
