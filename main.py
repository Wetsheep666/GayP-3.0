from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

user_states = {}

@app.route("/", methods=['GET'])
def home():
    return "LINE Bot is running."

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "from"}
        reply = "請輸入出發地點："

    elif state.get("step") == "from":
        state["from"] = text
        state["step"] = "to"
        user_states[user_id] = state
        reply = "請輸入目的地點："

    elif state.get("step") == "to":
        state["to"] = text
        state["step"] = "time"
        user_states[user_id] = state
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："

    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=None)
            state["time"] = dt.isoformat()
            user_states[user_id] = state

            # 刪除舊資料
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 插入資料
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from"],
                "destination": state["to"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None
            }).execute()

            # 查找潛在共乘對象
            result = supabase.table("rides").select("*") \
                .eq("origin", state["from"]).eq("destination", state["to"]) \
                .is_("matched_user", "null").neq("user_id", user_id).execute()

            match = None
            for r in result.data:
                try:
                    r_time = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    diff = abs((dt - r_time).total_seconds())
                    if diff <= 600:
                        match = r
                        break
                except Exception:
                    continue

            if match:
                total_fare = 200
                share = total_fare // 2

                supabase.table("rides").update({
                    "matched_user": match["user_id"],
                    "fare": total_fare,
                    "share_fare": share
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "fare": total_fare,
                    "share_fare": share
                }).eq("user_id", match["user_id"]).execute()

                reply = (
                    f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n"
                    f"🧑‍🤝‍🧑 成功配對對象：{match['user_id'][-5:]}\n"
                    f"💰 每人預估費用：${share}"
                )
            else:
                reply = (
                    f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n"
                    f"目前暫無共乘對象。"
                )

            user_states.pop(user_id)

        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："

    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            lines = []
            for r in result.data:
                line = f"{r['origin']} → {r['destination']} 時間: {r['time'][11:16]}"
                if r.get("matched_user"):
                    line += f" | 共乘對象: {r['matched_user'][-5:]}"
                if r.get("share_fare"):
                    line += f" | 預估費用: ${r['share_fare']}"
                lines.append(line)
            reply = "📋 你的預約如下：\n" + "\n".join(lines)
        else:
            reply = "你目前沒有任何預約。"

    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "🗑️ 所有預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」或「取消」來操作共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
