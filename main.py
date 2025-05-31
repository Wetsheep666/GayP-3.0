from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime
import requests

# 載入 .env
load_dotenv()

# 初始化
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
            state["time"] = dt.isoformat()
            user_states[user_id] = state

            # 先清除舊資料
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 新增預約資料
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from"],
                "destination": state["to"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None
            }).execute()

            # 尋找可配對對象（10 分鐘內、相同起訖點）
            candidates = supabase.table("rides") \
                .select("*") \
                .eq("origin", state["from"]) \
                .eq("destination", state["to"]) \
                .is_("matched_user", None) \
                .neq("user_id", user_id) \
                .execute()

            matched = None
            user_time = dt.replace(tzinfo=None)

            for c in candidates.data:
                try:
                    cand_time = datetime.datetime.fromisoformat(c["time"]).replace(tzinfo=None)
                    delta = abs((cand_time - user_time).total_seconds())
                    if delta <= 600:
                        matched = c
                        break
                except:
                    continue

            if matched:
                # 計算距離
                gkey = os.getenv("GOOGLE_API_KEY")
                origin = state["from"]
                destination = state["to"]
                g_url = f"https://maps.googleapis.com/maps/api/distancematrix/json"
                params = {
                    "origins": origin,
                    "destinations": destination,
                    "key": gkey,
                    "mode": "driving",
                    "language": "zh-TW"
                }
                res = requests.get(g_url, params=params).json()
                try:
                    meters = res["rows"][0]["elements"][0]["distance"]["value"]
                    km = meters / 1000
                    total_fare = max(25, int(km * 25))
                except:
                    total_fare = 200
                share = total_fare // 2

                # 更新雙方 matched_user, fare, share_fare
                supabase.table("rides").update({
                    "matched_user": matched["user_id"],
                    "fare": total_fare,
                    "share_fare": share
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "fare": total_fare,
                    "share_fare": share
                }).eq("user_id", matched["user_id"]).execute()

                reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {dt.strftime('%H:%M')}\n\n🧑‍🤝‍🧑 成功配對！\n🚕 共乘對象：{matched['user_id']}\n💰 總費用：${total_fare}，你需支付：${share}"
            else:
                reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {dt.strftime('%H:%M')}\n\n目前暫無共乘對象。"
            user_states.pop(user_id)
        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："
    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            lines = []
            for r in result.data:
                match = r.get("matched_user")
                fare = r.get("share_fare")
                s = f"{r['origin']} → {r['destination']} 時間: {r['time']}"
                if match:
                    s += f"\n👥 共乘對象：{match}"
                if fare:
                    s += f"\n💰 你需支付：${fare}"
                lines.append(s)
            reply = "📋 你的預約如下：\n" + "\n\n".join(lines)
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
