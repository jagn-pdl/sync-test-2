#!/usr/bin/env python3
"""
sync_project.py — Sync between Google Drive ↔ GitHub ↔ Local PC

Usage:
  python sync_project.py pull                        # Drive → GitHub → Local
  python sync_project.py push "your commit message"  # Local → GitHub → Drive

Requirements:
  pip install PyGithub google-api-python-client google-auth-httplib2 google-auth-oauthlib gitpython

Setup:
  1. Copy config.example.json → config.json and fill in your values.
  2. Place your Google OAuth credentials JSON at the path specified in config.json.
  3. On first run, a browser window will open for Google OAuth consent.
"""

import argparse
import io
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# ── Third-party ──────────────────────────────────────────────────────────────
try:
    import git                                          # gitpython
    from github import Github                           # PyGithub
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc}\n"
        "Run: pip install PyGithub google-api-python-client "
        "google-auth-httplib2 google-auth-oauthlib gitpython"
    )

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_FILE   = Path(__file__).parent / "config.json"
TOKEN_FILE    = Path(__file__).parent / "google_token.json"
SCOPES        = ["https://www.googleapis.com/auth/drive"]

GITIGNORE_DEFAULTS = [
    "# Python",
    "__pycache__/", "*.py[cod]", "*.pyo", "*.pyd",
    ".Python", "build/", "dist/", "*.egg-info/", ".eggs/",
    "venv/", ".venv/", "env/", ".env/",
    "",
    "# Environment / secrets",
    ".env", "*.env", "config.json", "google_token.json",
    "credentials.json", "service_account.json",
    ".drive_checksums.json", ".drive_mtimes.json",
    "",
    "# IDEs",
    ".idea/", ".vscode/", "*.suo", "*.user",
    "",
    "# OS",
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "",
    "# Logs",
    "*.log", "logs/",
    "",
    "# Node",
    "node_modules/", "npm-debug.log*",
    "",
    "# Java / Kotlin",
    "*.class", "*.jar", "*.war", "target/",
    "",
    "# Compiled / binary",
    "*.exe", "*.dll", "*.so", "*.o", "*.a",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[!] Config file not found: {CONFIG_FILE}")
        print("    Creating a template — please fill it in and re-run.\n")
        template = {
            "github_token":         "ghp_YOUR_PERSONAL_ACCESS_TOKEN",
            "github_repo":          "your-username/your-repo-name",
            "github_branch":        "main",
            "local_project_dir":    "/path/to/your/local/project",
            "google_drive_folder_id": "YOUR_GOOGLE_DRIVE_FOLDER_ID",
            "google_credentials_file": str(Path(__file__).parent / "credentials.json"),
        }
        CONFIG_FILE.write_text(json.dumps(template, indent=2))
        sys.exit(f"Template written to {CONFIG_FILE}. Edit it and run again.")

    with open(CONFIG_FILE) as fh:
        cfg = json.load(fh)

    required = [
        "github_token", "github_repo", "github_branch",
        "local_project_dir", "google_drive_folder_id",
        "google_credentials_file",
    ]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"[!] Missing config keys: {missing}")
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# Google Drive helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_drive_service(credentials_file: str):
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


import hashlib

def _md5(path: Path) -> str:
    """Compute MD5 hex digest of a local file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def drive_list_files(service, folder_id: str) -> list[dict]:
    """Return all files (non-trashed) inside a Drive folder, recursively.
    Includes md5Checksum and modifiedTime for change detection."""
    results = []

    def _recurse(fid: str, prefix: str = ""):
        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{fid}' in parents and trashed = false",
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            items = resp.get("files", [])
            for item in items:
                rel_path = f"{prefix}{item['name']}"
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    _recurse(item["id"], rel_path + "/")
                else:
                    results.append({**item, "rel_path": rel_path})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _recurse(folder_id)
    return results


def drive_download_all(service, folder_id: str, dest_dir: Path):
    """Download only new or changed files from Drive folder into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    files = drive_list_files(service, folder_id)
    if not files:
        print("  [Drive] Folder is empty — nothing to download.")
        return

    downloaded = skipped = 0

    # Google Workspace export map
    export_map = {
        "application/vnd.google-apps.document":
            ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document", ".docx"),
        "application/vnd.google-apps.spreadsheet":
            ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation":
            ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation", ".pptx"),
    }

    for item in files:
        mime = item["mimeType"]
        local_path = dest_dir / item["rel_path"]

        if mime in export_map:
            export_mime, ext = export_map[mime]
            local_path = local_path.with_suffix(ext)
        else:
            export_mime = None

        local_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Change detection ──────────────────────────────────────────────
        # For regular files: compare Drive md5Checksum vs local file md5.
        # For Google Workspace files: no md5 available — compare modifiedTime
        # against local file mtime stored in a sidecar cache file.
        needs_download = True
        if local_path.exists():
            drive_md5 = item.get("md5Checksum")
            if drive_md5:
                # Regular binary/text file — reliable md5 comparison
                if _md5(local_path) == drive_md5:
                    needs_download = False
            else:
                # Google Workspace file — use cached modifiedTime
                cache_file = dest_dir / ".drive_mtimes.json"
                try:
                    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
                except Exception:
                    cache = {}
                cached_mtime = cache.get(item["id"])
                drive_mtime  = item.get("modifiedTime", "")
                if cached_mtime and cached_mtime == drive_mtime:
                    needs_download = False

        if not needs_download:
            skipped += 1
            print(f"  [Drive] Unchanged: {item['rel_path']}")
            continue

        # ── Download ──────────────────────────────────────────────────────
        if export_mime:
            req = service.files().export_media(fileId=item["id"], mimeType=export_mime)
        else:
            req = service.files().get_media(fileId=item["id"], supportsAllDrives=True)

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        local_path.write_bytes(buf.getvalue())
        downloaded += 1
        print(f"  [Drive] Downloaded: {item['rel_path']}")

        # Update mtime cache for Workspace files
        if not item.get("md5Checksum"):
            cache_file = dest_dir / ".drive_mtimes.json"
            try:
                cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
            except Exception:
                cache = {}
            cache[item["id"]] = item.get("modifiedTime", "")
            cache_file.write_text(json.dumps(cache, indent=2))

    print(f"  [Drive] Done — {downloaded} downloaded, {skipped} unchanged.")


def drive_upload_all(service, folder_id: str, src_dir: Path, ignore_spec):
    """Upload only new or changed files from src_dir to Drive folder."""
    uploaded = skipped = 0

    for local_file in sorted(src_dir.rglob("*")):
        if not local_file.is_file():
            continue
        rel = local_file.relative_to(src_dir)
        if ignore_spec and ignore_spec.match_file(str(rel)):
            continue
        # Never upload the mtime cache file itself
        if rel.name == ".drive_mtimes.json":
            continue

        mime, _ = mimetypes.guess_type(str(local_file))
        mime = mime or "application/octet-stream"

        # Find or create parent folder chain on Drive
        parent_id = _ensure_drive_path(service, folder_id, rel.parent)

        # Check if file already exists on Drive (get id + md5)
        existing = service.files().list(
            q=f"name='{local_file.name}' and '{parent_id}' in parents and trashed=false",
            fields="files(id, md5Checksum)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])

        # Change detection: if file exists on Drive and md5 matches, skip
        if existing and existing[0].get("md5Checksum"):
            if _md5(local_file) == existing[0]["md5Checksum"]:
                skipped += 1
                print(f"  [Drive] Unchanged: {rel}")
                continue

        media = MediaFileUpload(str(local_file), mimetype=mime, resumable=True)
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media,
                supportsAllDrives=True,
            ).execute()
            uploaded += 1
            print(f"  [Drive] Updated:   {rel}")
        else:
            service.files().create(
                body={"name": local_file.name, "parents": [parent_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            uploaded += 1
            print(f"  [Drive] Uploaded:  {rel}")

    print(f"  [Drive] Done — {uploaded} uploaded, {skipped} unchanged.")


def _ensure_drive_path(service, root_id: str, rel_path: Path) -> str:
    """Walk/create folder hierarchy on Drive, return leaf folder ID."""
    current_id = root_id
    if str(rel_path) in (".", ""):
        return current_id
    for part in rel_path.parts:
        resp = service.files().list(
            q=(f"name='{part}' and '{current_id}' in parents "
               "and mimeType='application/vnd.google-apps.folder' "
               "and trashed=false"),
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = resp.get("files", [])
        if folders:
            current_id = folders[0]["id"]
        else:
            folder = service.files().create(
                body={
                    "name": part,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [current_id],
                },
                fields="id",
                supportsAllDrives=True,
            ).execute()
            current_id = folder["id"]
    return current_id


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_github_repo(token: str, repo_name: str):
    from github import Auth
    g = Github(auth=Auth.Token(token))
    try:
        repo = g.get_repo(repo_name)
        repo._token = token  # stash for clone URL building
        return repo
    except Exception as exc:
        sys.exit(f"[!] Cannot access GitHub repo '{repo_name}': {exc}")


def github_pull_to_local(repo, branch: str, local_dir: Path):
    """Clone or pull the GitHub repo into local_dir. Returns the git.Repo object."""
    clone_url = repo.clone_url.replace(
        "https://", f"https://x-access-token:{repo._token}@"
    )
    if (local_dir / ".git").exists():
        print(f"  [Git]   Pulling latest from '{branch}'…")
        gr = git.Repo(str(local_dir))
        gr.remotes.origin.pull(branch)
    else:
        print(f"  [Git]   Cloning repo into {local_dir}…")
        local_dir.mkdir(parents=True, exist_ok=True)
        gr = git.Repo.clone_from(clone_url, str(local_dir), branch=branch)
    print("  [Git]   Pull complete.")
    return gr


def github_push_from_local(repo, branch: str, local_dir: Path, commit_message: str):
    """Stage all changes, commit, and push to GitHub."""
    gr = git.Repo(str(local_dir))
    gr.git.add(A=True)

    if not gr.index.diff("HEAD") and not gr.untracked_files:
        print("  [Git]   Nothing to commit — working tree clean.")
        gr.close()
        return

    gr.index.commit(commit_message)
    origin = gr.remotes.origin
    origin.push(refspec=f"{branch}:{branch}")
    print(f"  [Git]   Pushed to GitHub ({branch}): '{commit_message}'")
    gr.close()


# ═══════════════════════════════════════════════════════════════════════════════
# .gitignore
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_gitignore(local_dir: Path):
    gi_path = local_dir / ".gitignore"
    if gi_path.exists():
        print("  [.gitignore] Already exists — skipping auto-generation.")
        return

    gi_path.write_text("\n".join(GITIGNORE_DEFAULTS) + "\n")
    print(f"  [.gitignore] Generated at {gi_path}")


def load_ignore_spec(local_dir: Path):
    """Return a pathspec matcher for .gitignore rules (if pathspec is installed)."""
    gi_path = local_dir / ".gitignore"
    if not gi_path.exists():
        return None
    try:
        from pathspec import PathSpec
        return PathSpec.from_lines("gitwildmatch", gi_path.read_text().splitlines())
    except ImportError:
        print("  [warn] 'pathspec' not installed — .gitignore will not be applied "
              "during Drive upload.\n         pip install pathspec")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Git-aware Drive download  (only fetch files that differ from GitHub)
# ═══════════════════════════════════════════════════════════════════════════════

def drive_download_changed_only(service, folder_id: str, repo_path: Path,
                                baseline_dir: Path = None):
    """Download Drive files into repo_path, skipping unchanged files.

    baseline_dir: persistent local project folder used to store the Drive
    checksum/mtime cache across runs. If None or does not exist yet (first run),
    falls back to repo_path.

    Change detection strategy
    ─────────────────────────
    Regular files:   Drive supplies an md5Checksum. We cache the LAST SEEN
                     Drive md5 in .drive_checksums.json (keyed by file ID).
                     On next run we compare Drive's current md5 against the
                     cached value — if equal the file has not changed on Drive.
                     This avoids recomputing md5 from the local file, which
                     breaks on Windows because Git's autocrlf rewrites line
                     endings on checkout, making the local md5 differ from
                     Drive's even when the content is logically identical.

    Workspace files: Drive does not supply md5Checksum for Docs/Sheets/Slides.
                     We cache modifiedTime in the same JSON file instead.
    """
    export_map = {
        "application/vnd.google-apps.document":
            ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document", ".docx"),
        "application/vnd.google-apps.spreadsheet":
            ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation":
            ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation", ".pptx"),
    }

    files = drive_list_files(service, folder_id)
    if not files:
        print("  [Drive] Folder is empty — nothing to download.")
        return

    # The cache directory must survive temp-dir deletion between runs.
    # Use baseline_dir (the persistent local project folder) when available.
    _cache_dir = (baseline_dir
                  if baseline_dir and baseline_dir.exists()
                  else repo_path)
    _cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_dir / ".drive_checksums.json"
    try:
        drive_cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    except Exception:
        drive_cache = {}

    cache_dirty = False
    downloaded = skipped = 0

    for item in files:
        mime = item["mimeType"]
        local_path = repo_path / item["rel_path"]

        if mime in export_map:
            export_mime, ext = export_map[mime]
            local_path = local_path.with_suffix(ext)
        else:
            export_mime = None

        # ── Change detection ─────────────────────────────────────────────
        # Key: Drive file ID.
        # Value: md5Checksum for regular files, modifiedTime for Workspace files.
        file_id = item["id"]
        drive_md5 = item.get("md5Checksum")
        needs_download = True

        if drive_md5:
            # Regular file — compare Drive's md5 against last-seen Drive md5.
            # Do NOT compute md5 from the local file: Git autocrlf on Windows
            # rewrites line endings on checkout, making local md5 != Drive md5
            # even when nothing has actually changed.
            cached = drive_cache.get(file_id, {})
            if cached.get("md5") == drive_md5:
                needs_download = False
        else:
            # Google Workspace file — fall back to modifiedTime comparison.
            cached = drive_cache.get(file_id, {})
            drive_mtime = item.get("modifiedTime", "")
            if cached.get("mtime") and cached["mtime"] == drive_mtime:
                needs_download = False

        if not needs_download:
            skipped += 1
            print(f"  [Drive] Unchanged: {item['rel_path']}")
            continue

        # ── Download ──────────────────────────────────────────────────────
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if export_mime:
            req = service.files().export_media(fileId=item["id"], mimeType=export_mime)
        else:
            req = service.files().get_media(fileId=item["id"], supportsAllDrives=True)

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        local_path.write_bytes(buf.getvalue())
        downloaded += 1
        print(f"  [Drive] Downloaded: {item['rel_path']}")

        # Update cache with the Drive-side fingerprint we just confirmed.
        if drive_md5:
            drive_cache[file_id] = {"md5": drive_md5}
        else:
            drive_cache[file_id] = {"mtime": item.get("modifiedTime", "")}
        cache_dirty = True

    # Always write the cache when it has content, not only when something
    # was downloaded — this ensures the file exists on first run so that
    # the next run can skip unchanged files correctly.
    if drive_cache and cache_dirty:
        cache_file.write_text(json.dumps(drive_cache, indent=2))

    print(f"  [Drive] Done — {downloaded} downloaded, {skipped} unchanged.")


# ═══════════════════════════════════════════════════════════════════════════════
# Pull  (Drive → GitHub → Local)
# ═══════════════════════════════════════════════════════════════════════════════

def do_pull(cfg: dict, commit_message: str = ""):
    local_dir = Path(cfg["local_project_dir"])
    drive_folder_id = cfg["google_drive_folder_id"]
    branch = cfg["github_branch"]

    print("\n━━━  PULL: Google Drive → GitHub → Local  ━━━\n")

    # 1. Authenticate
    print("[1/3] Authenticating with Google Drive…")
    drive_svc = get_drive_service(cfg["google_credentials_file"])
    repo      = get_github_repo(cfg["github_token"], cfg["github_repo"])

    # 2. Clone GitHub repo into temp dir, overlay only CHANGED Drive files,
    #    let Git detect what actually changed, then push back to GitHub.
    print("\n[2/3] Syncing Drive → GitHub (changed files only)…")
    repo_tmp_path = Path(tempfile.mkdtemp())
    try:
        repo_path = repo_tmp_path / "repo"

        # Clone (or pull) latest GitHub state — this is our baseline
        gr = github_pull_to_local(repo, branch, repo_path)
        gr.close()

        ensure_gitignore(repo_path)

        # Download only files that differ from the persistent local copy
        drive_download_changed_only(drive_svc, drive_folder_id, repo_path,
                                    baseline_dir=local_dir)

        # Git now knows exactly what changed — commit & push only those
        github_push_from_local(
            repo, branch, repo_path,
            commit_message or "chore: sync from Google Drive [automated]"
        )
    finally:
        import gc, stat
        gc.collect()
        def _force_rmtree(p):
            def _on_error(func, path, exc):
                os.chmod(path, stat.S_IWRITE)
                func(path)
            shutil.rmtree(str(p), onexc=_on_error)
        _force_rmtree(repo_tmp_path)

    # 3. Pull the updated GitHub state to local PC
    print("\n[3/3] Pulling from GitHub → local PC…")
    repo = get_github_repo(cfg["github_token"], cfg["github_repo"])
    gr   = github_pull_to_local(repo, branch, local_dir)
    gr.close()
    ensure_gitignore(local_dir)

    print("\n✅  Pull complete.\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Push  (Local → GitHub → Drive)
# ═══════════════════════════════════════════════════════════════════════════════

def do_push(cfg: dict, commit_message: str):
    local_dir = Path(cfg["local_project_dir"])
    drive_folder_id = cfg["google_drive_folder_id"]
    branch = cfg["github_branch"]

    print("\n━━━  PUSH: Local → GitHub → Google Drive  ━━━\n")

    if not local_dir.exists():
        sys.exit(f"[!] local_project_dir does not exist: {local_dir}")

    # 1. Ensure .gitignore exists
    print("[1/3] Checking .gitignore…")
    ensure_gitignore(local_dir)

    # 2. Push local → GitHub (Git tracks exactly what changed)
    print("\n[2/3] Pushing local → GitHub…")
    repo = get_github_repo(cfg["github_token"], cfg["github_repo"])

    if not (local_dir / ".git").exists():
        print("  [Git]   No .git found — initialising repo and adding remote…")
        gr = git.Repo.init(str(local_dir))
        gr.create_remote("origin", repo.clone_url)
        gr.git.checkout("-b", branch)

    github_push_from_local(repo, branch, local_dir, commit_message)

    # 3. Upload to Drive — only files that differ (md5 comparison)
    print("\n[3/3] Uploading changed files to Google Drive…")
    drive_svc   = get_drive_service(cfg["google_credentials_file"])
    ignore_spec = load_ignore_spec(local_dir)
    drive_upload_all(drive_svc, drive_folder_id, local_dir, ignore_spec)

    print("\n✅  Push complete.\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Sync project between Google Drive ↔ GitHub ↔ Local PC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_project.py pull
  python sync_project.py push "feat: add login page"
        """,
    )
    parser.add_argument(
        "action",
        choices=["pull", "push"],
        help="pull = Drive→GitHub→Local  |  push = Local→GitHub→Drive",
    )
    parser.add_argument(
        "commit_message",
        nargs="?",
        default="",
        help="Commit message (required for push, optional for pull)",
    )
    args = parser.parse_args()

    if args.action == "push" and not args.commit_message:
        parser.error("A commit message is required for 'push'.")

    cfg = load_config()

    if args.action == "pull":
        do_pull(cfg, args.commit_message)
    else:
        do_push(cfg, args.commit_message)


if __name__ == "__main__":
    main()
