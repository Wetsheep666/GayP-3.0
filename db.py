# db.py
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

if __name__ == "__main__":
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        print("資料庫連線成功：", cur.fetchone())
        cur.close()
        conn.close()
    except Exception as e:
        print("資料庫連線失敗：", e)
