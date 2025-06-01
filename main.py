from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
from geopy.distance import geodesic
import os
import datetime

# 載入 .env
load_dotenv()

# 初始化 Flask、LINE Bot、Supabase
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 暫存使用者狀態
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

@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude
    state = user_states.get(user_id, {})

    if state.get("step") == "from":
        state.update({
            "from_lat": lat,
            "from_lng": lng,
            "step": "to"
        })
        reply = "請傳送目的地位置📍"
    elif state.get("step") == "to":
        state.update({
            "to_lat": lat,
            "to_lng": lng,
            "step": "time"
        })
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："
    else:
        reply = "請先輸入「預約」來開始流程。"

    user_states[user_id] = state
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
            user_time = dt.replace(tzinfo=None)

            # 刪除舊資料
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 儲存新資料
            supabase.table("rides").insert({
                "user_id": user_id,
                "from_lat": state["from_lat"],
                "from_lng": state["from_lng"],
                "to_lat": state["to_lat"],
                "to_lng": state["to_lng"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None
            }).execute()

            # 搜尋可配對對象
            candidates = supabase.table("rides") \
                .select("*") \
                .is_("matched_user", None) \
                .neq("user_id", user_id) \
                .execute().data

            match = None
            for r in candidates:
                try:
                    rt = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    if abs((rt - user_time).total_seconds()) > 600:
                        continue
                    from_dist = geodesic((state["from_lat"], state["from_lng"]),
                                         (r["from_lat"], r["from_lng"])).meters
                    to_dist = geodesic((state["to_lat"], state["to_lng"]),
                                       (r["to_lat"], r["to_lng"])).meters
                    if from_dist <= 300 and to_dist <= 300:
                        match = r
                        break
                except Exception as e:
                    print("[配對錯誤]", e)
                    continue

            if match:
                km = geodesic((state["from_lat"], state["from_lng"]),
                              (state["to_lat"], state["to_lng"])).km
                fare = max(50, int(km * 50))
                share = fare // 2

                # 更新配對資料
                supabase.table("rides").update({
                    "matched_user": match["user_id"],
                    "fare": fare,
                    "share_fare": share
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "fare": fare,
                    "share_fare": share
                }).eq("user_id", match["user_id"]).execute()

                reply = f"✅ 預約成功！你與 {match['user_id']} 成功配對 🎉\n共乘費用：${fare}，你需支付：${share}"
            else:
                reply = "✅ 預約成功，目前尚無共乘對象。"

            user_states.pop(user_id)

        except Exception as e:
            reply = f"⚠️ 發生錯誤：{str(e)}，請重新輸入（例如：2025-06-01 18:00）"

    elif text.lower() in ["查詢", "查詢預約"]:
        data = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if data:
            r = data[0]
            reply = f"📋 預約資訊：\n時間：{r['time']}\n共乘對象：{r.get('matched_user') or '無'}\n💰 你需支付：${r.get('share_fare') or '？'}"
        else:
            reply = "你目前沒有任何預約。"

    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "✅ 所有預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」或「取消」開始使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
