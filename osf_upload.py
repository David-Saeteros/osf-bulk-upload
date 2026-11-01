#!/usr/bin/env python3
"""
osf_upload.py - upload a whole folder (with all its subfolders) to an OSF project.

The OSF website only lets you drag in individual files. This script mirrors a local folder into a
project's OSF Storage, creating the subfolders and uploading every file, and it can be re-run safely:
by default it only uploads files that are new or have changed.

Quick start
-----------
    pip install requests
    python osf_upload.py abc12 "path/to/my_folder" --dry-run     # see what would happen
    python osf_upload.py abc12 "path/to/my_folder"               # do it

    abc12 = your project ID: the five characters after osf.io/ in the project's URL.
    The script asks for your OSF personal access token (input is hidden). Create one at
    https://osf.io/settings/tokens with the scope "osf.full_write".

Common options
--------------
    --dest results/2024         put the files in this folder of OSF Storage (created if needed);
                                default is the top level of OSF Storage
    --exclude "*.csv" "_backup*"  skip files or folders matching these patterns (can repeat)
    --on-conflict changed       (default) upload new files and files whose content changed
                  skip          upload new files only; never touch files already on OSF
                  overwrite     upload everything, replacing what is on OSF
    --dry-run                   list what would be uploaded, without changing anything

How it works (for the curious)
------------------------------
OSF has two web APIs:
  * api.osf.io        - information about projects (used here to check the project and your rights)
  * files.osf.io      - "WaterButler", the file service (used to list, create folders and upload)
Every request carries your token in an "Authorization: Bearer <token>" header. Nothing is sent
anywhere else, and the token is never written to disk.

License: MIT. Only dependency: requests (pip install requests). Python 3.8+.
"""

import argparse
import fnmatch
import getpass
import hashlib
import os
import sys
import time
from pathlib import Path, PurePosixPath

try:
    import requests
except ImportError:  # friendly message instead of a traceback
    sys.exit("This script needs the 'requests' package. Install it with:  pip install requests")

API = "https://api.osf.io/v2"
FILES = "https://files.osf.io/v1"

# Files and folders that are almost never meant to be shared. Add your own with --exclude.
DEFAULT_EXCLUDES = [".git", ".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__",
                    ".ipynb_checkpoints", "~$*"]


# =================================================================================================
# Small helpers
# =================================================================================================
class OSFError(Exception):
    """An error with a message that tells the user what to do."""


def explain_http_error(response, action):
    """Turn an HTTP error from OSF into an actionable message."""
    code = response.status_code
    hints = {
        401: "Your token was rejected. Check that you copied it completely, or create a new one at "
             "https://osf.io/settings/tokens",
        403: "OSF refused the change. If earlier files uploaded fine, OSF is probably throttling you: "
             "wait 10-15 minutes and run the same command again (finished files are skipped). Otherwise, usual causes: (1) the token does not have the 'osf.full_write' "
             "scope; (2) your account has only read access to this project; (3) the ID belongs to a "
             "registration, which is frozen and cannot be changed.",
        404: "Not found. Check the project ID (the five characters after osf.io/ in the URL).",
        429: "OSF is rate-limiting requests. Wait a few minutes and run the script again; files "
             "already uploaded will be skipped.",
    }
    hint = hints.get(code, "OSF returned an unexpected error; try again in a few minutes.")
    return OSFError(f"{action} failed (HTTP {code}). {hint}")


class OSFSession:
    """A requests session that adds the token and retries temporary failures."""

    def __init__(self, token, retries=4):
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.retries = retries

    def request(self, method, url, action, ok=(200, 201), allow=(), **kwargs):
        """Send a request. Retries on network errors and on HTTP 429/5xx with growing waits.
        Returns the response if its status is in `ok` or `allow`; otherwise raises OSFError."""
        for attempt in range(self.retries + 1):
            data = kwargs.get("data")
            if hasattr(data, "seek"):
                data.seek(0)  # rewind the file before re-sending it
            try:
                response = self.http.request(method, url, timeout=(30, 600), **kwargs)
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise OSFError(f"{action} failed: could not reach OSF ({exc}).")
            else:
                if response.status_code in ok or response.status_code in allow:
                    return response
                # 403 in the middle of a long upload is usually OSF throttling, not a real permission
                # problem: wait and retry it once (real permission errors fail again and are reported).
                transient = response.status_code in (429, 500, 502, 503, 504) or (
                    response.status_code == 403 and method == "PUT" and attempt == 0)
                if not transient or attempt == self.retries:
                    raise explain_http_error(response, action)
                if response.status_code == 403:
                    print("    OSF refused the request; waiting 60 s and trying again...")
                    time.sleep(60)
                    continue
            time.sleep(2 ** attempt * 2)  # 2, 4, 8, 16 s


def md5_of(path, chunk=1024 * 1024):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def is_excluded(rel_path, patterns):
    """True if the file/folder (relative POSIX path) or any of its parts matches a pattern."""
    parts = PurePosixPath(rel_path).parts
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# =================================================================================================
# OSF Storage operations
# =================================================================================================
class OSFStorage:
    """Folder listing, folder creation and file upload in one project's OSF Storage."""

    def __init__(self, session, project_id):
        self.s = session
        self.project = project_id
        self.base = f"{FILES}/resources/{project_id}/providers/osfstorage"
        self._listing_cache = {}

    def check_project(self):
        """Fail early, with a clear message, if the project cannot be written to."""
        me = self.s.request("GET", f"{API}/users/me/", "Checking your token").json()
        name = me["data"]["attributes"].get("full_name", "?")

        response = self.s.request("GET", f"{API}/nodes/{self.project}/", "Opening the project",
                                  allow=(404, 410))
        if response.status_code in (404, 410):
            reg = self.s.request("GET", f"{API}/registrations/{self.project}/", "Opening the project",
                                 allow=(404, 410))
            if reg.status_code == 200:
                raise OSFError(f"'{self.project}' is a registration. Registrations are frozen and "
                               "cannot be changed; upload to the original project instead.")
            raise OSFError(f"No project with ID '{self.project}' that your account can see.")

        attrs = response.json()["data"]["attributes"]
        permissions = attrs.get("current_user_permissions", [])
        if "write" not in permissions and "admin" not in permissions:
            raise OSFError(f"Your account ({name}) can only read project '{attrs.get('title')}'. "
                           "Ask an administrator to give you 'Read + Write' access.")
        return name, attrs.get("title", self.project)

    def list_folder(self, folder_path):
        """Contents of a folder ('/' = top level; otherwise WaterButler path like '/5f3a.../').
        Returns {name: item}, where item has kind, path, size, md5."""
        if folder_path not in self._listing_cache:
            response = self.s.request("GET", self.base + folder_path, "Listing an OSF folder")
            items = {}
            for entry in response.json().get("data", []):
                a = entry["attributes"]
                items[a["name"]] = {
                    "kind": a["kind"],
                    "path": a["path"],
                    "size": a.get("size"),
                    "md5": ((a.get("extra") or {}).get("hashes") or {}).get("md5"),
                }
            self._listing_cache[folder_path] = items
        return self._listing_cache[folder_path]

    def ensure_folder(self, parent_path, name, dry_run=False):
        """Return the path of folder `name` inside `parent_path`, creating it if needed."""
        existing = self.list_folder(parent_path).get(name)
        if existing:
            if existing["kind"] != "folder":
                raise OSFError(f"A file named '{name}' already exists where a folder is needed.")
            return existing["path"]
        if dry_run:
            fake = f"{parent_path}{name}/(new)/"
            self._listing_cache[fake] = {}
            self._listing_cache[parent_path][name] = {"kind": "folder", "path": fake}
            return fake
        response = self.s.request("PUT", self.base + parent_path, f"Creating folder '{name}'",
                                  params={"kind": "folder", "name": name}, allow=(409,))
        if response.status_code == 409:  # created meanwhile by someone else: re-read the listing
            self._listing_cache.pop(parent_path, None)
            return self.list_folder(parent_path)[name]["path"]
        path = response.json()["data"]["attributes"]["path"]
        self._listing_cache[parent_path][name] = {"kind": "folder", "path": path}
        self._listing_cache[path] = {}
        return path

    def upload_new(self, parent_path, name, local_file):
        with open(local_file, "rb") as fh:
            self.s.request("PUT", self.base + parent_path, f"Uploading '{name}'",
                           params={"kind": "file", "name": name}, data=fh)

    def upload_replace(self, file_path, name, local_file):
        with open(local_file, "rb") as fh:
            self.s.request("PUT", self.base + file_path, f"Updating '{name}'",
                           params={"kind": "file"}, data=fh)


# =================================================================================================
# Main procedure
# =================================================================================================
def collect_local_files(root, excludes):
    """All files under `root` (sorted), as (relative POSIX path, absolute Path), minus exclusions."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        # prune excluded folders so os.walk does not enter them
        dirnames[:] = sorted(d for d in dirnames
                             if not is_excluded(f"{rel_dir}/{d}".lstrip("/"), excludes))
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}".lstrip("/")
            if not is_excluded(rel, excludes):
                files.append((rel, Path(dirpath) / name))
    return files


def run(args):
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        raise OSFError(f"Local folder not found: {root}")

    token = os.environ.get("OSF_TOKEN") or getpass.getpass(
        "OSF personal access token (input hidden; create one at https://osf.io/settings/tokens): ")
    token = token.strip().strip('"').strip("'")
    if not token:
        raise OSFError("No token given.")

    storage = OSFStorage(OSFSession(token), args.project)
    user, title = storage.check_project()
    excludes = DEFAULT_EXCLUDES + (args.exclude or [])
    files = collect_local_files(root, excludes)
    dest_parts = [p for p in PurePosixPath("/" + args.dest.strip("/")).parts if p != "/"]

    print(f"Account : {user}")
    print(f"Project : {title} (osf.io/{args.project})")
    print(f"From    : {root}")
    print(f"To      : OSF Storage/{'/'.join(dest_parts)}")
    print(f"Files   : {len(files)} ({human_size(sum(p.stat().st_size for _, p in files))}); "
          f"mode: {args.on_conflict}{'; DRY RUN, nothing will be changed' if args.dry_run else ''}\n")

    # Create (or find) the destination folder
    folder_cache = {}
    dest_path = "/"
    for part in dest_parts:
        dest_path = storage.ensure_folder(dest_path, part, args.dry_run)
    folder_cache[""] = dest_path

    def remote_folder_for(rel_dir):
        """WaterButler path of the remote folder mirroring local `rel_dir`, created on demand."""
        if rel_dir not in folder_cache:
            parent, _, name = rel_dir.rpartition("/")
            folder_cache[rel_dir] = storage.ensure_folder(remote_folder_for(parent), name, args.dry_run)
        return folder_cache[rel_dir]

    counts = {"uploaded": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for i, (rel, local) in enumerate(files, 1):
        rel_dir, _, name = rel.rpartition("/")
        prefix = f"[{i}/{len(files)}] {rel}"
        try:
            parent = remote_folder_for(rel_dir)
            remote = storage.list_folder(parent).get(name)

            if remote is None:
                action = "uploaded"
            elif remote["kind"] == "folder":
                raise OSFError("a folder with this name already exists on OSF")
            elif args.on_conflict == "skip":
                action = "skipped"
            elif args.on_conflict == "overwrite":
                action = "updated"
            else:  # "changed": compare content fingerprints
                same = remote["md5"] == md5_of(local) if remote["md5"] else remote["size"] == local.stat().st_size
                action = "unchanged" if same else "updated"

            label = {"uploaded": "new -> upload", "updated": "changed -> update",
                     "unchanged": "unchanged", "skipped": "exists -> skip"}[action]
            if action in ("uploaded", "updated") and not args.dry_run:
                if action == "uploaded":
                    storage.upload_new(parent, name, local)
                else:
                    storage.upload_replace(remote["path"], name, local)
            print(f"{prefix}: {label}")
            counts[action] += 1
        except OSFError as exc:
            print(f"{prefix}: FAILED - {exc}")
            counts["failed"] += 1
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                print("\nStopping: the remaining files would fail for the same reason.")
                break

    verb = "would be " if args.dry_run else ""
    print("\nSummary: " + ", ".join(f"{n} {verb}{k}" if k in ("uploaded", "updated") else f"{n} {k}"
                                    for k, n in counts.items() if n))
    if args.dry_run:
        print("Dry run only. Run again without --dry-run to upload.")
    return 1 if counts["failed"] else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Upload a folder, with all its subfolders, to an OSF project's OSF Storage.",
        epilog="Example: python osf_upload.py abc12 ./my_study --dest materials --exclude '*.tmp'")
    parser.add_argument("project", help="project (or component) ID: the 5 characters after osf.io/")
    parser.add_argument("folder", help="local folder whose contents will be uploaded")
    parser.add_argument("--dest", default="/", help="folder in OSF Storage to upload into (default: top level)")
    parser.add_argument("--exclude", nargs="+", metavar="PATTERN",
                        help="skip files/folders matching these patterns, e.g. '*.csv' 'data/raw'")
    parser.add_argument("--on-conflict", choices=["changed", "skip", "overwrite"], default="changed",
                        help="what to do with files already on OSF (default: update only if changed)")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except OSFError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue; finished files are skipped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
