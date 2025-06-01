from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime
import requests
import random
from geopy.distance import geodesic

# 載入環境變數
load_dotenv()

# 初始化 Flask、LINE、Supabase
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 儲存使用者對話狀態
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

@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    state = user_states.get(user_id, {})
    lat = event.message.latitude
    lng = event.message.longitude

    if state.get("step") == "from":
        state["from_lat"] = lat
        state["from_lng"] = lng
        state["step"] = "to"
        user_states[user_id] = state
        reply = "請傳送目的地位置📍"
    elif state.get("step") == "to":
        state["to_lat"] = lat
        state["to_lng"] = lng
        state["step"] = "time"
        user_states[user_id] = state
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："
    else:
        reply = "請先輸入「預約」來開始設定共乘資訊"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "from"}
        reply = "請傳送出發地位置📍"

    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["time"] = dt.isoformat()
            user_states.pop(user_id, None)

            supabase.table("rides").delete().eq("user_id", user_id).execute()

            supabase.table("rides").insert({
                "user_id": user_id,
                "from_lat": state["from_lat"],
                "from_lng": state["from_lng"],
                "to_lat": state["to_lat"],
                "to_lng": state["to_lng"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None,
                "driver_id": None
            }).execute()

            user_time = dt.replace(tzinfo=None)
            all_rides = supabase.table("rides") \
                .select("*") \
                .eq("matched_user", None) \
                .neq("user_id", user_id) \
                .execute().data

            matched = None
            for r in all_rides:
                try:
                    t = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    time_diff = abs((t - user_time).total_seconds())
                    from_dist = geodesic((r["from_lat"], r["from_lng"]),
                                         (state["from_lat"], state["from_lng"])).meters
                    to_dist = geodesic((r["to_lat"], r["to_lng"]),
                                       (state["to_lat"], state["to_lng"])).meters
                    if time_diff <= 600 and from_dist <= 500 and to_dist <= 500:
                        matched = r
                        break
                except:
                    continue

            if matched:
                dist_km = geodesic(
                    (state["from_lat"], state["from_lng"]),
                    (state["to_lat"], state["to_lng"])
                ).km
                total_fare = max(50, int(dist_km * 50))
                share = total_fare // 2

                drivers = supabase.table("drivers").select("*").execute().data
                driver = random.choice(drivers) if drivers else None
                driver_name = driver["name"] if driver else "N/A"
                driver_phone = driver["phone"] if driver else "N/A"
                driver_id = driver["id"] if driver else None

                supabase.table("rides").update({
                    "matched_user": matched["user_id"],
                    "fare": total_fare,
                    "share_fare": share,
                    "driver_id": driver_id
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "fare": total_fare,
                    "share_fare": share,
                    "driver_id": driver_id
                }).eq("user_id", matched["user_id"]).execute()

                reply = (
                    f"✅ 預約成功！\n"
                    f"🧑‍🤝‍🧑 成功配對！\n"
                    f"🚕 共乘對象：{matched['user_id']}\n"
                    f"💰 總費用：${total_fare}，你需支付：${share}\n"
                    f"👨‍✈️ 司機：{driver_name}（{driver_phone}）"
                )
            else:
                reply = (
                    f"✅ 預約成功！\n"
                    f"目前暫無共乘對象。"
                )
        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："

    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            ride = result.data[0]
            reply = (
                f"📋 你的預約：\n"
                f"時間：{ride['time']}\n"
                f"共乘對象：{ride.get('matched_user', '無')}\n"
                f"💰 分攤費用：${ride.get('share_fare', '？')}"
            )
        else:
            reply = "你目前沒有任何預約。"

    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        reply = "🗑️ 預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」或「取消」來使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
