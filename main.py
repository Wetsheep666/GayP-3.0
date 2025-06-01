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

# 初始化
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 使用者狀態暫存
user_states = {}

# 健康檢查用
@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running."

# 處理 LINE Webhook
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 處理位置訊息
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
        reply = "請傳送目的地位置📍"
    elif state.get("step") == "to":
        state["to_lat"] = lat
        state["to_lng"] = lng
        state["step"] = "time"
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："
    else:
        reply = "請先輸入「預約」來開始設定共乘資訊"
    
    user_states[user_id] = state
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 處理文字訊息
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
            user_time = dt.replace(tzinfo=None)
            state["time"] = dt.isoformat()
            user_states.pop(user_id, None)

            # 清除舊資料
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 新增預約資料
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

            # 尋找可配對對象（10分鐘內、距離500公尺內）
            candidates = supabase.table("rides") \
                .select("*") \
                .eq("matched_user", None) \
                .neq("user_id", user_id) \
                .execute().data

            matched = None
            for r in candidates:
                try:
                    t = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    if abs((t - user_time).total_seconds()) > 600:
                        continue
                    from_dist = geodesic((r["from_lat"], r["from_lng"]), (state["from_lat"], state["from_lng"])).meters
                    to_dist = geodesic((r["to_lat"], r["to_lng"]), (state["to_lat"], state["to_lng"])).meters
                    if from_dist <= 500 and to_dist <= 500:
                        matched = r
                        break
                except:
                    continue

            if matched:
                # 計算費用：每公里 50 元
                distance_km = geodesic(
                    (state["from_lat"], state["from_lng"]),
                    (state["to_lat"], state["to_lng"])
                ).km
                total_fare = max(50, int(distance_km * 50))
                share = total_fare // 2

                # 分配司機
                drivers = supabase.table("drivers").select("*").execute().data
                driver = random.choice(drivers) if drivers else None
                driver_name = driver["name"] if driver else "N/A"
                driver_phone = driver["phone"] if driver else "N/A"
                driver_id = driver["id"] if driver else None

                # 更新兩位乘客的資料
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

                reply = f"✅ 預約成功！\n🧑‍🤝‍🧑 成功配對！\n🚕 共乘對象：{matched['user_id']}\n💰 總費用：${total_fare}，你需支付：${share}\n👨‍✈️ 司機：{driver_name}（{driver_phone}）"
            else:
                reply = "✅ 預約成功！\n目前暫無共乘對象。"
        
        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："
    
    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if result:
            r = result[0]
            reply = f"📋 預約資訊：\n時間：{r['time']}\n共乘對象：{r.get('matched_user') or '無'}\n💰 分攤費用：${r.get('share_fare') or '？'}"
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
