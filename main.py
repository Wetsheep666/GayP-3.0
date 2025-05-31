from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# 設定 LINE Token
line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

@app.route("/", methods=["GET"])
def home():
    return "GayP 3.0 is running!", 200

@app.route("/callback", methods=["POST"])
def callback():
    # 取得 X-Line-Signature 標頭值
    signature = request.headers["X-Line-Signature"]

    # 取得請求體內容
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# 接收訊息事件處理
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    reply = "你剛剛說的是：「" + user_message + "」"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# 主程式
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
