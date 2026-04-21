# piwigo_to_slideshow

Sync photos from a [Piwigo](https://piwigo.org/) gallery to a [SlideShow Digital Signage](https://slideshow.digital/) device over WebDAV, preserving the album directory structure.

## How it works

The script fetches all Piwigo photos carrying a given tag (default: `Cadre-photo`) and performs a **true two-way sync** with the SlideShow device:

- **Uploads** photos that exist in Piwigo but are missing from the SlideShow
- **Deletes** photos on the SlideShow that no longer carry the tag in Piwigo
- **Skips** photos already present on both sides

The Piwigo album hierarchy is mirrored on the SlideShow:

```
Piwigo                              SlideShow (WebDAV)
──────                              ──────────────────
galleries/                          /webdav/piwigo/
  Holidays/                           Holidays/
    Summer 2024/                        Summer 2024/
      DSC_0001.jpg        →              42_DSC_0001.jpg
  Family/                             Family/
    Christmas 2025/                     Christmas 2025/
      photo.jpg           →              58_photo.jpg
```

Files are prefixed with their Piwigo image ID to avoid name collisions.

## Requirements

- Python 3.10+
- `requests`

```bash
pip install requests
```

## Installation

```bash
git clone https://github.com/Elwinmage/piwigo_to_slideshow.git
cd piwigo_to_slideshow
cp piwigo_to_slideshow_conf.example piwigo_to_slideshow.conf
chmod 600 piwigo_to_slideshow.conf
```

Edit `piwigo_to_slideshow.conf` with your credentials.

## Configuration

The `.conf` file uses the INI format:

```ini
[piwigo]
url      = https://my-piwigo.example.com
user     = admin
password = secret
tag      = Cadre-photo
# Piwigo 16+: API key (alternative to user/password)
# Generate one in your Piwigo profile → API Keys
api_key  =

[slideshow]
url      = http://192.168.0.219:8080
user     = photos
password = secret
# Subfolder to keep Piwigo photos separate from other content
folder   = piwigo

[options]
# Number of images per Piwigo API request (recommended: 500)
per_page = 500
```

### Configuration priority

Settings are resolved in this order (first match wins):

1. CLI arguments (`--piwigo-url`, `--slideshow-user`, etc.)
2. Environment variables (`PIWIGO_URL`, `SLIDESHOW_URL`, etc.)
3. Config file
4. Built-in defaults

### Config file location

The config file is auto-detected in this order:

1. `./piwigo_to_slideshow.conf` (next to the script)
2. `~/.config/piwigo_to_slideshow.conf`
3. `/etc/piwigo_to_slideshow.conf`

Or specified explicitly with `--config /path/to/file.conf`.

### Piwigo authentication

Two modes are supported:

- **Login/password** — classic session-cookie authentication
- **API Key** (Piwigo 16+) — `X-PIWIGO-API` header, recommended for automated scripts. Generate a key in your Piwigo profile → "API Keys" section

When `api_key` is set, `user` and `password` are ignored.

## Usage

### Sync

```bash
# Full sync (upload missing + delete orphans)
python piwigo_to_slideshow.py

# Dry run — simulate without modifying anything
python piwigo_to_slideshow.py --dry-run

# Verbose logging
python piwigo_to_slideshow.py -v
```

The script is **idempotent**: running it twice in a row, the second run does nothing.

### List files

```bash
# Files currently on the SlideShow
python piwigo_to_slideshow.py --list

# Tagged photos on Piwigo
python piwigo_to_slideshow.py --list-piwigo

# Show only the first 20 entries
python piwigo_to_slideshow.py --list --limit 20
python piwigo_to_slideshow.py --list-piwigo --limit 20
```

### Wipe SlideShow content

```bash
# Delete all files and folders from the target directory
python piwigo_to_slideshow.py --wipe

# Skip confirmation prompt
python piwigo_to_slideshow.py --wipe --yes
```

> **Warning**: if `folder` is empty in the config, `--wipe` will delete **everything** on the WebDAV root. With `folder = piwigo`, only the `piwigo/` subfolder is affected.

### Automate with cron

```bash
# Sync every hour
0 * * * * /usr/bin/python3 /path/to/piwigo_to_slideshow.py >> /var/log/piwigo_sync.log 2>&1
```

## All CLI options

```
usage: piwigo_to_slideshow.py [-h] [--config FILE]
                              [--piwigo-url URL] [--piwigo-user USER]
                              [--piwigo-pass PASS] [--piwigo-tag TAG]
                              [--piwigo-api-key KEY]
                              [--slideshow-url URL] [--slideshow-user USER]
                              [--slideshow-pass PASS] [--slideshow-folder DIR]
                              [--per-page N] [--dry-run]
                              [--list] [--list-piwigo] [--limit N]
                              [--wipe] [--yes] [--verbose]

Options:
  --config, -c FILE      Path to .conf file (default: auto-detected)
  --dry-run              Simulate without uploading/deleting anything
  --list                 List files on the SlideShow device and exit
  --list-piwigo          List tagged photos on Piwigo and exit
  --limit N              Limit list output to N entries (0 = all)
  --wipe                 Delete ALL files/folders from the SlideShow target
  --yes, -y              Skip confirmation prompt for --wipe
  --verbose, -v          Enable debug logging
  --per-page N           Piwigo API pagination size (default: 500)
```

## Environment variables

All settings can be overridden via environment variables:

| Variable | Description |
|---|---|
| `PIWIGO_URL` | Piwigo base URL |
| `PIWIGO_USER` | Piwigo username |
| `PIWIGO_PASS` | Piwigo password |
| `PIWIGO_TAG` | Tag to filter photos |
| `PIWIGO_API_KEY` | Piwigo 16+ API key |
| `SLIDESHOW_URL` | SlideShow base URL |
| `SLIDESHOW_USER` | SlideShow / WebDAV username |
| `SLIDESHOW_PASS` | SlideShow / WebDAV password |
| `SLIDESHOW_FOLDER` | Target subfolder on SlideShow |
| `PER_PAGE` | Piwigo API page size |

## How the sync works

1. **Fetch** all images tagged with the configured tag from Piwigo (`pwg.tags.getImages`)
2. **Extract** the album path from each image's `element_url` (e.g. `.../galleries/Holidays/Summer/photo.jpg` → `Holidays/Summer`)
3. **List** all files currently on the SlideShow via WebDAV `PROPFIND`
4. **Compare** the two sets by relative path
5. **Upload** missing files (download from Piwigo, PUT to SlideShow WebDAV)
6. **Delete** orphan files (on SlideShow but no longer tagged in Piwigo)
7. **Create** subdirectories automatically via WebDAV `MKCOL`

## Compatibility

- **Piwigo**: tested with Piwigo 16.3.0, should work with older versions (13+)
- **SlideShow**: tested with SlideShow Digital Signage on Android (WebDAV on port 8080)
- **Python**: requires 3.10+ (type hint syntax)

## Tips

- Use `folder = piwigo` in the config to keep synced photos in a dedicated subfolder, separate from manually uploaded content on the SlideShow.
- In SlideShow, create a **Content** of type "Files randomly" with path `piwigo/**` to display all synced photos including subfolders.
- The `per_page` setting controls how many images are fetched per API call. With large libraries (10,000+ photos), `500` is a good balance between speed and reliability.
- If the SlideShow device resets connections during bulk uploads, increase the delay in the script (`time.sleep(0.3)` → `time.sleep(1.0)`).

## License

MIT
