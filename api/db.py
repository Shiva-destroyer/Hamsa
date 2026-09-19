import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL

def conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)

def one(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchone()

def all_(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchall()

def run(sql, params=()):
    with conn() as c:
        c.execute(sql, params)
