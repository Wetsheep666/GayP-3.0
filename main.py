from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime
import math

# 載入 .env
load_dotenv()

app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

user_states = {}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # 地球半徑 (公里)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

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
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "origin"}
        reply = "📍 請傳送你的出發地點（使用位置訊息）"
    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["time"] = dt.isoformat()

            supabase.table("rides").delete().eq("user_id", user_id).execute()
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin_lat": state["origin_lat"],
                "origin_lng": state["origin_lng"],
                "destination_lat": state["destination_lat"],
                "destination_lng": state["destination_lng"],
                "time": state["time"],
                "matched_user": None
            }).execute()

            candidates = supabase.table("rides") \
                .select("*") \
                .is_("matched_user", None) \
                .neq("user_id", user_id) \
                .execute()

            matched = None
            for c in candidates.data:
                try:
                    delta = abs((datetime.datetime.fromisoformat(c["time"]).replace(tzinfo=None) - dt).total_seconds())
                    o_dist = haversine(state["origin_lat"], state["origin_lng"], c["origin_lat"], c["origin_lng"])
                    d_dist = haversine(state["destination_lat"], state["destination_lng"], c["destination_lat"], c["destination_lng"])
                    if delta <= 600 and o_dist < 1.0 and d_dist < 1.0:
                        matched = c
                        break
                except:
                    continue

            if matched:
                supabase.table("rides").update({"matched_user": matched["user_id"]}).eq("user_id", user_id).execute()
                supabase.table("rides").update({"matched_user": user_id}).eq("user_id", matched["user_id"]).execute()
                reply = f"✅ 預約成功並成功配對！\n🧍‍♂️ 你與 {matched['user_id']} 共乘。\n🚕 預約時間：{dt.strftime('%H:%M')}"
            else:
                reply = f"✅ 預約成功！\n目前尚無共乘對象，已為你保留預約資訊。"
            user_states.pop(user_id)
        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："
    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            reply = "\n\n".join([
                f"🛺 時間：{r['time']}\n配對對象：{r.get('matched_user', '尚未配對')}"
                for r in result.data
            ])
        else:
            reply = "你目前沒有任何預約。"
    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "✅ 所有預約已取消。"
    else:
        reply = "請輸入「預約」、「查詢」或「取消」。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude
    state = user_states.get(user_id, {})

    if state.get("step") == "origin":
        state["origin_lat"] = lat
        state["origin_lng"] = lng
        state["step"] = "destination"
        user_states[user_id] = state
        reply = "📍 出發地已儲存，請傳送你的目的地位置"
    elif state.get("step") == "destination":
        state["destination_lat"] = lat
        state["destination_lng"] = lng
        state["step"] = "time"
        user_states[user_id] = state
        reply = "✅ 目的地已儲存。\n請輸入預約時間（格式：2025-06-01 18:00）："
    else:
        reply = "請輸入「預約」開始設定共乘資訊。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
