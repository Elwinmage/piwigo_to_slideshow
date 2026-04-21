#!/usr/bin/env python3
"""
Piwigo → SlideShow Digital Signage sync script.

Fetches all photos tagged "Cadre-photo" from a Piwigo server
and syncs them to a SlideShow device via WebDAV, preserving
the album directory structure.

True sync: compares files on SlideShow with Piwigo tagged photos.
- Uploads photos missing from SlideShow
- Removes photos on SlideShow that are no longer tagged in Piwigo

Requirements:
  pip install requests

Usage:
  python piwigo_to_slideshow.py                  # sync
  python piwigo_to_slideshow.py --dry-run        # simulate
  python piwigo_to_slideshow.py --list           # list files on SlideShow
  python piwigo_to_slideshow.py --list-piwigo    # list tagged photos on Piwigo
  python piwigo_to_slideshow.py --wipe           # delete everything on SlideShow
"""

import argparse
import configparser
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote

import requests

# ---------------------------------------------------------------------------
# Configuration — loaded from .conf file, env vars, then CLI args
# ---------------------------------------------------------------------------
DEFAULTS = {
    "piwigo_url": "https://your-piwigo.example.com",
    "piwigo_user": "admin",
    "piwigo_pass": "admin",
    "piwigo_tags": "",     # comma-separated list of tags, empty = all photos
    "piwigo_api_key": "",  # Piwigo 16+ API key (alternative to user/pass)

    "slideshow_url": "http://192.168.1.100:8080",
    "slideshow_user": "admin",
    "slideshow_pass": "admin",
    "slideshow_folder": "",

    "per_page": 100,
}

# Default config file locations (first match wins)
CONFIG_SEARCH_PATHS = [
    Path(__file__).parent / "piwigo_to_slideshow.conf",
    Path.home() / ".config" / "piwigo_to_slideshow.conf",
    Path("/etc/piwigo_to_slideshow.conf"),
]


def load_config(config_path: str | None = None) -> dict:
    """
    Load configuration from .conf file.
    Priority: CLI --config path > search paths > built-in defaults.
    Returns a flat dict matching DEFAULTS keys.
    """
    cfg = configparser.ConfigParser()
    found = None

    if config_path:
        # Explicit path from CLI
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        cfg.read(config_path, encoding="utf-8")
        found = config_path
    else:
        # Search default locations
        for p in CONFIG_SEARCH_PATHS:
            if p.is_file():
                cfg.read(str(p), encoding="utf-8")
                found = str(p)
                break

    result = dict(DEFAULTS)
    if found:
        log.debug("Loaded config from %s", found)
        # Map INI sections/keys → flat dict
        mapping = {
            ("piwigo", "url"):      "piwigo_url",
            ("piwigo", "user"):     "piwigo_user",
            ("piwigo", "password"): "piwigo_pass",
            ("piwigo", "tags"):     "piwigo_tags",
            ("piwigo", "tag"):      "piwigo_tags",   # backward compat
            ("piwigo", "api_key"):  "piwigo_api_key",
            ("slideshow", "url"):      "slideshow_url",
            ("slideshow", "user"):     "slideshow_user",
            ("slideshow", "password"): "slideshow_pass",
            ("slideshow", "folder"):   "slideshow_folder",
            ("options", "per_page"):   "per_page",
        }
        for (section, key), flat_key in mapping.items():
            if cfg.has_option(section, key):
                val = cfg.get(section, key)
                if flat_key == "per_page":
                    result[flat_key] = int(val) if val else DEFAULTS["per_page"]
                else:
                    result[flat_key] = val
    return result

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("piwigo2slideshow")


# ---------------------------------------------------------------------------
# Piwigo helpers
# ---------------------------------------------------------------------------
class PiwigoClient:
    """Minimal Piwigo API client using ws.php JSON interface."""

    def __init__(self, base_url: str, username: str, password: str,
                 api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        # Pass format=json as query parameter (some Piwigo versions
        # ignore it when sent as POST data)
        self.ws = f"{self.base_url}/ws.php?format=json"
        self.session = requests.Session()

        if api_key:
            # Piwigo 16+ API key auth via X-PIWIGO-API header
            self.session.headers["X-PIWIGO-API"] = api_key
            log.info("Using Piwigo API key authentication")
            self._verify_api_key()
        else:
            self._login(username, password)

    @staticmethod
    def _parse_json(resp: requests.Response) -> dict:
        """
        Parse JSON from a Piwigo response, stripping any PHP
        warnings/notices that may precede the JSON body.
        """
        text = resp.text.strip()
        if not text:
            return {}

        # PHP warnings appear as HTML before the JSON object.
        # Find the first '{' which starts the actual JSON.
        idx = text.find("{")
        if idx > 0:
            log.debug("Stripped %d bytes of PHP output before JSON", idx)
            text = text[idx:]
        if not text:
            return {}
        return json.loads(text)

    def _login(self, username: str, password: str):
        resp = self.session.post(self.ws, data={
            "method": "pwg.session.login",
            "username": username,
            "password": password,
        })
        resp.raise_for_status()

        # Some Piwigo versions return empty body on login
        if resp.text.strip():
            try:
                result = self._parse_json(resp)
                if result.get("stat") == "fail":
                    raise RuntimeError(f"Piwigo login failed: {result}")
            except (json.JSONDecodeError, ValueError):
                pass  # Non-JSON response, check session below

        # Verify login succeeded by checking session status
        self._verify_session(username)

    def _verify_session(self, expected_user: str):
        """Check that we are logged in (not guest)."""
        status_resp = self.session.post(self.ws, data={
            "method": "pwg.session.getStatus",
        })
        status_resp.raise_for_status()
        status = self._parse_json(status_resp)
        if status.get("stat") != "ok":
            raise RuntimeError("Piwigo login failed: could not verify session")
        session_user = status.get("result", {}).get("username", "")
        if session_user.lower() == "guest":
            raise RuntimeError(
                f"Piwigo login failed: still logged in as guest. "
                f"Check credentials for user '{expected_user}'."
            )
        log.info("Logged in to Piwigo as '%s'", session_user)

    def _verify_api_key(self):
        """Verify that the API key works."""
        result = self._call("pwg.session.getStatus")
        user = result.get("username", "unknown")
        log.info("Piwigo API key valid, user: '%s'", user)

    def _call(self, method: str, **kwargs) -> dict:
        """Call a Piwigo API method and return the 'result' dict."""
        data = {"method": method, **kwargs}
        resp = self.session.post(self.ws, data=data)
        resp.raise_for_status()
        payload = self._parse_json(resp)
        if payload.get("stat") != "ok":
            raise RuntimeError(f"Piwigo API error ({method}): {payload}")
        return payload.get("result", {})

    def get_tag_id(self, tag_name: str) -> int:
        """Resolve a tag name to its numeric ID."""
        result = self._call("pwg.tags.getList")
        tags = result.get("tags", [])
        for t in tags:
            if t["name"].lower() == tag_name.lower():
                return int(t["id"])
        raise ValueError(f"Tag '{tag_name}' not found on Piwigo server")

    def get_images_by_tag(self, tag_id: int, per_page: int = 500) -> list[dict]:
        """Fetch all images for a single tag (handles pagination)."""
        images = []
        page = 0
        
        while True:
            log.info("Fetching Piwigo images (page %d, total collecté: %d)...", page, len(images))
            result = self._call(
                "pwg.tags.getImages",
                tag_id=tag_id,
                per_page=per_page,
                page=page,
            )
            
            page_images = result.get("images", [])
            if not page_images:
                break
            images.extend(page_images)
            if len(page_images) < per_page:
                break
            page += 1
            
        log.info("Found %d images total with tag ID %d", len(images), tag_id)
        return images

    def get_images_by_tags(self, tag_names: list[str], per_page: int = 500) -> list[dict]:
        """
        Fetch images matching any of the given tags.
        Deduplicates by image ID across tags.
        """
        seen_ids: set[int] = set()
        all_images: list[dict] = []

        for tag_name in tag_names:
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            tag_id = self.get_tag_id(tag_name)
            images = self.get_images_by_tag(tag_id, per_page=per_page)
            for img in images:
                img_id = int(img["id"])
                if img_id not in seen_ids:
                    seen_ids.add(img_id)
                    all_images.append(img)

        log.info("Found %d unique images across %d tag(s)", len(all_images), len(tag_names))
        return all_images

    def get_all_images(self, per_page: int = 500) -> list[dict]:
        """Fetch ALL images from Piwigo (no tag filter), handles pagination."""
        images = []
        page = 0

        while True:
            log.info("Fetching all Piwigo images (page %d, collected: %d)...", page, len(images))
            result = self._call(
                "pwg.categories.getImages",
                cat_id=0,
                recursive="true",
                per_page=per_page,
                page=page,
            )

            page_images = result.get("images", [])
            if not page_images:
                break
            images.extend(page_images)
            if len(page_images) < per_page:
                break
            page += 1

        log.info("Found %d images total (all photos)", len(images))
        return images

    
    @staticmethod
    def extract_album_path(image: dict) -> str:
        url = image.get("element_url", "")
        if not url:
            return ""

        marker = "/galleries/"
        idx = url.find(marker)
        if idx < 0:
            # Gestion du cas où l'URL est encodée
            url = unquote(url)
            idx = url.find(marker)
            if idx < 0:
                return ""

        # On récupère tout ce qui est après /galleries/
        path_part = url[idx + len(marker):]
        segments = path_part.split("/")
        
        if len(segments) <= 1:
            return ""
            
        # On garde tous les segments sauf le dernier (le nom du fichier)
        album_parts = segments[:-1]
        
        # Nettoyage et reconstruction propre du chemin
        clean = [unquote(seg).strip() for seg in album_parts if seg]
        return "/".join(clean)
    
    
    def download_image(self, url: str) -> bytes:
        """Download an image from Piwigo (uses session cookies)."""
        resp = self.session.get(url, stream=False)
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# SlideShow WebDAV helpers
# ---------------------------------------------------------------------------
class SlideshowWebDAV:
    """Upload / delete / list files on SlideShow via WebDAV."""

    def __init__(self, base_url: str, username: str, password: str, folder: str = ""):
        self.base_url = base_url.rstrip("/")
        self.webdav_root = f"{self.base_url}/webdav"
        self.auth = (username, password)
        # Root folder on the device for all synced content
        self.folder = folder.strip("/")
        self._created_folders: set[str] = set()
        if self.folder:
            self._ensure_folder_recursive(self.folder)

    @property
    def _target_url(self) -> str:
        if self.folder:
            return f"{self.webdav_root}/{self.folder}"
        return self.webdav_root

    def _ensure_folder_recursive(self, folder_path: str):
        """
        Crée l'arborescence de dossiers complète (ex: Roxane/Volley/Departement)
        sur le SlideShow si elle n'existe pas.
        """
        if not folder_path or folder_path == ".":
            return

        # On nettoie le chemin et on sépare les dossiers
        parts = folder_path.strip("/").split("/")
        current_path = ""
        
        for part in parts:
            # On construit le chemin petit à petit : Roxane, puis Roxane/Volley...
            if current_path:
                current_path = f"{current_path}/{part}"
            else:
                current_path = part
            
            # Si on a déjà créé ce dossier durant cette session, on passe
            if current_path in self._created_folders:
                continue

            # On prépare l'URL WebDAV (le dossier racine 'piwigo' est déjà dans webdav_root)
            encoded_path = quote(current_path)
            url = f"{self.webdav_root}/{encoded_path}/"
            
            try:
                # MKCOL est la commande WebDAV pour créer un dossier
                resp = requests.request("MKCOL", url, auth=self.auth)
                
                # 201 = Créé avec succès
                # 405 = Existe déjà (c'est bon aussi)
                if resp.status_code in (201, 405):
                    self._created_folders.add(current_path)
                else:
                    log.debug("MKCOL sur %s a renvoyé %d (peut-être déjà existant)", current_path, resp.status_code)
            except Exception as e:
                log.error("Erreur lors de la création du dossier %s: %s", current_path, e)

    def _full_path(self, rel_path: str) -> str:
        """Build the full WebDAV URL for a relative path (encode each segment)."""
        segments = rel_path.split("/")
        encoded = "/".join(quote(s, safe="") for s in segments)
        if self.folder:
            folder_enc = "/".join(quote(s, safe="") for s in self.folder.split("/"))
            return f"{self.webdav_root}/{folder_enc}/{encoded}"
        return f"{self.webdav_root}/{encoded}"

    def list_files(self) -> set[str]:
        """List relative file paths currently on the SlideShow device."""
        return set(f["path"] for f in self.list_files_detailed())

    def list_files_detailed(self) -> list[dict]:
        """
        Recursively list files with metadata via PROPFIND Depth: 1.
        Walks subdirectories one by one since SlideShow's WebDAV
        does not reliably support Depth: infinity.
        Returns dicts with 'name', 'path' (relative), 'size', 'modified'.
        """
        base_prefix = "/webdav/"
        if self.folder:
            base_prefix = f"/webdav/{self.folder}/"

        all_files: list[dict] = []
        # Queue of WebDAV URLs to explore
        dirs_to_visit = [f"{self._target_url}/"]

        while dirs_to_visit:
            url = dirs_to_visit.pop(0)
            entries = self._propfind_depth1(url)

            for entry in entries:
                href = entry["href"]
                decoded = unquote(href).rstrip("/")

                # Normalize: collapse double slashes and clean up
                while "//" in decoded:
                    decoded = decoded.replace("//", "/")

                # Build relative path
                idx = decoded.find(base_prefix)
                if idx >= 0:
                    rel_path = decoded[idx + len(base_prefix):].lstrip("/")
                else:
                    rel_path = decoded.split("/")[-1].lstrip("/")

                # Clean up any remaining double slashes in rel_path
                while "//" in rel_path:
                    rel_path = rel_path.replace("//", "/")

                if not rel_path:
                    continue

                if entry["is_collection"]:
                    # Queue this subdirectory for exploration
                    decoded_path = unquote(entry["href"]).rstrip("/")
                    # Normalize the URL too
                    clean_path = decoded_path
                    while "//" in clean_path:
                        clean_path = clean_path.replace("//", "/")
                    # Reconstruct with single slash after host:port
                    subdir_url = f"{self.base_url}{clean_path}/"
                    dirs_to_visit.append(subdir_url)
                else:
                    name = rel_path.split("/")[-1]
                    all_files.append({
                        "name": name,
                        "path": rel_path,
                        "size": entry.get("size", 0),
                        "modified": entry.get("modified", ""),
                        "content_type": entry.get("content_type", ""),
                    })

        log.info("WebDAV listing: %d files found", len(all_files))
        return sorted(all_files, key=lambda f: f["path"])

    def _propfind_depth1(self, url: str) -> list[dict]:
        """
        PROPFIND with Depth: 1 on a single directory.
        Returns a list of entries (files and subdirectories),
        excluding the directory itself.
        """
        headers = {"Depth": "1", "Content-Type": "application/xml"}
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:displayname/>"
            "<d:getcontentlength/>"
            "<d:getlastmodified/>"
            "<d:getcontenttype/>"
            "<d:resourcetype/>"
            "</d:prop>"
            "</d:propfind>"
        )
        try:
            resp = requests.request("PROPFIND", url, auth=self.auth,
                                    headers=headers, data=body)
        except requests.exceptions.ConnectionError as e:
            log.warning("PROPFIND connection error on %s: %s", url, e)
            return []

        if resp.status_code not in (200, 207):
            log.warning("PROPFIND %s failed: %d", url, resp.status_code)
            return []

        entries = []
        try:
            root = ET.fromstring(resp.content)
            ns = {"d": "DAV:"}
            responses = root.findall(".//d:response", ns)

            # The first response is the directory itself — skip it
            for i, response in enumerate(responses):
                href = response.findtext("d:href", "", ns)

                # Detect if this is a collection (directory)
                restype = response.find(".//d:propstat/d:prop/d:resourcetype", ns)
                is_collection = (
                    restype is not None
                    and restype.find("d:collection", ns) is not None
                )

                # Skip the directory itself (first entry, or matching the request URL)
                decoded_href = unquote(href).rstrip("/")
                decoded_url = unquote(url).rstrip("/")
                if decoded_href == decoded_url or i == 0 and is_collection:
                    continue

                props = response.find(".//d:propstat/d:prop", ns)
                size_str = props.findtext("d:getcontentlength", "", ns) if props is not None else ""
                modified = props.findtext("d:getlastmodified", "", ns) if props is not None else ""
                ctype = props.findtext("d:getcontenttype", "", ns) if props is not None else ""

                entries.append({
                    "href": href,
                    "is_collection": is_collection,
                    "size": int(size_str) if size_str.isdigit() else 0,
                    "modified": modified,
                    "content_type": ctype,
                })
        except ET.ParseError as e:
            log.warning("Could not parse PROPFIND response for %s: %s", url, e)

        return entries

    def upload(self, rel_path: str, data: bytes) -> bool:
        """
        Upload a file to the SlideShow device via WebDAV PUT.
        rel_path can contain subdirectories (e.g. 'Vacances/Été 2024/photo.jpg').
        Parent folders are created automatically.
        """
        # Ensure parent directories exist
        parts = rel_path.split("/")
        if len(parts) > 1:
            parent_dir = "/".join(parts[:-1])
            full_parent = f"{self.folder}/{parent_dir}" if self.folder else parent_dir
            self._ensure_folder_recursive(full_parent)

        url = self._full_path(rel_path)
        resp = requests.put(url, auth=self.auth, data=data)
        if resp.status_code in (200, 201, 204):
            log.info("  ✓ Uploaded %s (%d KB)", rel_path, len(data) // 1024)
            return True
        log.error("  ✗ Upload failed for %s: %d %s",
                  rel_path, resp.status_code, resp.text[:200])
        return False

    def delete(self, rel_path: str) -> bool:
        """Delete a file from the SlideShow device."""
        url = self._full_path(rel_path)
        resp = requests.delete(url, auth=self.auth)
        if resp.status_code in (200, 204):
            log.info("  ✓ Deleted %s", rel_path)
            return True
        log.error("  ✗ Delete failed for %s: %d", rel_path, resp.status_code)
        return False

    def wipe(self) -> bool:
        """
        Delete ALL files and folders under the target directory.
        Uses a single WebDAV DELETE on the folder, then recreates it empty.
        """
        url = f"{self._target_url}/"
        resp = requests.delete(url, auth=self.auth)
        if resp.status_code in (200, 204):
            log.info("Deleted folder: %s", self.folder or "(root)")
            # Recreate the folder so it's ready for new uploads
            if self.folder:
                self._created_folders.clear()
                self._ensure_folder_recursive(self.folder)
            return True
        elif resp.status_code == 404:
            log.info("Folder already empty or does not exist")
            return True
        else:
            log.error("Wipe failed: %d %s", resp.status_code,
                      resp.text[:200] if resp.text else "")
            return False


def wipe_slideshow(args):
    """Delete all files and folders from the SlideShow target directory."""
    slideshow = SlideshowWebDAV(
        args.slideshow_url, args.slideshow_user,
        args.slideshow_pass, args.slideshow_folder,
    )
    target = args.slideshow_folder or "(root WebDAV)"
    print(f"\n⚠  This will DELETE all files and folders in: {target}")
    print(f"   on SlideShow at {args.slideshow_url}\n")

    if not args.yes:
        confirm = input("Are you sure? Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    if slideshow.wipe():
        print("✓ All files deleted.")
    else:
        print("✗ Wipe failed.")


def make_rel_path(image: dict, album_path: str) -> str:
    """
    Build the relative path for a Piwigo image, preserving album hierarchy.
    Example: 'Vacances/Été 2024/42_DSC_0001.jpg'
    """
    original = image.get("file", f"image_{image['id']}.jpg")
    filename = f"{image['id']}_{original}"
    if album_path:
        return f"{album_path}/{filename}"
    return filename


def parse_tags(tags_str: str) -> list[str]:
    """
    Parse a tags string into a list of tag names.
    Supports comma-separated values. Returns empty list if blank.
    """
    if not tags_str or not tags_str.strip():
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def fetch_images(piwigo: PiwigoClient, tags: list[str],
                 per_page: int) -> list[dict]:
    """
    Fetch images from Piwigo based on tags configuration.
    - Empty tags list → all images
    - Single tag → images with that tag
    - Multiple tags → images with any of those tags (deduplicated)
    """
    if not tags:
        log.info("No tags specified — fetching ALL images from Piwigo")
        return piwigo.get_all_images(per_page=per_page)
    elif len(tags) == 1:
        tag_id = piwigo.get_tag_id(tags[0])
        return piwigo.get_images_by_tag(tag_id, per_page=per_page)
    else:
        return piwigo.get_images_by_tags(tags, per_page=per_page)


# ---------------------------------------------------------------------------
# Listing commands
# ---------------------------------------------------------------------------
def _human_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def list_slideshow(args):
    """List files currently on the SlideShow device."""
    slideshow = SlideshowWebDAV(
        args.slideshow_url, args.slideshow_user,
        args.slideshow_pass, args.slideshow_folder,
    )
    files = slideshow.list_files_detailed()
    if not files:
        print("No files found on SlideShow.")
        return

    # Print formatted table
    total_size = sum(f["size"] for f in files)
    show = files[:args.limit] if args.limit else files

    print(f"\n{'#':>6}  {'Path':<55} {'Size':>10}  {'Modified'}")
    print("─" * 110)
    for i, f in enumerate(show, 1):
        print(f"{i:>6}  {f['path']:<55} {_human_size(f['size']):>10}  {f['modified']}")
    if args.limit and len(files) > args.limit:
        print(f"       ... and {len(files) - args.limit} more files")
    print("─" * 110)
    print(f"       {len(files)} file(s), {_human_size(total_size)} total\n")


def list_piwigo(args):
    """List photos on Piwigo (filtered by tags or all)."""
    piwigo = PiwigoClient(args.piwigo_url, args.piwigo_user, args.piwigo_pass,
                          api_key=args.piwigo_api_key)
    tags = parse_tags(args.piwigo_tags)
    images = fetch_images(piwigo, tags, per_page=args.per_page)

    if not images:
        label = ", ".join(tags) if tags else "all photos"
        print(f"No images found ({label}).")
        return

    print(f"\n{'#':>6}  {'ID':>7}  {'Album/File':<60} {'Size':>12}  {'Date'}")
    print("─" * 110)
    show = images[:args.limit] if args.limit else images
    for i, img in enumerate(show, 1):
        w = img.get("width", "?")
        h = img.get("height", "?")
        dims = f"{w}x{h}"
        date = img.get("date_available", img.get("date_creation", ""))
        album_path = PiwigoClient.extract_album_path(img)
        fname = img.get("file", f"image_{img['id']}")
        display = f"{album_path}/{fname}" if album_path else fname
        print(f"{i:>6}  {img['id']:>7}  {display:<60} {dims:>12}  {date}")
    if args.limit and len(images) > args.limit:
        print(f"         ... and {len(images) - args.limit} more photos")
    print("─" * 110)
    label = f"tag(s): {', '.join(tags)}" if tags else "all photos"
    print(f"       {len(images)} photo(s) — {label}\n")


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------
def sync(args):
    # --- Connect to Piwigo ---
    piwigo = PiwigoClient(args.piwigo_url, args.piwigo_user, args.piwigo_pass,
                          api_key=args.piwigo_api_key)
    tags = parse_tags(args.piwigo_tags)
    images = fetch_images(piwigo, tags, per_page=args.per_page)

    if not images:
        label = ", ".join(tags) if tags else "all photos"
        log.warning("No images found (%s). Nothing to sync.", label)
        return

    # --- Connect to SlideShow ---
    slideshow = SlideshowWebDAV(
        args.slideshow_url, args.slideshow_user,
        args.slideshow_pass, args.slideshow_folder,
    )

    # --- Build desired file set (rel_path → image metadata) ---
    desired_files: dict[str, dict] = {}
    for img in images:
        album_path = PiwigoClient.extract_album_path(img)
        rel_path = make_rel_path(img, album_path)
        desired_files[rel_path] = img

    # --- Get current files on SlideShow ---
    log.info("Listing files on SlideShow...")
    existing = slideshow.list_files()
    log.info("SlideShow: %d files, Piwigo: %d files expected",
             len(existing), len(desired_files))

    # --- What to upload (in Piwigo but missing from SlideShow) ---
    to_upload = [
        (rel_path, img)
        for rel_path, img in desired_files.items()
        if rel_path not in existing
    ]

    # --- What to remove (on SlideShow but no longer tagged in Piwigo) ---
    to_remove = existing - set(desired_files.keys())

    already_synced = len(desired_files) - len(to_upload)
    log.info("Sync plan: %d to upload, %d to remove, %d already synced",
             len(to_upload), len(to_remove), already_synced)

    if not to_upload and not to_remove:
        log.info("Nothing to do — everything is in sync.")
        return

    # --- Upload missing images ---
    uploaded = 0
    errors = 0
    for i, (rel_path, img) in enumerate(to_upload, 1):
        img_url = (
            img.get("element_url")
            or img.get("derivatives", {}).get("large", {}).get("url")
            or img.get("derivatives", {}).get("medium", {}).get("url")
        )
        if not img_url:
            log.warning("  No download URL for image %s, skipping", img.get("id"))
            continue

        if args.dry_run:
            log.info("  [DRY RUN] Would upload %s", rel_path)
            uploaded += 1
            continue

        try:
            log.info("  [%d/%d] Downloading %s ...", i, len(to_upload), rel_path)
            data = piwigo.download_image(img_url)
            if slideshow.upload(rel_path, data):
                uploaded += 1
            else:
                errors += 1
        except Exception as e:
            log.error("  Error processing %s: %s", rel_path, e)
            errors += 1

        # Small delay to avoid overwhelming the SlideShow device
        time.sleep(0.3)

    # --- Remove orphan files (no longer tagged in Piwigo) ---
    removed = 0
    if to_remove:
        log.info("Removing %d files no longer matching Piwigo selection...",
                 len(to_remove))
        for rel_path in sorted(to_remove):
            if args.dry_run:
                log.info("  [DRY RUN] Would delete %s", rel_path)
                removed += 1
            else:
                if slideshow.delete(rel_path):
                    removed += 1

    # --- Summary ---
    log.info(
        "Done: %d uploaded, %d removed, %d errors, %d already synced",
        uploaded, removed, errors, already_synced,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    # First pass: extract --config without triggering --help
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", "-c", default=None, metavar="FILE")
    known, _ = pre.parse_known_args()
    cfg = load_config(known.config)

    # Main parser with all arguments
    p = argparse.ArgumentParser(
        description="Sync Piwigo tagged photos to SlideShow Digital Signage via WebDAV",
    )

    # Config file
    p.add_argument("--config", "-c", default=None, metavar="FILE",
                    help="Path to .conf file (default: auto-detected)")

    # Piwigo settings
    g = p.add_argument_group("Piwigo")
    g.add_argument("--piwigo-url",
                    default=os.getenv("PIWIGO_URL", cfg["piwigo_url"]),
                    help="Piwigo base URL")
    g.add_argument("--piwigo-user",
                    default=os.getenv("PIWIGO_USER", cfg["piwigo_user"]),
                    help="Piwigo username")
    g.add_argument("--piwigo-pass",
                    default=os.getenv("PIWIGO_PASS", cfg["piwigo_pass"]),
                    help="Piwigo password")
    g.add_argument("--piwigo-tags",
                    default=os.getenv("PIWIGO_TAGS", cfg["piwigo_tags"]),
                    help="Comma-separated tag names to filter photos "
                         "(e.g. 'Cadre-photo' or 'Cadre-photo,Volley'). "
                         "Empty = sync all photos")
    g.add_argument("--piwigo-api-key",
                    default=os.getenv("PIWIGO_API_KEY", cfg["piwigo_api_key"]),
                    help="Piwigo 16+ API key (alternative to user/password)")

    # SlideShow settings
    g = p.add_argument_group("SlideShow")
    g.add_argument("--slideshow-url",
                    default=os.getenv("SLIDESHOW_URL", cfg["slideshow_url"]),
                    help="SlideShow base URL (e.g. http://192.168.1.100:8080)")
    g.add_argument("--slideshow-user",
                    default=os.getenv("SLIDESHOW_USER", cfg["slideshow_user"]),
                    help="SlideShow username")
    g.add_argument("--slideshow-pass",
                    default=os.getenv("SLIDESHOW_PASS", cfg["slideshow_pass"]),
                    help="SlideShow password")
    g.add_argument("--slideshow-folder",
                    default=os.getenv("SLIDESHOW_FOLDER", cfg["slideshow_folder"]),
                    help="Subfolder on SlideShow (optional)")

    # Behavior
    g = p.add_argument_group("Options")
    g.add_argument("--per-page", type=int,
                    default=int(os.getenv("PER_PAGE", cfg["per_page"])),
                    help="Piwigo pagination size")
    g.add_argument("--dry-run", action="store_true",
                    help="Simulate without uploading/deleting anything")
    g.add_argument("--list", action="store_true", dest="list_slideshow",
                    help="List files currently on the SlideShow device and exit")
    g.add_argument("--list-piwigo", action="store_true",
                    help="List photos tagged on Piwigo and exit")
    g.add_argument("--limit", type=int, default=0, metavar="N",
                    help="Limit --list/--list-piwigo output to N entries (0=all)")
    g.add_argument("--wipe", action="store_true",
                    help="Delete ALL files and folders from SlideShow target directory")
    g.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompt for --wipe")
    g.add_argument("--verbose", "-v", action="store_true",
                    help="Enable debug logging")

    return p.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # --- List mode: SlideShow ---
        if args.list_slideshow:
            list_slideshow(args)
            return

        # --- List mode: Piwigo ---
        if args.list_piwigo:
            list_piwigo(args)
            return

        # --- Wipe mode ---
        if args.wipe:
            wipe_slideshow(args)
            return

        # --- Sync mode ---
        log.info("=== Piwigo → SlideShow sync ===")
        tags = parse_tags(args.piwigo_tags)
        tag_label = ", ".join(tags) if tags else "(all photos)"
        log.info("Piwigo:    %s  (tags: %s)", args.piwigo_url, tag_label)
        log.info("SlideShow: %s  (folder: %s)",
                 args.slideshow_url, args.slideshow_folder or "/")
        sync(args)

    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
