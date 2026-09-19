"""User media storage. Photos are validated, EXIF/GPS-stripped BEFORE anything touches disk, and stored
only when attached to a report/dispute (call `attach` once the report/dispute row exists; unattached files are purged
after 30 days by jobs.py).

    save_media(identity_hash, kind, data, mime) -> reference   # reference = uploads.id (uuid string)

Files live in UPLOAD_DIR/<kind>/<uuid>.<ext>; the name is server-generated, no user input reaches the filesystem path.
PDFs are stored byte-for-byte (no PDF metadata stripping is done -- documented limitation)."""
import io
import os
import uuid
from pathlib import Path

from PIL import Image, ImageOps

import db

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
KINDS = {"report", "dispute"}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
_PIL_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class MediaRejected(ValueError):
    pass


def upload_dir() -> Path:
    return Path(os.environ.get("UPLOAD_DIR", "api/uploads")).resolve()


def _magic_ok(mime: str, d: bytes) -> bool:
    if mime == "image/jpeg": return d[:3] == b"\xff\xd8\xff"
    if mime == "image/png":  return d[:8] == b"\x89PNG\r\n\x1a\n"
    if mime == "image/webp": return d[:4] == b"RIFF" and d[8:12] == b"WEBP"
    if mime == "application/pdf": return d[:5] == b"%PDF-"
    return False


def strip_metadata(data: bytes, mime: str) -> bytes:
    """Re-encode from raw pixels so no EXIF/GPS/XMP/ICC/comment block can survive. EXIF orientation is applied first."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise MediaRejected("image too large")
        img.load()
        img = ImageOps.exif_transpose(img)
    except MediaRejected:
        raise
    except Exception:                       # corrupt file, decompression bomb, ...
        raise MediaRejected("unreadable image")
    has_alpha = "A" in img.getbands() or "transparency" in img.info
    target = "RGBA" if (has_alpha and mime != "image/jpeg") else "RGB"
    img = img.convert(target)
    clean = Image.frombytes(target, img.size, img.tobytes())      # fresh image: carries no metadata at all
    out = io.BytesIO()
    clean.save(out, format=_PIL_FORMAT[mime])
    return out.getvalue()


def save_media(identity_hash: str, kind: str, data: bytes, mime: str) -> str:
    if kind not in KINDS:
        raise MediaRejected("invalid kind")
    if mime not in _EXT:
        raise MediaRejected("unsupported type")
    if not data or len(data) > MAX_BYTES:
        raise MediaRejected("file empty or larger than 10 MB")
    if not _magic_ok(mime, data):
        raise MediaRejected("content does not match declared type")
    body = data if mime == "application/pdf" else strip_metadata(data, mime)   # strip BEFORE writing
    uid = uuid.uuid4()
    rel = f"{kind}/{uid}.{_EXT[mime]}"
    base = upload_dir()
    dest = (base / rel).resolve()
    if base not in dest.parents:                                  # defence in depth; rel is server-built
        raise MediaRejected("invalid path")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    try:
        db.run("INSERT INTO uploads (id, identity_hash, kind, path, mime, created_at) VALUES (%s, %s, %s, %s, %s, now())",
               (uid, identity_hash, kind, rel, mime))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return str(uid)


def attach(reference: str, to_type: str, to_id) -> bool:
    """Mark an upload as attached to a report/dispute row so the 30-day unattached purge skips it."""
    row = db.one("UPDATE uploads SET attached_to_type = %s, attached_to_id = %s WHERE id = %s RETURNING id",
                 (to_type, to_id, reference))
    return bool(row)


def delete_file(rel_path: str) -> bool:
    """Remove a stored file if (and only if) it resolves inside UPLOAD_DIR. True if the file is gone afterwards."""
    if not rel_path:
        return True
    base = upload_dir()
    p = (base / rel_path).resolve()
    if base not in p.parents:
        return False
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False
