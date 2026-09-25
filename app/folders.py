"""Folders for sources, so a newsroom with a couple hundred sources can find its way around.
Folders can hold folders. A source in no folder is 'Unsorted'."""
from .db import now

UNSORTED = 0


def all_folders(db):
    return db.q("SELECT * FROM source_folders ORDER BY name COLLATE NOCASE")


def children_map(folders):
    kids = {}
    for f in folders:
        kids.setdefault(f["parent_id"], []).append(f)
    return kids


def descendants(db, fid, folders=None):
    """The folder and every folder inside it, at any depth."""
    kids = children_map(folders or all_folders(db))
    out, stack = [], [fid]
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.append(x)
        stack += [k["id"] for k in kids.get(x, [])]
    return out


def ancestors(db, fid, folders=None):
    """[top, …, the folder itself]."""
    by_id = {f["id"]: f for f in (folders or all_folders(db))}
    chain, seen = [], set()
    while fid in by_id and fid not in seen:
        seen.add(fid)
        chain.insert(0, by_id[fid])
        fid = by_id[fid]["parent_id"]
    return chain


def flat(db, folders=None):
    """Every folder in tree order with its depth and a 'Government / County' style label, for dropdowns."""
    folders = folders if folders is not None else all_folders(db)
    kids = children_map(folders)
    out = []

    def walk(parent, depth, prefix):
        for f in kids.get(parent, []):
            label = f"{prefix}{f['name']}"
            out.append({**f, "depth": depth, "label": label})
            walk(f["id"], depth + 1, label + " / ")
    walk(None, 0, "")
    return out


def source_ids(db, fid):
    """Every source in the folder, including its subfolders. UNSORTED means sources in no folder."""
    if fid == UNSORTED:
        return [r["id"] for r in db.q("SELECT id FROM sources WHERE folder_id IS NULL AND builtin=0")]
    ids = descendants(db, fid)
    return [r["id"] for r in db.q(f"SELECT id FROM sources WHERE folder_id IN ({','.join('?' * len(ids))})", ids)]


def create(db, name, parent_id=None):
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("Give the folder a name.")
    if parent_id and not db.val("SELECT 1 FROM source_folders WHERE id=?", (parent_id,)):
        parent_id = None
    return db.insert("source_folders", name=name, parent_id=parent_id or None, created_at=now())


def move_folder(db, fid, parent_id):
    """Put a folder inside another (or at the top). Refuses to put a folder inside itself."""
    if parent_id and parent_id in descendants(db, fid):
        raise ValueError("A folder can't go inside itself.")
    db.run("UPDATE source_folders SET parent_id=? WHERE id=?", (parent_id or None, fid))


def delete(db, fid):
    """Remove a folder. What was inside moves up one level, so nothing is lost."""
    f = db.one("SELECT * FROM source_folders WHERE id=?", (fid,))
    if not f:
        return
    db.run("UPDATE sources SET folder_id=? WHERE folder_id=?", (f["parent_id"], fid))
    db.run("UPDATE source_folders SET parent_id=? WHERE parent_id=?", (f["parent_id"], fid))
    db.run("DELETE FROM source_folders WHERE id=?", (fid,))

