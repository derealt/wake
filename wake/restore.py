"""Putting your things back.

Restoring never destroys anything either: whatever stands where your thing used to be
(usually the agent's replacement) is moved into the vault's `displaced` area first.
"""
import os

from .ident import identity
from .vault import Vault


def _moved_dirs(items):
    """Directories the agent moved: where they are now -> where they were."""
    return [(k["now"], k["path"]) for k in items
            if k["kind"] == "dir" and k.get("now") and "moved" in (k.get("whys") or []) and k["now"] != k["path"]]


def original_path(path, moves):
    """Translate a path recorded inside a moved directory back to where it was before the move."""
    for _ in range(16):
        for now, orig in moves:
            if path.startswith(now + "/"):
                path = orig + path[len(now):]
                break
        else:
            return path
    return path


def restore_one(body, vault, k, moves=(), dry=False):
    path = original_path(k["path"], moves)
    kind = k["kind"]
    actions = []
    if kind == "dir":
        now = k.get("now")
        at_now = identity(now) if now else None
        if at_now and at_now.key == k["key"] and now != path and not os.path.lexists(path):
            if not dry:
                os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
                os.rename(now, path)
            actions.append(("moved back", path))
        elif not os.path.isdir(path):
            if not dry:
                if os.path.lexists(path):
                    vault.move_in(path, "displaced")
                os.makedirs(path, exist_ok=True)
                if k.get("mode") is not None:
                    os.chmod(path, k["mode"])
            actions.append(("recreated", path))
        if not dry:
            body.mark_restored(k["key"])
        return actions

    whys = k.get("whys") or []
    changed = any(w in ("overwritten", "deleted", "replaced") for w in whys)
    cur = identity(path)
    now = k.get("now")
    at_now = identity(now) if now and now != path else None
    if cur is not None and cur.key == k["key"] and not changed:
        if not dry:
            body.mark_restored(k["key"])
        return [("already in place", path)]
    if cur is not None:
        if not dry:
            vault.move_in(path, "displaced")
        actions.append(("moved aside what stood there", path))
    if not dry:
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
    if k.get("store"):
        if not dry:
            if k.get("sk") == "link":
                try:
                    os.link(k["store"], path, follow_symlinks=False)
                except OSError:
                    Vault.copy(k["store"], path)
            else:
                Vault.copy(k["store"], path)
        actions.append(("put back", path))
    elif at_now and at_now.key == k["key"]:
        if not dry:
            os.rename(now, path)
        actions.append(("moved back", path))
        at_now = None
    else:
        actions.append(("nothing kept to restore from", path))
    if at_now and at_now.key == k["key"]:
        if not dry:
            vault.move_in(now, "displaced")
        actions.append(("took away the agent's name for it", now))
    if not dry:
        try:
            if kind == "file" and k.get("mode") is not None:
                os.chmod(path, k["mode"])
        except OSError:
            pass
        body.mark_restored(k["key"])
    return actions


def restore_all(body, vault, only=None, dry=False):
    items = [k for k in body.kept_all() if not k.get("restored")]
    moves = _moved_dirs(items)
    dirs = sorted((k for k in items if k["kind"] == "dir"), key=lambda k: k["path"].count("/"))
    files = [k for k in items if k["kind"] != "dir"]
    actions = []
    for k in dirs + files:
        p = original_path(k["path"], moves)
        if only and not any(p == o or p.startswith(o.rstrip("/") + "/") for o in only):
            continue
        actions += restore_one(body, vault, k, moves, dry)
    return actions
