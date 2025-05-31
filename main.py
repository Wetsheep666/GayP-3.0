import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import psycopg2
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# 儲存使用者資訊（如果第一次見面）
def save_user(line_id, name):
    conn = get_connection()
    cur = conn.cursor()
    # 檢查使用者是否存在
    cur.execute("SELECT id FROM users WHERE line_id = %s;", (line_id,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (line_id, name) VALUES (%s, %s);", (line_id, name))
        conn.commit()
    cur.close()
    conn.close()

# 儲存預約資料
def save_reservation(line_id, origin, destination, departure_time, shared, payment_method):
    conn = get_connection()
    cur = conn.cursor()
    # 先取得 user_id
    cur.execute("SELECT id FROM users WHERE line_id = %s;", (line_id,))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        return False
    user_id = user[0]
    cur.execute(
        "INSERT INTO reservations (user_id, origin, destination, departure_time, shared, payment_method) VALUES (%s,%s,%s,%s,%s,%s);",
        (user_id, origin, destination, departure_time, shared, payment_method)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    # 簡單示範：假設用戶輸入格式是 "預約 起點;終點;YYYY-MM-DD HH:MM;共乘(True/False);付款方式"
    # 例如：預約 台北車站;桃園機場;2025-06-01 14:00;True;現金
    if text.startswith("預約"):
        try:
            data = text[len("預約"):].strip()
            origin, destination, departure_time, shared_str, payment_method = [x.strip() for x in data.split(";")]
            shared = shared_str.lower() == "true"

            # 先存使用者名字（用 user_id 代替暫時）
            save_user(user_id, user_id)

            # 儲存預約
            success = save_reservation(user_id, origin, destination, departure_time, shared, payment_method)
            if success:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="預約成功！"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到使用者，請重新註冊。"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="格式錯誤，請依格式輸入。"))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入預約指令，格式：\n預約 起點;終點;YYYY-MM-DD HH:MM;共乘(True/False);付款方式"))

if __name__ == "__main__":
    app.run(port=8000)
