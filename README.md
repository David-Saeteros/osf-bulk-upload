# osf-bulk-upload

Upload a whole folder, subfolders included, to an [OSF](https://osf.io) project in one command.

The OSF website lets you drag in files but not folders, so sharing a project's code, materials or
results means rebuilding the folder structure by hand. This script mirrors a local folder into a
project's **OSF Storage**: it creates the subfolders, uploads every file, and on later runs uploads only
what is new or has changed.

- One file, one dependency (`requests`), Python 3.8 or newer
- Your token is typed in hidden and never saved
- `--dry-run` shows what would happen before anything is uploaded
- Safe to re-run: unchanged files are skipped (compared by MD5 checksum)
- Clear messages for the usual problems (read-only token, read-only access, registrations, wrong ID)

## 1. Get a token (once)

1. Sign in to OSF and open **[Settings → Personal access tokens](https://osf.io/settings/tokens)**.
2. Click **Create token**, give it any name, and tick the scope **`osf.full_write`**.
   Without that scope the token can only read, and uploads fail with "HTTP 403".
3. Copy the token. OSF shows it only once. Treat it like a password: don't paste it into emails, chats or
   shared documents. You can revoke it on the same page at any time.

## 2. Install (once)

You need Python 3.8+ ([python.org](https://www.python.org/downloads/); on Windows tick
*"Add python.exe to PATH"* during installation). Then download
[`osf_upload.py`](osf_upload.py) (or clone this repository) and install the one dependency:

```
pip install requests
```

## 3. Upload

Find your **project ID**: the five characters after `osf.io/` in the project's address. For
`https://osf.io/abc12/` it is `abc12`. Components (sub-projects) have their own ID.

Open a terminal (Windows: *PowerShell*; macOS: *Terminal*) in the folder that contains
`osf_upload.py`, and first check what would happen:

```
python osf_upload.py abc12 "C:\Users\me\Documents\my_study" --dry-run
```

The script asks for your token (nothing appears while you paste it, which is normal; press Enter),
checks that you can write to the project, and lists every file with what it would do. If it looks
right, run the same command without `--dry-run`:

```
python osf_upload.py abc12 "C:\Users\me\Documents\my_study"
```

The **contents** of `my_study` go to the top level of OSF Storage. To put them inside a folder,
use `--dest`:

```
python osf_upload.py abc12 "C:\Users\me\Documents\my_study" --dest my_study
```

On macOS and Linux, use `python3` and paths like `~/Documents/my_study`.

## Options

| option | what it does |
|---|---|
| `--dest PATH` | upload into this folder of OSF Storage, e.g. `--dest materials/2024`; missing folders are created. Default: top level |
| `--exclude PATTERN ...` | skip files or folders whose name or path matches, e.g. `--exclude "*.csv" "_backup*" "data/raw"` |
| `--on-conflict changed` | **default.** Upload new files and replace files whose content changed; leave identical files alone |
| `--on-conflict skip` | upload new files only; never touch files already on OSF |
| `--on-conflict overwrite` | upload everything again, replacing what is on OSF |
| `--dry-run` | list what would happen without changing anything |

Always skipped: `.git`, `__pycache__`, `.ipynb_checkpoints`, `.DS_Store`, `Thumbs.db`, `desktop.ini`
and Office lock files (`~$*`).

**Not typing the token each time.** Set the environment variable `OSF_TOKEN` in the terminal
window, keeping the quotation marks:

```
# Windows PowerShell
$env:OSF_TOKEN = "your-token"
# macOS / Linux
export OSF_TOKEN="your-token"
```

It lasts until you close that window.

## Example output

```
Account : Jane Doe
Project : My study (osf.io/abc12)
From    : C:\Users\me\Documents\my_study
To      : OSF Storage/
Files   : 3 (2.4 MB); mode: changed

[1/3] README.md: new -> upload
[2/3] code/analysis.R: changed -> update
[3/3] code/helpers/plot.R: unchanged

Summary: 1 uploaded, 1 updated, 1 unchanged
```

## Troubleshooting

| message | what to do |
|---|---|
| `HTTP 401` | The token is wrong or incomplete. Create a new one and copy it whole. |
| `HTTP 403` | The token lacks `osf.full_write`, or your account can only read the project. Create a token with the right scope, or ask an administrator for "Read + Write" access. |
| `... is a registration` | Registrations are frozen. Upload to the project the registration was made from. |
| `No project with ID ...` | Check the ID, and that you are signed in with an account that is a contributor. |
| `HTTP 429` or the network drops | Run the same command again. Files already uploaded are skipped. |
| `'python' is not recognized` | Python is not installed or not on PATH. Reinstall it and tick "Add python.exe to PATH". |

Notes:
- OSF Storage accepts files up to 5 GB each. Larger files need an add-on storage (for example
  Dropbox or Google Drive connected to the project).
- The script only adds and updates files. It never deletes anything on OSF; remove files you no longer
  want on the OSF website.
- Uploading to an add-on storage (GitHub, Dropbox, ...) is not supported; the script targets OSF Storage.

## How it works

The script uses OSF's public APIs: `api.osf.io` to check your account and your rights on the project,
and `files.osf.io` (WaterButler) to list folders, create them and upload files. Every request carries
your token in an `Authorization` header, sent only to OSF. The code is a single, commented file;
reading it top to bottom takes a few minutes.

## License

MIT. See [LICENSE](LICENSE). Contributions and bug reports are welcome through
[issues](../../issues).
