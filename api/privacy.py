"""Right-to-erasure. Removes the session, consent records and photos not attached to a report or
dispute. Reports/disputes already filed stay with the regulator until resolved (the bot says so)."""
import db
import storage


def erase(identity_hash: str) -> dict:
    session = db.one("WITH d AS (DELETE FROM wa_sessions WHERE phone_hash = %s RETURNING 1) SELECT count(*) AS n FROM d",
                     (identity_hash,))["n"]
    consent = db.one("WITH d AS (DELETE FROM consent_log WHERE user_identifier_hash = %s RETURNING 1) SELECT count(*) AS n FROM d",
                     (identity_hash,))["n"]
    removed = 0
    for u in db.all_("SELECT id, path FROM uploads WHERE identity_hash = %s AND attached_to_id IS NULL", (identity_hash,)):
        if storage.delete_file(u["path"]):
            db.run("DELETE FROM uploads WHERE id = %s", (u["id"],))
            removed += 1
    return {"session": session, "consent": consent, "photos_removed": removed}
