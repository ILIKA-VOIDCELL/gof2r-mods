#!/usr/bin/env python3
"""gof2r-mods: the community mod catalog of the Galaxy on Fire 2 remake.

The game's Mod Browser (Online tab) reads catalog.json from this repository. A mod is submitted as one small file,
submissions/<mod id>.json, naming its GitHub owner and either a direct download link of the mod's .zip ("download": an
update is a pull request changing the link) or the author's own GitHub repository ("github": every new release there with
the mod's zip is picked up by the hourly publish, no pull request needed). On merge, the publish
workflow downloads the zip, checks it, and attaches it to a release of this repository together with a preview and a
thumbnail; the game downloads only from those releases. The release assets' download counts are the catalog's numbers:
<id>.zip = downloads, <id>-preview.jpg = views (the game loads that image when a player opens the mod's page).

  python tools/catalog.py check --author LOGIN --base REF FILE...   a pull request: who may change what, every changed
                                                                      submission downloaded and checked (no writes)
  python tools/catalog.py publish                                    every new or changed submission (or a followed
                                                                      repository's new release) released, catalog.json
                                                                      written (needs GITHUB_TOKEN with contents: write)
  python tools/catalog.py stats                                      views / downloads summed from the releases
  python tools/catalog.py inspect FILE.zip                           check a mod zip locally, as the workflows do
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("GITHUB_REPOSITORY", "JoppieToppie/gof2r-mods")
# Who may change any submission and anything outside submissions/ (the repository's own people).
MAINTAINERS = {m.strip().lower() for m in os.environ.get("CATALOG_MAINTAINERS", "JoppieToppie").split(",") if m.strip()}
CATALOG = os.path.join(ROOT, "catalog.json")
SUBMISSIONS = os.path.join(ROOT, "submissions")
FORMAT = 1

MAX_ZIP = 1024 ** 3              # a release asset may be 2 GiB; mods past 1 GiB are refused
MAX_UNPACKED = 4 * 1024 ** 3     # a zip bomb's guard
MAX_FILES = 20000
MAX_IMAGE = 32 * 1024 ** 2
ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")   # ModManifest.ValidId
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
# Files a mod never needs: anything a computer or phone runs by itself. Mod scripts are Lua (.lua), run sandboxed by the
# game after the player allows them; the catalog marks those mods.
BLOCKED = {".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
           ".wsf", ".wsh", ".msi", ".msp", ".scr", ".jar", ".apk", ".aab", ".ipa", ".sh", ".bash", ".command", ".lnk",
           ".reg", ".hta", ".cpl", ".py", ".pyw", ".app", ".deb", ".rpm", ".appimage", ".pif", ".gadget", ".msc"}
SUBMISSION_KEYS = {"download", "github", "asset", "owner", "notes"}
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
ASSET_RE = re.compile(r"^[^/\\]{1,200}\.zip$", re.IGNORECASE)


class ModError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------- mod.json

def loose_json(text):
    """JSON as the game reads mod.json (Newtonsoft): // and /* */ comments and trailing commas allowed."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def text_of(value):
    """A text field as mod.json has it: a string, or { "en": ..., "de": ... } (kept as it is for the game)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        d = {str(k): str(v) for k, v in value.items() if isinstance(v, (str, int, float)) and str(v).strip()}
        return d or None
    raise ModError("a text must be a string or { \"en\": \"...\" }")


def english(value, fallback=""):
    if isinstance(value, dict):
        return value.get("en") or value.get("") or next(iter(value.values()), fallback)
    return value or fallback


def version_at_least(a, b):
    """ModManifest.VersionAtLeast: number by number ("1.10" > "1.9"), other parts as text (case-insensitive)."""
    x = re.split(r"[.\- ]", a or "0")
    y = re.split(r"[.\- ]", b or "0")
    for i in range(max(len(x), len(y))):
        p = x[i] if i < len(x) else "0"
        q = y[i] if i < len(y) else "0"
        if re.fullmatch(r"[+-]?\d+", p) and re.fullmatch(r"[+-]?\d+", q):
            c = (int(p) > int(q)) - (int(p) < int(q))
        else:
            pl, ql = p.lower(), q.lower()
            c = (pl > ql) - (pl < ql)
        if c:
            return c > 0
    return True


# ---------------------------------------------------------------------------------------------------- the zip

def normalise(name):
    return name.replace("\\", "/")


def inspect_zip(path):
    """A mod zip checked as the game reads it (ZipSource: mod.json at the root, or inside one top folder). Returns
    { manifest fields, scripts, files, unpacked, preview (bytes or None) }; raises ModError with what is wrong."""
    size = os.path.getsize(path)
    if size > MAX_ZIP:
        raise ModError(f"the zip is {size / 1024 ** 2:.0f} MB; at most {MAX_ZIP // 1024 ** 2} MB")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ModError("not a zip archive")
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise ModError(f"{len(infos)} files; at most {MAX_FILES}")
        unpacked = 0
        for i in infos:
            n = normalise(i.filename)
            parts = n.split("/")
            if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts:
                raise ModError(f"unsafe path in the zip: {i.filename}")
            ext = os.path.splitext(n)[1].lower()
            if ext in BLOCKED:
                raise ModError(f"{i.filename}: {ext} files aren't allowed in a mod")
            if i.flag_bits & 0x1:
                raise ModError(f"{i.filename} is encrypted")
            unpacked += i.file_size
        if unpacked > MAX_UNPACKED:
            raise ModError(f"unpacks to {unpacked / 1024 ** 3:.1f} GB; at most {MAX_UNPACKED // 1024 ** 3} GB")
        names = {normalise(i.filename).lower(): i for i in infos}
        prefix = ""
        if "mod.json" not in names:
            inner = [n for n in names if n.endswith("/mod.json") and n.count("/") == 1]
            if not inner:
                raise ModError("no mod.json (at the zip's root or in one top folder)")
            prefix = inner[0][: inner[0].index("/") + 1]
        files = [n for n in names if n.startswith(prefix)]
        bad = z.testzip()
        if bad:
            raise ModError(f"{bad} is damaged in the zip")
        try:
            raw = z.read(names[prefix + "mod.json"]).decode("utf-8-sig")
            m = loose_json(raw)
        except (UnicodeDecodeError, ValueError) as e:
            raise ModError(f"mod.json can't be read: {e}")
        if not isinstance(m, dict):
            raise ModError("mod.json must be an object { ... }")
        mod_id = m.get("id")
        if not isinstance(mod_id, str) or not ID_RE.match(mod_id):
            raise ModError(f"mod.json: the id {mod_id!r} may only use a-z, 0-9, _ and - (at most 40)")
        version = str(m.get("version", "1.0")).strip() or "1.0"
        deps = m.get("dependencies") or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ModError("mod.json: \"dependencies\" must be a list of mod ids")
        preview_name = str(m.get("preview") or "preview.png")
        preview = None
        pi = names.get(prefix + normalise(preview_name).lower())
        if pi is not None and pi.file_size <= MAX_IMAGE:
            preview = z.read(pi)
        website = m.get("website")
        if website is not None and (not isinstance(website, str) or not re.match(r"^https?://", website)):
            website = None
        return {
            "id": mod_id,
            "name": text_of(m.get("name")) or mod_id,
            "version": version[:40],
            "author": (str(m.get("author")).strip()[:80] if m.get("author") else None),
            "description": text_of(m.get("description")),
            "credits": text_of(m.get("credits")),
            "website": website,
            "dependencies": [d.strip() for d in deps if d.strip()],
            "scripts": any(n.endswith(".lua") for n in files),
            "files": len(files),
            "unpacked": unpacked,
            "preview": preview,
        }


# ---------------------------------------------------------------------------------------------------- images

def images(preview_bytes, title):
    """The preview (1280x720) and the list's thumbnail (320x180) as JPEG, cropped to 16:9 the way the game shows them
    (scale-and-crop); a mod without a usable preview gets its name on a dark card."""
    from PIL import Image, ImageDraw, ImageFont
    Image.MAX_IMAGE_PIXELS = 40_000_000
    img = None
    if preview_bytes:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                img = Image.open(io.BytesIO(preview_bytes))
                img.load()
            img = img.convert("RGB")
        except Exception:
            img = None
    if img is None:
        img = Image.new("RGB", (1280, 720), (10, 22, 32))
        d = ImageDraw.Draw(img)
        for y in range(720):
            d.line([(0, y), (1280, y)], fill=(10, 22 + y // 40, 32 + y // 18))
        try:
            font = ImageFont.load_default(size=72)
        except TypeError:
            font = ImageFont.load_default()
        text = title[:40]
        box = d.textbbox((0, 0), text, font=font)
        d.text(((1280 - (box[2] - box[0])) / 2, (720 - (box[3] - box[1])) / 2), text, font=font, fill=(216, 236, 247))

    def crop(w, h):
        sw, sh = img.size
        scale = max(w / sw, h / sh)
        r = img.resize((max(w, round(sw * scale)), max(h, round(sh * scale))), Image.LANCZOS)
        left, top = (r.width - w) // 2, (r.height - h) // 2
        out = io.BytesIO()
        r.crop((left, top, left + w, top + h)).save(out, "JPEG", quality=88, optimize=True, progressive=False)
        return out.getvalue()

    return crop(1280, 720), crop(320, 180)


# ---------------------------------------------------------------------------------------------------- submissions

def read_submission(path):
    """submissions/<id>.json: { "download": "https://... .zip" or "github": "<user>/<repo>" (+ "asset": "<name>.zip"),
    "owner": "<GitHub login>", "notes": "..." }."""
    mod_id = os.path.splitext(os.path.basename(path))[0]
    if not ID_RE.match(mod_id):
        raise ModError(f"{os.path.basename(path)}: the file name must be the mod's id (a-z, 0-9, _ and -)")
    try:
        with open(path, encoding="utf-8-sig") as f:
            s = json.load(f)
    except ValueError as e:
        raise ModError(f"{os.path.basename(path)} isn't valid JSON: {e}")
    return parse_submission(mod_id, s)


def parse_submission(mod_id, s):
    if not isinstance(s, dict):
        raise ModError("a submission is an object { \"download\": ..., \"owner\": ... }")
    extra = set(s) - SUBMISSION_KEYS
    if extra:
        raise ModError("unknown fields: " + ", ".join(sorted(extra)))
    url, repo, asset, owner = s.get("download"), s.get("github"), s.get("asset"), s.get("owner")
    if not isinstance(owner, str) or not LOGIN_RE.match(owner):
        raise ModError("\"owner\" must be your GitHub user name")
    if (url is None) == (repo is None):
        raise ModError("give either \"download\" (a link to the zip) or \"github\" (\"user/repository\", its releases)")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise ModError("\"download\" must be an https:// link to the mod's .zip")
    if repo is not None and (not isinstance(repo, str) or not REPO_RE.match(repo)):
        raise ModError("\"github\" must be \"user/repository\"")
    if asset is not None and (repo is None or not isinstance(asset, str) or not ASSET_RE.match(asset)):
        raise ModError("\"asset\" names the .zip in a \"github\" repository's releases")
    return {"id": mod_id, "download": url.strip() if url else None, "github": repo, "asset": asset, "owner": owner}


def source_url(sub):
    """Where the submission's zip is now: its "download" link, or the zip of the newest release of its "github"
    repository (the asset named "asset", else <id>.zip, else the release's only .zip)."""
    if sub["download"]:
        return sub["download"]
    try:
        rel, _ = api("GET", f"repos/{sub['github']}/releases/latest")
    except urllib.error.HTTPError as e:
        raise ModError(f"{sub['github']} has no release yet (or isn't public): {e.code}")
    zips = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")]
    want = (sub["asset"] or f"{sub['id']}.zip").lower()
    pick = [a for a in zips if a["name"].lower() == want] or (zips if len(zips) == 1 and not sub["asset"] else [])
    if not pick:
        raise ModError(f"the newest release of {sub['github']} ({rel.get('tag_name')}) has no {sub['asset'] or sub['id'] + '.zip'}"
                       + ("" if sub["asset"] else " (nor a single .zip)"))
    return pick[0]["browser_download_url"]


def repo_owner_ok(sub, author):
    """A followed repository is the owner's own (a maintainer may set up another, an organisation's)."""
    if not sub["github"] or (author or "").lower() in MAINTAINERS:
        return None
    if sub["github"].split("/")[0].lower() != sub["owner"].lower():
        return f"\"github\" must be a repository of yours ({sub['owner']}/...); ask a maintainer for an organisation's"
    return None


def may_change(author, old_owner, new_owner):
    """Who may add or change a submission: its owner (a new one: the person submitting it), or a maintainer.
    Returns None when allowed, else why not."""
    a = (author or "").lower()
    if a in MAINTAINERS:
        return None
    if old_owner is not None and old_owner.lower() != a:
        return f"this mod belongs to {old_owner}; only they (or a maintainer) can change it"
    if new_owner is not None and new_owner.lower() != a:
        return f"\"owner\" must be your own GitHub name ({author})"
    return None


def check_update(entry, info, sha):
    """A new zip for a mod already in the catalog: a higher version, or the same files. Returns why not, or None."""
    if entry is None:
        return None
    if info["version"] == entry["version"]:
        return None if sha == entry["sha256"] else f"version {info['version']} is already published with other files: raise \"version\" in mod.json"
    if version_at_least(entry["version"], info["version"]):
        return f"version {info['version']} is older than the published {entry['version']}"
    return None


def download(url, dest):
    """The submission's zip, at most MAX_ZIP; returns its SHA-256."""
    req = urllib.request.Request(url, headers={"User-Agent": "gof2r-mods-catalog"})
    h = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            length = r.headers.get("Content-Length")
            if length and int(length) > MAX_ZIP:
                raise ModError(f"the download is {int(length) / 1024 ** 2:.0f} MB; at most {MAX_ZIP // 1024 ** 2} MB")
            first = True
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                if first:
                    first = False
                    if not chunk.startswith(b"PK"):
                        raise ModError("the link doesn't give a .zip file (a web page?): it must be a direct download link")
                total += len(chunk)
                if total > MAX_ZIP:
                    raise ModError(f"the download is over {MAX_ZIP // 1024 ** 2} MB")
                h.update(chunk)
                f.write(chunk)
    except urllib.error.URLError as e:
        raise ModError(f"the download failed: {e}")
    if total == 0:
        raise ModError("the download is empty")
    return h.hexdigest()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------- the catalog

def load_catalog():
    if not os.path.exists(CATALOG):
        return {"format": FORMAT, "updated": None, "repository": REPO, "mods": []}
    with open(CATALOG, encoding="utf-8") as f:
        return json.load(f)


def save_catalog(cat):
    cat["format"] = FORMAT
    cat["repository"] = REPO
    cat["mods"].sort(key=lambda m: m["id"])
    cat["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(CATALOG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
        f.write("\n")


def find(cat, mod_id):
    return next((m for m in cat["mods"] if m["id"] == mod_id), None)


def tag_of(mod_id, version):
    return f"{mod_id}-v{re.sub(r'[^0-9A-Za-z._-]', '_', version)}"


def asset_url(tag, name):
    return f"https://github.com/{REPO}/releases/download/{tag}/{name}"


def entry_of(info, sub, url, sha, size, tag, old):
    today = datetime.date.today().isoformat()
    return {
        "id": info["id"],
        "name": info["name"],
        "version": info["version"],
        "author": info["author"] or sub["owner"],
        "owner": sub["owner"],
        "description": info["description"],
        "credits": info["credits"],
        "website": info["website"],
        "dependencies": info["dependencies"],
        "scripts": info["scripts"],
        "files": info["files"],
        "size": size,
        "sha256": sha,
        "tag": tag,
        "download": asset_url(tag, f"{info['id']}.zip"),
        "preview": asset_url(tag, f"{info['id']}-preview.jpg"),
        "thumb": asset_url(tag, f"{info['id']}-thumb.jpg"),
        "published": (old or {}).get("published") or today,
        "updated": today,
        "views": (old or {}).get("views", 0),
        "downloads": (old or {}).get("downloads", 0),
        "source": url,
        "follows": sub["github"],
    }


# ---------------------------------------------------------------------------------------------------- GitHub

def api(method, url, data=None, content_type="application/json"):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and method != "GET":
        raise SystemExit("GITHUB_TOKEN is needed")
    if not url.startswith("https://"):
        url = "https://api.github.com/" + url.lstrip("/")
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "gof2r-mods-catalog", "Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        text = r.read()
        link = r.headers.get("Link") or ""
    nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return (json.loads(text) if text else None), (nxt.group(1) if nxt else None)


def all_releases():
    out, url = [], f"repos/{REPO}/releases?per_page=100"
    while url:
        page, url = api("GET", url)
        out.extend(page)
    return out


def release(tag, title, notes, assets):
    """A release of this repository named 'tag' with 'assets' ([(name, bytes or path, type)]): made, or the missing
    assets added to the one that exists (a publish that stopped half way)."""
    try:
        rel, _ = api("GET", f"repos/{REPO}/releases/tags/{tag}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rel, _ = api("POST", f"repos/{REPO}/releases", {"tag_name": tag, "name": title, "body": notes,
                                                         "make_latest": "false"})
    have = {a["name"] for a in rel.get("assets", [])}
    upload = rel["upload_url"].split("{")[0]
    for name, content, ctype in assets:
        if name in have:
            continue
        if isinstance(content, str):
            with open(content, "rb") as f:
                content = f.read()
        api("POST", f"{upload}?name={name}", content, ctype)


# ---------------------------------------------------------------------------------------------------- commands

def summary(lines):
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def git_show(ref, path):
    try:
        return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, check=True,
                              text=True, encoding="utf-8").stdout
    except subprocess.CalledProcessError:
        return None


def cmd_check(args):
    """A pull request: only submissions/<id>.json files (unless a maintainer), each by its owner, each zip checked."""
    cat = load_catalog()
    lines, failed = ["## Mod submission check", ""], False
    files = list(args.files)
    if args.files_from:
        with open(args.files_from, encoding="utf-8") as f:
            files += [l.strip() for l in f if l.strip()]
    for path in files:
        rel = normalise(path)
        if not re.fullmatch(r"submissions/[^/]+\.json", rel):
            if (args.author or "").lower() not in MAINTAINERS:
                lines.append(f"- ❌ `{rel}`: a pull request may only add or change files in `submissions/`")
                failed = True
            continue
        mod_id = os.path.splitext(os.path.basename(rel))[0]
        old_text = git_show(args.base, rel) if args.base else None
        old_owner = None
        if old_text:
            try:
                old_owner = json.loads(old_text).get("owner")
            except ValueError:
                pass
        if old_owner is None and find(cat, mod_id):
            old_owner = find(cat, mod_id).get("owner")
        full = os.path.join(ROOT, rel)
        try:
            if not os.path.exists(full):   # removed
                why = may_change(args.author, old_owner, None)
                if why:
                    raise ModError(why)
                lines.append(f"- ✅ `{mod_id}`: removed from the catalog on merge (its releases stay)")
                continue
            sub = read_submission(full)
            why = may_change(args.author, old_owner, sub["owner"]) or repo_owner_ok(sub, args.author)
            if why:
                raise ModError(why)
            url = source_url(sub)
            with tempfile.TemporaryDirectory() as tmp:
                zp = os.path.join(tmp, "mod.zip")
                sha = download(url, zp)
                size = os.path.getsize(zp)
                info = inspect_zip(zp)
            if info["id"] != mod_id:
                raise ModError(f"the zip's mod.json says id \"{info['id']}\": name the file submissions/{info['id']}.json")
            why = check_update(find(cat, mod_id), info, sha)
            if why:
                raise ModError(why)
            missing = [d.split(">=")[0].strip() for d in info["dependencies"] if not find(cat, d.split(">=")[0].strip())]
            note = f" · needs {', '.join(missing)}, not in the catalog" if missing else ""
            follows = f" · follows {sub['github']}'s releases" if sub["github"] else ""
            lines.append(f"- ✅ `{mod_id}` {info['version']}{follows}: {english(info['name'])} by {info['author'] or sub['owner']}, "
                         f"{info['files']} files, {size / 1024 ** 2:.1f} MB, "
                         f"{'Lua scripts' if info['scripts'] else 'no scripts'}"
                         f"{'' if info['preview'] else ', no preview image (a card with its name is made)'}{note}")
        except ModError as e:
            lines.append(f"- ❌ `{mod_id}`: {e}")
            failed = True
    if len(lines) == 2:
        lines.append("Nothing to check.")
    summary(lines)
    return 1 if failed else 0


def cmd_publish(args):
    """Every submission whose download link changed (or that is new) is released; removed ones leave the catalog."""
    cat = load_catalog()
    lines, failed, changed = ["## Publish", ""], False, False
    subs = {}
    for name in sorted(os.listdir(SUBMISSIONS)) if os.path.isdir(SUBMISSIONS) else []:
        if not name.endswith(".json"):
            continue
        try:
            sub = read_submission(os.path.join(SUBMISSIONS, name))
            subs[sub["id"]] = sub
        except ModError as e:
            lines.append(f"- ❌ `{name}`: {e}")
            failed = True
    for entry in list(cat["mods"]):
        if entry["id"] not in subs:
            cat["mods"].remove(entry)
            changed = True
            lines.append(f"- `{entry['id']}` removed")
    rejected = cat.setdefault("rejected", {})
    for k in list(rejected):
        if k not in subs:
            del rejected[k]
            changed = True
    for mod_id, sub in subs.items():
        old = find(cat, mod_id)
        url = None
        try:
            url = source_url(sub)
            if old and old.get("source") == url and old.get("owner") == sub["owner"] and not args.force:
                continue
            if rejected.get(mod_id, {}).get("source") == url and not args.force:
                continue   # refused before: told once, not every hour
            with tempfile.TemporaryDirectory() as tmp:
                zp = os.path.join(tmp, "mod.zip")
                sha = download(url, zp)
                info = inspect_zip(zp)
                if info["id"] != mod_id:
                    raise ModError(f"the zip's mod.json says id \"{info['id']}\"")
                why = check_update(old, info, sha)
                if why:
                    raise ModError(why)
                if old and old["sha256"] == sha:   # the same files from a new link
                    old["source"], old["owner"] = url, sub["owner"]
                    changed = True
                    lines.append(f"- `{mod_id}`: same files, link updated")
                    continue
                tag = tag_of(mod_id, info["version"])
                preview, thumb = images(info["preview"], english(info["name"], mod_id))
                title = f"{english(info['name'], mod_id)} {info['version']}"
                notes = (f"**{english(info['name'], mod_id)}** {info['version']} by {info['author'] or sub['owner']}\n\n"
                         f"{english(info['description'])}\n\nInstall it from the game: Mods > Mod Browser > Online.")
                size = os.path.getsize(zp)
                if not args.dry_run:
                    release(tag, title, notes, [(f"{mod_id}.zip", zp, "application/zip"),
                                                (f"{mod_id}-preview.jpg", preview, "image/jpeg"),
                                                (f"{mod_id}-thumb.jpg", thumb, "image/jpeg")])
                entry = entry_of(info, sub, url, sha, size, tag, old)
                if old:
                    cat["mods"].remove(old)
                cat["mods"].append(entry)
                rejected.pop(mod_id, None)
                changed = True
                lines.append(f"- ✅ `{mod_id}` {info['version']} published")
        except (ModError, urllib.error.HTTPError, urllib.error.URLError) as e:
            lines.append(f"- ❌ `{mod_id}`: {e}")
            failed = True
            if url and sub["github"]:   # a followed repository's release: kept, so the hourly run skips it
                rejected[mod_id] = {"source": url, "why": str(e)}
                changed = True
    if changed:
        save_catalog(cat)
    elif len(lines) == 2:
        lines.append("Nothing new.")
    summary(lines)
    return 1 if failed else 0


def cmd_stats(args):
    """Each mod's views (its -preview.jpg fetched) and downloads (its .zip fetched), over all its versions."""
    cat = load_catalog()
    views, downloads = {}, {}
    for rel in all_releases():
        for a in rel.get("assets", []):
            name, count = a["name"], a.get("download_count", 0)
            if name.endswith("-preview.jpg"):
                k = name[: -len("-preview.jpg")]
                views[k] = views.get(k, 0) + count
            elif name.endswith(".zip"):
                k = name[:-4]
                downloads[k] = downloads.get(k, 0) + count
    changed = False
    for m in cat["mods"]:
        v, d = views.get(m["id"], 0), downloads.get(m["id"], 0)
        if m.get("views") != v or m.get("downloads") != d:
            m["views"], m["downloads"] = v, d
            changed = True
    if changed:
        save_catalog(cat)
    print("stats:", "updated" if changed else "unchanged", f"({len(cat['mods'])} mods)")
    return 0


def cmd_inspect(args):
    try:
        info = inspect_zip(args.zip)
    except ModError as e:
        print("not accepted:", e)
        return 1
    info = dict(info, preview=f"{len(info['preview'])} bytes" if info["preview"] else None)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--author", required=True)
    c.add_argument("--base")
    c.add_argument("--files-from", help="a file listing the changed files, one per line (git diff --name-only)")
    c.add_argument("files", nargs="*")
    c.set_defaults(fn=cmd_check)
    pb = sub.add_parser("publish")
    pb.add_argument("--force", action="store_true", help="download every submission again")
    pb.add_argument("--dry-run", action="store_true", help="no releases made (catalog.json still written)")
    pb.set_defaults(fn=cmd_publish)
    s = sub.add_parser("stats")
    s.set_defaults(fn=cmd_stats)
    i = sub.add_parser("inspect")
    i.add_argument("zip")
    i.set_defaults(fn=cmd_inspect)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
