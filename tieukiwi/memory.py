"""3-tier memory for Tieu Kiwi.

Tier 1 - Team shared KB (RAG): see tieukiwi.rag (index_docs / search).
Tier 2 - Thread/artifact memory: per-review state keyed by channel_id + thread_ts,
         stored in the thread_state table. Where the Layer C feedback loop lives.
Tier 3 - Per-user memory (preferences/role/style), keyed by user_id. TODO (later).
"""

import psycopg

from . import db, rag


# --- Tier 1: team shared KB (thin wrappers over rag) ---

def kb_search(query, k=4):
    return rag.search(query, k)


# --- Tier 2: thread/artifact memory (thread_state table) ---

def get_thread_state(channel_id, thread_ts):
    sql = "SELECT state_json FROM thread_state WHERE channel_id=%s AND thread_ts=%s"
    with db.conn() as c:
        row = c.execute(sql, (channel_id, thread_ts)).fetchone()
    return row[0] if row else {}


def save_thread_state(channel_id, thread_ts, state):
    # Upsert the per-thread state blob (keyed on channel_id + thread_ts).
    sql = """
    INSERT INTO thread_state (channel_id, thread_ts, state_json, updated_at)
    VALUES (%s, %s, %s, now())
    ON CONFLICT (channel_id, thread_ts)
    DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = now()
    """
    with db.conn() as c:
        c.execute(sql, (channel_id, thread_ts, psycopg.types.json.Json(state or {})))


def delete_thread_state(channel_id, thread_ts):
    # Discard the per-thread state blob (e.g. user cancels an in-progress review loop).
    # Returns True iff a row was actually removed — callers should treat False as
    # "nothing to discard" rather than reporting success (guards against a
    # duplicate/racing discard call finding the row already gone).
    sql = "DELETE FROM thread_state WHERE channel_id=%s AND thread_ts=%s"
    with db.conn() as c:
        cur = c.execute(sql, (channel_id, thread_ts))
        return cur.rowcount > 0


# --- Tier 3: per-user memory (TODO, later) ---

def get_user_memory(user_id):
    # TODO: persist per-user preferences/role/style (own table or store).
    raise NotImplementedError("Tier 3 per-user memory not implemented yet.")


def save_user_memory(user_id, prefs):
    # TODO: persist per-user preferences/role/style.
    raise NotImplementedError("Tier 3 per-user memory not implemented yet.")
