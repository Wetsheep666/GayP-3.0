from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
from geopy.distance import geodesic
import os
import datetime

# === 環境與服務初始化 ===
load_dotenv()
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
user_states = {}

# === 基本首頁與 Webhook 路由 ===
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

# === 接收位置訊息 ===
@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude
    address = event.message.address or "未知地點"
    state = user_states.get(user_id, {})

    if state.get("step") == "from":
        state.update({
            "from_address": address,
            "from_lat": lat,
            "from_lng": lng,
            "step": "to"
        })
        reply = "請傳送目的地點（左下角「+」➜ 地點 📍）"
    elif state.get("step") == "to":
        state.update({
            "to_address": address,
            "to_lat": lat,
            "to_lng": lng,
            "step": "time"
        })
        reply = "請輸入預約時間（格式：2025-06-01 18:00）："
    else:
        reply = "請先輸入「預約」開始流程。"

    user_states[user_id] = state
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# === 處理文字訊息 ===
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    # === 開始預約 ===
    if text in ["預約", "我要搭車"]:
        profile = supabase.table("profiles").select("*").eq("user_id", user_id).execute().data
        if not profile:
            user_states[user_id] = {"step": "name"}
            reply = "請先輸入您的姓名："
        else:
            user_states[user_id] = {"step": "from"}
            reply = "請傳送出發地點（左下角「+」➜ 地點 📍）"

    # === 輸入偏好資料（永久儲存於 profiles 表）===
    elif state.get("step") == "name":
        state["name"] = text
        state["step"] = "gender"
        reply = "請輸入性別（男/女）："
    elif state.get("step") == "gender":
        if text not in ["男", "女"]:
            reply = "⚠️ 請輸入「男」或「女」"
        else:
            state["gender"] = text
            state["step"] = "pet"
            reply = "是否可接受共乘對象攜帶寵物？（是/否）"
    elif state.get("step") == "pet":
        state["pet_friendly"] = text == "是"
        state["step"] = "smoke"
        reply = "是否可接受共乘對象吸菸？（是/否）"
    elif state.get("step") == "smoke":
        state["smoke_friendly"] = text == "是"
        supabase.table("profiles").upsert({
            "user_id": user_id,
            "name": state["name"],
            "gender": state["gender"],
            "pet_friendly": state["pet_friendly"],
            "smoke_friendly": state["smoke_friendly"]
        }).execute()
        state["step"] = "from"
        reply = "✅ 資料已儲存！請傳送出發地點（左下角「+」➜ 地點 📍）"

    # === 預約搭車並配對 ===
    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            user_time = dt.replace(tzinfo=None)
            state["time"] = dt.isoformat()

            # 取得偏好資料
            user_profile = supabase.table("profiles").select("*").eq("user_id", user_id).execute().data[0]

            # 清除舊預約
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 儲存新的預約
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from_address"],
                "origin_lat": state["from_lat"],
                "origin_lng": state["from_lng"],
                "destination": state["to_address"],
                "destination_lat": state["to_lat"],
                "destination_lng": state["to_lng"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None
            }).execute()

            # 嘗試配對
            result = supabase.table("rides").select("*") \
                .is_("matched_user", None).neq("user_id", user_id).execute().data

            match = None
            for r in result:
                try:
                    rt = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    if abs((user_time - rt).total_seconds()) > 600:
                        continue
                    o_dist = geodesic((state["from_lat"], state["from_lng"]), (r["origin_lat"], r["origin_lng"])).meters
                    d_dist = geodesic((state["to_lat"], state["to_lng"]), (r["destination_lat"], r["destination_lng"])).meters
                    if o_dist > 1000 or d_dist > 1000:
                        continue

                    match_profile = supabase.table("profiles").select("*") \
                        .eq("user_id", r["user_id"]).execute().data[0]

                    # 雙方皆須接受彼此偏好條件
                    if not user_profile["pet_friendly"] and match_profile["pet_friendly"]:
                        continue
                    if not user_profile["smoke_friendly"] and match_profile["smoke_friendly"]:
                        continue
                    if user_profile["gender"] != match_profile["gender"]:
                        continue

                    match = r
                    break
                except Exception as e:
                    print("配對錯誤：", e)
                    continue

            if match:
                distance_km = geodesic(
                    (state["from_lat"], state["from_lng"]),
                    (state["to_lat"], state["to_lng"])
                ).km
                fare = max(50, int(distance_km * 50))
                share = fare // 2

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

                preview_link = f"https://www.google.com/maps/dir/?api=1&origin={state['from_lat']},{state['from_lng']}&destination={state['to_lat']},{state['to_lng']}&travelmode=driving"

                notify_msg = f"✅ 成功配對！\n🧭 路線：{state['from_address']} → {state['to_address']}\n💰 費用：${fare}，你需支付 ${share}\n🗺️ 預覽路線：{preview_link}"
                line_bot_api.push_message(match["user_id"], TextSendMessage(text=notify_msg))
                reply = notify_msg
            else:
                reply = f"✅ 已預約！但目前沒有符合的共乘對象。"

            user_states.pop(user_id, None)

        except Exception as e:
            reply = f"⚠️ 請確認時間格式正確（2025-06-01 18:00），錯誤：{e}"

    # === 查詢與取消 ===
    elif text in ["查詢", "查詢預約"]:
        r = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if r:
            r = r[0]
            reply = f"📋 你的預約：\n{r['origin']} → {r['destination']}，時間：{r['time']}\n👥 共乘對象：{r['matched_user'] or '尚未配對'}\n💰 你需支付：{r['share_fare'] or '待定'}"
        else:
            reply = "目前沒有預約紀錄。"

    elif text in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "🗑️ 已取消你的預約。"

    else:
        reply = "請輸入：「預約」、「查詢」、「取消」或先設定用戶偏好。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
