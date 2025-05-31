from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime

# 載入 .env 設定
load_dotenv()
app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

user_states = {}

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running."

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
            time_iso = dt.isoformat()
            user_states[user_id] = state

            # 儲存使用者資料
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from"],
                "destination": state["to"],
                "time": time_iso,
                "matched_user": None
            }).execute()

            # 查找其他尚未配對且時間相近的用戶
            result = supabase.table("rides") \
                .select("*") \
                .eq("origin", state["from"]) \
                .eq("destination", state["to"]) \
                .is_("matched_user", None) \
                .neq("user_id", user_id) \
                .execute()

            match = None
            debug = []

            for r in result.data:
                try:
                    r_time = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    diff = abs((dt - r_time).total_seconds())
                    debug.append(f"比對 {r['user_id'][-5:]}, 差 {int(diff)} 秒")
                    if diff <= 600:
                        match = r
                        break
                except Exception as e:
                    debug.append(f"錯誤: {e}")

            if match:
                # 雙方寫入 matched_user
                supabase.table("rides").update({"matched_user": match["user_id"]}).eq("user_id", user_id).execute()
                supabase.table("rides").update({"matched_user": user_id}).eq("user_id", match["user_id"]).execute()
                reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n🚕 成功配對用戶：{match['user_id'][-5:]}"
            else:
                reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n目前尚無共乘對象。"

            if debug:
                reply += "\n\n[DEBUG]\n" + "\n".join(debug)

            user_states.pop(user_id)

        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："

    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            ride = result.data[0]
            time_str = ride["time"][11:16]
            matched = ride.get("matched_user")
            if matched:
                reply = f"📋 你已預約：\n從 {ride['origin']} 到 {ride['destination']}，時間 {time_str}\n✅ 配對用戶：{matched[-5:]}"
            else:
                reply = f"📋 你已預約：\n從 {ride['origin']} 到 {ride['destination']}，時間 {time_str}\n尚未配對"
        else:
            reply = "你目前沒有任何預約。"

    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "🗑️ 所有預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」或「取消」來使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
