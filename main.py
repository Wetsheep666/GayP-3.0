from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from supabase import create_client
import os
import datetime
from dotenv import load_dotenv

load_dotenv()

# LINE
line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

# Supabase
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase = create_client(supabase_url, supabase_key)

# Flask
app = Flask(__name__)

@app.route("/")
def hello():
    return "Linebot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# handler 註冊區
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text == "我要預約":
        supabase.table("user_state").upsert({
            "user_id": user_id,
            "state": "asking_start"
        }).execute()
        send_reply(event.reply_token, "請輸入出發地（例如：台大）")
        return

    state_result = supabase.table("user_state").select("*").eq("user_id", user_id).execute()
    user_state = state_result.data[0] if state_result.data else None

    if user_state:
        state = user_state["state"]

        if state == "asking_start":
            supabase.table("user_state").update({
                "start": text,
                "state": "asking_end"
            }).eq("user_id", user_id).execute()
            send_reply(event.reply_token, "請輸入目的地（例如：信義安和）")

        elif state == "asking_end":
            supabase.table("user_state").update({
                "end": text,
                "state": "asking_time"
            }).eq("user_id", user_id).execute()
            send_reply(event.reply_token, "請輸入時間（格式：2025-06-05 18:00）")

        elif state == "asking_time":
            try:
                time_obj = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
                start = user_state["start"]
                end = user_state["end"]

                # 簡單配對
                result = supabase.table("carpool").select("*").execute()
                match = None
                for r in result.data:
                    delta = abs(datetime.datetime.fromisoformat(r["time"]) - time_obj)
                    if delta.total_seconds() <= 600 and r["start"] == start:
                        match = r
                        break

                group_id = match["group_id"] if match else f"group-{user_id[:6]}-{int(datetime.datetime.now().timestamp())}"

                supabase.table("carpool").insert({
                    "user_id": user_id,
                    "start": start,
                    "end": end,
                    "time": time_obj.isoformat(),
                    "group_id": group_id
                }).execute()

                supabase.table("user_state").delete().eq("user_id", user_id).execute()

                reply = f"預約成功！出發：{start} → {end}，時間：{time_obj.strftime('%m/%d %H:%M')}\n群組：{group_id}"
                send_reply(event.reply_token, reply)

            except Exception:
                send_reply(event.reply_token, "時間格式錯誤，請重新輸入（格式：2025-06-05 18:00）")

    elif text == "查詢預約":
        result = supabase.table("carpool").select("*").eq("user_id", user_id).order("time").execute()
        if result.data:
            messages = []
            for r in result.data:
                t = datetime.datetime.fromisoformat(r["time"]).strftime("%m/%d %H:%M")
                messages.append(f"{r['start']}→{r['end']} / {t} / 群組：{r['group_id']}")
            send_reply(event.reply_token, "你的預約：\n" + "\n".join(messages))
        else:
            send_reply(event.reply_token, "你目前沒有任何預約。")

    elif text == "取消預約":
        supabase.table("carpool").delete().eq("user_id", user_id).execute()
        supabase.table("user_state").delete().eq("user_id", user_id).execute()
        send_reply(event.reply_token, "已取消你的所有預約與暫存資料。")

    else:
        send_reply(event.reply_token, "請輸入「我要預約」、「查詢預約」或「取消預約」")

def send_reply(token, msg):
    line_bot_api.reply_message(token, TextSendMessage(text=msg))

if __name__ == "__main__":
    app.run()
